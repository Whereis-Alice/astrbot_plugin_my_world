"""Unfinished shared work survives a trip without becoming a future obligation."""
from __future__ import annotations

import copy
import json
import unittest

from tests.fake_astrbot import install

install()

from core.visit_recall import visit_memory_excerpt  # noqa: E402
from tests import test_engine as engine_fixture  # noqa: E402
from tests import test_visits_actor as actor_fixture  # noqa: E402
from tests import test_visits_memory_review_o as recall_fixture  # noqa: E402


class VisitFollowupMemoryTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = actor_fixture.VisitActorTests.asyncSetUp
    long_visit = recall_fixture.VisitRecallReviewTests.long_visit

    def journey(self):
        visit = self.long_visit()
        visit.update(ended_at=self.clock.now(), shared_activity={
            "version": 1, "stage": 0, "revision": 8, "pending": ["棋盘上的最后一局还没分出胜负"],
            "milestones": [],
        }, private_moments=[{"seq": 1, "at": self.clock.now(), "promise": "下次带来自己画的新棋子"}])
        return visit

    async def stored(self, visit):
        await self.port.remember(engine_fixture.SESSION, visit)
        rows = await self.db.call("query_memories", session_id=engine_fixture.SESSION)
        return next(row for row in rows if json.loads(row.get("context") or "{}").get("visit_id") == visit["id"])

    async def test_pending_and_personal_promise_reach_bounded_next_visit_materials(self):
        visit = self.journey()
        row = await self.stored(visit)
        recalled = (await self.port._past_visits(engine_fixture.SESSION, self.local["persona_key"], visit["remote"]))[0]
        for value in ("最后一局还没分出胜负", "下次带来自己画的新棋子", "成立与否以双方原话为准", "再见时可重选"):
            self.assertIn(value, row["content"])
            self.assertIn(value, recalled)
        self.assertIn("扶稳了差点倾倒的樱桃蛋糕", recalled)
        self.assertIn("这次已告别归家", recalled)
        self.assertLessEqual(len(recalled), 450)
        self.assertEqual(self.llm.calls, [])

    async def test_new_confirmed_revision_can_clear_old_unfinished_snapshot(self):
        visit = self.journey()
        old = await self.stored(visit)
        visit["shared_activity"].update(revision=9, pending=[])
        visit["log"].append({"seq": 24, "kind": "milestone", "actor_id": visit["host_id"], "text": "这一局已结束，棋子收回盒里"})
        updated = await self.stored(visit)
        self.assertEqual(updated["id"], old["id"])
        self.assertNotIn("最后一局还没分出胜负", updated["content"])
        self.assertIn("棋子收回盒里", updated["content"])
        await self.port.remember(engine_fixture.SESSION, visit)
        self.assertEqual((await self.db.call("memory_by_id", updated["id"]))["content"], updated["content"])

    async def test_same_revision_cannot_rewrite_a_saved_pending_snapshot(self):
        visit = self.journey()
        old = await self.stored(visit)
        visit["shared_activity"]["pending"] = ["未确认的新事情"]
        visit["log"].append({"seq": 24, "kind": "say", "actor_id": visit["host_id"], "text": "再见"})
        self.assertEqual((await self.stored(visit))["content"], old["content"])

    async def test_manual_edit_does_not_recreate_old_followups(self):
        visit = self.journey()
        old = await self.stored(visit)
        edited = "我自己整理过的相处记忆：" + "只保留当时的笑话。" * 80
        self.db.raw.revise_memory(old["id"], {"content": edited}, manual=True, reason="自己整理")
        visit["shared_activity"].update(revision=9, pending=["新的计划"])
        self.assertEqual((await self.stored(visit))["content"], edited)
        recalled = (await self.port._past_visits(engine_fixture.SESSION, self.local["persona_key"], visit["remote"]))[0]
        self.assertTrue(edited.startswith(recalled[:-1]))
        self.assertNotIn("新棋子", recalled)

    async def test_stale_additions_metadata_cannot_invent_a_followup(self):
        row = await self.stored(self.journey())
        meta = json.loads(row["context"])
        before = copy.deepcopy(row)
        meta["visit_additions"]["unfinished"][-1]["pending"] = ["我答应了转账"]
        recalled = visit_memory_excerpt(row["content"], meta)
        self.assertNotIn("转账", recalled)
        self.assertEqual(row, before)

    async def test_only_unsettled_active_choices_become_unfinished_notes(self):
        visit = self.journey()
        visit["shared_activity"].update(pending=[], action_steps={visit["host_id"]: [
            {"status": "elapsed", "description": "整理没做完的拼图"},
            {"status": "elapsed", "description": "已收起棋盘", "settled_revision": 7},
            {"status": "cancelled", "description": "早已取消的散步"},
            {"status": "declined", "description": "没有答应的游戏"},
        ]})
        row = await self.stored(visit)
        self.assertIn("尚未确认结果：整理没做完的拼图", row["content"])
        for item in ("早已取消", "没有答应", "已收起棋盘"):
            self.assertNotIn(item, row["content"])

    async def test_missing_shared_extension_does_not_put_actor_at_future_destination(self):
        visit = copy.deepcopy(self.visit)
        now = self.clock.now()
        visit.update(outing_version=1, outing={"version": 1, "stage": 0, "place": "山顶", "activity": "看星星",
            "started_at": now + 120, "until": now + 420, "travel_seconds": 120, "origin_place": "庭院", "route": []},
            scene={"place": "山顶", "activity": "看星星"})
        self.llm.default_reply = '{"say":"看看路边的小花"}'
        await self.port.decide(engine_fixture.SESSION, "turn", visit)
        materials = json.loads(self.llm.calls[-1]["prompt"])
        self.assertIn("途中", materials["current_place"])
        self.assertIn("尚未抵达", materials["current_place"])
        self.assertFalse(materials["activity_progress"]["ongoing"])
        self.assertEqual(materials["activity_progress"]["remaining_minutes"], 5)
        self.assertEqual(materials["activity_progress"]["starts_in_seconds"], 120)

    async def test_future_activity_without_travel_remains_at_known_origin(self):
        visit = copy.deepcopy(self.visit)
        now = self.clock.now()
        visit.update(outing_version=1, outing={"version": 1, "stage": 0, "place": "屋顶", "activity": "看云",
            "started_at": now + 120, "until": now + 420, "origin_place": "客厅", "route": []},
            scene={"place": "屋顶", "activity": "看云"})
        self.llm.default_reply = '{"say":"先在客厅等一会儿"}'
        await self.port.decide(engine_fixture.SESSION, "turn", visit)
        self.assertEqual(json.loads(self.llm.calls[-1]["prompt"])["current_place"], "客厅")

    async def test_control_stays_in_memory_after_the_action_is_pruned(self):
        visit = self.journey()
        visit["shared_activity"].update(pending=[], action_steps={})
        visit["log"].append({"seq": 24, "kind": "action_control", "actor_id": visit["guest_id"],
            "action_id": "s0-a1", "operation": "cancel", "text": "取消了自己的动作：去花园摘果子"})
        row = await self.stored(visit)
        self.assertIn("我取消或拒绝", row["content"])
        self.assertIn("取消了自己的动作：去花园摘果子", row["content"])


if __name__ == "__main__":
    unittest.main()
