"""Trip recall keeps settled events and tolerates unrelated legacy metadata."""
from __future__ import annotations

import json
import unittest

from tests import test_visits_actor as actor_fixture
from tests import test_visits_memory_review_o as memory_fixture


class VisitRecallReview27Tests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = actor_fixture.VisitActorTests.asyncSetUp
    long_visit = memory_fixture.VisitRecallReviewTests.long_visit
    recall = memory_fixture.VisitRecallReviewTests.recall

    async def test_shared_result_is_retained_after_followup_conversation(self):
        visit = self.long_visit()
        visit["log"][-2]["kind"] = "result"
        # Shared scene resolution is followed by actual conversation, rather
        # than being coincidentally the penultimate fact in the memory.
        for index in range(5):
            visit["log"].insert(-1, {"seq": 23 + index,
                "actor_id": visit["remote"]["id"], "kind": "say",
                "text": f"后来又聊了第{index}件小事。" + "窗外的雨还没停。" * 25})
        visit["log"][-1]["seq"] = 28
        row, recalled = await self.recall(visit)
        self.assertIn("[result] 两人扶稳了差点倾倒的樱桃蛋糕", recalled)
        self.assertIn("送你到门口", recalled)
        self.assertLessEqual(len(recalled), 450)
        self.assertEqual(len(self.llm.calls), 1)
        self.assertIn("后来又聊了第4件小事", row["content"])

    async def test_non_object_legacy_context_cannot_break_current_visit_inference(self):
        await self.port.remember(actor_fixture.fixture.SESSION,
                                 {**self.visit, "status": "completed"})
        for context in ([], ["old"], "old", 12, True):
            self.db.raw.add_memory(session_id=actor_fixture.fixture.SESSION,
                persona_id=self.local["persona_key"], scope="group_persona", node_id="study",
                content="旧的普通事件 " + self.visit["remote"]["id"], memory_type="event",
                source="life_event", context=json.dumps(context))
        result = await self.port.decide(actor_fixture.fixture.SESSION, "turn", self.visit)
        self.assertIn("带点心", result["say"])
        materials = json.loads(self.llm.calls[-1]["prompt"])
        self.assertEqual(len(materials["previous_visits"]), 1)
        self.assertIn("棋盘已经摆好了", materials["previous_visits"][0])
        self.assertEqual(len(self.llm.calls), 1)
