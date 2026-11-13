"""Supported goal changes and explicit shared quiet after an observed action timer."""
from __future__ import annotations

import copy
import json
import unittest

from tests import fake_astrbot
fake_astrbot.install()

from tests import test_visit_shared_flow_20261113 as fixture  # noqa: E402
from tests import test_visits_flow_review_r as restart_fixture  # noqa: E402
from core.visits.activities import activity_context  # noqa: E402
from core.visits.snapshots import validate_snapshot  # noqa: E402


action = fixture.action


class VisitWaitSwitchEdgeTests(unittest.IsolatedAsyncioTestCase):
    make_world = fixture.SharedVisitFlowTests.make_world
    exhaust = fixture.SharedVisitFlowTests.exhaust
    asyncSetUp = fixture.SharedVisitFlowTests.asyncSetUp
    advance = fixture.SharedVisitFlowTests.advance
    start_shared = fixture.SharedVisitFlowTests.start_shared
    restart = restart_fixture.OutingFlowReviewTests.restart

    async def test_legacy_scene_goal_offer_does_not_require_cancelling_the_current_foreground_action(self):
        visit_id, host, _ = await self.start_shared(host_action=action("读自己的书", 120))
        old_actions = copy.deepcopy(host["shared_activity"]["action_steps"])
        self.b.llm.default_reply = json.dumps({"say": "待会儿去花园做点别的，好吗？",
            "scene": {"place": "花园", "activity": "准备野餐"},
            "action_step": action("到花园后再铺自己的野餐布", 1)}, ensure_ascii=False)
        await self.b.service.wake_local_experience(visit_id, self.scope, host["local"]["actor_id"],
            host["local"]["persona_key"], self.b.clock.now(), seq=1)
        await self.b.service._advance(visit_id)
        self.assertEqual((host.get("outing_offer") or {}).get("place"), "花园",
            "支持的scene形状提议被当作旧活动的冲突动作，无法交给朋友独立决定")
        self.assertEqual(host["shared_activity"]["action_steps"], old_actions)
        self.assertEqual(host["outing"]["place"], "书房")

    async def test_legacy_scene_destination_action_does_not_start_before_the_guest_accepts_the_goal(self):
        visit_id, host, _ = await self.start_shared()
        self.b.llm.default_reply = json.dumps({"say": "去花园准备野餐，好吗？",
            "scene": {"place": "花园", "activity": "准备野餐"},
            "action_step": action("到花园后再铺自己的野餐布", 1)}, ensure_ascii=False)
        await self.b.service.wake_local_experience(visit_id, self.scope, host["local"]["actor_id"],
            host["local"]["persona_key"], self.b.clock.now(), seq=1)
        await self.b.service._advance(visit_id)
        self.assertEqual(host["outing_offer"]["place"], "花园")
        self.assertEqual(host["outing"]["place"], "书房")
        self.assertFalse(host["shared_activity"]["action_steps"],
            "朋友尚未接受新去处，目的地动作却已经在旧站开始计时")
        self.a.llm.default_reply = json.dumps({"outing_accept": True, "pause_minutes": 20}, ensure_ascii=False)
        self.advance(self.cfg.poll_seconds + 1)
        await self.a.service._advance(visit_id)
        steps = host["shared_activity"]["action_steps"][host["host_id"]]
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["stage"], 1)
        self.assertEqual(steps[0]["started_at"], host["outing"]["started_at"])
        self.assertEqual(host["outing"]["place"], "花园")

    async def test_both_characters_can_choose_quiet_without_an_old_elapsed_timer_waking_the_same_decision_again(self):
        _, host, _ = await self.start_shared(host_action=action("翻一页自己的书", 1))
        quiet = json.dumps({"continue_activity": True, "pause_minutes": 20}, ensure_ascii=False)
        self.b.llm.default_reply = quiet
        self.a.llm.default_reply = quiet
        self.advance(1.01)
        await self.b.service.tick()
        self.advance(self.cfg.poll_seconds + 1)
        await self.a.service.tick()
        self.assertGreater(host["outing"]["quiet_until"], self.b.clock.now() + 1000)
        self.assertEqual(host["outing"]["pause_choices"], {host["host_id"]: 20, host["guest_id"]: 20})
        before = (len(self.b.llm.calls), len(self.a.llm.calls))
        for _ in range(3):
            self.advance(1)
            await self.b.service.tick()
            await self.a.service.tick()
        self.assertEqual((len(self.b.llm.calls), len(self.a.llm.calls)), before,
            "已到点但未宣称成功的旧动作让双方刚确认的二十分钟安静立即失效")
        self.assertFalse(host["shared_activity"]["milestones"])

    async def test_timer_elapsed_while_waiting_for_guest_is_not_acknowledged_by_the_old_host_choice(self):
        _, host, _ = await self.start_shared(host_action=action("翻一页自己的书", 1))
        self.b.llm.default_reply = json.dumps({"continue_activity": True, "pause_minutes": 20,
            "action_step": action("再翻下一页自己的书", 1)}, ensure_ascii=False)
        self.a.llm.default_reply = json.dumps({"continue_activity": True, "pause_minutes": 20})
        self.advance(1.01)
        await self.b.service.tick()
        self.advance(self.cfg.poll_seconds + 1)
        await self.a.service.tick()
        old, new = host["shared_activity"]["action_steps"][host["host_id"]]
        self.assertEqual(host["outing"].get("quiet_action_ids"), [old["id"]])
        self.assertEqual(set(activity_context(host, self.b.clock.now())["ready_action_ids"]), {old["id"], new["id"]})
        self.assertFalse(self.b.service._activity_waiting(host))
        before = len(self.b.llm.calls)
        self.b.llm.default_reply = json.dumps({"continue_activity": True, "pause_minutes": 20})
        self.advance(1)
        await self.b.service.tick()
        self.assertEqual(len(self.b.llm.calls), before + 1)
        self.assertFalse(host["shared_activity"]["milestones"])

    async def test_timer_elapsed_during_host_thinking_remains_new_attention_after_both_choose_quiet(self):
        visit_id, host, _ = await self.start_shared(host_action=action("读眼前这页书", 1))
        quiet = json.dumps({"continue_activity": True, "pause_minutes": 20})
        self.b.llm.default_reply = quiet
        self.a.llm.default_reply = quiet
        decide = self.b.port.decide

        async def delayed_decide(scope, phase, snapshot):
            result = await decide(scope, phase, snapshot)
            self.advance(2)
            return result

        self.b.port.decide = delayed_decide
        await self.b.service.wake_local_experience(visit_id, self.scope, host["local"]["actor_id"],
            host["local"]["persona_key"], self.b.clock.now(), seq=1)
        await self.b.service._advance(visit_id)
        self.advance(self.cfg.poll_seconds + 1)
        await self.a.service.tick()
        self.assertNotIn("quiet_action_ids", host["outing"])
        self.assertTrue(activity_context(host, self.b.clock.now())["ready_action_ids"])
        self.assertFalse(self.b.service._activity_waiting(host))

    async def test_confirmed_quiet_survives_reload_and_cannot_be_forged_without_a_current_guest_observation(self):
        visit_id, host, guest = await self.start_shared(host_action=action("翻一页自己的书", 1))
        quiet = json.dumps({"continue_activity": True, "pause_minutes": 20})
        self.b.llm.default_reply = quiet
        self.a.llm.default_reply = quiet
        self.advance(1.01)
        await self.b.service.tick()
        self.advance(self.cfg.poll_seconds + 1)
        await self.a.service.tick()
        acknowledged = copy.deepcopy(host["outing"]["quiet_action_ids"])
        await self.restart(self.a)
        guest = self.a.service._state["visits"][visit_id]
        self.assertEqual(guest["outing"]["quiet_action_ids"], acknowledged)
        self.assertTrue(self.a.service._activity_waiting(guest))
        self.assertEqual(activity_context(guest, self.a.clock.now())["ready_action_ids"], acknowledged)
        forged = self.b.service._wire(host)
        forged["outing"]["quiet_action_ids"] = []
        with self.assertRaises(ValueError):
            validate_snapshot(guest, forged, now=self.a.clock.now())
        legacy_guest = copy.deepcopy(guest)
        for key in ("pause_choices", "pause_at", "cadence_seconds", "quiet_action_ids"):
            legacy_guest["outing"].pop(key, None)
        forged = self.b.service._wire(host)
        for key in ("pause_choices", "pause_at", "cadence_seconds"):
            forged["outing"].pop(key, None)
        with self.assertRaises(ValueError):
            validate_snapshot(legacy_guest, forged, now=self.a.clock.now())
        legacy = copy.deepcopy(host)
        legacy["outing"].pop("quiet_action_ids")
        self.assertFalse(self.b.service._activity_waiting(legacy), "旧版本没有观察确认集时保留原到点唤醒行为")

    async def stale_terminal_guest(self):
        visit_id, host, guest = await self.start_shared()
        self.advance(2)
        await self.b.service._finish(host, "completed", "今天先到这里。", notify=False)
        await self.a.service._finish(guest, "completed", "今天先到这里。", notify=False)
        guest["ended_at"] -= 1
        guest["remembered"] = True
        guest["finish_review_attempted"] = True
        await self.a.service._save()
        return visit_id, host, guest

    async def assert_corrected_memory(self, visit_id, host, guest):
        self.assertEqual(guest["ended_at"], host["ended_at"])
        self.assertNotIn("remembered", guest)
        writes = []
        remember = self.a.port.remember

        async def capture_memory(scope, snapshot):
            writes.append(copy.deepcopy(snapshot))
            await remember(scope, snapshot)

        self.a.port.remember = capture_memory
        await self.a.service._write_memory(visit_id)
        self.assertTrue(guest.get("remembered"))
        self.assertEqual(writes[-1]["ended_at"], host["ended_at"])

    async def test_end_reply_with_only_corrected_end_time_invalidates_the_already_written_guest_memory(self):
        visit_id, host, guest = await self.stale_terminal_guest()
        guest["end_pending"] = {"attempts": 0, "next_at": self.a.clock.now()}
        await self.a.service._notify_end(visit_id)
        await self.assert_corrected_memory(visit_id, host, guest)

    async def test_end_push_with_only_corrected_end_time_invalidates_the_already_written_guest_memory(self):
        visit_id, host, guest = await self.stale_terminal_guest()
        await self.b.service._send(visit_id, {"op": "end", "visit": self.b.service._wire(host)})
        await self.assert_corrected_memory(visit_id, host, guest)


if __name__ == "__main__":
    unittest.main()
