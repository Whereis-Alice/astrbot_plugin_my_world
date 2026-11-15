"""Failed visit choices remain private and bounded without cancelling an encounter."""
from __future__ import annotations

import copy
from dataclasses import replace
import unittest

from tests import fake_astrbot
fake_astrbot.install()

from core.visits.decisions import VisitDecisionError  # noqa: E402
from core.visits.model_retry import valid_retry  # noqa: E402
from core.visits.activities import record_action  # noqa: E402
from core.visits.outings import begin_activity  # noqa: E402
from tests import test_visits_model_retry as fixture  # noqa: E402
from tests import test_visit_shared_activities_20261113 as activity_fixture  # noqa: E402


class FormatPauseTests(unittest.IsolatedAsyncioTestCase):
    host_turn = fixture.VisitModelRetryTests.host_turn
    offered = fixture.VisitModelRetryTests.offered
    reload_service = fixture.VisitModelRetryTests.reload_service

    async def asyncSetUp(self):
        await fixture.VisitModelRetryTests.asyncSetUp(self)
        for service in (self.a, self.b):
            service.config = replace(service.config, outings_enabled=True)

    async def pause(self, service, actor, visit_id, *, phase="turn"):
        attempts = []

        async def invalid(scope, current_phase, current):
            attempts.append(current_phase)
            raise VisitDecisionError("format", format_issue="json_object",
                                     repair_text='{"say":"未执行的私有草稿')

        actor.decide = invalid
        for delay in (30, 60):
            self.assertIsNone(await service._decide(visit_id, phase))
            self.now += delay
        self.assertIsNone(await service._decide(visit_id, phase))
        current = service._state["visits"][visit_id]
        self.assertEqual(current["status"], "active")
        self.assertTrue(current["model_retry"]["paused"])
        self.assertTrue(valid_retry(current, service._decision_cursor(current, phase)))
        self.assertEqual(len(attempts), 3)
        return attempts

    async def test_provider_failures_do_not_use_up_the_first_format_correction(self):
        visit_id, visit = await self.host_turn()
        before = copy.deepcopy(visit["log"])
        calls = visit["calls"][visit["host_id"]]
        categories = iter(("provider", "provider", "provider", "format", "format"))
        observed = []

        async def decide(scope, phase, current):
            observed.append(copy.deepcopy(current.get("model_retry")))
            category = next(categories, None)
            if category:
                raise VisitDecisionError(category, format_issue="json_object", repair_text="私有待修复结构")
            return {"say": "这句是纠错后才确认的。"}

        self.b_actor.decide = decide
        for index, delay in enumerate((30, 60, 300, 30, 60), 1):
            self.assertIsNone(await self.b._decide(visit_id, "turn"))
            self.assertEqual(visit["model_retry"]["next_at"], self.now + delay)
            self.assertEqual(visit["model_retry"]["failures"], index)
            if index >= 4:
                self.assertEqual(visit["model_retry"]["format_attempts"], index - 3)
            self.assertTrue(valid_retry(visit, self.b._decision_cursor(visit, "turn")))
            self.now += delay
        result = await self.b._decide(visit_id, "turn")
        self.assertEqual(result["say"], "这句是纠错后才确认的。")
        self.assertEqual(visit["calls"][visit["host_id"]], calls + 6)
        self.assertEqual(visit["log"], before)
        self.assertNotIn("model_retry", visit)
        self.assertEqual(observed[-1]["format_attempts"], 2)

    async def test_invalid_action_is_only_a_private_repair_choice(self):
        visit_id, visit = await self.host_turn()
        visit.pop("outing", None)
        before = copy.deepcopy(visit["log"])
        before_decision = copy.deepcopy(visit.get("decision"))

        async def invalid(scope, phase, current):
            return {"say": "还没转给朋友的那句话", "thought": "未执行时的想法",
                    "action_step": activity_fixture.step("未经确认活动的动作", 1)}

        self.b_actor.decide = invalid
        self.assertIsNone(await self.b._decide(visit_id, "turn"))
        retry = visit["model_retry"]
        self.assertEqual(retry["format_issue"], "action_references")
        self.assertEqual(retry["repair_choice"]["say"], "还没转给朋友的那句话")
        self.assertNotIn("candidate", retry)
        self.assertEqual(visit.get("decision"), before_decision)
        self.assertEqual(visit["log"], before)
        self.assertNotIn("还没转给朋友", str(self.b._wire(visit)))
        self.assertNotIn("未经确认活动的动作", str(self.b._wire(visit)))

    async def test_provider_outage_during_correction_keeps_the_private_draft(self):
        visit_id, visit = await self.host_turn()
        categories = iter(("format", "provider"))
        observed = []

        async def decide(scope, phase, current):
            observed.append(copy.deepcopy(current.get("model_retry")))
            category = next(categories, None)
            if category:
                raise VisitDecisionError(category, format_issue="json_object", repair_text="仅本地修复用的原稿")
            return {"say": "模型通道恢复后的合法原话。"}

        self.b_actor.decide = decide
        for delay in (30, 30):
            self.assertIsNone(await self.b._decide(visit_id, "turn"))
            self.now += delay
        self.assertEqual(visit["model_retry"]["format_attempts"], 1)
        self.assertEqual(visit["model_retry"]["repair_text"], "仅本地修复用的原稿")
        self.assertNotIn("仅本地修复用的原稿", str(self.b._wire(visit)))
        self.assertTrue(await self.b._decide(visit_id, "turn"))
        self.assertEqual(observed[-1]["repair_text"], "仅本地修复用的原稿")
        self.assertNotIn("model_retry", visit)

    async def test_pause_survives_reload_and_heartbeat_or_schedule_changes(self):
        visit_id, visit = await self.host_turn()
        before = copy.deepcopy(visit["log"])
        attempts = await self.pause(self.b, self.b_actor, visit_id)
        calls = copy.deepcopy(visit["calls"])
        resumed = await self.reload_service(self.b)
        current = resumed._state["visits"][visit_id]
        for index in range(5):
            self.now += 10
            current["next_at"] = self.now
            current["observed_at"] = self.now
            self.assertIsNone(await resumed._decide(visit_id, "turn"))
        self.assertEqual(len(attempts), 3)
        self.assertEqual(current["calls"], calls)
        self.assertEqual(current["log"], before)
        self.assertTrue(current["model_retry"]["paused"])
        self.assertFalse(self.b_actor.memories)

    async def test_same_guest_snapshot_does_not_reopen_paused_choice(self):
        visit_id, host = await self.host_turn()
        await self.b._advance(visit_id)
        guest = self.a._state["visits"][visit_id]
        self.a._synchronize(guest, self.b._wire(host), host["host_id"])
        self.assertEqual(guest["next_actor"], guest["guest_id"])
        attempts = await self.pause(self.a, self.a_actor, visit_id)
        before = copy.deepcopy(guest["log"])
        for index in range(3):
            self.now += 5
            remote = self.b._wire(host)
            remote["next_at"] = self.now
            self.a._synchronize(guest, remote, host["host_id"])
            self.assertIsNone(await self.a._decide(visit_id, "turn"))
        self.assertEqual(len(attempts), 3)
        self.assertEqual(guest["log"], before)
        self.assertTrue(guest["model_retry"]["paused"])

    async def test_scene_pause_keeps_event_unresolved_without_more_requests(self):
        visit_id, visit = await self.host_turn()
        visit["scene_stage"] = "event_offer"
        before = copy.deepcopy(visit["log"])
        attempts = await self.pause(self.b, self.b_actor, visit_id, phase="scene")
        for index in range(3):
            self.now += 10
            await self.b._scene(visit_id, "event_offer")
        self.assertEqual(len(attempts), 3)
        self.assertEqual(visit["log"], before)
        self.assertEqual(visit["event_stage"], "none")
        self.assertTrue(visit["model_retry"]["paused"])

    async def test_new_local_experience_can_resume_without_sharing_its_contents(self):
        visit_id, visit = await self.host_turn()
        before = copy.deepcopy(visit["log"])
        await self.pause(self.b, self.b_actor, visit_id)
        calls = visit["calls"][visit["host_id"]]
        self.assertTrue(await self.b.wake_local_experience(visit_id, visit["scope"],
            visit["local"]["actor_id"], visit["local"]["persona_key"], self.now, seq=1))
        self.assertNotIn("local_experience_seq", self.b._wire(visit))

        async def corrected(scope, phase, current):
            return {"say": "现在有了新的具体话题。"}

        self.b_actor.decide = corrected
        result = await self.b._decide(visit_id, "turn")
        self.assertEqual(result["say"], "现在有了新的具体话题。")
        self.assertEqual(visit["calls"][visit["host_id"]], calls + 1)
        self.assertEqual(visit["log"], before)
        self.assertNotIn("model_retry", visit)

    async def test_non_significant_reading_does_not_release_the_pause(self):
        visit_id, visit = await self.host_turn()
        attempts = await self.pause(self.b, self.b_actor, visit_id)
        self.assertTrue(await self.b.wake_local_experience(visit_id, visit["scope"],
            visit["local"]["actor_id"], visit["local"]["persona_key"], self.now, seq=1, significant=False))
        self.assertIsNone(await self.b._decide(visit_id, "turn"))
        self.assertTrue(visit["model_retry"]["paused"])
        self.assertEqual(len(attempts), 3)

    async def test_actual_action_clock_progress_reopens_pause_without_assuming_success(self):
        visit_id, visit = await self.host_turn()
        started = self.now
        begin_activity(visit, {"place": "客厅", "activity": "自主观察", "minutes": 30}, started)
        self.assertTrue(record_action(visit, visit["host_id"], activity_fixture.step("观察一个细节", 120), started))
        await self.pause(self.b, self.b_actor, visit_id)
        before = copy.deepcopy(visit["log"])
        self.assertFalse(self.b._advance_shared_clock(visit))
        self.assertFalse(self.b._resume_paused_decision(visit))
        self.now = started + 121
        self.assertTrue(self.b._advance_shared_clock(visit))

        async def corrected(scope, phase, current):
            return {"say": "刚才那一步的时间到了，结果还要看看。"}

        self.b_actor.decide = corrected
        result = await self.b._decide(visit_id, "turn")
        self.assertTrue(result)
        self.assertEqual(visit["log"], before)
        self.assertEqual(visit["shared_activity"]["summary"], "")
        self.assertFalse(visit["shared_activity"]["done"])
        self.assertEqual(visit["shared_activity"]["action_steps"][visit["host_id"]][0]["status"], "elapsed")

    async def test_agreed_return_and_lease_end_are_not_delayed_by_pause(self):
        visit_id, visit = await self.host_turn()
        begin_activity(visit, {"place": "客厅", "activity": "一起坐一会", "minutes": 2,
                              "return_after": True}, self.now, {"return_after": True})
        visit["outing"]["farewells"] = {visit["host_id"]: "已经约好的告别原话", visit["guest_id"]: "对方已经确认的告别"}
        deadline = visit["outing"]["until"]
        attempts = await self.pause(self.b, self.b_actor, visit_id)
        self.now = deadline
        self.assertIsNone(await self.b._decide(visit_id, "turn"))
        self.assertEqual(visit["status"], "completed")
        self.assertEqual(visit["ended_at"], deadline)
        self.assertEqual(len(attempts), 3)
        self.assertNotIn("model_retry", visit)
        self.assertNotIn("未执行的私有草稿", str(visit["log"]))
        await self.b._observe_memory(visit_id)
        self.assertNotIn("未执行的私有草稿", str(self.b_actor.memories))

    async def test_pause_at_lease_end_finishes_without_new_generation(self):
        visit_id, visit = await self.host_turn()
        attempts = await self.pause(self.b, self.b_actor, visit_id)
        self.now = visit["expires_at"]
        self.assertIsNone(await self.b._decide(visit_id, "turn"))
        self.assertEqual(visit["status"], "completed")
        self.assertEqual(len(attempts), 3)
        self.assertNotIn("model_retry", visit)


if __name__ == "__main__":
    unittest.main()
