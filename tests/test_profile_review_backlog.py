"""Contact reviews must outlive the short conversation context window."""
import json
import unittest
from unittest.mock import AsyncMock

from core.evidence import source_snapshot
from core.ports import LLMReply
from tests import test_consolidate as fixture
from tests import test_engine as engine_fixture

SESSION, USER = fixture.SESSION, fixture.USER


class ProfileReviewBacklogTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.ConsolidateTestCase.asyncSetUp

    def observe(self, text, *, session=SESSION, eligible=True, **extra):
        row = {'user_id': USER, 'text': text, 'origin': session, **extra}
        source = source_snapshot(row, session)
        self.db.observe_source(source, eligible=eligible)
        return row, source['id']

    async def test_backlog_uses_original_eligible_speech_and_actual_session(self):
        self.world.memory.sleep_review = False
        private = fixture.PRIVATE
        self.consolidator._sessions_of = lambda _: [SESSION, private]
        old, original_id = self.observe('我喜欢热茶', session=private)
        rejected = [
            self.observe('我喜欢咖啡', eligible=False)[1],
            self.observe('我喜欢假消息', kind='tool_result')[1],
            self.observe('我喜欢别处的话', session='foreign')[1],
        ]
        self.profiles.touch(private, USER)
        prompts = []

        async def model(_system, prompt):
            prompts.append(prompt)
            return json.dumps({'facts': [{'user_id': USER, 'text': '喜欢热茶',
                'evidence': '我喜欢热茶', 'source_session': private}]})

        forged = {**old, 'source_id': original_id, 'text': '我喜欢冷茶'}
        result = await self.consolidator.run(session_id=SESSION, llm=model,
            include_pending=True, chat_records=[forged, {'origin': 'foreign', 'user_id': USER, 'text': '未经授权'}])
        self.assertTrue(result.ok, result.skipped)
        self.assertEqual(len(prompts), 1)
        self.assertIn('我喜欢热茶', prompts[0])
        for text in ('我喜欢冷茶', '我喜欢咖啡', '我喜欢假消息', '我喜欢别处的话', '未经授权'):
            self.assertNotIn(text, prompts[0])
        self.assertEqual(self.profiles.facts(SESSION, USER), [])
        self.assertEqual(self.profiles.facts(private, USER)[0]['source_session'], private)
        self.assertEqual(self.db.reviewed_profile_sources(rejected, 'full'), set())

    async def test_nap_receipts_leave_facts_for_full_review_without_replaying_finished_sources(self):
        _, source_id = self.observe('我喜欢热茶')
        self.db.mark_profile_reviewed([source_id], 'nap', 1)
        self.assertEqual(self.db.pending_profile_sources([SESSION], 'nap'), [])
        self.assertEqual([r['source_id'] for r in self.db.pending_profile_sources([SESSION], 'full')], [source_id])
        model = AsyncMock(return_value='{}')
        await self.consolidator.run(session_id=SESSION, llm=model, include_pending=True)
        self.assertEqual(model.await_count, 1)
        self.assertEqual(self.db.pending_profile_sources([SESSION], 'full'), [])
        await self.consolidator.run(session_id=SESSION, llm=model, include_pending=True)
        self.assertEqual(model.await_count, 1)

    async def test_preview_and_failure_leave_persisted_sources_for_retry(self):
        _, source_id = self.observe('我喜欢热茶')
        preview = await self.consolidator.run(session_id=SESSION, llm=AsyncMock(return_value='{}'),
                                              include_pending=True, dry_run=True)
        self.assertTrue(preview.ok)
        self.assertIn('我喜欢热茶', preview.preview['user'])
        self.assertEqual(self.db.reviewed_profile_sources([source_id], 'full'), set())
        failure = await self.consolidator.run(session_id=SESSION, llm=AsyncMock(side_effect=RuntimeError('暂时不可用')),
                                              include_pending=True)
        self.assertFalse(failure.ok)
        self.assertTrue(failure.retry_needed)
        self.assertEqual(self.db.reviewed_profile_sources([source_id], 'full'), set())

    async def test_disabled_profile_stage_never_fetches_or_consumes_backlog(self):
        _, source_id = self.observe('我喜欢热茶')
        self.world.profile.consolidate_enabled = False
        self.db.pending_profile_sources = AsyncMock(side_effect=AssertionError('不可读取画像来源'))
        model = AsyncMock(return_value='{}')
        result = await self.consolidator.run(session_id=SESSION, llm=model, include_pending=True)
        self.assertTrue(result.ok)
        self.assertEqual(model.await_count, 0)
        self.db.pending_profile_sources.assert_not_called()
        self.assertEqual(self.db.reviewed_profile_sources([source_id], 'full'), set())

    async def test_overflow_is_left_pending_instead_of_consuming_the_daily_pass(self):
        records = [{"user_id": USER, "text": f"这是第{n}条发言", "message_id": str(n)}
                   for n in range(401)]
        first = await self.consolidator.run(session_id=SESSION, llm=AsyncMock(return_value='{}'),
                                            chat_records=records)
        self.assertTrue(first.ok)
        self.assertTrue(first.retry_needed)
        ids = [source_snapshot(row, SESSION)['id'] for row in records]
        self.assertEqual(len(self.db.reviewed_profile_sources(ids, 'full')), 400)
        second = await self.consolidator.run(session_id=SESSION, llm=AsyncMock(return_value='{}'),
                                             chat_records=records)
        self.assertTrue(second.ok)
        self.assertFalse(second.retry_needed)
        self.assertEqual(len(self.db.reviewed_profile_sources(ids, 'full')), 401)


class ProfileReviewBacklogEngineTests(unittest.IsolatedAsyncioTestCase):
    node_id = 'study'
    asyncSetUp = engine_fixture.EngineTestCase.asyncSetUp

    async def test_review_recovers_a_quote_absent_from_live_chat(self):
        session = engine_fixture.SESSION
        old = {'user_id': USER, 'text': '我今天换了新的记忆插件', 'at': self.clock.now() - 86400,
               'origin': session, 'message_id': 'old-contact-quote'}
        source = source_snapshot(old, session)
        self.db.raw.observe_source(source, eligible=True)
        self.engine.profiles.touch(session, USER)
        state = await self.engine.load_state(session, cold_start=False)
        self.assertEqual(state.recent_chat, [])
        prompts = []

        async def model(**kwargs):
            prompts.append(kwargs['prompt'])
            return LLMReply(ok=True, text=json.dumps({'facts': [{
                'user_id': USER, 'kind': '近况', 'text': '今天换了新的记忆插件',
                'evidence': '我今天换了新的记忆插件', 'source_session': session}]}))

        self.engine.consolidate_llm.generate = AsyncMock(side_effect=model)
        result = await self.engine.consolidate_now(state)
        self.assertTrue(result['ok'])
        self.assertEqual(len(prompts), 1)
        self.assertIn(old['text'], prompts[0])
        self.assertEqual(self.engine.profiles.facts(session, USER)[0]['text'], '今天换了新的记忆插件')
        self.assertEqual(self.db.raw.query_memories(session_id=session), [])
        self.assertIn(source['id'], self.db.raw.reviewed_profile_sources([source['id']], 'full'))
