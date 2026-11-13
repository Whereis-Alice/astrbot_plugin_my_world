"""Open personal actions, independent clocks and public results without invented progress."""
from __future__ import annotations

import copy
import unittest

from tests import fake_astrbot
fake_astrbot.install()

from core.visits.activities import (  # noqa: E402
    activity_context, advance_activity, apply_scene_update, next_activity_due,
    record_action, validate_shared_activity,
)
from core.visits.outings import begin_activity, valid_activity  # noqa: E402
from core.visits.protocol import clean_action_step, clean_decision, clean_outing_offer, clean_scene_update  # noqa: E402


HOST, GUEST = "host:alice", "guest:isla"
NOW = 1800000000.0


def step(description="冲泡茶叶", seconds=180, *, waiting=False, requires_peer=False, peer_action_id=None):
    result = {"description": description, "seconds": seconds, "waiting": waiting, "requires_peer": requires_peer}
    if peer_action_id is not None:
        result["peer_action_id"] = peer_action_id
    return result


def visit(*, minutes=30, travel=0, return_after=False):
    value = {"status": "active", "outing_version": 1, "host_id": HOST, "guest_id": GUEST,
             "expires_at": NOW + 3600, "scene": {"place": "客厅"}, "local": {"place": "客厅"}}
    offer = clean_outing_offer({"place": "厨房" if not travel else "公园", "activity": "一起喝茶",
                               "minutes": minutes, "travel_seconds": travel,
                               "pause_minutes": 20, "return_after": return_after})
    begin_activity(value, offer, NOW, {"pause_minutes": 5, "return_after": return_after}, floor_seconds=15)
    return value


