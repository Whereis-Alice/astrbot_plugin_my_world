"""A visit's live action prerequisites match the protocol offered to its model."""
from __future__ import annotations

import copy
import json
import unittest

from tests import fake_astrbot
fake_astrbot.install()

from tests import test_visits_actor as fixture  # noqa: E402
from core.visits.activities import activity_context, record_action  # noqa: E402
from core.visits.decisions import VisitDecisionError  # noqa: E402
from core.visits.flow import VisitFlow  # noqa: E402
from core.visits.outings import begin_activity  # noqa: E402


class VisitPhaseCapabilityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.VisitActorTests.asyncSetUp
    node_id = fixture.VisitActorTests.node_id

    def shared_visit(self, role="guest"):
        value = copy.deepcopy(self.visit)
        own, friend = "self:" + self.local["actor_id"], value["remote"]["id"]
        value.update(outing_version=1, role=role, expires_at=self.clock.now() + 3600,
            host_id=own if role == "host" else friend, guest_id=friend if role == "host" else own)
        value.pop("outing", None)
        return value

    async def request(self, value, raw, *, phase="turn"):
        captured = {}
        original = self.llm.generate

        async def generate(**kwargs):
            captured.update(kwargs)
            return await original(**kwargs)

        self.llm.generate = generate
        self.llm.default_reply = json.dumps(raw, ensure_ascii=False) if isinstance(raw, dict) else raw
        try:
            decision = await self.port.decide(fixture.fixture.SESSION, phase, value)
        finally:
            self.llm.generate = original
        return decision, json.loads(captured["prompt"]), captured

    @staticmethod
    def step(description, seconds=1):
        return {"description": description, "seconds": seconds, "waiting": False, "requires_peer": False}

    async def test_no_activity_host_can_propose_and_guest_accept_then_both_choose_short_actions(self):
        host = self.shared_visit("host")
        offer = {"place": "她自己选的旧钟楼", "activity": "听齿轮声找出停顿的一处", "minutes": 12}
        proposal, material, captured = await self.request(host, {"outing": offer,
            "action_step": self.step("东道主俯身听自己的那一侧", 2.5)})
        self.assertFalse(material["decision_capabilities"]["confirmed_activity"])
        self.assertTrue(material["decision_capabilities"]["can_propose_outing"])
        self.assertIn("这一轮就可自由提出第一段outing", captured["system_prompt"])
        self.assertNotIn("活动结束时才可选outcome或新outing", captured["system_prompt"])
        self.assertEqual(VisitFlow._shared_choice_issue(host, proposal, self.clock.now()), "")

        guest = self.shared_visit("guest")
        guest["outing_offer"] = proposal["outing"]
        guest["outing_host_step"] = proposal["action_step"]
        accepted, material, _ = await self.request(guest, {"outing_accept": True,
            "action_step": self.step("访客听另一侧的停顿", 1.25)})
        self.assertTrue(material["decision_capabilities"]["pending_offer"])
        self.assertEqual(VisitFlow._shared_choice_issue(guest, accepted, self.clock.now()), "")
        begin_activity(guest, proposal["outing"], self.clock.now(), accepted)
        self.assertTrue(record_action(guest, guest["host_id"], proposal["action_step"], self.clock.now()))
        self.assertTrue(record_action(guest, guest["guest_id"], accepted["action_step"], self.clock.now()))
        actions = activity_context(guest, self.clock.now())["actions"]
        self.assertEqual({row["seconds"] for row in actions}, {1.25, 2.5})
        self.assertFalse(guest["shared_activity"]["milestones"])

    async def test_guest_without_activity_can_suggest_without_impossible_action_envelope(self):
        value = self.shared_visit()
        decision, materials, captured = await self.request(value, {"say": "我想去听钟楼的声音。"})
        self.assertFalse(materials["decision_capabilities"]["confirmed_activity"])
        self.assertFalse(materials["decision_capabilities"]["pending_offer"])
        rules = captured["system_prompt"]
        self.assertIn("现在不提交action_step", rules)
        self.assertNotIn('结构示例：{"action_step"', rules)
        self.assertEqual(VisitFlow._shared_choice_issue(value, decision, self.clock.now()), "")
        self.assertFalse(value.get("outing"))

    async def test_pending_offer_requires_independent_choice_and_does_not_assert_started_activity(self):
        value = self.shared_visit()
        value["outing_offer"] = {"place": "远处钟楼", "activity": "听机械声", "minutes": 5, "travel_seconds": 90}
        decision, materials, captured = await self.request(value, {"outing_accept": False, "say": "我想先留在这里。"})
        self.assertTrue(materials["decision_capabilities"]["pending_offer"])
        self.assertFalse(materials["decision_capabilities"]["confirmed_activity"])
        self.assertIn("先独立给outing_accept:true或false", captured["system_prompt"])
        self.assertFalse(decision["outing_accept"])
        self.assertFalse(value.get("outing"))

    async def test_running_action_and_travel_conditions_are_visible_with_real_seconds(self):
        value = self.shared_visit()
        begin_activity(value, {"place": "钟楼", "activity": "听声音", "minutes": 5, "travel_seconds": 90}, self.clock.now())
        self.assertTrue(record_action(value, value["guest_id"], self.step("等抵达后听自己的那一侧", 2), self.clock.now()))
        _, materials, captured = await self.request(value, {"continue_activity": True, "say": "到了再听。"})
        caps = materials["decision_capabilities"]
        self.assertTrue(caps["traveling"])
        self.assertTrue(caps["foreground_busy"])
        self.assertFalse(caps["waiting_busy"])
        self.assertEqual(caps["actions_start_after_seconds"], 90)
        self.assertEqual(len(caps["own_open_action_ids"]), 1)
        self.assertIn("当前仍在路上", captured["system_prompt"])
        self.assertIn("选择另一类动作", captured["system_prompt"])
        self.assertEqual(captured["max_tokens"], 2048)

    async def test_ready_result_is_offered_to_host_and_not_guest(self):
        value = self.shared_visit("host")
        begin_activity(value, {"place": "钟楼", "activity": "听声音", "minutes": 5}, self.clock.now())
        self.assertTrue(record_action(value, value["host_id"], self.step("听自己这边", 1), self.clock.now()))
        self.clock.advance(2)
        _, materials, captured = await self.request(value, {"continue_activity": True})
        self.assertEqual(len(materials["decision_capabilities"]["ready_action_ids"]), 1)
        self.assertIn("东道主本轮可用scene_update", captured["system_prompt"])
        self.assertEqual(captured["max_tokens"], 2048)
        value["role"] = "guest"
        _, materials, captured = await self.request(value, {"continue_activity": True})
        self.assertEqual(materials["decision_capabilities"]["ready_action_ids"], [])
        self.assertIn("访客不提交scene_update", captured["system_prompt"])

    async def test_parse_failure_keeps_bounded_unexecuted_text_for_local_correction(self):
        value = self.shared_visit()
        raw = '{"say":' + "invalid draft " * 1000
        with self.assertRaises(VisitDecisionError) as caught:
            await self.request(value, raw)
        self.assertEqual(caught.exception.repair_text, raw[:8000])
        self.assertEqual(caught.exception.format_issue, "json_object")
        self.assertNotIn("invalid draft", json.dumps(value.get("log", [])))

    async def test_existing_cooperation_requires_explicit_peer_flag_and_repairs_that_conflict(self):
        value = self.shared_visit()
        begin_activity(value, {"place": "书桌", "activity": "一起折纸", "minutes": 5}, self.clock.now())
        partner = {**self.step("我捏住左侧纸角", 3), "requires_peer": True}
        self.assertTrue(record_action(value, value["host_id"], partner, self.clock.now()))
        reference = activity_context(value, self.clock.now())["actions"][0]["id"]
        raw = {"action_step": {**self.step("我捏住右侧纸角", 3), "peer_action_id": reference}}
        with self.assertRaises(VisitDecisionError) as caught:
            await self.request(value, raw)
        self.assertEqual(caught.exception.format_issue, "cooperation_flags")
        value["model_retry"] = {"phase": "turn", "category": "format", "format_attempts": 1,
            "format_issue": caught.exception.format_issue, "repair_text": caught.exception.repair_text}
        fixed = copy.deepcopy(raw)
        fixed["action_step"]["requires_peer"] = True
        decision, material, captured = await self.request(value, fixed)
        self.assertIn(reference, material["decision_capabilities"]["peer_cooperation_ids"])
        self.assertIn("peer_action_id存在时不能requires_peer:false", captured["system_prompt"])
        self.assertEqual(VisitFlow._shared_choice_issue(value, decision, self.clock.now()), "")
        self.assertEqual(decision["action_step"]["description"], raw["action_step"]["description"])
        self.assertFalse(value["shared_activity"]["milestones"])

    async def test_agreed_return_exposes_exact_time_and_diagnoses_long_action_without_shortening_it(self):
        value = self.shared_visit()
        begin_activity(value, {"place": "书桌", "activity": "折纸", "minutes": 2 / 60, "return_after": True},
            self.clock.now(), {"return_after": True})
        long_choice, materials, captured = await self.request(value,
            {"continue_activity": True, "action_step": self.step("完整折好一个新作品", 25)})
        before = copy.deepcopy(value)
        self.assertEqual(VisitFlow._shared_choice_issue(value, long_choice, self.clock.now()), "action_time")
        self.assertEqual(long_choice["action_step"]["seconds"], 25)
        self.assertEqual(value, before)
        caps = materials["decision_capabilities"]
        self.assertEqual(caps["available_action_seconds"], 2)
        self.assertEqual(caps["action_must_finish_at"], value["outing"]["until"])
        self.assertIn("保留未完打算", captured["system_prompt"])
        short_choice, _, _ = await self.request(value,
            {"continue_activity": True, "action_step": self.step("把这张纸放好留待下次继续", 1.5)})
        self.assertEqual(VisitFlow._shared_choice_issue(value, short_choice, self.clock.now()), "")
        self.assertEqual(value["outing"]["until"], before["outing"]["until"])

    async def test_existing_cooperation_partner_duration_uses_the_same_agreed_return_deadline(self):
        value = self.shared_visit()
        begin_activity(value, {"place": "书桌", "activity": "折纸", "minutes": 4 / 60, "return_after": True},
            self.clock.now(), {"return_after": True})
        request = {**self.step("一起按住折线", 3), "requires_peer": True}
        self.assertTrue(record_action(value, value["host_id"], request, self.clock.now()))
        reference = activity_context(value, self.clock.now())["actions"][0]["id"]
        self.clock.advance(2)
        choice, _, _ = await self.request(value, {"continue_activity": True,
            "action_step": {**self.step("我按住自己的这边", 1), "requires_peer": True, "peer_action_id": reference}})
        before = copy.deepcopy(value)
        self.assertEqual(VisitFlow._shared_choice_issue(value, choice, self.clock.now()), "action_time")
        self.assertEqual(value, before)
        value["outing"]["return_agreed"] = False
        self.assertEqual(VisitFlow._shared_choice_issue(value, choice, self.clock.now()), "")

    async def test_matching_repair_survives_provider_wait_and_stays_private_prompt_data(self):
        value = self.shared_visit()
        value["model_retry"] = {"phase": "turn", "category": "provider", "failures": 4,
            "format_attempts": 1, "format_issue": "action_references",
            "repair_text": '{"say":"我的原意","action_step":{"description":"未执行"}}',
            "repair_choice": {"say": "我的原意", "action": "未执行"}}
        before = copy.deepcopy(value)
        decision, materials, captured = await self.request(value, {"say": "我的原意"})
        draft = materials["failed_unexecuted_choice"]
        self.assertEqual(draft["status"], "not_executed")
        self.assertEqual(draft["choice"]["say"], "我的原意")
        self.assertNotIn("我的原意", captured["system_prompt"])
        self.assertIn("保留仍适用的原意、台词和选择", captured["system_prompt"])
        self.assertEqual(value, before)
        self.assertEqual(decision["say"], "我的原意")
        self.assertNotIn("未执行", json.dumps(materials["confirmed_log"]))
        for change in ({"phase": "scene"}, {"format_attempts": 0}, {"paused": True}):
            value["model_retry"] = {**before["model_retry"], **change}
            _, materials, _ = await self.request(value, {"say": "现在听着。"})
            self.assertNotIn("failed_unexecuted_choice", materials)

    def test_phase_output_budgets_allow_envelopes_without_unbounded_increase(self):
        port = fixture.VisitActorPort
        shared = {"outing_version": 1}
        for configured in (64, 240, 512):
            for phase in ("turn", "scene"):
                self.assertEqual(port._decision_output_limit(phase, shared, {}, max_output_tokens=configured), 1536)
                self.assertEqual(port._decision_output_limit(phase, shared, {"ready_action_ids": ["s0-a1"]},
                    max_output_tokens=configured), 2048)
            self.assertLessEqual(port._decision_output_limit("propose", shared, {}, max_output_tokens=configured), 1536)
        finish = port._interaction_rules("finish", shared, {})
        self.assertNotIn("action_step=", finish)
        self.assertNotIn("propose先", finish)


if __name__ == "__main__":
    unittest.main()
