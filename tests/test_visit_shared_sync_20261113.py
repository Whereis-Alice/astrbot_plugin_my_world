"""Catch-up, delayed signed replies and exact shared activity departure clocks."""
from __future__ import annotations

import asyncio
import copy
import json
import unittest

from core.visits.protocol import clean_decision, envelope
from core.visits.snapshots import validate_snapshot
from core.visits.transport import VisitTransportError
from tests import test_visit_shared_flow_20261113 as fixture
from tests import test_visits_flow_review_r as restart_fixture


action = fixture.action


class SharedVisitSyncTests(unittest.IsolatedAsyncioTestCase):
    make_world = fixture.SharedVisitFlowTests.make_world
    exhaust = fixture.SharedVisitFlowTests.exhaust
    asyncSetUp = fixture.SharedVisitFlowTests.asyncSetUp
    advance = fixture.SharedVisitFlowTests.advance
    start_shared = fixture.SharedVisitFlowTests.start_shared
    host_reply = fixture.SharedVisitFlowTests.host_reply
    restart = restart_fixture.OutingFlowReviewTests.restart

    async def test_long_host_catchup_retains_guest_evidence_after_milestone_window_rotates(self):
        visit_id, host, guest = await self.start_shared(
            host_action=action("倒第一杯茶", 1), guest_action=action("摆自己的杯子", 1))
        self.advance(2)
        ready = [row["id"] for records in host["shared_activity"]["action_steps"].values() for row in records]
        self.b.service._record_turn(host, host["host_id"], clean_decision({
            "say": "茶和杯子都准备好了。", "continue_activity": True,
            "scene_update": {"summary": "准备好了", "action_ids": ready},
            "action_step": action("我读第一段", 1)}))
        for number in range(10):
            self.advance(2)
            step = host["shared_activity"]["action_steps"][host["host_id"]][-1]
            self.b.service._record_turn(host, host["host_id"], clean_decision({
                "say": f"我继续读第{number + 2}段。", "continue_activity": True,
                "scene_update": {"summary": f"读完第{number + 1}段", "action_ids": [step["id"]]},
                "action_step": action(f"我读第{number + 2}段", 1)}))
        await self.b.service._save()
        reply = await self.a.service._send(visit_id, {"op": "status"})
        self.a.service._synchronize(guest, reply["visit"], guest["host_id"])
        self.assertEqual(len(host["shared_activity"]["milestones"]), 8)
        self.assertEqual(guest["shared_activity"], host["shared_activity"])
        self.assertEqual(guest["log"], host["log"])

    async def test_early_next_activity_records_real_departure_without_claiming_planned_duration(self):
        visit_id, host, guest = await self.start_shared()
        planned = host["outing"]["until"]
        self.advance(30)
        self.b.service._record_turn(host, host["host_id"], clean_decision({
            "say": "去湖边坐坐吧。", "outing": {"place": "湖边", "activity": "看湖面",
            "minutes": 20, "travel_seconds": 90, "pause_minutes": 5}}))
        await self.b.service._save()
        self.a.llm.default_reply = json.dumps({"say": "好，现在就去。", "outing_accept": True,
            "pause_minutes": 5}, ensure_ascii=False)
        self.advance(self.cfg.poll_seconds + 1)
        await self.a.service.wake_local_experience(visit_id, self.scope, guest["local"]["actor_id"],
            guest["local"]["persona_key"], self.a.clock.now(), seq=1)
        departure = self.b.clock.now()
        await self.a.service._advance(visit_id)
        old, new = host["outing"]["route"]
        self.assertEqual(old.get("ended_at"), departure)
        self.assertEqual(old["until"], planned)
        self.assertLess(old["ended_at"], old["until"])
        self.assertEqual(new["departed_at"], departure)
        self.assertEqual(guest["outing"], host["outing"])

    async def test_delayed_step_ack_after_host_push_does_not_regress_or_repeat_guest_choice(self):
        await self._delayed_step_ack()

    async def test_lost_step_ack_after_host_push_survives_reload_and_exact_duplicate_delivery(self):
        await self._delayed_step_ack(reload=True)

    async def test_silent_cancel_ack_after_host_push_uses_exact_terminal_action_evidence(self):
        await self._delayed_step_ack(silent="cancel")

    async def test_silent_decline_ack_after_host_push_uses_exact_terminal_action_evidence(self):
        await self._delayed_step_ack(silent="decline")

    async def _delayed_step_ack(self, *, reload=False, silent=""):
        visit_id, host, guest = await self.start_shared(
            host_action=action("一起搬桌子", 10, requires_peer=True) if silent == "decline" else action("倒自己的茶", 1),
            guest_action=None if silent == "decline" else action("摆自己的杯子", 120 if silent else 1))
        self.advance(2)
        ids = [row["id"] for owner, rows in host["shared_activity"]["action_steps"].items()
               for row in rows if not silent or silent == "cancel" and owner == host["host_id"]]
        self.b.service._record_turn(host, host["host_id"], clean_decision({
            "say": "下一步各自尝一口。", "continue_activity": True,
            **({"scene_update": {"summary": "茶已准备好", "action_ids": ids}} if ids else {})}))
        await self.b.service._save()
        if silent:
            owner = host["guest_id"] if silent == "cancel" else host["host_id"]
            target = host["shared_activity"]["action_steps"][owner][0]["id"]
            self.a.llm.default_reply = json.dumps({"say": "", "continue_activity": True,
                "action_control": {"action_id": target, "operation": silent, "reason": "现在想休息"}}, ensure_ascii=False)
        else:
            self.a.llm.default_reply = json.dumps({"say": "我尝一小口。", "continue_activity": True,
                "pause_minutes": 5, "action_step": action("我尝一小口茶", 1)}, ensure_ascii=False)
        self.advance(self.cfg.poll_seconds + 1)
        if silent:
            await self.a.service.wake_local_experience(visit_id, self.scope, guest["local"]["actor_id"],
                guest["local"]["persona_key"], self.a.clock.now(), seq=1)
        delivered, release = asyncio.Event(), asyncio.Event()
        request = self.a.service.transport.request

        async def delay_step(url, body):
            result = await request(url, body)
            if body["payload"]["op"] == "step":
                delivered.set()
                await release.wait()
                if reload:
                    raise VisitTransportError("模拟旧回执丢失", category="channel")
            return result

        self.a.service.transport.request = delay_step
        advancing = asyncio.create_task(self.a.service._advance(visit_id))
        try:
            await asyncio.wait_for(delivered.wait(), 5)
            self.advance(2)
            if silent:
                self.host_reply(next_action=action("我整理自己的桌面", 1))
            else:
                step = host["shared_activity"]["action_steps"][host["guest_id"]][-1]
                self.host_reply(update={"summary": "她尝过自己的茶", "action_ids": [step["id"]]})
            await self.b.service.wake_local_experience(visit_id, self.scope, host["local"]["actor_id"],
                host["local"]["persona_key"], self.b.clock.now(), seq=1)
            await self.b.service._advance(visit_id)
            confirmed = copy.deepcopy(guest["shared_activity"])
            packet = copy.deepcopy(guest.get("outbox"))
            release.set()
            if reload:
                with self.assertRaises(VisitTransportError):
                    await advancing
                await self.restart(self.a)
                guest = self.a.service._state["visits"][visit_id]
                reply = await self.a.service._send(visit_id, {"op": "status"})
                self.a.service._synchronize_guest_reply(guest, reply)
                await self.a.service._save()
                duplicate = await self.a.service.transport.request(host["local"].get("url")
                    or self.b.service.config.public_url, packet)
                self.assertEqual(duplicate["payload"]["reply_to"], packet["message_id"])
            else:
                await advancing
        finally:
            release.set()
            if not advancing.done():
                await advancing
        self.assertEqual(guest["shared_activity"], confirmed)
        self.assertEqual(guest["shared_activity"], host["shared_activity"])
        self.assertFalse(guest.get("outbox"))
        self.assertFalse(guest.get("unsent_turn"))
        choices = [row for row in host["log"] if row["actor_id"] == host["guest_id"]
                   and row["kind"] == "action_plan" and row["text"] == "我尝一小口茶"]
        self.assertEqual(len(choices), 0 if silent else 1)

    async def test_signed_guest_cancel_can_replace_own_action_and_survive_reload(self):
        visit_id, host, guest = await self.start_shared(guest_action=action("继续读自己的书", 120))
        old_id = host["shared_activity"]["action_steps"][host["guest_id"]][0]["id"]
        self.b.service._record_turn(host, host["host_id"], clean_decision({
            "say": "可以继续坐着，也可以换一件事。", "continue_activity": True}))
        await self.b.service._save()
        self.advance(self.cfg.poll_seconds + 1)
        await self.a.service.wake_local_experience(visit_id, self.scope, guest["local"]["actor_id"],
            guest["local"]["persona_key"], self.a.clock.now(), seq=1)
        self.a.llm.default_reply = json.dumps({"say": "我先放下书，去拿自己的杯子。",
            "continue_activity": True, "action_control": {"action_id": old_id, "operation": "cancel",
            "reason": "现在想喝茶"}, "action_step": action("拿自己的杯子", 1)}, ensure_ascii=False)
        await self.a.service._advance(visit_id)
        self.assertEqual(guest["shared_activity"], host["shared_activity"])
        rows = host["shared_activity"]["action_steps"][host["guest_id"]]
        self.assertEqual([row["description"] for row in rows], ["拿自己的杯子"])
        await self.restart(self.a)
        self.assertEqual(self.a.service._state["visits"][visit_id]["shared_activity"], host["shared_activity"])

    async def test_host_cannot_forge_guest_cancel_or_change_cooperation_reference(self):
        _, host, guest = await self.start_shared(host_action=action("邀请一起搬桌子", 10, requires_peer=True),
            guest_action=action("等一起搬桌子", 10, requires_peer=True))
        forged = self.b.service._wire(host)
        row = forged["shared_activity"]["action_steps"][host["guest_id"]][0]
        row.update(status="cancelled", ended_at=self.a.clock.now(), controlled_by=host["guest_id"],
            control_operation="cancel", control_action_id=row["id"])
        forged["shared_activity"]["revision"] += 1
        with self.assertRaises(ValueError):
            validate_snapshot(guest, forged, now=self.a.clock.now())
        forged = self.b.service._wire(host)
        forged["shared_activity"]["action_steps"][host["guest_id"]][0]["peer_action_id"] = (
            host["shared_activity"]["action_steps"][host["host_id"]][0]["id"])
        with self.assertRaises(ValueError):
            validate_snapshot(guest, forged, now=self.a.clock.now())

    async def test_host_cannot_forge_guest_declining_its_cooperation_invitation(self):
        _, host, guest = await self.start_shared(host_action=action("邀请一起搬桌子", 10, requires_peer=True))
        forged = self.b.service._wire(host)
        row = forged["shared_activity"]["action_steps"][host["host_id"]][0]
        row.update(status="declined", ended_at=self.a.clock.now(), controlled_by=host["guest_id"],
            control_operation="decline", control_action_id=row["id"], control_reason="编造的拒绝理由")
        forged["shared_activity"]["revision"] += 1
        with self.assertRaises(ValueError):
            validate_snapshot(guest, forged, now=self.a.clock.now())

    async def test_guest_can_cancel_own_invitation_and_explicitly_join_peer_in_one_signed_turn(self):
        visit_id, host, guest = await self.start_shared(host_action=action("我抬左侧", 10, requires_peer=True),
            guest_action=action("我提议抬另一张桌子", 10, requires_peer=True))
        peer_id = host["shared_activity"]["action_steps"][host["host_id"]][0]["id"]
        own_id = host["shared_activity"]["action_steps"][host["guest_id"]][0]["id"]
        self.b.service._record_turn(host, host["host_id"], clean_decision({
            "say": "先搬这张桌子吧，你愿意抬右侧吗？", "continue_activity": True}))
        await self.b.service._save()
        self.advance(self.cfg.poll_seconds + 1)
        await self.a.service.wake_local_experience(visit_id, self.scope, guest["local"]["actor_id"],
            guest["local"]["persona_key"], self.a.clock.now(), seq=1)
        self.a.llm.default_reply = json.dumps({"say": "好，我收回刚才的提议，来抬右侧。",
            "continue_activity": True, "action_control": {"action_id": own_id, "operation": "cancel"},
            "action_step": action("我愿意抬右侧", 12, requires_peer=True, peer_action_id=peer_id)}, ensure_ascii=False)
        await self.a.service._advance(visit_id)
        steps = host["shared_activity"]["action_steps"]
        left, right = steps[host["host_id"]][0], steps[host["guest_id"]][0]
        self.assertEqual(left["status"], "running")
        self.assertEqual(right["status"], "running")
        self.assertEqual(left["joint_id"], right["joint_id"])
        self.assertEqual(right["peer_action_id"], peer_id)
        self.assertEqual(guest["shared_activity"], host["shared_activity"])

    async def test_stale_signed_snapshot_cannot_acknowledge_an_unexecuted_guest_step(self):
        visit_id, host, guest = await self.start_shared(host_action=action("倒自己的茶", 1))
        old = self.b.service._wire(host)
        self.advance(2)
        self.b.service._record_turn(host, host["host_id"], clean_decision({
            "say": "接下来各自尝茶。", "continue_activity": True}))
        await self.b.service._save()
        self.a.llm.default_reply = json.dumps({"say": "我准备拿杯子。", "continue_activity": True,
            "action_step": action("拿自己的杯子", 1)}, ensure_ascii=False)
        self.advance(self.cfg.poll_seconds + 1)
        request = self.a.service.transport.request
        secret = self.a.service._find_peer(self.scope, self.peer_id)["secret"]

        async def unexecuted_reply(url, body):
            if body["payload"]["op"] != "step":
                return await request(url, body)
            return envelope(secret, host["host_id"], host["guest_id"],
                {"visit": old, "reply_to": body["message_id"]}, self.b.clock.now())

        self.a.service.transport.request = unexecuted_reply
        with self.assertRaises(ValueError):
            await self.a.service._advance(visit_id)
        self.assertTrue(guest.get("outbox"))
        self.assertTrue(guest.get("unsent_turn"))
        self.assertFalse(any(row["text"] == "拿自己的杯子" for row in host["log"]))

    async def test_switching_destination_before_arrival_keeps_the_unreached_stop_as_a_plan(self):
        visit_id, host, guest = await self.start_shared(travel=600)
        old_arrival = host["outing"]["started_at"]
        self.advance(30)
        self.b.service._record_turn(host, host["host_id"], clean_decision({
            "say": "还是先在路旁歇一会儿吧。", "outing": {"place": "路旁", "activity": "暂时休息",
            "minutes": 20, "pause_minutes": 5}}))
        await self.b.service._save()
        self.a.llm.default_reply = json.dumps({"say": "好，就在这里坐一会儿。", "outing_accept": True,
            "pause_minutes": 5}, ensure_ascii=False)
        self.advance(self.cfg.poll_seconds + 1)
        await self.a.service.wake_local_experience(visit_id, self.scope, guest["local"]["actor_id"],
            guest["local"]["persona_key"], self.a.clock.now(), seq=1)
        departure = self.b.clock.now()
        await self.a.service._advance(visit_id)
        old_stop, current = host["outing"]["route"]
        self.assertEqual(old_stop["started_at"], old_arrival)
        self.assertEqual(old_stop["ended_at"], departure)
        self.assertLess(departure, old_arrival)
        self.assertEqual(current["started_at"], departure)
        self.assertEqual(guest["outing"], host["outing"])

    async def test_host_cannot_silently_drop_unsettled_guest_action(self):
        _, host, guest = await self.start_shared(guest_action=action("读自己的书", 120))
        forged = self.b.service._wire(host)
        forged["shared_activity"]["action_steps"].pop(host["guest_id"])
        forged["shared_activity"]["revision"] += 1
        with self.assertRaises(ValueError):
            validate_snapshot(guest, forged, now=self.a.clock.now())

    async def test_invalid_result_reference_preserves_ready_choices_and_cools_down_before_correction(self):
        visit_id, host, _ = await self.start_shared(host_action=action("倒自己的茶", 1))
        self.advance(2)
        step = host["shared_activity"]["action_steps"][host["host_id"]][0]
        self.host_reply(update={"summary": "茶已经喝完", "action_ids": ["s0-a999"]})
        before_log = copy.deepcopy(host["log"])
        await self.b.service._advance(visit_id)
        self.assertEqual(host["log"], before_log)
        self.assertFalse(host["shared_activity"]["milestones"])
        self.assertNotIn("settled_revision", step)
        retry = host.get("model_retry") or {}
        self.assertEqual(retry.get("category"), "format")
        calls = len(self.b.llm.calls)
        for _ in range(3):
            self.advance(1)
            await self.b.service.tick()
        self.assertEqual(len(self.b.llm.calls), calls)
        self.host_reply(update={"summary": "倒好了自己的茶", "action_ids": [step["id"]]})
        self.advance(retry["next_at"] - self.b.clock.now() + 0.1)
        await self.b.service._advance(visit_id)
        self.assertEqual(len(host["shared_activity"]["milestones"]), 1)
        self.assertEqual(host["status"], "active")


if __name__ == "__main__":
    unittest.main()
