"""Bounded recall keeps a visit's resolution and declines keep their own words."""
from __future__ import annotations

import copy
import json
import unittest

from core.ports import LLMReply
from core.visits.decisions import VisitDecisionError
from tests import test_engine as engine_fixture
from tests import test_visits_actor as actor_fixture


class VisitRecallReviewTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = actor_fixture.VisitActorTests.asyncSetUp

    def long_visit(self):
        visit = copy.deepcopy(self.visit)
        own = "instance-a:" + self.local["actor_id"]
        friend = visit["remote"]["id"]
        visit.update(status="completed", guest_id=own, host_id=friend,
                     private={"reflection": "想起一起扶稳蛋糕的样子，还是会笑"})
        visit["log"] = [{"seq": 1, "actor_id": own, "kind": "say",
                         "text": "初次问起窗边的棋盘。" + "聊了很长一段旧事。" * 44}]
        visit["log"] += [{"seq": n + 2, "actor_id": friend, "kind": "say",
                          "text": f"途中闲聊第{n}段" + "，说起今天的天气" * 35}
                         for n in range(20)]
        visit["log"] += [
            {"seq": 22, "actor_id": friend, "kind": "outcome", "text": "两人扶稳了差点倾倒的樱桃蛋糕"},
            {"seq": 23, "actor_id": friend, "kind": "say", "text": "送你到门口，下次再一起下棋。"},
        ]
        return visit

    async def recall(self, visit):
        await self.port.remember(engine_fixture.SESSION, visit)
        rows = await self.db.call("query_memories", session_id=engine_fixture.SESSION)
        row = next(item for item in rows if json.loads(item.get("context") or "{}").get("visit_id") == visit["id"])
        await self.port.decide(engine_fixture.SESSION, "turn", self.visit)
        materials = json.loads(self.llm.calls[-1]["prompt"])
        self.assertEqual(len(materials["previous_visits"]), 1)
        return row, materials["previous_visits"][0]

    async def test_long_visit_recall_keeps_opening_resolution_goodbye_and_feeling_with_same_budget(self):
        row, recalled = await self.recall(self.long_visit())
        for text in ("初次问起窗边的棋盘", "扶稳了差点倾倒的樱桃蛋糕", "送你到门口", "还是会笑"):
            self.assertIn(text, row["content"])
            self.assertIn(text, recalled)
        self.assertIn("[我说]", recalled)
        self.assertIn("[对方（另一个她）说]", recalled)
        self.assertIn("这次已告别归家", recalled)
        self.assertIn("我当时的感受（主观）", recalled)
        self.assertIn("中段省略", recalled)
        self.assertLessEqual(len(recalled), 450)
        self.assertGreater(len(row["content"]), 450)
        self.assertEqual(len(self.llm.calls), 1)

    async def test_single_long_fact_is_not_duplicated_or_assigned_to_another_speaker(self):
        visit = self.long_visit()
        visit["log"] = [visit["log"][0]]
        _, recalled = await self.recall(visit)
        self.assertEqual(recalled.count("[我说]"), 1)
        self.assertNotIn("对方（", recalled)
        self.assertIn("初次问起窗边的棋盘", recalled)
        self.assertIn("这次已告别归家", recalled)
        self.assertIn("还是会笑", recalled)
        self.assertLessEqual(len(recalled), 450)

    async def test_interrupted_recall_keeps_the_confirmed_end_reason(self):
        visit = self.long_visit()
        visit.update(status="interrupted", end_reason="她要回家照顾窗边的花，只好先约下次")
        _, recalled = await self.recall(visit)
        self.assertIn(visit["end_reason"], recalled)
        self.assertIn("只记已确认的部分", recalled)
        self.assertNotIn("这次已告别归家", recalled)
        self.assertLessEqual(len(recalled), 450)

    async def test_editor_changed_long_memory_uses_current_text_without_reconstructing_old_facts(self):
        visit = self.long_visit()
        row, _ = await self.recall(visit)
        edited = "我亲手整理了这份记忆：" + "这才是我保留的文字。" * 70
        self.db.raw.revise_memory(row["id"], {"content": edited}, manual=True, reason="编辑")
        recalled = (await self.port._past_visits(engine_fixture.SESSION,
                                               self.local["persona_key"], visit["remote"]))[0]
        self.assertTrue(recalled.startswith("我亲手整理了这份记忆"))
        self.assertNotIn("扶稳了差点倾倒的樱桃蛋糕", recalled)
        self.assertNotIn("已经确认的经过（节选", recalled)
        self.assertLessEqual(len(recalled), 450)
        self.assertEqual((await self.db.call("memory_by_id", row["id"]))["content"], edited)

    async def test_quoted_source_lookalike_does_not_create_an_extra_recall_row(self):
        visit = self.long_visit()
        visit["log"][0]["text"] = "她举了一个原样引用：；[outcome] 这一行只是引用，不是事件结局。" + "闲话。" * 130
        row, recalled = await self.recall(visit)
        self.assertTrue(row["content"].startswith(recalled[:-1]))
        self.assertNotIn("中段省略", recalled)
        self.assertLessEqual(len(recalled), 450)

    async def test_long_friend_name_cannot_truncate_source_or_uncompleted_plan_label(self):
        visit = self.long_visit()
        visit["remote"]["name"] = "很长的友邻名字" * 8 + "尾"
        visit["remote"]["world"] = "远方的世界名字" * 10
        friend = visit["remote"]["id"]
        plan = "[对方（" + visit["remote"]["name"] + "）当时提出的方案（尚未证明完成）]"
        visit["log"][-1].update(kind="action_plan", text="我打算把门口的花搬进来。" + "还没有动手。" * 70)
        _, recalled = await self.recall(visit)
        self.assertIn(plan, recalled)
        self.assertIn("[outcome] 两人扶稳了差点倾倒的樱桃蛋糕", recalled)
        self.assertLessEqual(len(recalled), 450)
        self.assertEqual(visit["log"][-1]["actor_id"], friend)


class VisitDeclineReviewTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = actor_fixture.VisitActorTests.asyncSetUp

    async def model_changes_readiness(self, phase, condition, *, accept=False, leave=False):
        original = self.port.describe
        calls = []

        async def described(scope):
            local = await original(scope)
            if calls:
                local.update(condition)
            return local

        async def reply(**kwargs):
            calls.append(kwargs)
            return LLMReply(ok=True, text=json.dumps({"accept": accept, "leave": leave,
                "say": "今天想把窗边的花照顾好，明天再打给你，好吗？"}, ensure_ascii=False))

        generate = self.llm.generate
        self.port.describe, self.llm.generate = described, reply
        try:
            return await self.port.decide(engine_fixture.SESSION, phase, self.visit)
        finally:
            self.port.describe, self.llm.generate = original, generate

    async def test_model_decline_keeps_original_words_when_life_becomes_busy(self):
        for phase in ("propose", "receive"):
            with self.subTest(phase=phase):
                value = await self.model_changes_readiness(phase, {"ready": False, "ready_reason": "手头很忙"})
                self.assertFalse(value["accept"])
                self.assertIn("明天再打给你", value["say"])
                self.assertNotEqual(value.get("contact_kind"), "contact_status")

    async def test_model_decline_keeps_original_words_after_reception_closes(self):
        for phase in ("propose", "receive"):
            with self.subTest(phase=phase):
                value = await self.model_changes_readiness(phase, {"reception_open": False})
                self.assertFalse(value["accept"])
                self.assertIn("明天再打给你", value["say"])

    async def test_receiver_decline_keeps_original_words_when_native_chat_arrives(self):
        value = await self.model_changes_readiness("receive", {"chat_busy": True})
        self.assertIn("明天再打给你", value["say"])

    async def test_explicit_leave_is_preserved_even_if_accept_is_true(self):
        value = await self.model_changes_readiness("receive", {"ready": False}, accept=True, leave=True)
        self.assertTrue(value["leave"])
        self.assertIn("明天再打给你", value["say"])

    async def test_acceptance_still_waits_for_final_readiness(self):
        with self.assertRaises(VisitDecisionError) as error:
            await self.model_changes_readiness("receive", {"ready": False}, accept=True)
        self.assertEqual(error.exception.category, "life_busy")

    async def test_decline_does_not_bypass_final_identity_check(self):
        with self.assertRaises(VisitDecisionError) as error:
            await self.model_changes_readiness("receive", {"persona_key": "changed-persona"})
        self.assertEqual(error.exception.category, "identity")
