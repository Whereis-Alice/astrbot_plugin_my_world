"""New visitor control facts need the exact submitted personal turn, not host prose."""
from __future__ import annotations

import copy
import unittest

from tests import fake_astrbot
fake_astrbot.install()

from core.visits.activities import apply_action_control, record_action  # noqa: E402
from core.visits.flow import VisitFlow  # noqa: E402
from core.visits.outings import begin_activity  # noqa: E402
from core.visits.protocol import clean_decision, clean_outing_offer, text  # noqa: E402
from core.visits.snapshots import validate_snapshot  # noqa: E402
from tests.test_visit_shared_activities_20261113 import GUEST, HOST, NOW, step, visit  # noqa: E402


class VisitorControlLogProofTests(unittest.TestCase):
    def make_visit(self, operation="cancel"):
        local = visit()
        local.update(id="control-proof", role="guest", arrived_at=NOW, created_at=NOW,
                     manual=True, max_calls=100, turn_limit=100, event_stage="none",
                     next_actor=GUEST, turns={HOST: 1, GUEST: 0}, log=[
                         {"seq": 1, "actor_id": HOST, "kind": "say", "text": "可以按自己的节奏来。", "at": NOW}])
        owner = GUEST if operation == "cancel" else HOST
        self.assertTrue(record_action(local, owner, step("慢慢读自己的书" if operation == "cancel" else "一起搬桌子",
                                                         120, requires_peer=operation == "decline"), NOW))
        target = local["shared_activity"]["action_steps"][owner][0]
        control = {"action_id": target["id"], "operation": operation, "reason": "现在想休息"}
        return local, control

    def submit(self, local, control, *, extra=None):
        local["decision"] = clean_decision({"say": "", "continue_activity": True,
                                            "action_control": control, **(extra or {})})
        local["decision_cursor"] = VisitFlow._decision_cursor(local, "turn")
        local["unsent_turn"] = True
        remote = copy.deepcopy(local)
        target = next(row for rows in remote["shared_activity"]["action_steps"].values()
                      for row in rows if row["id"] == control["action_id"])
        wording = "取消了自己的动作：" if control["operation"] == "cancel" else "婉拒了对方的协作提议："
        wording += target["description"]
        if control.get("reason"):
            wording += "；理由：" + control["reason"]
        result = local["decision"]
        for kind, value in (("say", result["say"]), ("action_plan", result["action"] or
                            (result.get("action_step") or {}).get("description", ""))):
            if value:
                remote["log"].append({"seq": len(remote["log"]) + 1, "actor_id": GUEST,
                                      "kind": kind, "text": value, "at": NOW + 1})
        self.assertTrue(apply_action_control(remote, GUEST, control, NOW + 1))
        remote["log"].append({"seq": len(remote["log"]) + 1, "actor_id": GUEST,
                              "kind": "action_control", "text": text(wording, 500), "at": NOW + 1.01,
                              "action_id": control["action_id"], "operation": control["operation"],
                              **({"reason": control["reason"]} if control.get("reason") else {})})
        remote["turns"][GUEST] += 1
        remote["next_actor"] = HOST
        if result.get("action_step"):
            self.assertTrue(record_action(remote, GUEST, result["action_step"], NOW + 1.02))
        return remote

    def test_correct_cancel_and_decline_are_confirmed(self):
        for operation in ("cancel", "decline"):
            with self.subTest(operation=operation):
                local, control = self.make_visit(operation)
                remote = self.submit(local, control)
                self.assertEqual(validate_snapshot(local, remote, now=NOW + 2)["log"], remote["log"])

    def test_host_cannot_add_guest_control_fact_without_a_submitted_choice(self):
        local, control = self.make_visit()
        remote = self.submit(local, control)
        for key in ("decision", "decision_cursor", "unsent_turn"):
            local.pop(key, None)
        remote["shared_activity"] = copy.deepcopy(local["shared_activity"])
        remote["turns"] = copy.deepcopy(local["turns"])
        with self.assertRaises(ValueError):
            validate_snapshot(local, remote, now=NOW + 2)

    def test_unsubmitted_or_different_control_cannot_authorize_a_new_log(self):
        for change in ("unsent", "action_id", "operation", "reason"):
            with self.subTest(change=change):
                local, control = self.make_visit()
                remote = self.submit(local, control)
                remote["shared_activity"] = copy.deepcopy(local["shared_activity"])
                if change == "unsent":
                    local["unsent_turn"] = False
                else:
                    local["decision"]["action_control"][change] = {
                        "action_id": "s0-a999", "operation": "decline", "reason": "别的理由"}[change]
                with self.assertRaises(ValueError):
                    validate_snapshot(local, remote, now=NOW + 2)

    def test_log_cannot_claim_control_when_retained_action_is_still_running(self):
        local, control = self.make_visit()
        remote = self.submit(local, control)
        remote["shared_activity"] = copy.deepcopy(local["shared_activity"])
        with self.assertRaises(ValueError):
            validate_snapshot(local, remote, now=NOW + 2)

    def test_exact_cursor_and_guest_turn_are_required(self):
        changes = {"phase": "scene", "status": "finishing", "log_seq": 0,
                   "turns": {HOST: 0, GUEST: 0}, "next_actor": HOST,
                   "outing_stage": 99, "quiet_until": NOW, "event_stage": "offered"}
        for field, value in changes.items():
            with self.subTest(field=field):
                local, control = self.make_visit()
                remote = self.submit(local, control)
                local["decision_cursor"][field] = value
                with self.assertRaises(ValueError):
                    validate_snapshot(local, remote, now=NOW + 2)
        for delta in (0, 2):
            with self.subTest(guest_turn_delta=delta):
                local, control = self.make_visit()
                remote = self.submit(local, control)
                remote["turns"][GUEST] = local["turns"][GUEST] + delta
                with self.assertRaises(ValueError):
                    validate_snapshot(local, remote, now=NOW + 2)

    def test_control_metadata_and_public_wording_must_match_personal_choice(self):
        for field, value in (("action_id", "s0-a999"), ("operation", "decline"),
                             ("reason", "编造的理由"), ("text", "她已经完成了整本书"),
                             ("at", NOW - 1)):
            with self.subTest(field=field):
                local, control = self.make_visit()
                remote = self.submit(local, control)
                remote["log"][-1][field] = value
                with self.assertRaises(ValueError):
                    validate_snapshot(local, remote, now=NOW + 2)

    def test_control_log_must_follow_the_exact_spoken_and_action_plan_prefix(self):
        local, control = self.make_visit()
        remote = self.submit(local, control, extra={"say": "我先放下书。", "action_step": step("取自己的杯子", 1)})
        self.assertEqual(validate_snapshot(local, remote, now=NOW + 2)["log"], remote["log"])
        for index in (1, 2):
            with self.subTest(index=index):
                forged = copy.deepcopy(remote)
                forged["log"][index]["text"] = "凭空编造的原话或动作"
                with self.assertRaises(ValueError):
                    validate_snapshot(local, forged, now=NOW + 2)
        forged = copy.deepcopy(remote)
        forged["log"].insert(1, {"seq": 2, "actor_id": HOST, "kind": "say", "text": "插入了其他回合", "at": NOW + 1})
        for index, row in enumerate(forged["log"], 1):
            row["seq"] = index
        with self.assertRaises(ValueError):
            validate_snapshot(local, forged, now=NOW + 2)

    def test_one_submitted_control_cannot_produce_duplicate_public_facts(self):
        local, control = self.make_visit()
        remote = self.submit(local, control)
        remote["log"].append({**remote["log"][-1], "seq": len(remote["log"]) + 1})
        with self.assertRaises(ValueError):
            validate_snapshot(local, remote, now=NOW + 2)

    def test_old_confirmed_control_prefix_survives_cleared_decision(self):
        local, control = self.make_visit()
        remote = self.submit(local, control)
        confirmed = validate_snapshot(local, remote, now=NOW + 2)
        for key in ("decision", "decision_cursor", "unsent_turn"):
            confirmed.pop(key, None)
        self.assertEqual(validate_snapshot(confirmed, copy.deepcopy(confirmed), now=NOW + 3)["log"], confirmed["log"])

    def test_control_remains_valid_after_same_turn_replacement_prunes_old_action(self):
        local, control = self.make_visit()
        remote = self.submit(local, control, extra={"action_step": step("取自己的杯子", 1)})
        self.assertNotIn(control["action_id"], [row["id"] for row in remote["shared_activity"]["action_steps"][GUEST]])
        self.assertEqual(validate_snapshot(local, remote, now=NOW + 2)["log"], remote["log"])

    def test_control_remains_valid_when_same_turn_accepts_a_new_stop(self):
        local, control = self.make_visit()
        local["outing_offer"] = clean_outing_offer({"place": "湖边", "activity": "看湖面", "minutes": 20,
                                                   "travel_seconds": 90, "pause_minutes": 5})
        remote = self.submit(local, control, extra={"outing_accept": True, "pause_minutes": 5})
        begin_activity(remote, local["outing_offer"], NOW + 1.02, local["decision"])
        self.assertFalse(remote["shared_activity"]["action_steps"])
        self.assertEqual(validate_snapshot(local, remote, now=NOW + 2)["log"], remote["log"])

    def test_empty_reason_and_leave_can_still_publish_the_real_control(self):
        local, control = self.make_visit()
        control["reason"] = ""
        remote = self.submit(local, control, extra={"leave": True})
        remote.update(status="completed", ended_at=NOW + 1.02)
        self.assertEqual(validate_snapshot(local, remote, now=NOW + 2)["log"], remote["log"])


if __name__ == "__main__":
    unittest.main()
