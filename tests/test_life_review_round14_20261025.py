"""Real service/engine regressions for local learning and persona-owned life seeds."""

import asyncio
import json
import tempfile
import types
import unittest
from hashlib import sha256
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from tests import test_engine as fixture
from tests import test_profile_weaver_integration as profiles
from tests.test_profile_extraction_queue import Event

SESSION = fixture.SESSION


class LocalProfileLearningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.plugin = types.SimpleNamespace(
            config={'weaver_session_based': False}, store=types.SimpleNamespace(data_dir=root / 'my_world'),
            logger=types.SimpleNamespace(debug=Mock()),
            context=types.SimpleNamespace(get_current_chat_provider_id=AsyncMock(return_value='judge')))
        self.service = profiles.ProfileWeaverService(self.plugin, str(root))
        self.service.auto_extract_tools = object()
        self.queue = self.service.extraction_queue
        self.addAsyncCleanup(self.queue.close)

    def values(self, user):
        return {row['name']: row['value'] for row in self.service.detail_for('', user).get('fields', [])}

    async def test_explicit_fact_is_saved_while_other_users_models_are_waiting(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def model(**kwargs):
            calls.append(kwargs['event'].get_sender_id())
            if len(calls) == 2:
                entered.set()
            await release.wait()

        self.plugin.context.tool_loop_agent = model
        for user in ('51', '52'):
            self.assertTrue(self.queue.schedule(Event('我喜欢摄影，最近在练习构图', user=user), None))
        await asyncio.wait_for(entered.wait(), 2)
        running_models = set(self.queue.tasks)
        self.assertTrue(self.queue.schedule(Event('我的职业是摄影师', user='53'), None))
        # Wait for the third job itself, not a guessed scheduler delay.
        local, = self.queue.tasks - running_models
        try:
            await asyncio.wait_for(asyncio.shield(local), .5)
            self.assertEqual(self.values('53')['职业'], '摄影师')
            self.assertEqual(calls, ['51', '52'])
        finally:
            release.set()
            await asyncio.gather(*self.queue.tasks)

    async def test_local_correction_waits_for_same_users_earlier_model_write(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def model(**kwargs):
            event = kwargs['event']
            entered.set()
            await release.wait()
            self.service.store.upsert_field(
                user_id=event.get_sender_id(), subject_name=event.get_sender_name(),
                field_name='职业', value='编辑', session_id=None,
                actor=self.service.actor(event, 'auto_extract'), source_kind='self_report',
                evidence=event.message_str, allow_custom_field=True)

        self.plugin.context.tool_loop_agent = model
        self.queue.schedule(Event('我在出版社工作，主要负责审稿', user='54'), None)
        await asyncio.wait_for(entered.wait(), 2)
        before = set(self.queue.tasks)
        self.queue.schedule(Event('我的职业是摄影师', user='54'), None)
        local, = self.queue.tasks - before
        try:
            await asyncio.sleep(0)
            self.assertFalse(local.done())
            self.assertNotIn('职业', self.values('54'))
        finally:
            release.set()
            await asyncio.gather(*self.queue.tasks)
        self.assertEqual(self.values('54')['职业'], '摄影师')

    async def test_response_profile_tool_suppresses_both_local_and_model_extraction(self):
        self.plugin.context.tool_loop_agent = AsyncMock()
        for index, names in enumerate((['profileweaver_remember_field'], 'profileweaver_forget_field')):
            with self.subTest(names=names):
                user = str(60 + index)
                event = Event('我的职业是摄影师', user=user)
                self.queue.schedule(event, types.SimpleNamespace(tools_call_name=names))
                await asyncio.gather(*self.queue.tasks)
                self.assertEqual(self.values(user), {})
        self.plugin.context.tool_loop_agent.assert_not_awaited()


class PersonaSeedTests(unittest.IsolatedAsyncioTestCase):
    node_id = 'study'
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    async def idle(self):
        async with self.engine.session_state(SESSION) as state:
            state.world_time, state.state, state.energy = 600, 'idle', .9

    async def test_new_persona_has_own_daily_idea_and_old_persona_stays_consumed(self):
        await self.idle()
        original = self.persona.text
        self.engine._ask_judge = AsyncMock(return_value='{"text":""}')
        await self.engine.seed_life_matter(SESSION)
        self.persona.text = '你是严谨克制的学者。'
        self.engine._ask_judge.return_value = '{"text":"研究一段论证"}'
        await self.engine.seed_life_matter(SESSION)
        state = await self.engine.load_state(SESSION, cold_start=False)
        self.assertEqual([m['text'] for m in state.life_matters], ['研究一段论证'])
        self.assertEqual(state.life_matters[0]['persona_key'], sha256(self.persona.text.encode()).hexdigest()[:24])
        self.persona.text = original
        await self.engine.seed_life_matter(SESSION)
        self.assertEqual(self.engine._ask_judge.await_count, 2)

    async def test_persona_snapshot_and_interests_cannot_come_from_different_characters(self):
        await self.idle()
        original = self.persona.text
        key = sha256(original.encode()).hexdigest()[:24]
        async with self.engine.session_state(SESSION) as state:
            state.life_interests = {'read': {'personas': {key: {
                'label': '读少女漫画', 'sessions': 3, 'minutes': 60,
                'days': ['2023-11-12', '2023-11-13', '2023-11-14'], 'last_at': self.clock.now()}}}}
        calls = 0

        async def changed_persona(_session):
            nonlocal calls
            calls += 1
            return original if calls == 1 else '你是严谨克制的学者。'

        self.engine._persona_text = changed_persona
        self.engine._ask_judge = AsyncMock(return_value='{"text":"研究一段论证"}')
        await self.engine.seed_life_matter(SESSION)
        source = json.loads(self.engine._ask_judge.call_args.args[2])
        self.assertEqual(source['persona'], original)
        self.assertEqual(source['interests'][0]['label'], '读少女漫画')
        self.assertFalse((await self.engine.load_state(SESSION, cold_start=False)).life_matters)

    async def test_legacy_daily_marker_is_adopted_once_and_other_persona_can_start(self):
        await self.idle()
        original = self.persona.text
        key = f'life_seed:{SESSION}:{self.engine._today_key(self.clock.now())}'
        self.engine._ask_judge = AsyncMock(return_value='{"text":""}')
        for marker in (True, {'attempts': 1, 'done': True}, {'attempts': 3, 'retry_at': 0}):
            with self.subTest(marker=marker):
                self.persona.text = original
                self.db.raw.kv_set(key, marker)
                self.engine._ask_judge.reset_mock()
                await self.engine.seed_life_matter(SESSION)
                self.engine._ask_judge.assert_not_awaited()
                self.persona.text = '你是严谨克制的学者。'
                await self.engine.seed_life_matter(SESSION)
                self.engine._ask_judge.assert_awaited_once()
                self.engine.reload_config()
                self.persona.text = original
                await self.engine.seed_life_matter(SESSION)
                self.engine._ask_judge.assert_awaited_once()

    async def test_failed_old_persona_does_not_overwrite_new_personas_consumed_day(self):
        await self.idle()
        original = self.persona.text
        entered, release = asyncio.Event(), asyncio.Event()

        async def judge(_session, _system, user):
            if json.loads(user)['persona'] == original:
                entered.set()
                await release.wait()
                return None
            return '{"text":""}'

        self.engine._ask_judge = AsyncMock(side_effect=judge)
        old = asyncio.create_task(self.engine.seed_life_matter(SESSION))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            self.persona.text = '你是严谨克制的学者。'
            await self.engine.seed_life_matter(SESSION)
        finally:
            release.set()
            await asyncio.wait_for(old, 2)
        await self.engine.seed_life_matter(SESSION)
        self.assertEqual(self.engine._ask_judge.await_count, 2)
        self.persona.text = original
        await self.engine.seed_life_matter(SESSION)
        self.assertEqual(self.engine._ask_judge.await_count, 2)
        self.clock.advance(max(15, self.engine.world.limits.life_matter_interval_minutes) * 60)
        await self.engine.seed_life_matter(SESSION)
        self.assertEqual(self.engine._ask_judge.await_count, 3)