class SharedActivityTests(unittest.TestCase):
    def test_unknown_actions_and_fractional_seconds_remain_open(self):
        self.assertEqual(clean_action_step(step("把纸船折出一个新造型", 0.125))["seconds"], 0.125)
        cleaned = clean_decision({"action_step": step("观察罕见的云形", 45.25),
                                  "scene_update": {"summary": "只确认眼前的公开变化", "pending": ["还没观察完"]}})
        self.assertEqual(cleaned["action_step"]["description"], "观察罕见的云形")
        self.assertEqual(cleaned["scene_update"]["pending"], ["还没观察完"])
        self.assertEqual(clean_outing_offer({"place": "窗边", "activity": "看一眼云", "minutes": 0.5})["minutes"], 0.5)

    def test_malformed_timers_and_public_objects_do_not_become_choices(self):
        for seconds in (True, "3", float("nan"), float("inf"), -1, 0, 14401):
            self.assertEqual(clean_action_step(step(seconds=seconds)), {})
        self.assertEqual(clean_action_step({**step(), "waiting": "false"}), {})
        self.assertEqual(clean_scene_update({"summary": "变化", "objects": [{"private": "thought"}]}), {})
        self.assertEqual(clean_scene_update({"summary": "变化", "done": "true"}), {})
        self.assertEqual(clean_scene_update({"summary": "变化", "pending": ["事项"] * 9}), {})
        self.assertEqual(clean_outing_offer({"place": "湖边", "activity": "走走", "travel_seconds": True}), {})
        self.assertEqual(clean_outing_offer({"place": "湖边", "activity": "走走", "minutes": -1}), {})

    def test_short_action_finishes_its_clock_inside_long_quiet_company(self):
        value = visit()
        self.assertTrue(record_action(value, HOST, step(), NOW))
        quiet = value["outing"]["quiet_until"]
        self.assertEqual(quiet, NOW + 1200)
        self.assertEqual(next_activity_due(value, NOW), NOW + 180)
        self.assertFalse(advance_activity(value, NOW + 179))
        self.assertTrue(advance_activity(value, NOW + 180))
        action = value["shared_activity"]["action_steps"][HOST][0]
        self.assertEqual(action["status"], "elapsed")
        self.assertEqual(value["outing"]["quiet_until"], quiet)
        self.assertEqual(value["shared_activity"]["summary"], "")
        self.assertFalse(value["shared_activity"]["done"])
        self.assertEqual(value["shared_activity"]["milestones"], [])

    def test_independent_host_operation_can_settle_while_guest_only_chats(self):
        value = visit()
        record_action(value, HOST, step(), NOW)
        update = {"summary": "茶已冲好，尚未评价味道", "objects": ["一壶茶"], "pending": ["各自尝尝茶"]}
        self.assertFalse(apply_scene_update(value, HOST, update, NOW + 179))
        self.assertTrue(apply_scene_update(value, HOST, update, NOW + 180))
        self.assertEqual(value["shared_activity"]["objects"], ["一壶茶"])
        self.assertEqual(value["shared_activity"]["milestones"][0]["actions"].keys(), {HOST})
        self.assertFalse(apply_scene_update(value, HOST, update, NOW + 181))
        self.assertEqual(len(value["shared_activity"]["milestones"]), 1)

    def test_guest_choice_may_be_resolved_by_host_but_guest_cannot_publish_world(self):
        value = visit()
        record_action(value, GUEST, step("摆好自己的杯子", 2), NOW)
        self.assertFalse(apply_scene_update(value, GUEST, {"summary": "擅自宣称双方都答应了"}, NOW + 3))
        self.assertTrue(apply_scene_update(value, HOST, {"summary": "她放下了自己的杯子"}, NOW + 3))
        self.assertFalse(record_action(value, "unknown", step(), NOW + 3))

    def test_cooperation_waits_for_peer_and_does_not_backdate_the_start(self):
        value = visit()
        record_action(value, HOST, step("一起抬桌子，我抬左边", 10, requires_peer=True), NOW)
        self.assertIsNone(next_activity_due(value, NOW))
        self.assertFalse(advance_activity(value, NOW + 100))
        self.assertFalse(apply_scene_update(value, HOST, {"summary": "桌子已经搬好"}, NOW + 100))
        reference = value["shared_activity"]["action_steps"][HOST][0]["id"]
        record_action(value, GUEST, step("我愿意抬右边", 20, requires_peer=True, peer_action_id=reference), NOW + 100)
        first = value["shared_activity"]["action_steps"][HOST][0]
        second = value["shared_activity"]["action_steps"][GUEST][0]
        self.assertEqual(first["started_at"], NOW + 100)
        self.assertEqual(second["started_at"], first["started_at"])
        self.assertEqual(first["joint_id"], second["joint_id"])
        self.assertFalse(apply_scene_update(value, HOST, {"summary": "桌子已经搬好"}, NOW + 110))
        self.assertTrue(apply_scene_update(value, HOST, {"summary": "两人把桌子移到了窗边",
            "action_ids": [first["id"], second["id"]]}, NOW + 120))
        validate_shared_activity(value["shared_activity"], {HOST, GUEST}, outing=value["outing"], now=NOW + 120)

    def test_one_person_can_arrange_cups_while_waiting_for_water(self):
        value = visit()
        self.assertTrue(record_action(value, HOST, step("等水烧开", 180, waiting=True), NOW))
        self.assertTrue(record_action(value, HOST, step("摆杯子", 4), NOW + 1))
        self.assertFalse(record_action(value, HOST, step("同时做另一项专注动作", 10), NOW + 2))
        self.assertFalse(record_action(value, HOST, step("再启动另一个等待过程", 60, waiting=True), NOW + 2))
        self.assertEqual(next_activity_due(value, NOW + 2), NOW + 5)
        advance_activity(value, NOW + 5)
        context = activity_context(value, NOW + 5)
        self.assertEqual([row["status"] for row in context["actions"]], ["running", "elapsed"])
        self.assertEqual(next_activity_due(value, NOW + 5), NOW + 180)
        validate_shared_activity(value["shared_activity"], {HOST, GUEST}, outing=value["outing"], now=NOW + 5)

    def test_delayed_poll_keeps_real_action_deadline_and_invents_no_continuation(self):
        value = visit()
        record_action(value, HOST, step("倒茶", 1.5), NOW)
        advance_activity(value, NOW + 600)
        row = value["shared_activity"]["action_steps"][HOST][0]
        self.assertEqual(row["elapsed_at"], NOW + 1.5)
        self.assertEqual(row["until"], NOW + 1.5)
        self.assertEqual(len(value["shared_activity"]["action_steps"][HOST]), 1)
        self.assertEqual(value["shared_activity"]["milestones"], [])
        self.assertIsNone(next_activity_due(value, NOW + 600))

    def test_context_is_read_only_and_never_reports_elapsed_as_success(self):
        value = visit()
        record_action(value, HOST, step("尝茶", 2.25), NOW)
        saved = copy.deepcopy(value)
        context = activity_context(value, NOW + 3)
        self.assertEqual(value, saved)
        self.assertEqual(context["actions"][0]["status"], "elapsed")
        self.assertFalse(context["done"])
        self.assertEqual(context["ready_action_ids"], [context["actions"][0]["id"]])
        context["pending"].append("不能回写")
        self.assertEqual(value, saved)

    def test_new_stop_preserves_history_but_cannot_settle_old_station_actions(self):
        value = visit()
        record_action(value, HOST, step("摆杯子", 2), NOW)
        apply_scene_update(value, HOST, {"summary": "摆好了杯子", "pending": ["还没倒茶"]}, NOW + 3)
        record_action(value, GUEST, step("折纸船", 30), NOW + 3)
        begin_activity(value, clean_outing_offer({"place": "公园", "activity": "放纸船", "minutes": 10}), NOW + 5)
        self.assertEqual(len(value["shared_activity"]["milestones"]), 1)
        self.assertIn("还没倒茶", value["shared_activity"]["pending"])
        self.assertIn("尚未确认结果：折纸船", value["shared_activity"]["pending"])
        self.assertEqual(value["shared_activity"]["action_steps"], {})
        self.assertFalse(apply_scene_update(value, HOST, {"summary": "旧动作忽然完成了"}, NOW + 50))
        validate_shared_activity(value["shared_activity"], {HOST, GUEST}, outing=value["outing"], now=NOW + 50)

    def test_travel_is_separate_and_destination_actions_cannot_start_early(self):
        value = visit(minutes=0.5, travel=125.25)
        activity = value["outing"]
        self.assertEqual(activity["departed_at"], NOW)
        self.assertEqual(activity["started_at"], NOW + 125.25)
        self.assertEqual(activity["until"], NOW + 155.25)
        self.assertEqual(next_activity_due(value, NOW), NOW + 125.25)
        self.assertTrue(activity_context(value, NOW + 30)["traveling"])
        self.assertEqual(activity_context(value, NOW + 30)["origin_place"], "客厅")
        self.assertTrue(record_action(value, HOST, step("在终点摆杯子", 1), NOW + 30))
        scheduled = activity_context(value, NOW + 30)["actions"][0]
        self.assertEqual(scheduled["status"], "scheduled")
        self.assertEqual(scheduled["accepted_at"], NOW + 30)
        self.assertEqual(scheduled["started_at"], NOW + 125.25)
        self.assertEqual(scheduled["until"], NOW + 126.25)
        self.assertFalse(advance_activity(value, NOW + 125.25))
        self.assertFalse(apply_scene_update(value, HOST, {"summary": "提前摆好了杯子"}, NOW + 125.25))
        self.assertEqual(activity_context(value, NOW + 125.25)["actions"][0]["status"], "running")
        self.assertFalse(activity_context(value, NOW + 125.25)["traveling"])
        validate_shared_activity(value["shared_activity"], {HOST, GUEST}, outing=activity, now=NOW + 30)
        self.assertTrue(valid_activity(activity, expires_at=value["expires_at"], now=NOW))

    def test_long_travel_has_coherent_future_arrival_proof(self):
        value = visit(travel=1800)
        self.assertTrue(valid_activity(value["outing"], expires_at=value["expires_at"], now=NOW))
        for key, altered in (("departed_at", NOW + 301), ("travel_until", NOW + 1799), ("travel_seconds", -1)):
            activity = copy.deepcopy(value["outing"])
            activity[key] = altered
            activity["route"][-1][key] = altered
            self.assertFalse(valid_activity(activity, expires_at=value["expires_at"], now=NOW))

    def test_cooperation_selected_on_the_road_is_scheduled_for_arrival(self):
        value = visit(travel=125.25)
        record_action(value, HOST, step("到公园一起展开野餐布", 3, requires_peer=True), NOW)
        reference = value["shared_activity"]["action_steps"][HOST][0]["id"]
        record_action(value, GUEST, step("我愿意拉另一边", 5, requires_peer=True, peer_action_id=reference), NOW + 1)
        actions = activity_context(value, NOW + 1)["actions"]
        self.assertEqual([row["status"] for row in actions], ["scheduled", "scheduled"])
        self.assertEqual({row["started_at"] for row in actions}, {NOW + 125.25})
        self.assertFalse(apply_scene_update(value, HOST, {"summary": "野餐布已展开"}, NOW + 129))
        self.assertTrue(apply_scene_update(value, HOST, {"summary": "双方展开了野餐布",
            "action_ids": [row["id"] for row in actions]}, NOW + 130.25))
        validate_shared_activity(value["shared_activity"], {HOST, GUEST}, outing=value["outing"], now=NOW + 130.25)

    def test_agreed_return_cannot_be_silently_overrun_or_action_truncated(self):
        value = visit(minutes=0.5, return_after=True)
        self.assertFalse(record_action(value, HOST, step("做一件至少要一分钟的事", 60), NOW))
        self.assertTrue(record_action(value, HOST, step("准备共同动作", 20, requires_peer=True), NOW))
        reference = value["shared_activity"]["action_steps"][HOST][0]["id"]
        self.assertFalse(record_action(value, GUEST, step("我准备好了", 2, requires_peer=True, peer_action_id=reference), NOW + 15))
        self.assertEqual(value["shared_activity"]["action_steps"][HOST][0]["status"], "awaiting_peer")
        self.assertTrue(value["outing"]["return_agreed"])

    def test_milestones_are_bounded_and_reloaded_results_remain_consumed(self):
        value = visit()
        for number in range(12):
            at = NOW + number * 3
            record_action(value, HOST, step(f"第{number}步", 1), at)
            self.assertTrue(apply_scene_update(value, HOST, {"summary": f"公开结果{number}"}, at + 1))
        self.assertEqual(len(value["shared_activity"]["milestones"]), 8)
        restored = validate_shared_activity(value["shared_activity"], {HOST, GUEST}, outing=value["outing"], now=NOW + 40)
        value["shared_activity"] = restored
        self.assertFalse(apply_scene_update(value, HOST, {"summary": "重复结算"}, NOW + 40))
        self.assertEqual(restored["milestones"][-1]["summary"], "公开结果11")

    def test_unknown_or_malformed_saved_state_is_rejected_as_value_error(self):
        value = visit()
        record_action(value, HOST, step(), NOW)
        original = value["shared_activity"]
        for key, altered in (("waiting", None), ("status", {}), ("stage", True), ("until", float("nan")),
                             ("started_at", NOW - 1), ("id", "")):
            saved = copy.deepcopy(original)
            saved["action_steps"][HOST][0][key] = altered
            with self.assertRaises(ValueError):
                validate_shared_activity(saved, {HOST, GUEST}, outing=value["outing"], now=NOW)
        saved = copy.deepcopy(original)
        saved["action_steps"][HOST][0].pop("waiting")
        with self.assertRaises(ValueError):
            validate_shared_activity(saved, {HOST, GUEST}, outing=value["outing"], now=NOW)

    def test_elapsed_and_cooperative_proofs_cannot_be_forged(self):
        value = visit()
        record_action(value, HOST, step(), NOW)
        forged = copy.deepcopy(value["shared_activity"])
        row = forged["action_steps"][HOST][0]
        row.update(status="elapsed", elapsed_at=row["until"])
        with self.assertRaises(ValueError):
            validate_shared_activity(forged, {HOST, GUEST}, outing=value["outing"], now=NOW + 1)
        forged = copy.deepcopy(value["shared_activity"])
        forged["action_steps"][HOST][0].update(requires_peer=True, joint_id="unpaired")
        with self.assertRaises(ValueError):
            validate_shared_activity(forged, {HOST, GUEST}, outing=value["outing"], now=NOW)

    def test_old_journeys_need_no_shared_extension(self):
        value = visit()
        value.pop("shared_activity")
        self.assertEqual(validate_shared_activity(None, {HOST, GUEST}), {})
        self.assertEqual(activity_context(value, NOW), {})
        self.assertTrue(valid_activity(value["outing"], expires_at=value["expires_at"], now=NOW))
        self.assertTrue(record_action(value, HOST, step("自己决定一个新动作", 0.25), NOW))


if __name__ == "__main__":
    unittest.main()
