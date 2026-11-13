"""Short action handoffs and provider latency through two signed visit services."""
from __future__ import annotations

import copy
import json
import unittest
from unittest import mock

from tests import fake_astrbot
fake_astrbot.install()

from tests import test_visit_shared_flow_20261113 as fixture  # noqa: E402
from tests import test_visits_flow_review_r as restart_fixture  # noqa: E402
from core.visits import flow  # noqa: E402
from core.visits.protocol import clean_decision  # noqa: E402


action = fixture.action


class VisitRhythmLatencyTests(unittest.IsolatedAsyncioTestCase):
    make_world = fixture.SharedVisitFlowTests.make_world
    exhaust = fixture.SharedVisitFlowTests.exhaust
    asyncSetUp = fixture.SharedVisitFlowTests.asyncSetUp
    advance = fixture.SharedVisitFlowTests.advance
    start_shared = fixture.SharedVisitFlowTests.start_shared
    host_reply = fixture.SharedVisitFlowTests.host_reply
    restart = restart_fixture.OutingFlowReviewTests.restart

    async def test_next_guest_action_deadline_can_wake_the_handoff_before_poll_delay(self):
        visit_id, host, guest = await self.start_shared(host_action=action("倒自己的茶", 1),
            guest_action=action("摆自己的杯子", 3))
        first = host["shared_activity"]["action_steps"][host["host_id"]][0]["id"]
        self.host_reply(update={"summary": "倒好了自己的茶", "action_ids": [first]})
        self.advance(1.01)
        await self.b.service.tick()
        calls = len(self.a.llm.calls)
        self.a.llm.default_reply = json.dumps({"continue_activity": True, "pause_minutes": 20}, ensure_ascii=False)
        self.advance(2.01)
        await self.a.service.tick()
        self.assertEqual(len(self.a.llm.calls), calls + 1,
            "三秒动作已经到期，固定poll延迟仍阻塞轮到访客的交接")
        self.assertEqual(guest["shared_activity"], host["shared_activity"])

    async def test_busy_foreground_action_is_rejected_before_public_turn_and_action_plan_commit(self):
        visit_id, host, _ = await self.start_shared(host_action=action("读自己的书", 120))
        before_log = copy.deepcopy(host["log"])
        before_turns = dict(host["turns"])
        before_actions = copy.deepcopy(host["shared_activity"]["action_steps"])
        self.host_reply(next_action=action("去整理自己的桌子", 1))
        await self.b.service.wake_local_experience(visit_id, self.scope, host["local"]["actor_id"],
            host["local"]["persona_key"], self.b.clock.now(), seq=1)
        await self.b.service._advance(visit_id)
        self.assertEqual(host["log"], before_log, "未接纳的新动作仍被写入公开原话与行动计划")
        self.assertEqual(host["turns"], before_turns)
        self.assertEqual(host["shared_activity"]["action_steps"], before_actions)
        self.assertEqual((host.get("model_retry") or {}).get("category"), "format")

    async def test_provider_finishing_after_agreed_return_does_not_append_late_turn_before_old_goodbye(self):
        visit_id, host, _ = await self.start_shared(return_after=True)
        until = host["outing"]["until"]
        self.advance(until - self.b.clock.now() - 2)
        self.host_reply(next_action=action("我再摆一下杯子", 1))
        decide = self.b.port.decide

        async def delayed_decide(scope, phase, snapshot):
            result = await decide(scope, phase, snapshot)
            self.advance(3)
            return result

        self.b.port.decide = delayed_decide
        await self.b.service._advance(visit_id)
        self.assertEqual(host["status"], "completed")
        self.assertEqual(host["ended_at"], until)
        self.assertFalse(any(row["at"] > until for row in host["log"]),
            "慢推理的迟到言行被写在时间更早的原定告别之前")
        self.assertFalse(any(row["text"] == "我再摆一下杯子" for row in host["log"]))

    async def test_busy_guest_action_is_not_recorded_as_an_accepted_signed_turn(self):
        visit_id, host, guest = await self.start_shared(guest_action=action("读自己的书", 120))
        self.host_reply()
        await self.b.service.wake_local_experience(visit_id, self.scope, host["local"]["actor_id"],
            host["local"]["persona_key"], self.b.clock.now(), seq=1)
        await self.b.service._advance(visit_id)
        before_log, before_turns = copy.deepcopy(host["log"]), dict(host["turns"])
        self.a.llm.default_reply = json.dumps({"continue_activity": True,
            "action_step": action("开始整理自己的桌子", 1)}, ensure_ascii=False)
        self.advance(self.cfg.poll_seconds + 1)
        await self.a.service._advance(visit_id)
        self.assertEqual(host["log"], before_log)
        self.assertEqual(host["turns"], before_turns)
        self.assertFalse(guest.get("outbox"))
        self.assertEqual((guest.get("model_retry") or {}).get("category"), "format")

    async def test_action_longer_than_remaining_agreed_visit_uses_bounded_correction(self):
        visit_id, host, _ = await self.start_shared(return_after=True)
        until = host["outing"]["until"]
        self.advance(until - self.b.clock.now() - 10)
        before_log = copy.deepcopy(host["log"])
        self.host_reply(next_action=action("开始煮一壶茶", 11))
        await self.b.service._advance(visit_id)
        self.assertEqual(host["log"], before_log)
        self.assertEqual(host["status"], "active")
        self.assertEqual((host.get("model_retry") or {}).get("category"), "format")
        self.advance(10.1)
        await self.b.service.tick()
        self.assertEqual(host["status"], "completed")
        self.assertEqual(host["ended_at"], until)

    async def test_successful_cancel_remains_public_after_replacement_pruning_and_reload(self):
        visit_id, host, guest = await self.start_shared(guest_action=action("读自己的书", 120))
        target = host["shared_activity"]["action_steps"][host["guest_id"]][0]["id"]
        self.host_reply()
        await self.b.service.wake_local_experience(visit_id, self.scope, host["local"]["actor_id"],
            host["local"]["persona_key"], self.b.clock.now(), seq=1)
        await self.b.service._advance(visit_id)
        self.a.llm.default_reply = json.dumps({"thought": "不公开的私人想法", "continue_activity": True,
            "action_control": {"action_id": target, "operation": "cancel", "reason": "现在想喝茶"},
            "action_step": action("拿自己的杯子", 1)}, ensure_ascii=False)
        self.advance(self.cfg.poll_seconds + 1)
        await self.a.service._advance(visit_id)
        self.assertFalse(any(row["id"] == target for row in host["shared_activity"]["action_steps"][host["guest_id"]]))
        controls = [row for row in host["log"] if row["kind"] == "action_control"]
        self.assertEqual(len(controls), 1)
        self.assertEqual(controls[0]["actor_id"], host["guest_id"])
        self.assertEqual(controls[0]["action_id"], target)
        self.assertEqual(controls[0]["operation"], "cancel")
        self.assertEqual(controls[0]["reason"], "现在想喝茶")
        self.assertIn("读自己的书", controls[0]["text"])
        self.assertNotIn("不公开的私人想法", json.dumps(host["log"], ensure_ascii=False))
        self.assertEqual(host["log"], guest["log"])
        await self.restart(self.a)
        self.assertEqual(self.a.service._state["visits"][visit_id]["log"], host["log"])

    async def test_final_action_commit_failure_leaves_no_partial_cancel_or_turn(self):
        _, host, _ = await self.start_shared(host_action=action("读自己的书", 120))
        target = host["shared_activity"]["action_steps"][host["host_id"]][0]["id"]
        before = copy.deepcopy(host)
        original = flow.record_action

        def refuse_after_control_journal(visit, actor, step, now):
            if any(row["kind"] == "action_control" for row in visit["log"]):
                return False
            return original(visit, actor, step, now)

        decision = clean_decision({"say": "先放下书再整理桌子。", "continue_activity": True,
            "action_control": {"action_id": target, "operation": "cancel"},
            "action_step": action("整理自己的桌子", 1)})
        with mock.patch.object(flow, "record_action", side_effect=refuse_after_control_journal):
            with self.assertRaises(ValueError):
                self.b.service._record_turn(host, host["host_id"], decision)
        self.assertEqual(host, before)

    async def test_commit_uses_one_time_read_at_agreed_return_boundary(self):
        _, host, _ = await self.start_shared(return_after=True)
        until = host["outing"]["until"]
        base, reads = until - 1, 0

        def moving_now():
            nonlocal reads
            value = base + reads * 0.25
            reads += 1
            return value

        decision = clean_decision({"say": "我摆一下杯子。", "continue_activity": True,
            "action_step": action("摆自己的杯子", 1)})
        with mock.patch.object(self.b.service, "_now", side_effect=moving_now):
            self.b.service._record_turn(host, host["host_id"], decision)
        row = host["shared_activity"]["action_steps"][host["host_id"]][-1]
        self.assertEqual(row["accepted_at"], base)
        self.assertEqual(row["started_at"], base)
        self.assertEqual(row["until"], until)
        self.assertEqual([row["at"] for row in host["log"][-2:]], [base, base])

    async def test_host_proposed_return_checks_own_step_before_guest_acceptance_and_corrects_without_fake_start(self):
        visit_id, host, guest = await self.start_shared()
        before_log = copy.deepcopy(host["log"])
        self.b.llm.default_reply = json.dumps({"say": "去湖边坐一分钟，再一起回家吧。",
            "outing": {"place": "湖边", "activity": "坐一会儿", "minutes": 1,
                "return_after": True, "pause_minutes": 0}, "farewell": "下次见。",
            "action_step": action("我整理自己的东西", 61)}, ensure_ascii=False)
        await self.b.service.wake_local_experience(visit_id, self.scope, host["local"]["actor_id"],
            host["local"]["persona_key"], self.b.clock.now(), seq=1)
        await self.b.service._advance(visit_id)
        self.assertEqual(host["log"], before_log)
        self.assertFalse(host.get("outing_offer"))
        self.assertFalse(host["shared_activity"]["action_steps"])
        self.assertFalse(guest.get("outbox"))
        retry = host.get("model_retry") or {}
        self.assertEqual(retry.get("category"), "format")
        self.b.llm.default_reply = json.dumps({"say": "去湖边坐一分钟，再一起回家吧。",
            "outing": {"place": "湖边", "activity": "坐一会儿", "minutes": 1,
                "return_after": True, "pause_minutes": 0}, "farewell": "下次见。",
            "action_step": action("我整理自己的东西", 59)}, ensure_ascii=False)
        self.advance(retry["next_at"] - self.b.clock.now() + 0.1)
        await self.b.service._advance(visit_id)
        self.assertTrue(host.get("outing_offer"))
        self.assertFalse(host["shared_activity"]["action_steps"])
        self.a.llm.default_reply = json.dumps({"say": "好，一分钟后回家。", "outing_accept": True,
            "return_after": True, "farewell": "我先回家了。"}, ensure_ascii=False)
        self.advance(self.cfg.poll_seconds + 1)
        await self.a.service._advance(visit_id)
        self.assertEqual(len(host["outing"]["route"]), 2)
        self.assertTrue(host["outing"]["return_agreed"])
        self.assertEqual(host["shared_activity"], guest["shared_activity"])
        self.assertEqual(host["shared_activity"]["action_steps"][host["host_id"]][-1]["seconds"], 59)


if __name__ == "__main__":
    unittest.main()
