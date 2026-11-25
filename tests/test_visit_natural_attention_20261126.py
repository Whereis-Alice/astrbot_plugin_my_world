"""Private attention and agreed quiet keep both characters free across real clocks."""
from __future__ import annotations

import copy
import json
import unittest

from tests import fake_astrbot
fake_astrbot.install()

from core.visits.outings import pause_activity, valid_activity  # noqa: E402
from core.visits.snapshots import validate_snapshot  # noqa: E402
from tests import test_visit_shared_flow_20261113 as fixture  # noqa: E402
from tests import test_visits_flow_review_r as restart_fixture  # noqa: E402


class NaturalAttentionTests(unittest.IsolatedAsyncioTestCase):
    make_world = fixture.SharedVisitFlowTests.make_world
    exhaust = fixture.SharedVisitFlowTests.exhaust
    asyncSetUp = fixture.SharedVisitFlowTests.asyncSetUp
    advance = fixture.SharedVisitFlowTests.advance
    start_shared = fixture.SharedVisitFlowTests.start_shared
    restart = restart_fixture.OutingFlowReviewTests.restart

    async def private_choice(self, visit_id, host, *, pause=2, seq=1):
        self.b.llm.default_reply = json.dumps({"thought": "安静地享受眼前的陪伴",
            "continue_activity": True, "pause_minutes": pause}, ensure_ascii=False)
        await self.b.service.wake_local_experience(visit_id, self.scope, host["local"]["actor_id"],
            host["local"]["persona_key"], self.b.clock.now(), seq=seq)
        await self.b.service._advance(visit_id)

    async def continue_past_estimate(self, visit_id, host, *, host_pause=7, guest_pause=8):
        self.advance(host["outing"]["until"] - self.b.clock.now() + 1)
        self.b.llm.default_reply = json.dumps({"say": "想在这里再安静坐一会儿。",
            "continue_activity": True, "pause_minutes": host_pause}, ensure_ascii=False)
        self.a.llm.default_reply = json.dumps({"say": "嗯，我也愿意再坐一会儿。",
            "continue_activity": True, "pause_minutes": guest_pause}, ensure_ascii=False)
        await self.b.service._advance(visit_id)
        self.advance(self.cfg.poll_seconds + 1)
        await self.a.service._advance(visit_id)

    async def test_private_choice_keeps_own_attention_without_peer_line_and_survives_reload(self):
        visit_id, host, _ = await self.start_shared()
        before_log, expiry = copy.deepcopy(host["log"]), host["expires_at"]
        guest_calls = len(self.a.llm.calls)
        await self.private_choice(visit_id, host)
        self.assertEqual(host["next_actor"], host["host_id"])
        self.assertEqual(host["log"], before_log)
        self.assertEqual(host["expires_at"], expiry)
        self.assertEqual(host["next_at"], self.b.clock.now() + 120)
        wire = self.b.service._wire(host)
        self.assertNotIn("attention_wait", wire)
        self.assertNotIn("安静地享受眼前的陪伴", json.dumps(wire, ensure_ascii=False))
        wait = copy.deepcopy(host["attention_wait"])
        await self.restart(self.b)
        host = self.b.service._state["visits"][visit_id]
        self.assertEqual(host["attention_wait"], wait)
        calls = len(self.b.llm.calls)
        self.advance(60)
        await self.b.service.tick()
        await self.a.service._advance(visit_id)
        self.assertEqual((len(self.b.llm.calls), len(self.a.llm.calls)), (calls, guest_calls))
        self.advance(61)
        await self.b.service.tick()
        self.assertEqual(len(self.b.llm.calls), calls + 1)
        self.assertEqual(len(self.a.llm.calls), guest_calls)

    async def test_new_action_clock_wakes_private_attention_without_settling_old_elapsed(self):
        visit_id, host, guest = await self.start_shared(host_action=fixture.action("倒自己的茶", 1),
            guest_action=fixture.action("摆自己的杯子", 3))
        self.advance(1.01)
        await self.private_choice(visit_id, host)
        steps = host["shared_activity"]["action_steps"]
        first = steps[host["host_id"]][0]
        self.assertEqual(first["status"], "elapsed")
        self.assertNotIn("settled_revision", first)
        calls = (len(self.b.llm.calls), len(self.a.llm.calls))
        self.advance(0.5)
        await self.b.service._advance(visit_id)
        self.assertEqual(len(self.b.llm.calls), calls[0])
        self.advance(1.51)
        await self.b.service.tick()
        self.assertEqual(len(self.b.llm.calls), calls[0] + 1)
        self.assertEqual(len(self.a.llm.calls), calls[1])
        self.assertFalse(host["shared_activity"]["milestones"])
        self.assertFalse(host["shared_activity"]["done"])
        self.assertEqual(host["next_actor"], host["host_id"])

    async def test_new_important_group_interaction_wakes_own_private_wait(self):
        visit_id, host, _ = await self.start_shared()
        await self.private_choice(visit_id, host, pause=10)
        calls = len(self.b.llm.calls)
        self.advance(1)
        await self.private_choice(visit_id, host, seq=2)
        self.assertEqual(len(self.b.llm.calls), calls + 1)
        self.assertEqual(host["attention_wait"]["basis"]["local_attention_seq"], 2)
        self.assertFalse(host.get("local_experience_pending"))

    async def test_peer_group_attention_still_hands_off_for_peer_independent_choice(self):
        visit_id, host, guest = await self.start_shared()
        await self.private_choice(visit_id, host, pause=10)
        await self.a.service.wake_local_experience(visit_id, self.scope, guest["local"]["actor_id"],
            guest["local"]["persona_key"], self.a.clock.now(), seq=1)
        await self.a.service._advance(visit_id)
        await self.b.service._advance(visit_id)
        self.assertEqual(host["next_actor"], host["guest_id"])
        self.assertNotIn("attention_wait", host)
        self.a.llm.default_reply = json.dumps({"say": "群里刚提到了我想试的东西。",
            "continue_activity": True, "pause_minutes": 3}, ensure_ascii=False)
        calls = len(self.a.llm.calls)
        self.advance(self.cfg.poll_seconds + 1)
        await self.a.service._advance(visit_id)
        self.assertEqual(len(self.a.llm.calls), calls + 1)
        self.assertFalse(host.get("remote_experience_pending"))
        self.assertTrue(any(row["text"] == "群里刚提到了我想试的东西。" for row in host["log"]))

    async def test_both_signed_choices_keep_quiet_after_soft_estimate_without_changing_route(self):
        visit_id, host, guest = await self.start_shared()
        route = copy.deepcopy(host["outing"]["route"])
        await self.continue_past_estimate(visit_id, host)
        self.assertEqual(host["outing"]["route"], route)
        self.assertEqual(host["outing"], guest["outing"])
        self.assertEqual(host["outing"]["quiet_clock_version"], 2)
        self.assertEqual(host["outing"]["quiet_until"], self.b.clock.now() + 8 * 60)
        self.assertEqual(host["outing"]["pause_choices"], {host["host_id"]: 7, host["guest_id"]: 8})
        before = (len(self.a.llm.calls), len(self.b.llm.calls), len(self.operations))
        self.advance(120)
        await self.a.service.tick()
        await self.b.service.tick()
        self.assertEqual((len(self.a.llm.calls), len(self.b.llm.calls), len(self.operations)), before)
        await self.restart(self.a)
        self.assertEqual(self.a.service._state["visits"][visit_id]["outing"], host["outing"])

    async def test_v2_quiet_obeys_finite_contact_and_joint_return_keeps_original_deadline(self):
        visit_id, host, guest = await self.start_shared()
        await self.continue_past_estimate(visit_id, host, host_pause=240, guest_pause=240)
        self.assertEqual(host["outing"]["quiet_until"], host["expires_at"])
        turns = copy.deepcopy(host["turns"])
        expiry = host["expires_at"]
        self.advance(host["expires_at"] - self.b.clock.now() + 1)
        await self.b.service.tick()
        self.assertEqual(host["status"], "completed")
        self.assertEqual(host["ended_at"], expiry)
        self.assertEqual(host["turns"], turns)
        # A hard return remains a different, original agreement.
        trial = copy.deepcopy(host)
        trial["outing"]["return_agreed"] = True
        trial["quiet_clock_supported"] = True
        pause_activity(trial, 240, 240, trial["outing"]["until"] - 1)
        self.assertEqual(trial["outing"]["quiet_until"], trial["outing"]["until"])
        self.assertNotIn("quiet_clock_version", trial["outing"])

    async def test_later_real_progress_renews_contact_without_rewriting_confirmed_quiet(self):
        visit_id, host, guest = await self.start_shared()
        await self.continue_past_estimate(visit_id, host, host_pause=240, guest_pause=240)
        quiet = copy.deepcopy(host["outing"])
        expiry = host["expires_at"]
        self.advance(expiry - self.b.clock.now() - 120)
        self.b.llm.default_reply = json.dumps({"say": "听见群里提到那件事，我想起来了。",
            "continue_activity": True, "pause_minutes": 7}, ensure_ascii=False)
        await self.b.service.wake_local_experience(visit_id, self.scope, host["local"]["actor_id"],
            host["local"]["persona_key"], self.b.clock.now(), seq=1)
        await self.b.service._advance(visit_id)
        self.assertGreater(host["expires_at"], expiry)
        self.assertEqual(host["outing"], quiet)
        self.assertEqual(guest["outing"], quiet)
        self.assertEqual(guest["expires_at"], host["expires_at"])
        validate_snapshot(guest, self.b.service._wire(host), now=self.a.clock.now())

    async def test_old_peer_clock_and_unnegotiated_or_forged_v2_proofs_are_rejected(self):
        visit_id, host, guest = await self.start_shared()
        legacy = copy.deepcopy(host)
        legacy["quiet_clock_supported"] = False
        pause_activity(legacy, 7, 8, legacy["outing"]["until"] + 1)
        self.assertEqual(legacy["outing"]["quiet_until"], legacy["outing"]["until"])
        self.assertNotIn("quiet_clock_version", legacy["outing"])
        await self.continue_past_estimate(visit_id, host)
        wire = self.b.service._wire(host)
        for changed in ("capability", "pause", "clock", "contact", "ready"):
            forged = copy.deepcopy(wire)
            if changed == "capability":
                forged.pop("quiet_clock_version")
            elif changed == "pause":
                forged["outing"]["pause_choices"][host["guest_id"]] = 9
            elif changed == "clock":
                forged["quiet_clock_version"] = True
            elif changed == "contact":
                forged["outing"]["quiet_contact_until"] += 1
            else:
                forged["outing"]["quiet_action_ids"] = ["1" * 32]
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                validate_snapshot(guest, forged, now=self.a.clock.now())
        self.assertFalse(valid_activity(host["outing"], expires_at=host["expires_at"], now=self.b.clock.now()))
        self.assertTrue(valid_activity(host["outing"], expires_at=host["expires_at"],
            now=self.b.clock.now(), quiet_clock_supported=True))


if __name__ == "__main__":
    unittest.main()
