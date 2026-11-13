"""Action references keep cooperation, cancellation and result scope independent."""
from __future__ import annotations

import copy
import json
import unittest

from tests import fake_astrbot
fake_astrbot.install()

from core.visits.activities import (  # noqa: E402
    activity_context, advance_activity, apply_action_control, apply_scene_update, record_action, validate_shared_activity,
)
from core.visits.protocol import clean_action_control, clean_action_step, clean_decision, clean_scene_update  # noqa: E402
from core.visits.outings import begin_activity  # noqa: E402
from tests import test_visit_shared_activities_20261113 as fixture  # noqa: E402


class ActionReferenceTests(unittest.TestCase):
    def records(self, value, actor):
        return value["shared_activity"]["action_steps"][actor]

    def pair(self, value):
        record_action(value, fixture.HOST, fixture.step("一起铺布，我拉左边", 1, requires_peer=True), fixture.NOW)
        first = self.records(value, fixture.HOST)[0]
        record_action(value, fixture.GUEST,
                      fixture.step("愿意拉这块布的右边", 2, requires_peer=True, peer_action_id=first["id"]), fixture.NOW)
        return first, self.records(value, fixture.GUEST)[0]

    def test_unrelated_cooperation_proposals_do_not_pair_automatically(self):
        value = fixture.visit()
        record_action(value, fixture.HOST, fixture.step("请帮我修风车", requires_peer=True), fixture.NOW)
        record_action(value, fixture.GUEST, fixture.step("请帮我拼这幅拼图", requires_peer=True), fixture.NOW)
        statuses = [row["status"] for rows in value["shared_activity"]["action_steps"].values() for row in rows]
        self.assertEqual(statuses, ["awaiting_peer", "awaiting_peer"])

    def test_cancel_choice_survives_protocol_cleaning(self):
        result = clean_decision({"action_control": {"action_id": "s0-a1", "operation": "cancel"}})
        self.assertEqual(result.get("action_control"), {"action_id": "s0-a1", "operation": "cancel"})

    def test_scene_result_consumes_only_the_referenced_ready_action(self):
        value = fixture.visit()
        record_action(value, fixture.HOST, fixture.step("倒自己的茶", 1), fixture.NOW)
        record_action(value, fixture.GUEST, fixture.step("折自己的纸船", 1), fixture.NOW)
        records = value["shared_activity"]["action_steps"]
        chosen = records[fixture.HOST][0]["id"]
        self.assertTrue(apply_scene_update(value, fixture.HOST,
            {"summary": "东道主的杯子里已经有茶", "action_ids": [chosen]}, fixture.NOW + 2))
        self.assertNotIn("settled_revision", records[fixture.GUEST][0])
        self.assertEqual(value["shared_activity"]["milestones"][-1]["actions"], {fixture.HOST: [chosen]})

    def test_reference_chooses_exact_request_when_peer_has_two_different_proposals(self):
        value = fixture.visit()
        record_action(value, fixture.HOST, fixture.step("请帮我修风车", 1, requires_peer=True), fixture.NOW)
        record_action(value, fixture.HOST, fixture.step("一起等这班船", 3, waiting=True, requires_peer=True), fixture.NOW)
        requests = self.records(value, fixture.HOST)
        self.assertTrue(record_action(value, fixture.GUEST, fixture.step("愿意一起等这班船", 3, waiting=True,
            requires_peer=True, peer_action_id=requests[1]["id"]), fixture.NOW + 1))
        self.assertEqual(requests[0]["status"], "awaiting_peer")
        self.assertEqual(requests[1]["status"], "running")
        self.assertEqual(self.records(value, fixture.GUEST)[0]["joint_id"], requests[1]["joint_id"])
        validate_shared_activity(value["shared_activity"], {fixture.HOST, fixture.GUEST}, outing=value["outing"], now=fixture.NOW + 1)

    def test_missing_own_and_wrong_stage_references_do_not_free_or_create_slots(self):
        value = fixture.visit()
        record_action(value, fixture.HOST, fixture.step("待配合", 2, requires_peer=True), fixture.NOW)
        original = copy.deepcopy(value)
        for actor, reference in ((fixture.GUEST, "s0-a999"), (fixture.HOST, self.records(value, fixture.HOST)[0]["id"]),
                                 (fixture.GUEST, "s1-a1")):
            self.assertFalse(record_action(value, actor,
                fixture.step("错误加入", 1, requires_peer=True, peer_action_id=reference), fixture.NOW))
            self.assertEqual(value, original)

    def test_declining_a_specific_request_releases_its_owner_without_new_peer_choices(self):
        value = fixture.visit()
        record_action(value, fixture.HOST, fixture.step("一起修风车", 1, requires_peer=True), fixture.NOW)
        request = self.records(value, fixture.HOST)[0]
        self.assertTrue(apply_action_control(value, fixture.GUEST,
            {"action_id": request["id"], "operation": "decline", "reason": "这次想安静读书"}, fixture.NOW + 1))
        self.assertEqual(request["status"], "declined")
        self.assertEqual(request["controlled_by"], fixture.GUEST)
        self.assertNotIn(fixture.GUEST, value["shared_activity"]["action_steps"])
        validate_shared_activity(value["shared_activity"], {fixture.HOST, fixture.GUEST}, outing=value["outing"], now=fixture.NOW + 1)
        self.assertTrue(record_action(value, fixture.HOST, fixture.step("自己换一个办法", 1), fixture.NOW + 1))
        self.assertEqual(len(self.records(value, fixture.HOST)), 1)

    def test_cancelling_scheduled_action_does_not_cancel_route_or_invent_success(self):
        value = fixture.visit(travel=120)
        record_action(value, fixture.HOST, fixture.step("到了以后摆杯子", 2), fixture.NOW)
        row = self.records(value, fixture.HOST)[0]
        route = copy.deepcopy(value["outing"])
        self.assertTrue(apply_action_control(value, fixture.HOST, {"action_id": row["id"], "operation": "cancel"}, fixture.NOW + 1))
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(value["outing"], route)
        self.assertEqual(activity_context(value, fixture.NOW + 200)["ready_action_ids"], [])
        self.assertFalse(apply_scene_update(value, fixture.HOST, {"summary": "摆好了杯子", "action_ids": [row["id"]]}, fixture.NOW + 200))
        validate_shared_activity(value["shared_activity"], {fixture.HOST, fixture.GUEST}, outing=value["outing"], now=fixture.NOW + 1)

    def test_cancelling_started_cooperation_interrupts_only_linked_action(self):
        value = fixture.visit()
        left, right = self.pair(value)
        untils = left["until"], right["until"]
        self.assertTrue(apply_action_control(value, fixture.HOST, {"action_id": left["id"], "operation": "cancel"}, fixture.NOW + 0.5))
        self.assertEqual((left["status"], right["status"]), ("cancelled", "interrupted"))
        self.assertEqual((left["until"], right["until"]), untils)
        self.assertEqual(right["control_action_id"], left["id"])
        self.assertFalse(advance_activity(value, fixture.NOW + 10))
        self.assertEqual(activity_context(value, fixture.NOW + 10)["ready_action_ids"], [])
        validate_shared_activity(value["shared_activity"], {fixture.HOST, fixture.GUEST}, outing=value["outing"], now=fixture.NOW + 10)
        self.assertTrue(record_action(value, fixture.GUEST, fixture.step("自己把布收起来", 1), fixture.NOW + 10))
        self.assertEqual(len(self.records(value, fixture.GUEST)), 1)
        validate_shared_activity(value["shared_activity"], {fixture.HOST, fixture.GUEST}, outing=value["outing"], now=fixture.NOW + 10)

    def test_control_cannot_cancel_peer_reject_own_or_rewrite_settled_result(self):
        value = fixture.visit()
        record_action(value, fixture.HOST, fixture.step("自己的动作", 1), fixture.NOW)
        row = self.records(value, fixture.HOST)[0]
        for actor, operation in ((fixture.GUEST, "cancel"), (fixture.HOST, "decline"), (fixture.GUEST, "decline")):
            self.assertFalse(apply_action_control(value, actor, {"action_id": row["id"], "operation": operation}, fixture.NOW))
        self.assertTrue(apply_scene_update(value, fixture.HOST, {"summary": "自己的动作已确认", "action_ids": [row["id"]]}, fixture.NOW + 1))
        self.assertFalse(apply_action_control(value, fixture.HOST, {"action_id": row["id"], "operation": "cancel"}, fixture.NOW + 2))

    def test_both_cooperation_ids_required_and_unrelated_ready_step_is_not_consumed(self):
        value = fixture.visit()
        left, right = self.pair(value)
        record_action(value, fixture.HOST, fixture.step("等水开", 1, waiting=True), fixture.NOW)
        separate = self.records(value, fixture.HOST)[1]
        self.assertFalse(apply_scene_update(value, fixture.HOST, {"summary": "布铺好了", "action_ids": [left["id"]]}, fixture.NOW + 2))
        self.assertTrue(apply_scene_update(value, fixture.HOST,
            {"summary": "两人铺好了布", "action_ids": [left["id"], right["id"]]}, fixture.NOW + 2))
        self.assertNotIn("settled_revision", separate)
        self.assertEqual(activity_context(value, fixture.NOW + 2)["ready_action_ids"], [separate["id"]])

    def test_legacy_unreferenced_update_is_only_allowed_for_one_ready_action(self):
        value = fixture.visit()
        record_action(value, fixture.HOST, fixture.step("倒茶", 1), fixture.NOW)
        record_action(value, fixture.GUEST, fixture.step("折纸", 1), fixture.NOW)
        self.assertFalse(apply_scene_update(value, fixture.HOST, {"summary": "不知道归纳了哪一步"}, fixture.NOW + 2))
        own = self.records(value, fixture.HOST)[0]
        apply_action_control(value, fixture.HOST, {"action_id": own["id"], "operation": "cancel"}, fixture.NOW + 2)
        self.assertTrue(apply_scene_update(value, fixture.HOST, {"summary": "只有纸船这件事可确认"}, fixture.NOW + 2))

    def test_result_references_cannot_repeat_or_complete_other_unresolved_work(self):
        value = fixture.visit()
        record_action(value, fixture.HOST, fixture.step("倒茶", 1), fixture.NOW)
        record_action(value, fixture.GUEST, fixture.step("折纸", 10), fixture.NOW)
        own = self.records(value, fixture.HOST)[0]
        self.assertFalse(apply_scene_update(value, fixture.HOST,
            {"summary": "两个人都完成了", "action_ids": [own["id"]], "done": True}, fixture.NOW + 2))
        self.assertTrue(apply_scene_update(value, fixture.HOST, {"summary": "只倒好了茶", "action_ids": [own["id"]]}, fixture.NOW + 2))
        self.assertFalse(apply_scene_update(value, fixture.HOST, {"summary": "又完成一次", "action_ids": [own["id"]]}, fixture.NOW + 3))
        self.assertFalse(apply_scene_update(value, fixture.HOST, {"summary": "引用了上一站", "action_ids": ["s9-a1"]}, fixture.NOW + 3))

    def test_saved_legacy_started_joint_pair_remains_valid(self):
        value = fixture.visit()
        left, right = self.pair(value)
        right.pop("peer_action_id")  # v4.47 already-started data did not have this field.
        validate_shared_activity(value["shared_activity"], {fixture.HOST, fixture.GUEST}, outing=value["outing"], now=fixture.NOW)
        self.assertTrue(apply_scene_update(value, fixture.HOST,
            {"summary": "保留已确认旧协作的结果", "action_ids": [left["id"], right["id"]]}, fixture.NOW + 2))

    def test_malformed_references_controls_and_forged_repeat_evidence_rejected(self):
        self.assertEqual(clean_action_step(fixture.step(peer_action_id="s0-a1")), {})
        self.assertEqual(clean_action_control({"action_id": [], "operation": "cancel"}), {})
        self.assertEqual(clean_scene_update({"summary": "变化", "action_ids": ["s0-a1", "s0-a1"]}), {})
        value = fixture.visit()
        record_action(value, fixture.HOST, fixture.step("一步", 1), fixture.NOW)
        apply_scene_update(value, fixture.HOST, {"summary": "完成这一步"}, fixture.NOW + 1)
        duplicate = copy.deepcopy(value["shared_activity"]["milestones"][0])
        value["shared_activity"]["revision"] += 1
        duplicate["id"] += 1
        value["shared_activity"]["milestones"].append(duplicate)
        with self.assertRaises(ValueError):
            validate_shared_activity(value["shared_activity"], {fixture.HOST, fixture.GUEST}, outing=value["outing"], now=fixture.NOW + 1)

    def test_new_stage_does_not_turn_closed_actions_into_unfinished_work(self):
        value = fixture.visit()
        left, _ = self.pair(value)
        record_action(value, fixture.HOST, fixture.step("请求一起等船", 10, waiting=True, requires_peer=True), fixture.NOW)
        request = self.records(value, fixture.HOST)[1]
        apply_action_control(value, fixture.HOST, {"action_id": left["id"], "operation": "cancel"}, fixture.NOW + 0.5)
        apply_action_control(value, fixture.GUEST, {"action_id": request["id"], "operation": "decline"}, fixture.NOW + 0.5)
        begin_activity(value, {"place": "窗边", "activity": "看看云", "minutes": 5}, fixture.NOW + 1)
        self.assertEqual(value["shared_activity"]["pending"], [])
        self.assertEqual(value["shared_activity"]["action_steps"], {})

    def test_full_action_capacity_and_invalid_choices_do_not_discard_history(self):
        value = fixture.visit()
        for number in range(6):
            self.assertTrue(record_action(value, fixture.HOST, fixture.step(f"未结算片刻{number}", 1), fixture.NOW + 2 * number))
        original = copy.deepcopy(value)
        self.assertFalse(record_action(value, fixture.HOST, fixture.step("第七个片刻", 1), fixture.NOW + 10))
        self.assertEqual(value, original)
        last = self.records(value, fixture.HOST)[-1]
        self.assertTrue(apply_action_control(value, fixture.HOST, {"action_id": last["id"], "operation": "cancel"}, fixture.NOW + 10))
        self.assertTrue(record_action(value, fixture.HOST, fixture.step("取消后重新选择", 1), fixture.NOW + 10))
        self.assertEqual(len(self.records(value, fixture.HOST)), 6)

    def test_adapter_preserves_control_only_decision_and_requires_new_result_references(self):
        from tests import test_visits_actor as actor_fixture
        parser = actor_fixture.VisitActorPort._parse_decision
        result = parser("turn", json.dumps({"action_control": {"action_id": "s0-a1", "operation": "decline"}}))
        self.assertEqual(result["action_control"]["operation"], "decline")
        result = parser("turn", json.dumps({"scene_update": {"summary": "实际公开变化", "action_ids": ["s0-a1"]}}))
        self.assertEqual(result["scene_update"]["action_ids"], ["s0-a1"])
        with self.assertRaises(actor_fixture.VisitDecisionError) as failure:
            parser("turn", json.dumps({"scene_update": {"summary": "没有引用依据"}}))
        self.assertEqual(failure.exception.format_issue, "action_references")
        rules = actor_fixture.VisitActorPort._decision_output_rules("turn", {"model_retry": {
            "category": "format", "phase": "turn", "failures": 1, "format_issue": "action_references"}})
        self.assertIn("引用了现在不能接受、取消或结算的动作", rules)


if __name__ == "__main__":
    unittest.main()
