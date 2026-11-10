"""Current visit text is bounded without inventing occurrence dates or facts."""
from __future__ import annotations

import copy
import json
import unittest

from core.visit_recall import visit_memory_excerpt, visit_occurrence_interval, visit_occurrence_metadata
from tests import test_engine as engine_fixture
from tests import test_visits_actor as actor_fixture
from tests import test_visits_memory_review_o as recall_fixture


class VisitOccurrenceViewTests(unittest.TestCase):
    def metadata(self, **values):
        return {"kind": "world_visit", "confirmed": True, **values}

    def test_occurrence_uses_event_interval_and_never_storage_date(self):
        meta = self.metadata(occurred_start=1000, occurred_end=2000, created_at=9000)
        self.assertEqual(visit_occurrence_interval(meta), (1000., 2000.))
        self.assertEqual(visit_occurrence_interval(self.metadata(occurred_start=1000)), (1000., 1000.))
        self.assertIsNone(visit_occurrence_interval(self.metadata(created_at=9000)))
        self.assertIsNone(visit_occurrence_interval({**meta, "confirmed": False}))
        self.assertIsNone(visit_occurrence_interval({**meta, "occurred_end": 999}))

    def test_legacy_route_proves_only_confirmed_start_points(self):
        meta = self.metadata(visit_location={"version": 1, "role": "guest", "route": [
            {"started_at": 1000}, {"started_at": 2000, "until": 9000},
            {"started_at": False}, {"started_at": "3000"}, {"started_at": float("nan")}]})
        before = copy.deepcopy(meta["visit_location"]["route"][:2])
        self.assertEqual(visit_occurrence_interval(meta), (1000., 2000.))
        self.assertEqual(meta["visit_location"]["route"][:2], before)
        meta["visit_location"]["role"] = []
        self.assertIsNone(visit_occurrence_interval(meta))

    def test_malformed_timestamps_cannot_become_occurrence_evidence(self):
        for value in (None, True, False, 0, -1, "1000", [], {}, float("nan"), float("inf"), 10 ** 400):
            with self.subTest(value=value):
                self.assertEqual(visit_occurrence_metadata({"arrived_at": value, "ended_at": 2000}), {})
                self.assertIsNone(visit_occurrence_interval(self.metadata(occurred_start=value)))
        self.assertEqual(visit_occurrence_metadata({"arrived_at": 1000, "ended_at": 999}),
                         {"occurred_start": 1000.})

    def test_current_manual_or_gist_text_is_never_reconstructed_from_old_metadata(self):
        current = "我亲自保留的新要点：" + "旧正文已经不再适用。" * 90
        meta = self.metadata(visit_confirmation={"content": "stale", "log": ["old"]},
                             status="completed", old_content="并未保留的原始内容")
        before = copy.deepcopy(meta)
        result = visit_memory_excerpt(current, meta)
        self.assertTrue(current.startswith(result[:-1]))
        self.assertLessEqual(len(result), 450)
        self.assertNotIn("并未保留", result)
        self.assertEqual(meta, before)
        self.assertEqual(visit_memory_excerpt(current, meta, limit=0), "")


class VisitStoredViewTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = actor_fixture.VisitActorTests.asyncSetUp
    long_visit = recall_fixture.VisitRecallReviewTests.long_visit

    async def test_saved_times_and_bounded_view_come_only_from_current_confirmed_receipt(self):
        visit = self.long_visit()
        visit.update(arrived_at=1000., ended_at=2000.)
        await self.port.remember(engine_fixture.SESSION, visit)
        rows = await self.db.call("query_memories", session_id=engine_fixture.SESSION)
        row = next(item for item in rows if item["source"] == "life_event")
        meta = json.loads(row["context"])
        self.assertEqual(visit_occurrence_interval(meta), (1000., 2000.))
        self.assertGreater(row["created_at"], 2000.)
        before = copy.deepcopy(row)
        excerpt = visit_memory_excerpt(row["content"], meta)
        for fact in ("[我说]", "扶稳了差点倾倒的樱桃蛋糕", "送你到门口", "这次已告别归家"):
            self.assertIn(fact, excerpt)
        self.assertLessEqual(len(excerpt), 450)
        self.assertEqual(row, before)
        self.assertEqual(self.llm.calls, [])
