"""Accepted activity transitions preserve facts without moving objects by assumption."""
from __future__ import annotations

import copy
import unittest

from tests import fake_astrbot
fake_astrbot.install()

from core.visits.activities import apply_scene_update, record_action, validate_shared_activity  # noqa: E402
from core.visits.outings import begin_activity  # noqa: E402
from tests import test_visit_shared_activities_20261113 as fixture  # noqa: E402


class ActivityContinuityTests(unittest.TestCase):
    def prepared_tea(self, *, pending=None):
        value = fixture.visit()
        record_action(value, fixture.HOST, fixture.step("把茶冲好", 1), fixture.NOW)
        row = value["shared_activity"]["action_steps"][fixture.HOST][0]
        self.assertTrue(apply_scene_update(value, fixture.HOST, {
            "summary": "在厨房准备了一壶茶和两只杯子", "action_ids": [row["id"]],
            "objects": ["一壶茶", "两只杯子"], "pending": pending or []}, fixture.NOW + 1))
        return value

    def test_same_place_new_activity_keeps_prepared_objects(self):
        value = self.prepared_tea()
        original = copy.deepcopy(value["shared_activity"]["objects"])
        begin_activity(value, {"place": "厨房", "activity": "一起尝茶", "minutes": 10}, fixture.NOW + 2)
        self.assertEqual(value["shared_activity"]["objects"], original)
        self.assertEqual(value["shared_activity"]["goal"], "一起尝茶")
        self.assertEqual(value["shared_activity"]["milestones"][-1]["objects"], original)
        validate_shared_activity(value["shared_activity"], {fixture.HOST, fixture.GUEST}, outing=value["outing"], now=fixture.NOW + 2)

    def test_different_place_does_not_automatically_carry_objects(self):
        value = self.prepared_tea()
        begin_activity(value, {"place": "公园", "activity": "一起散步", "minutes": 10}, fixture.NOW + 2)
        self.assertEqual(value["shared_activity"]["objects"], [])
        self.assertEqual(value["shared_activity"]["milestones"][-1]["objects"], ["一壶茶", "两只杯子"])

    def test_new_automatic_notes_do_not_evict_eight_explicit_unfinished_matters(self):
        old = [f"双方明确留待继续的事项{number}" for number in range(8)]
        value = self.prepared_tea(pending=old)
        record_action(value, fixture.HOST, fixture.step("还没确认结果的新动作", 30), fixture.NOW + 1)
        begin_activity(value, {"place": "厨房", "activity": "接着一起待着", "minutes": 10, "event": "一件新小事"}, fixture.NOW + 2)
        self.assertEqual(value["shared_activity"]["pending"], old)
        self.assertEqual(value["scene"]["event"], "一件新小事")
        self.assertFalse(value["shared_activity"]["done"])

    def test_spare_capacity_carries_unfinished_action_and_new_event(self):
        value = self.prepared_tea(pending=["下次再一起比较两种茶"])
        record_action(value, fixture.GUEST, fixture.step("读完手里的这一页", 30), fixture.NOW + 1)
        begin_activity(value, {"place": "厨房", "activity": "接着聊", "minutes": 10, "event": "风吹动了桌上的纸"}, fixture.NOW + 2)
        self.assertEqual(value["shared_activity"]["pending"], [
            "下次再一起比较两种茶", "尚未确认结果：读完手里的这一页", "风吹动了桌上的纸"])

    def test_missing_old_place_proof_does_not_copy_objects_by_guess(self):
        value = self.prepared_tea()
        value["shared_activity"]["stage"] = 99
        begin_activity(value, {"place": "厨房", "activity": "重新确认眼前的事", "minutes": 10}, fixture.NOW + 2)
        self.assertEqual(value["shared_activity"]["objects"], [])


if __name__ == "__main__":
    unittest.main()
