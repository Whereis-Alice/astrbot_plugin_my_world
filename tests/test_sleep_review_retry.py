"""Only completed review stages consume the automatic review allowance."""
import json
import unittest
from types import SimpleNamespace

from core.engine.recall import RecallMixin
from core.evidence import source_snapshot
from tests import test_consolidate as fixture

SESSION, USER = fixture.SESSION, fixture.USER


class SleepReviewRetryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.ConsolidateTestCase.asyncSetUp

    def assert_pending_pass(self, result):
        cycle = ['activity', 'today']
        state = SimpleNamespace(sleep_rhythm={'review': {'cycle': cycle, 'passes': 0}})
        engine = SimpleNamespace(_sleep_review_cycle=lambda _: cycle, _now=lambda: 100)
        RecallMixin._record_sleep_review_attempt(engine, state, vars(result))
        self.assertEqual(state.sleep_rhythm['review']['passes'], 0)
        self.assertEqual(state.sleep_rhythm['review']['retry_at'], 400)

    async def test_changed_life_material_after_profile_success_keeps_a_pending_pass(self):
        self.profiles.touch(SESSION, USER)
        mid = self.memory.remember(session_id=SESSION, persona_id='', node_id='study', content='修好了风车')
        calls = []

        async def model(_system, prompt):
            calls.append(prompt)
            if len(calls) == 1:
                return json.dumps({'digests': {USER: '喜欢猫'}})
            self.db.revise_memory(mid, {'content': '在花园修好了风车'})
            return json.dumps({'forget': [{'id': mid, 'text': '过时的书房记录'}]})

        record = {'user_id': USER, 'text': '我喜欢猫', 'message_id': 'profile'}
        result = await self.consolidator.run(session_id=SESSION, llm=model, chat_records=[record])
        self.assertTrue(result.ok, result.skipped)
        self.assertTrue(result.retry_needed, result.skipped)
        self.assertEqual(self.db.memory_by_id(mid)['content'], '在花园修好了风车')
        self.assert_pending_pass(result)

        async def fresh(_system, prompt):
            calls.append(prompt)
            return '{}'

        result = await self.consolidator.run(session_id=SESSION, llm=fresh, chat_records=[record])
        self.assertTrue(result.ok)
        self.assertFalse(result.retry_needed)
        self.assertEqual(len(calls), 3)
        self.assertIn('在花园修好了风车', calls[-1])
        self.assertNotIn('我喜欢猫', calls[-1])

    async def test_overlapping_profile_batch_after_life_success_leaves_unseen_sources_pending(self):
        self.profiles.touch(SESSION, USER)
        self.memory.remember(session_id=SESSION, persona_id='', node_id='study', content='修好了风车')
        old = {'user_id': USER, 'text': '我喜欢猫', 'message_id': 'old'}
        new = {'user_id': USER, 'text': '我喜欢油画', 'message_id': 'new'}
        calls = []

        async def model(_system, prompt):
            calls.append(prompt)
            if len(calls) == 1:
                self.world.memory.sleep_review = False
                try:
                    await self.consolidator.run(session_id=SESSION, llm=lambda *_: fresh(), chat_records=[old])
                finally:
                    self.world.memory.sleep_review = True
            return '{}'

        async def fresh():
            return '{}'

        result = await self.consolidator.run(session_id=SESSION, llm=model, chat_records=[old, new])
        self.assertTrue(result.ok, result.skipped)
        self.assertTrue(result.retry_needed, result.skipped)
        self.assert_pending_pass(result)
        result = await self.consolidator.run(session_id=SESSION, llm=model, chat_records=[old, new])
        self.assertTrue(result.ok)
        self.assertFalse(result.retry_needed)
        self.assertEqual(len(calls), 3)
        self.assertIn('我喜欢油画', calls[-1])
        self.assertNotIn('我喜欢猫', calls[-1])

    async def test_fully_reviewed_overlap_does_not_schedule_an_unnecessary_retry(self):
        self.profiles.touch(SESSION, USER)
        self.memory.remember(session_id=SESSION, persona_id='', node_id='study', content='修好了风车')
        records = [
            {'user_id': USER, 'text': '我喜欢猫', 'message_id': 'old'},
            {'user_id': USER, 'text': '我喜欢油画', 'message_id': 'new'},
        ]
        calls = []

        async def fresh(*_):
            return '{}'

        async def model(_system, prompt):
            calls.append(prompt)
            if len(calls) == 1:
                self.world.memory.sleep_review = False
                try:
                    for record in records:
                        await self.consolidator.run(session_id=SESSION, llm=fresh, chat_records=[record])
                finally:
                    self.world.memory.sleep_review = True
            return '{}'

        result = await self.consolidator.run(session_id=SESSION, llm=model, chat_records=records)
        self.assertTrue(result.ok)
        self.assertTrue(result.skipped)
        self.assertFalse(result.retry_needed)
        result = await self.consolidator.run(session_id=SESSION, llm=model, chat_records=records)
        self.assertTrue(result.ok)
        self.assertFalse(result.retry_needed)
        self.assertEqual(len(calls), 2)

    async def test_revoked_quote_does_not_consume_the_pass_for_other_valid_sources(self):
        self.profiles.touch(SESSION, USER)
        self.memory.remember(session_id=SESSION, persona_id='', node_id='study', content='修好了风车')
        revoked = {'user_id': USER, 'text': '未经核实的偏好', 'message_id': 'revoked'}
        valid = {'user_id': USER, 'text': '我喜欢油画', 'message_id': 'valid'}
        source = source_snapshot(revoked, SESSION)
        self.db.observe_source(source, eligible=True)
        calls = []

        async def model(_system, prompt):
            calls.append(prompt)
            if len(calls) == 1:
                self.db._execute('UPDATE memory_source SET eligible=0 WHERE id=?', (source['id'],))
                return json.dumps({'digests': {USER: '过时的整批结论'}})
            return '{}'

        result = await self.consolidator.run(session_id=SESSION, llm=model, chat_records=[revoked, valid])
        self.assertTrue(result.ok)
        self.assertTrue(result.retry_needed)
        self.assertEqual(result.digests, 0)
        self.assert_pending_pass(result)
        result = await self.consolidator.run(session_id=SESSION, llm=model, chat_records=[revoked, valid])
        self.assertTrue(result.ok)
        self.assertFalse(result.retry_needed)
        self.assertEqual(len(calls), 3)
        self.assertIn('我喜欢油画', calls[-1])
        self.assertNotIn('未经核实的偏好', calls[-1])
