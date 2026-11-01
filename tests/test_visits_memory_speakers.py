"""Returning memories retain who spoke without guessing unknown identities."""
from __future__ import annotations

import json
import unittest

from tests import test_engine as engine_fixture
from tests import test_visits_actor as actor_fixture


class VisitMemorySpeakerTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = actor_fixture.VisitActorTests.asyncSetUp

    def conversation(self, role="guest"):
        own = "instance-a:" + self.local["actor_id"]
        friend = self.visit["remote"]["id"]
        return {**self.visit, "status": "completed", "role": role,
                "host_id": friend if role == "guest" else own,
                "guest_id": own if role == "guest" else friend,
                "remote": {**self.visit["remote"], "name": self.local["name"]},
                "log": [
                    {"seq": 1, "actor_id": own, "kind": "say", "text": "我想再下一盘。"},
                    {"seq": 2, "actor_id": friend, "kind": "say", "text": "我想先休息。"},
                    {"seq": 3, "actor_id": own, "kind": "action_plan", "text": "我去把棋子收好。"},
                    {"seq": 4, "actor_id": "unknown-actor", "kind": "say", "text": "我留下看书。"},
                ]}

    async def stored(self, visit):
        await self.port.remember(engine_fixture.SESSION, visit)
        rows = await self.db.call("query_memories", session_id=engine_fixture.SESSION)
        return next(row for row in rows if row["source"] == "life_event")

    async def test_same_named_characters_keep_first_person_speakers_when_recalled(self):
        for role in ("guest", "host"):
            with self.subTest(role=role):
                visit = self.conversation(role)
                visit["id"] = "speaker-" + role
                row = await self.stored(visit)
                self.assertIn("[我说] 我想再下一盘。", row["content"])
                self.assertIn("[对方（" + self.local["name"] + "）说] 我想先休息。", row["content"])
                recalled = await self.port._past_visits(engine_fixture.SESSION,
                                                       self.local["persona_key"], visit["remote"])
                self.assertIn("[我说] 我想再下一盘。", recalled[0])
                self.assertIn("）说] 我想先休息。", recalled[0])

    async def test_unknown_actor_is_not_assigned_to_self_or_friend(self):
        row = await self.stored(self.conversation())
        self.assertIn("[说话人未确认] 我留下看书。", row["content"])
        self.assertNotIn("[我说] 我留下看书。", row["content"])
        self.assertNotIn("）说] 我留下看书。", row["content"])

    async def test_action_plan_keeps_its_owner_and_uncompleted_status(self):
        row = await self.stored(self.conversation())
        self.assertIn("[我当时提出的方案（尚未证明完成）] 我去把棋子收好。", row["content"])

    async def test_friend_and_unknown_plans_are_not_assigned_to_self(self):
        visit = self.conversation()
        visit["log"] = [
            {"seq": 1, "actor_id": visit["remote"]["id"], "kind": "action_plan", "text": "我去泡茶。"},
            {"seq": 2, "actor_id": "unknown-actor", "kind": "action_plan", "text": "我去拿书。"},
        ]
        row = await self.stored(visit)
        self.assertIn("[对方（" + self.local["name"] + "）当时提出的方案（尚未证明完成）] 我去泡茶。", row["content"])
        self.assertIn("[提出者未确认的方案（尚未证明完成）] 我去拿书。", row["content"])

    async def test_legacy_short_actor_ids_are_recognised_without_names(self):
        visit = self.conversation()
        visit["remote"]["actor_id"] = "actor-b"
        visit["log"] = [
            {"seq": 1, "actor_id": self.local["actor_id"], "kind": "say", "text": "我先回家。"},
            {"seq": 2, "actor_id": "actor-b", "kind": "say", "text": "我送你到门口。"},
        ]
        row = await self.stored(visit)
        self.assertIn("[我说] 我先回家。", row["content"])
        self.assertIn("）说] 我送你到门口。", row["content"])

    async def test_ambiguous_short_identity_does_not_override_the_public_identity(self):
        visit = self.conversation()
        visit["remote"]["actor_id"] = self.local["actor_id"]
        visit["log"][0]["actor_id"] = self.local["actor_id"]
        row = await self.stored(visit)
        self.assertIn("[说话人未确认] 我想再下一盘。", row["content"])
        self.assertIn("[我当时提出的方案（尚未证明完成）] 我去把棋子收好。", row["content"])

    async def test_old_unattributed_memories_have_a_model_rule_against_guessing(self):
        await self.port.decide(engine_fixture.SESSION, "turn", self.visit)
        self.assertIn("未标注说话人的旧台词或未确认来源不能猜测归属", self.llm.calls[-1]["system_prompt"])

    async def test_late_confirmation_does_not_replace_manually_edited_memory(self):
        visit = self.conversation()
        row = await self.stored(visit)
        self.db.raw.revise_memory(row["id"], {"content": "我亲手整理的串门记忆"}, manual=True, reason="测试编辑")
        visit["log"].append({"seq": 5, "actor_id": visit["remote"]["id"], "kind": "say", "text": "下次见。"})
        await self.port.remember(engine_fixture.SESSION, visit)
        current = await self.db.call("memory_by_id", row["id"])
        self.assertEqual(current["content"], "我亲手整理的串门记忆")
        self.assertEqual(json.loads(current["context"])["visit_id"], visit["id"])
