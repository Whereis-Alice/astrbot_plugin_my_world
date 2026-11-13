"""Actual local chat receipts stay on their journey and never become peer facts."""
from __future__ import annotations

import copy
import asyncio
import json
from types import SimpleNamespace
import unittest

from core.visit_chat_bridge import (MAX_NOTES, capture_visit_chat_owner, local_chat_experiences,
                                   local_experience_lines, record_visit_chat, record_visit_reads,
                                   reconcile_visit_chat)
from core.visit_presence import presence_text, visit_activity_text, visit_context_text
from tests import test_engine as engine_fixture
from tests import test_visits_actor as actor_fixture
from core.engine.models import TickOutcome
from core.engine.speech_receipts import ReceiptOwner, remember_accepted
from core.visits import VisitConfig, VisitService
from core.visits.protocol import public_actor

GROUP = "test:GroupMessage:1"


class MemoryKV:
    def __init__(self):
        self.values = {}

    async def call(self, op, key, value=None):
        if op == "kv_get":
            return copy.deepcopy(self.values.get(key))
        if op == "kv_set":
            self.values[key] = copy.deepcopy(value)
            return None
        raise AssertionError(op)


class VisitChatBridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = 1100.
        self.visit = {"id": "old-trip", "scope": GROUP, "role": "guest", "status": "active",
            "arrived_at": 1000., "guest_id": "self-instance:self-actor", "host_id": "other:peer",
            "local": {"actor_id": "self-actor", "persona_key": "my-persona"},
            "remote": {"id": "other:peer", "name": "朋友"}, "log": []}
        self.visits = {self.visit["id"]: self.visit}
        self.active = self.visit
        self.wakes = []

        async def wake(**payload):
            self.wakes.append(payload)

        self.service = SimpleNamespace(_state={"visits": self.visits}, _closed=False, _loaded=True,
            active=lambda scope: self.active if scope == GROUP else None, wake_local_experience=wake)
        self.engine = SimpleNamespace(visits=self.service, db=MemoryKV(), _now=lambda: self.clock,
                                      state_key=lambda session: session)

    def owner(self):
        return capture_visit_chat_owner(self.engine, GROUP, persona_key="my-persona", expected_state_key=GROUP)

    async def test_reread_is_idempotent_and_suggestion_keeps_its_source_label(self):
        owner = self.owner()
        rows = [{"event_id": "human-1", "origin": GROUP, "raw_text": "要不要带她去河边玩？"}]
        await record_visit_reads(self.engine, owner, rows)
        await record_visit_reads(self.engine, owner, rows)
        data = await local_chat_experiences(self.engine, self.visit)
        self.assertEqual(data["seq"], 1)
        self.assertEqual(len(data["notes"]), 1)
        self.assertEqual(data["notes"][0]["kind"], "read")
        self.assertIn("未证明完成", local_experience_lines(data))
        self.assertEqual(self.visit["log"], [])
        self.assertEqual(len(self.wakes), 1)
        self.assertNotIn("text", self.wakes[0])

    async def test_draft_and_foreign_or_private_chat_do_not_get_receipts(self):
        owner = self.owner()
        self.assertFalse(await record_visit_chat(self.engine, owner, kind="draft", text="尚未发送", source_id="draft"))
        await record_visit_reads(self.engine, owner, [
            {"event_id": "foreign", "origin": "test:GroupMessage:2", "text": "别人群里的计划"},
            {"event_id": "self", "origin": GROUP, "text": "旧回复", "is_self": True},
        ])
        self.assertEqual((await local_chat_experiences(self.engine, self.visit))["seq"], 0)
        self.assertIsNone(capture_visit_chat_owner(self.engine, "test:FriendMessage:1", persona_key="my-persona"))
        self.assertIsNone(capture_visit_chat_owner(self.engine, GROUP, persona_key="someone-else"))
        self.assertIsNone(capture_visit_chat_owner(self.engine, GROUP, persona_key="my-persona", expected_state_key="foreign"))

    async def test_late_completed_read_goes_to_original_journey_not_new_trip(self):
        owner = self.owner()
        self.visit.update(status="completed", ended_at=1110.)
        self.active = copy.deepcopy(self.visit)
        self.active.update(id="new-trip", status="active", arrived_at=1120., ended_at=0.)
        self.visits[self.active["id"]] = self.active
        self.clock = 1140.
        self.assertTrue(await record_visit_chat(self.engine, owner, kind="read", text="实际请求里读过的原话", source_id="before-end"))
        self.assertFalse(await record_visit_chat(self.engine, owner, kind="sent", text="结束之后才说的话", source_id="after-end", at=self.clock))
        old = await local_chat_experiences(self.engine, self.visit)
        new = await local_chat_experiences(self.engine, self.active)
        self.assertEqual(old["notes"][0]["at"], 1100.)
        self.assertEqual(new["seq"], 0)

    async def test_identity_changed_or_invitation_only_cannot_capture(self):
        self.visit["arrived_at"] = 0
        self.assertIsNone(self.owner())
        self.visit["arrived_at"] = 1000.
        owner = self.owner()
        self.visit["local"]["persona_key"] = "changed"
        self.assertFalse(await record_visit_chat(self.engine, owner, kind="sent", text="旧角色的话", source_id="old"))

    async def test_text_eviction_retains_receipt_id_and_bounded_prompt(self):
        owner = self.owner()
        for index in range(MAX_NOTES + 12):
            await record_visit_chat(self.engine, owner, kind="sent", text=f"第{index}句实际回复", source_id=str(index))
        self.assertFalse(await record_visit_chat(self.engine, owner, kind="sent", text="第0句实际回复", source_id="0"))
        data = await local_chat_experiences(self.engine, self.visit, limit=64)
        self.assertEqual(len(data["notes"]), MAX_NOTES)
        self.assertEqual(data["seq"], MAX_NOTES + 12)
        self.assertLessEqual(len(local_experience_lines(data, limit=240)), 240)

    async def test_wake_failure_does_not_lose_an_actual_receipt(self):
        async def broken(**kwargs):
            raise OSError("unavailable")
        self.service.wake_local_experience = broken
        self.assertTrue(await record_visit_chat(self.engine, self.owner(), kind="sent", text="我实际答应先等一会", source_id="sent", significant=True))
        self.assertEqual((await local_chat_experiences(self.engine, self.visit))["notes"][0]["kind"], "sent")

    async def test_late_ordinary_read_notifies_sequence_without_requesting_wake(self):
        owner = self.owner()
        self.visit.update(status="completed", ended_at=1110., remembered=True)
        await record_visit_reads(self.engine, owner, [{"event_id": "ordinary", "origin": GROUP, "text": "明天的天气预报是晴天"}])
        self.assertEqual(len(self.wakes), 1)
        self.assertFalse(self.wakes[0]["significant"])
        self.assertEqual(self.wakes[0]["visit_id"], "old-trip")
        self.assertEqual(self.wakes[0]["seq"], 1)

    async def test_durable_receipt_recovers_failed_notification_after_reload(self):
        async def unavailable(**payload):
            raise OSError("reload interrupted notification")
        self.service.wake_local_experience = unavailable
        owner = self.owner()
        self.visit.update(status="completed", ended_at=1110., remembered=True)
        await record_visit_chat(self.engine, owner, kind="read", text="这句确实读到过", source_id="lost-wake")
        self.assertNotIn("local_experience_seq", self.visit)

        async def recovered(**payload):
            self.wakes.append(payload)
            self.visit["local_experience_seq"] = payload["seq"]
            self.visit.pop("remembered", None)
            return True
        self.service.wake_local_experience = recovered
        await reconcile_visit_chat(self.engine)
        self.assertEqual(self.visit["local_experience_seq"], 1)
        self.assertNotIn("remembered", self.visit)
        self.assertFalse(self.wakes[-1]["significant"])
        self.clock += 31
        await reconcile_visit_chat(self.engine)
        self.assertEqual(len(self.wakes), 1)

    async def test_recovery_wakes_only_new_important_experiences(self):
        async def unavailable(**payload):
            raise OSError("temporarily unavailable")
        self.service.wake_local_experience = unavailable
        await record_visit_chat(self.engine, self.owner(), kind="read", text="要不要带她去湖边", source_id="idea", significant=True)
        await record_visit_chat(self.engine, self.owner(), kind="read", text="普通聊天", source_id="ordinary")

        async def recovered(**payload):
            self.wakes.append(payload)
            self.visit["local_experience_seq"] = payload["seq"]
            return True
        self.service.wake_local_experience = recovered
        await reconcile_visit_chat(self.engine)
        self.assertTrue(self.wakes[-1]["significant"])
        self.assertEqual(self.wakes[-1]["seq"], 2)
        await record_visit_chat(self.engine, self.owner(), kind="read", text="另一句普通聊天", source_id="ordinary-2")
        self.visit["local_experience_seq"] = 2
        self.clock += 31
        await reconcile_visit_chat(self.engine)
        self.assertFalse(self.wakes[-1]["significant"])

    async def test_end_while_waiting_for_journal_read_cannot_record_later_speech(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.engine.db.call
        async def blocked(op, key, value=None):
            if op == "kv_get":
                entered.set()
                await release.wait()
            return await original(op, key, value)
        self.engine.db.call = blocked
        task = asyncio.create_task(record_visit_chat(self.engine, self.owner(), kind="sent",
            text="接受发送回执时已经过了实际归家时间", source_id="late-speech", at=1100.))
        await entered.wait()
        self.visit.update(status="completed", ended_at=1090.)
        release.set()
        self.assertFalse(await task)
        self.assertEqual(self.wakes, [])


class VisitMemoryLocalAdditionTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = actor_fixture.VisitActorTests.asyncSetUp

    async def stored(self):
        await self.port.remember(engine_fixture.SESSION, self.visit)
        rows = await self.db.call("query_memories", session_id=engine_fixture.SESSION)
        return next(row for row in rows if json.loads(row.get("context") or "{}").get("visit_id") == self.visit["id"])

    async def test_same_shared_log_adds_private_stage_reflection_on_one_receipt(self):
        self.visit.update(status="completed", ended_at=self.clock.now())
        old = await self.stored()
        self.visit["private_moments"] = [{"seq": 1, "at": self.clock.now(), "reflection": "她真的记得我上次没做完的东西"}]
        self.visit["private"] = {"reflection": "这次有点意外"}
        updated = await self.stored()
        self.assertEqual(updated["id"], old["id"])
        self.assertIn("没做完的东西", updated["content"])
        self.assertGreater(updated["next_review_at"], self.clock.now())
        self.visit["private_moments"][0]["reflection"] = "未经依据改写旧感受"
        rejected = await self.stored()
        self.assertEqual(rejected["content"], updated["content"])

    async def test_group_suggestion_remains_local_and_late_note_updates_one_memory(self):
        self.active = self.visit
        owner = capture_visit_chat_owner(self.engine, engine_fixture.SESSION, persona_key=self.local["persona_key"])
        self.assertIsNotNone(owner)
        self.visit.update(status="completed", ended_at=self.clock.now() + 1)
        old = await self.stored()
        await record_visit_chat(self.engine, owner, kind="read", text="要不要一起做蛋糕", source_id="read-1")
        updated = await self.stored()
        self.assertEqual(old["id"], updated["id"])
        self.assertIn("建议（未证明完成）", updated["content"])
        self.assertNotIn("要不要一起做蛋糕", json.dumps(self.visit["log"], ensure_ascii=False))
        self.assertEqual(len(json.loads(updated["context"])["visit_confirmation"]["log"]), len(self.visit["log"]))

    async def test_finish_reviews_visited_scene_even_when_home_is_busy(self):
        self.visit.update(status="completed", ended_at=self.clock.now())
        self.llm.default_reply = '{"reflection":"想起她刚才那一步，还是觉得好笑"}'
        result = await self.port.decide(engine_fixture.SESSION, "finish", self.visit)
        self.assertIn("好笑", result["reflection"])
        prompt = json.loads(self.llm.calls[-1]["prompt"])
        self.assertEqual(prompt["current_place"], "窗边")
        self.assertNotIn("current_personal_activity", prompt)
        self.assertEqual(prompt["review_of_ended_visit"]["status"], "completed")

    async def test_only_platform_accepted_speech_enters_the_local_visit(self):
        self.active = self.visit
        outcome = TickOutcome(session_id=engine_fixture.SESSION, speech_kind="reply")
        outcome.receipt_owner = ReceiptOwner(engine_fixture.SESSION, engine_fixture.SESSION,
                                            self.local["persona_key"], self.engine.world)
        outcome.messages = ["这句只是准备发送"]
        self.assertEqual((await local_chat_experiences(self.engine, self.visit))["seq"], 0)
        await remember_accepted(self.engine, outcome, engine_fixture.SESSION, "这句平台实际接受了", "accepted-1")
        await remember_accepted(self.engine, outcome, engine_fixture.SESSION, "这句平台实际接受了", "accepted-1")
        data = await local_chat_experiences(self.engine, self.visit)
        self.assertEqual(data["seq"], 1)
        self.assertEqual(data["notes"][0]["text"], "这句平台实际接受了")

    async def test_late_ordinary_sent_note_appends_to_old_memory_without_losing_read(self):
        self.active = self.visit
        owner = capture_visit_chat_owner(self.engine, engine_fixture.SESSION, persona_key=self.local["persona_key"])
        self.visit.update(status="completed", ended_at=self.clock.now() + 1)
        await record_visit_chat(self.engine, owner, kind="read", text="有人在群里问天气", source_id="read-weather")
        old = await self.stored()
        notices = []

        async def notice(**payload):
            notices.append(payload)
        self.engine.visits.wake_local_experience = notice
        await record_visit_chat(self.engine, owner, kind="sent", text="我答了天气的问题", source_id="sent-weather")
        updated = await self.stored()
        self.assertEqual(updated["id"], old["id"])
        self.assertIn("有人在群里问天气", updated["content"])
        self.assertIn("我答了天气的问题", updated["content"])
        self.assertEqual(len(notices), 1)
        self.assertFalse(notices[0]["significant"])
        self.assertEqual(notices[0]["seq"], 2)
        self.assertEqual(len(json.loads(updated["context"])["visit_confirmation"]["log"]), len(self.visit["log"]))

    async def test_reload_recovers_unnotified_chat_into_the_existing_sqlite_memory(self):
        config = VisitConfig(enabled=True, auto_enabled=False, history_keep=128)
        service = VisitService(self.db, config, self.port, clock=self.clock.now)
        self.addAsyncCleanup(service.close)
        await service._load()
        remote = {"actor_id": "peer", "persona_key": "peer-persona", "name": "朋友",
                  "world": "朋友的世界", "ready": True, "home_node": "home"}
        peer = service._add_peer(engine_fixture.SESSION, self.local, public_actor(remote, "other-instance"),
                                 "http://not-contacted.example", "test-key")
        visit = service._new_visit(engine_fixture.SESSION, peer, self.local, "chat-recovery-trip",
                                    "guest", "玩一会", False)
        visit.update(status="active", arrived_at=self.clock.now(), outing_version=1)
        service._append(visit, peer["id"], "say", "这一局先到这里")
        service._state["visits"][visit["id"]] = visit
        self.engine.visits = service
        owner = capture_visit_chat_owner(self.engine, engine_fixture.SESSION, persona_key=self.local["persona_key"])
        self.assertIsNotNone(owner)
        await service._finish(visit, "completed", "已经告别", notify=False)
        visit["finish_review_attempted"] = True
        await service._remember(visit["id"])
        rows = await self.db.call("query_memories", session_id=engine_fixture.SESSION)
        old = next(row for row in rows if json.loads(row.get("context") or "{}").get("visit_id") == visit["id"])

        async def unavailable(**payload):
            raise OSError("notification interrupted")
        service.wake_local_experience = unavailable
        await record_visit_chat(self.engine, owner, kind="read", text="群友约我们下次试试折纸", source_id="before-end")
        self.assertTrue(visit["remembered"])
        await service.close()
        resumed = VisitService(self.db, config, self.port, clock=self.clock.now)
        self.addAsyncCleanup(resumed.close)
        self.engine.visits = resumed
        await resumed._load()
        restored = resumed._state["visits"][visit["id"]]
        self.assertTrue(restored["remembered"])
        await reconcile_visit_chat(self.engine)
        self.assertNotIn("remembered", restored)
        await resumed._remember(visit["id"])
        rows = await self.db.call("query_memories", session_id=engine_fixture.SESSION)
        updated = next(row for row in rows if row["id"] == old["id"])
        self.assertIn("下次试试折纸", updated["content"])
        self.assertTrue(restored["remembered"])
        self.assertEqual(len([row for row in rows if json.loads(row.get("context") or "{}").get("visit_id") == visit["id"]]), 1)


class SharedActivityPresenceTests(unittest.TestCase):
    def test_open_actions_and_public_updates_parse_without_forcing_a_utterance(self):
        parsed = actor_fixture.VisitActorPort._parse_decision("turn",
            '{"action_step":{"description":"用手指比一比短木条","seconds":0.8,"waiting":false,"requires_peer":false}}')
        self.assertEqual(parsed["say"], "")
        self.assertEqual(parsed["action_step"]["seconds"], .8)
        settled = actor_fixture.VisitActorPort._parse_decision("turn",
            '{"scene_update":{"summary":"选定了两根一样长的木条","action_ids":["s0-a1"],"objects":["两根木条"],"pending":["还没组装"],"done":false}}')
        self.assertFalse(settled["scene_update"]["done"])

    def test_milestone_enters_context_but_running_action_is_not_a_result(self):
        visit = {"id": "one", "role": "guest", "guest_id": "self", "host_id": "peer",
            "local": {"actor_id": "self"}, "remote": {"id": "peer", "name": "朋友", "world": "花园"},
            "outing_version": 1, "outing": {"version": 1, "stage": 0, "place": "长椅", "activity": "拼一件小东西", "started_at": 100., "until": 300.},
            "shared_activity": {"stage": 0, "action_steps": {"self": [{"description": "拿起短木条比一比", "status": "running", "until": 150.}]}},
            "log": [{"actor_id": "peer", "kind": "milestone", "text": "选定了两根一样长的木条"}]}
        self.assertIn("正在拿起短木条", visit_activity_text(visit, now=120.))
        self.assertIn("结果尚未确认", visit_activity_text(visit, now=160.))
        self.assertIn("共同确认片刻", visit_context_text(visit, now=160.))
        visit["outing"].update(travel_seconds=40., origin_place="门口", started_at=200.)
        self.assertIn("尚未抵达", presence_text(visit, now=180.))


if __name__ == "__main__":
    unittest.main()
