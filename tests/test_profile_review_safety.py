"""Unusable sources and delayed quotes must not block or rewind contact reviews."""
import json
import unittest
from unittest.mock import AsyncMock

from core.evidence import source_snapshot
from tests import test_consolidate as fixture

SESSION, USER = fixture.SESSION, fixture.USER


class ProfileReviewSafetyTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.ConsolidateTestCase.asyncSetUp

    def observe(self, text, at, **extra):
        source = source_snapshot({'user_id': USER, 'text': text, 'at': at, **extra}, SESSION)
        self.db.observe_source(source, eligible=True)
        self.db._execute('UPDATE memory_source SET observed_at=? WHERE id=?', (at, source['id']))
        return source['id']

    async def test_quote_only_sources_do_not_hide_later_direct_speech(self):
        unusable = [self.observe('<quote>这只是引用</quote>', 1000 + n) for n in range(402)]
        valid = self.observe('我喜欢热茶', 2000)
        model = AsyncMock(return_value='{}')
        result = await self.consolidator.run(session_id=SESSION, llm=model, include_pending=True)
        self.assertTrue(result.ok)
        self.assertEqual(model.await_count, 1)
        prompt = model.call_args.args[1]
        self.assertIn('我喜欢热茶', prompt)
        self.assertNotIn('这只是引用', prompt)
        self.assertEqual(self.db.reviewed_profile_sources([valid], 'full'), {valid})
        self.assertEqual(self.db.reviewed_profile_sources(unusable, 'full'), set())

    async def test_older_backlog_fact_cannot_replace_a_newer_confirmed_fact(self):
        self.profiles.touch(SESSION, USER)
        current = self.profiles.note_fact(SESSION, USER, kind='喜好', text='不喜欢辣', evidence='我现在不喜欢辣')
        self.db.update_user_fact(fact_id=current['id'], last_confirmed_at=2000)
        source_id = self.observe('我喜欢辣', 1000)
        model = AsyncMock(return_value=json.dumps({'facts': [{
            'user_id': USER, 'kind': '喜好', 'text': '喜欢辣', 'evidence': '我喜欢辣'}]}))
        result = await self.consolidator.run(session_id=SESSION, llm=model, include_pending=True)
        self.assertTrue(result.ok)
        self.assertFalse(result.retry_needed)
        active = self.profiles.facts(SESSION, USER, statuses=['active'])
        self.assertEqual([row['text'] for row in active], ['不喜欢辣'])
        self.assertEqual(active[0]['last_confirmed_at'], 2000)
        self.assertEqual(self.db.reviewed_profile_sources([source_id], 'full'), {source_id})

    async def test_newer_correction_still_replaces_old_fact_and_keeps_source_time(self):
        self.profiles.touch(SESSION, USER)
        old = self.profiles.note_fact(SESSION, USER, kind='喜好', text='喜欢辣', observed_at=1000)
        self.observe('我现在不喜欢辣', 2000)
        model = AsyncMock(return_value=json.dumps({'facts': [{
            'user_id': USER, 'kind': '喜好', 'text': '不喜欢辣', 'evidence': '我现在不喜欢辣',
            '_observed_at': 9999999999, 'observed_at': 9999999999}]}))
        result = await self.consolidator.run(session_id=SESSION, llm=model, include_pending=True)
        self.assertEqual(result.facts, 1)
        active = self.profiles.facts(SESSION, USER, statuses=['active'])
        self.assertEqual([row['text'] for row in active], ['不喜欢辣'])
        self.assertEqual(active[0]['first_at'], 2000)
        self.assertEqual(active[0]['last_confirmed_at'], 2000)
        self.assertEqual(self.db.get_user_fact(fact_id=old['id'])['status'], 'past')

    async def test_repeated_old_fact_does_not_refresh_newer_confirmation(self):
        current = self.profiles.note_fact(SESSION, USER, kind='喜好', text='喜欢热茶', observed_at=2000)
        before = self.db.get_user_fact(fact_id=current['id'])
        self.observe('我喜欢热茶', 1000)
        model = AsyncMock(return_value=json.dumps({'facts': [{
            'user_id': USER, 'kind': '喜好', 'text': '喜欢热茶', 'evidence': '我喜欢热茶'}]}))
        result = await self.consolidator.run(session_id=SESSION, llm=model, include_pending=True)
        self.assertEqual(result.facts, 0)
        self.assertEqual(self.db.get_user_fact(fact_id=current['id']), before)

    async def test_fact_date_comes_only_from_matching_author_and_session(self):
        self.profiles.touch(SESSION, USER)
        self.observe('我喜欢热茶', 1000)
        self.observe('我喜欢热茶', 1200)
        self.observe('我喜欢热茶', 7000, user_id='someone-else')
        self.observe('我喜欢热茶', 8000, is_self=True)
        self.observe('我喜欢热茶', 9000, origin=fixture.PRIVATE)
        self.consolidator._sessions_of = lambda _: [SESSION, fixture.PRIVATE]
        model = AsyncMock(return_value=json.dumps({'facts': [{
            'user_id': USER, 'kind': '喜好', 'text': '喜欢热茶', 'evidence': '我喜欢热茶',
            'source_session': SESSION}]}))
        result = await self.consolidator.run(session_id=SESSION, llm=model, include_pending=True)
        self.assertEqual(result.facts, 1)
        self.assertEqual(self.profiles.facts(SESSION, USER)[0]['last_confirmed_at'], 1200)
        self.assertEqual(self.profiles.facts(fixture.PRIVATE, USER), [])

    async def test_quote_and_external_memory_pages_keep_quota_and_receipts_separate(self):
        texts = ['<quote>引用</quote>', '<Anamnesis-Memory>旧记忆</Anamnesis-Memory>',
                 '> 引用', '   ', '<RAG-Faiss-Memory>回忆</RAG-Faiss-Memory>', '真实发言']
        ids = [self.observe(text, 1000, message_id=str(n)) for n, text in enumerate(texts)]
        # Identical timestamps exercise the ID part of the pagination cursor.
        picked = self.db.pending_profile_sources([SESSION], 'full', limit=2)
        self.assertEqual([row['source_id'] for row in picked], [ids[-1]])
        self.assertEqual(self.db.reviewed_profile_sources(ids, 'full'), set())
        self.assertTrue(all(self.db.source_record(source_id)['eligible'] for source_id in ids))
        self.db.mark_profile_reviewed([ids[-1]], 'full', 3000)
        model = AsyncMock(return_value='{}')
        result = await self.consolidator.run(session_id=SESSION, llm=model, include_pending=True)
        self.assertTrue(result.ok)
        self.assertEqual(model.await_count, 0)

    async def test_later_fact_merge_advances_confirmation_without_replacing_first_date(self):
        first = self.profiles.note_fact(SESSION, USER, text='喜欢热茶', observed_at=1000)
        merged = self.profiles.note_fact(SESSION, USER, text='喜欢热茶', observed_at=2000)
        self.assertEqual(merged['action'], 'merge')
        row = self.db.get_user_fact(fact_id=first['id'])
        self.assertEqual(row['first_at'], 1000)
        self.assertEqual(row['last_confirmed_at'], 2000)

    async def test_newer_conflict_still_preserves_pinned_fact_as_authoritative(self):
        old = self.profiles.note_fact(SESSION, USER, kind='喜好', text='喜欢辣', pinned=True, observed_at=1000)
        newer = self.profiles.note_fact(SESSION, USER, kind='喜好', text='不喜欢辣', observed_at=2000)
        self.assertEqual(newer['action'], 'candidate')
        self.assertEqual(self.db.get_user_fact(fact_id=old['id'])['status'], 'active')
        candidate = self.db.get_user_fact(fact_id=newer['id'])
        self.assertEqual(candidate['status'], 'candidate')
        self.assertEqual(candidate['last_confirmed_at'], 2000)
