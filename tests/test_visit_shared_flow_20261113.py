"""Two real visit services commit independent model choices through signed HTTP."""
from __future__ import annotations

import copy
import json
import unittest

from core.visits.activities import activity_context
from core.visits.snapshots import validate_snapshot
from tests import test_visits_outings_q as fixture


def action(description, seconds, *, waiting=False, requires_peer=False):
    return {"description": description, "seconds": seconds, "waiting": waiting, "requires_peer": requires_peer}


class SharedVisitFlowTests(unittest.IsolatedAsyncioTestCase):
    make_world = fixture.OutingClockTests.make_world
    exhaust = fixture.OutingClockTests.exhaust
    asyncSetUp = fixture.OutingClockTests.asyncSetUp
    advance = fixture.OutingClockTests.advance

    async def start_shared(self, *, host_action=None, guest_action=None, travel=0, return_after=False):
        host = {"accept": True, "say": "先一起准备茶吧。", "outing": {
            "place": "湖边" if travel else "书房", "activity": "一起喝茶", "minutes": 30,
            "pause_minutes": 20, "travel_seconds": travel, "return_after": return_after},
            "farewell": "今天先到这里，下次再一起玩。"}
        guest = {"say": "好，我会自己准备杯子。", "outing_accept": True, "pause_minutes": 20,
                 "return_after": return_after, "farewell": "我先回家了，下次见。"}
        if host_action:
            host["action_step"] = host_action
        if guest_action:
            guest["action_step"] = guest_action
        self.b.llm.default_reply = json.dumps(host, ensure_ascii=False)
        self.a.llm.default_reply = json.dumps(guest, ensure_ascii=False)
        self.a.llm.replies = ['{"accept":true,"say":"能去你家玩一会儿吗？"}']
        pending = await self.a.service.invite(self.scope, self.peer_id, manual=True)
        visit_id = pending["id"]
        await self.b.service._advance(visit_id)
        await self.a.service._advance(visit_id)
        self.advance(self.cfg.poll_seconds + 1)
        await self.a.service._advance(visit_id)
        host_visit = self.b.service._state["visits"][visit_id]
        guest_visit = self.a.service._state["visits"][visit_id]
        self.assertEqual(host_visit["status"], "active")
        self.assertTrue(host_visit.get("outing"))
        self.assertEqual(host_visit["shared_activity"], guest_visit["shared_activity"])
        return visit_id, host_visit, guest_visit

    def host_reply(self, *, update=None, next_action=None):
        value = {"say": "接着做眼前这一步。", "continue_activity": True, "pause_minutes": 20}
        if update:
            value["scene_update"] = update
        if next_action:
            value["action_step"] = next_action
        self.b.llm.default_reply = json.dumps(value, ensure_ascii=False)

    async def test_two_models_commit_one_second_action_without_stretching_the_visit(self):
        visit_id, host, _ = await self.start_shared(
            host_action=action("我把茶倒进自己的杯子", 1), guest_action=action("我摆自己的杯子", 3))
        started, until, quiet = (host["outing"][key] for key in ("started_at", "until", "quiet_until"))
        steps = host["shared_activity"]["action_steps"]
        self.assertEqual(steps[host["host_id"]][0]["description"], "我把茶倒进自己的杯子")
        self.assertEqual(steps[host["guest_id"]][0]["description"], "我摆自己的杯子")
        self.assertGreaterEqual(len(self.a.llm.calls), 2)
        self.assertGreaterEqual(len(self.b.llm.calls), 1)
        self.host_reply(update={"summary": "东道主的杯子里已有茶，对方仍在摆自己的杯子"})
        self.advance(1.01)
        await self.b.service.tick()
        self.assertEqual(host["outing"]["until"], until)
        self.assertEqual(host["outing"]["quiet_until"], quiet)
        self.assertEqual(steps[host["host_id"]][0]["until"], started + 1)
        self.assertEqual(host["shared_activity"]["milestones"][-1]["actions"].keys(), {host["host_id"]})
        self.assertTrue(any(row["kind"] == "milestone" for row in host["log"]))

    async def test_following_turns_keep_each_models_new_personal_action(self):
        visit_id, host, guest = await self.start_shared(
            host_action=action("倒自己的茶", 1), guest_action=action("摆杯子", 2))
        self.host_reply(update={"summary": "东道主倒好了自己的茶"}, next_action=action("端起自己的杯子", 1))
        self.advance(1.01)
        await self.b.service.tick()
        self.a.llm.default_reply = json.dumps({"say": "我先尝一小口。", "continue_activity": True,
            "pause_minutes": 20, "action_step": action("我自己尝一口茶", 1.25)}, ensure_ascii=False)
        self.advance(self.cfg.poll_seconds + 1)
        await self.a.service._advance(visit_id)
        self.assertEqual(host["shared_activity"], guest["shared_activity"])
        self.assertEqual(host["shared_activity"]["action_steps"][host["guest_id"]][-1]["description"], "我自己尝一口茶")
        self.assertEqual(host["shared_activity"]["action_steps"][host["host_id"]][-1]["description"], "端起自己的杯子")
        self.assertEqual(len(host["outing"]["route"]), 1)
        self.assertFalse(guest.get("unsent_turn"))

    async def test_host_snapshot_cannot_insert_or_rewrite_a_guest_choice(self):
        _, host, guest = await self.start_shared(host_action=action("倒茶", 1), guest_action=action("摆杯子", 2))
        remote = self.b.service._wire(host)
        forged = copy.deepcopy(remote)
        forged["shared_activity"]["action_steps"][host["guest_id"]][0]["description"] = "东道主替她答应了新游戏"
        with self.assertRaises(ValueError):
            validate_snapshot(guest, forged, now=self.a.clock.now())
        forged = copy.deepcopy(remote)
        row = copy.deepcopy(forged["shared_activity"]["action_steps"][host["guest_id"]][0])
        row.update(id="s0-a999", description="未经本人选择的新动作")
        forged["shared_activity"]["action_steps"][host["guest_id"]].append(row)
        forged["shared_activity"]["next_action"] = 1000
        forged["shared_activity"]["revision"] += 1
        with self.assertRaises(ValueError):
            validate_snapshot(guest, forged, now=self.a.clock.now())

    async def test_cooperative_result_waits_for_the_other_models_independent_choice(self):
        visit_id, host, _ = await self.start_shared(host_action=action("共同抬桌子，我抬左边", 1, requires_peer=True))
        self.assertEqual(host["shared_activity"]["action_steps"][host["host_id"]][0]["status"], "awaiting_peer")
        self.host_reply(update={"summary": "桌子已经被两人搬完"})
        await self.b.service.wake_local_experience(visit_id, self.scope, host["local"]["actor_id"],
            host["local"]["persona_key"], self.b.clock.now(), seq=1)
        await self.b.service._advance(visit_id)
        self.assertFalse(host["shared_activity"]["milestones"])
        self.a.llm.default_reply = json.dumps({"say": "我来抬右边。", "continue_activity": True,
            "pause_minutes": 20, "action_step": action("我愿意抬右边", 2, requires_peer=True)}, ensure_ascii=False)
        self.advance(self.cfg.poll_seconds + 1)
        await self.a.service._advance(visit_id)
        steps = host["shared_activity"]["action_steps"]
        left, right = steps[host["host_id"]][0], steps[host["guest_id"]][0]
        self.assertEqual(left["started_at"], self.b.clock.now())
        self.assertEqual(left["started_at"], right["started_at"])
        self.assertEqual(left["joint_id"], right["joint_id"])
        self.assertFalse(host["shared_activity"]["milestones"])

    async def test_accepted_destination_action_is_scheduled_until_real_arrival(self):
        _, host, guest = await self.start_shared(host_action=action("到湖边再铺野餐布", 1),
                                                guest_action=action("到湖边再摆自己的杯子", 2), travel=125.25)
        context = activity_context(host, self.b.clock.now())
        self.assertTrue(context["traveling"])
        self.assertEqual({row["status"] for row in context["actions"]}, {"scheduled"})
        self.assertEqual(host["shared_activity"], guest["shared_activity"])
        self.assertFalse(any(row["kind"] == "activity" for row in host["log"]))
        self.advance(125.25)
        context = activity_context(host, self.b.clock.now())
        self.assertFalse(context["traveling"])
        self.assertEqual({row["status"] for row in context["actions"]}, {"running"})
        self.assertEqual(context["ready_action_ids"], [])

    async def test_group_experience_wake_preserves_quiet_choice_and_running_action(self):
        visit_id, host, _ = await self.start_shared(host_action=action("等水烧开", 180, waiting=True),
                                                  guest_action=action("静静读一会儿书", 180))
        quiet = host["outing"]["quiet_until"]
        steps = copy.deepcopy(host["shared_activity"]["action_steps"])
        accepted = await self.b.service.wake_local_experience(visit_id, self.scope, host["local"]["actor_id"],
            host["local"]["persona_key"], self.b.clock.now(), seq=7)
        self.assertTrue(accepted)
        self.assertEqual(host["outing"]["quiet_until"], quiet)
        self.assertEqual(host["shared_activity"]["action_steps"], steps)
        self.host_reply()
        await self.b.service._advance(visit_id)
        self.assertEqual(host["outing"]["quiet_until"], quiet)
        self.assertEqual(host["shared_activity"]["action_steps"], steps)
        wire = self.b.service._wire(host)
        self.assertNotIn("local_experience_pending", wire)
        self.assertFalse(host["shared_activity"]["milestones"])

    async def test_joint_action_agreed_return_keeps_both_servers_final_log_and_clock_equal(self):
        _, host, guest = await self.start_shared(
            host_action=action("共同展开野餐布，我拉左边", 1, requires_peer=True),
            guest_action=action("我愿意拉右边", 2, requires_peer=True), return_after=True)
        until = host["outing"]["until"]
        self.assertTrue(host["outing"]["return_agreed"])
        self.advance(until - self.a.clock.now() + 0.1)
        # The visitor may reach the return clock before the host polls.
        await self.a.service.tick()
        await self.b.service.tick()
        self.assertEqual(host["status"], "completed")
        self.assertEqual(guest["status"], "completed")
        self.assertEqual(host["ended_at"], until)
        self.assertEqual(guest["ended_at"], until)
        self.assertEqual(host["log"], guest["log"])
        self.assertEqual([row["at"] for row in host["log"][-2:]], [until, until])
        self.assertEqual({row["text"] for row in host["log"][-2:]},
                         {"今天先到这里，下次再一起玩。", "我先回家了，下次见。"})
        self.assertFalse(host["shared_activity"]["milestones"])


if __name__ == "__main__":
    unittest.main()
