"""A completed short visit still invalidates home plans and chat drafts in flight."""
from __future__ import annotations

import asyncio
import json
import types
import unittest

from core.engine import TickOutcome
from core.engine.life_inference import life_work
from core.events import parse_event_package
from tests import test_engine as engine_fixture
from tests import test_participation as chat_fixture


class HomePlanEpochTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = engine_fixture.EngineTestCase.asyncSetUp

    def install_visits(self):
        self.epoch = 0
        self.engine.visits = types.SimpleNamespace(active=lambda scope: None,
            presence_epoch=lambda scope: self.epoch)

    async def prepare_plan(self):
        self.install_visits()
        self.engine.world.proactive_chat_paused = True
        self.engine.decider.should_ask_llm = lambda state: True
        self.llm.replies = [json.dumps({"plan": [{"action": "read", "duration": 600}],
                                       "reason": "想继续读书", "inner": "书里还有一个谜题"})]
        async with self.engine.session_state(engine_fixture.SESSION) as state:
            state.state, state.energy = "idle", .9
            state.current_action = state.current_plan = None

    async def block_model(self, work):
        entered, release = asyncio.Event(), asyncio.Event()
        generate = self.llm.generate

        async def delayed(**kwargs):
            entered.set()
            await release.wait()
            return await generate(**kwargs)

        self.llm.generate = delayed
        task = asyncio.create_task(work())
        self.addCleanup(task.cancel)
        await asyncio.wait_for(entered.wait(), 5)
        # The protocol journal records arrival even when the whole trip finishes
        # before this provider returns. No current presence or home state changes.
        self.epoch += 1
        release.set()
        return await asyncio.wait_for(task, 5)

    async def test_real_home_plan_is_discarded_after_short_visit(self):
        await self.prepare_plan()
        result = await self.block_model(lambda: self.engine.maybe_decide(engine_fixture.SESSION, force=True))
        self.assertIsNone(result)
        saved = await self.engine.load_state(engine_fixture.SESSION, cold_start=False)
        self.assertIsNone(saved.current_action)
        self.assertIsNone(saved.current_plan)
        self.assertEqual(len(self.llm.calls), 1)

    async def test_arrival_during_persona_check_does_not_spend_a_model_call(self):
        await self.prepare_plan()
        original_generate = self.engine._background_generate
        persona = self.engine._persona_text

        async def arrival_during_persona(origin):
            result = await persona(origin)
            self.epoch += 1
            return result

        async def prepared(channel, **kwargs):
            self.engine._persona_text = arrival_during_persona
            return await original_generate(channel, **kwargs)

        self.engine._background_generate = prepared
        result = await self.engine.maybe_decide(engine_fixture.SESSION, force=True)
        self.assertIsNone(result)
        self.assertEqual(self.llm.calls, [])
        saved = await self.engine.load_state(engine_fixture.SESSION, cold_start=False)
        self.assertIsNone(saved.current_plan)

    async def test_generated_home_event_is_not_committed_after_short_visit(self):
        self.install_visits()
        package = '{"title":"找到茶叶","hook":"书架旁发现一包茶叶","mode":"choice"}'
        self.assertIsNotNone(parse_event_package(package))
        self.llm.replies = [package]
        self.engine.creator_llm = self.llm

        @life_work()
        async def start(engine):
            async with engine.session_state(engine_fixture.SESSION) as state:
                await engine._start_event(state, engine.node(state.node_id),
                    TickOutcome(engine_fixture.SESSION), tier="small", source="roll")

        await self.block_model(lambda: start(self.engine))
        saved = await self.engine.load_state(engine_fixture.SESSION, cold_start=False)
        self.assertEqual(saved.event_threads, [])
        self.assertEqual(saved.auto_event_count, 0)
        self.assertFalse(self.messenger.sent)
        self.assertEqual(len(self.llm.calls), 1)


class HomeChatEpochTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = chat_fixture.ParticipationTests.asyncSetUp
    chat = chat_fixture.ParticipationTests.chat
    burst = chat_fixture.ParticipationTests.burst
    block_model = HomePlanEpochTests.block_model

    async def test_real_participation_does_not_send_pre_visit_draft(self):
        self.epoch = 0
        self.engine.visits = types.SimpleNamespace(active=lambda scope: None,
            presence_epoch=lambda scope: self.epoch)
        await self.burst()
        result = await self.block_model(lambda: self.engine.maybe_participate(chat_fixture.GROUP))
        self.assertIsNone(result)
        self.assertFalse(self.messenger.sent)
        saved = await self.engine.load_state(chat_fixture.GROUP, cold_start=False)
        self.assertFalse(saved.awaiting_reply)
        self.assertNotEqual(saved.participation[chat_fixture.GROUP]["decision"], "joined")
        self.assertEqual(len(self.llm.calls), 1)
