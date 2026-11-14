"""Unsettled action capacity is visible without fabricating or dropping outcomes."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from tests import test_visit_shared_activities_20261113 as activity_fixture
from tests import test_visits_actor as actor_fixture
from core.visits.activities import action_capacity, activity_context, advance_activity, record_action
from core.visits.decisions import VisitDecisionError
from core.visits.flow import VisitFlow


HOST, GUEST, NOW = activity_fixture.HOST, activity_fixture.GUEST, activity_fixture.NOW


def full_stop(role="guest"):
    value = activity_fixture.visit()
    value["role"] = role
    for actor, count in ((GUEST, 6), (HOST, 5)):
        for index in range(count):
            assert record_action(value, actor, activity_fixture.step(f"choice-{index}", 1), NOW + index * 2)
    advance_activity(value, NOW + 20)
    return value


class ActionCapacityTests(unittest.TestCase):
    def test_actual_six_and_five_elapsed_pattern_exposes_remaining_slots_without_mutation(self):
        value = full_stop()
        original = copy.deepcopy(value)
        public = activity_context(value, NOW + 20)
        self.assertEqual(public["action_capacity"][GUEST], {"limit": 6, "unresolved": 6, "remaining": 0})
        self.assertEqual(public["action_capacity"][HOST], {"limit": 6, "unresolved": 5, "remaining": 1})
        self.assertEqual(len(public["ready_action_ids"]), 11)
        self.assertEqual(value, original)
        self.assertFalse(value["shared_activity"]["milestones"])

    def test_settled_and_explicitly_closed_choices_do_not_consume_capacity(self):
        value = full_stop()
        rows = value["shared_activity"]["action_steps"][GUEST]
        rows[0]["settled_revision"] = 1
        rows[1]["status"] = "cancelled"
        rows[2]["status"] = "declined"
        rows[3]["status"] = "interrupted"
        self.assertEqual(action_capacity(value, GUEST), {"limit": 6, "unresolved": 2, "remaining": 4})
        value["shared_activity"]["stage"] += 1
        self.assertEqual(action_capacity(value, GUEST), {"limit": 6, "unresolved": 0, "remaining": 6})

    def test_full_guest_can_cancel_own_unresolved_choice_and_start_another_in_the_same_turn(self):
        value = full_stop()
        original = copy.deepcopy(value)
        step = activity_fixture.step("new personal choice", 1)
        self.assertFalse(VisitFlow._shared_choice_executable(value,
            {"continue_activity": True, "action_step": step}, NOW + 20))
        own = value["shared_activity"]["action_steps"][GUEST][0]["id"]
        self.assertTrue(VisitFlow._shared_choice_executable(value, {"continue_activity": True,
            "action_control": {"action_id": own, "operation": "cancel"}, "action_step": step}, NOW + 20))
        self.assertEqual(value, original)

    def test_host_can_confirm_referenced_result_then_start_a_new_choice_without_erasing_the_stop(self):
        value = full_stop("host")
        self.assertTrue(record_action(value, HOST, activity_fixture.step("sixth host choice", 1), NOW + 20))
        advance_activity(value, NOW + 22)
        original = copy.deepcopy(value)
        own = value["shared_activity"]["action_steps"][HOST][0]["id"]
        self.assertTrue(VisitFlow._shared_choice_executable(value, {"continue_activity": True,
            "scene_update": {"summary": "A result the host independently confirmed", "action_ids": [own]},
            "action_step": activity_fixture.step("next personal choice", 1)}, NOW + 22))
        self.assertEqual(value, original)

    def test_capacity_retry_is_specific_and_retained_as_a_structural_label(self):
        error = VisitDecisionError("format", format_issue="action_capacity")
        self.assertEqual(error.format_issue, "action_capacity")
        rules = actor_fixture.VisitActorPort._decision_output_rules("turn", {"model_retry": {
            "category": "format", "phase": "turn", "attempts": 1, "format_issue": error.format_issue}})
        for text in ("已有6个", "elapsed", "ready_action_ids", "action_control", "不能自动宣称"):
            self.assertIn(text, rules)

    def test_failure_trace_contains_only_structural_capacity_and_no_private_content(self):
        value = full_stop()
        value.update(id="visit-capacity", private={"thought": "private wording"})
        value["decision"] = {"say": "private candidate"}
        trace = Mock()
        port = actor_fixture.VisitActorPort(SimpleNamespace(engine=SimpleNamespace(decision_trace=trace)))
        port.record_decision_issue("life", "turn", value, "action_capacity")
        args, details = trace.call_args
        self.assertEqual(args[1:3], ("world_visit", "invalid_format"))
        self.assertEqual(details["action_capacity"], {"limit": 6, "unresolved": 6, "remaining": 0})
        self.assertEqual(details["format_issue"], "action_capacity")
        self.assertNotIn("private", json.dumps(details))
        trace.side_effect = RuntimeError("unavailable journal")
        port.record_decision_issue("life", "turn", value, "action_capacity")


class ActionCapacityMaterialsTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = actor_fixture.VisitActorTests.asyncSetUp
    node_id = actor_fixture.VisitActorTests.node_id

    async def test_real_adapter_supplies_each_actors_capacity_and_guides_without_automatic_success(self):
        value = copy.deepcopy(self.visit)
        value.update(outing_version=1, host_id="instance-b:actor-b", guest_id="self:" + self.local["actor_id"],
                     expires_at=self.clock.now() + 3600)
        activity_fixture.begin_activity(value, {"place": "书房", "activity": "读书", "minutes": 30}, self.clock.now())
        for index in range(6):
            self.assertTrue(record_action(value, value["guest_id"],
                activity_fixture.step("self-selected choice", 1), self.clock.now() + index * 2))
        self.clock.advance(20)
        self.llm.default_reply = '{"continue_activity":true,"pause_minutes":5}'
        await self.port.decide(actor_fixture.fixture.SESSION, "turn", value)
        materials = json.loads(self.llm.calls[-1]["prompt"])
        self.assertEqual(materials["shared_activity"]["action_capacity"][value["guest_id"]]["remaining"], 0)
        rules = self.llm.calls[-1]["system_prompt"]
        self.assertIn("elapsed也占位置", rules)
        self.assertIn("不要反复提交第七个动作", rules)
        self.assertFalse(value["shared_activity"]["milestones"])


if __name__ == "__main__":
    unittest.main()
