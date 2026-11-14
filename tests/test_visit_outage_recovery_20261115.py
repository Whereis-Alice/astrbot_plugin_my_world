"""Existing encounters survive short outages within their original clocks and quotas."""
from __future__ import annotations

import copy
from dataclasses import replace
import unittest

from tests import fake_astrbot
fake_astrbot.install()

from core.visits import VisitService  # noqa: E402
from core.visits.activities import record_action  # noqa: E402
from core.visits.flow import VisitFlow  # noqa: E402
from core.visits.model_retry import ACTIVE_DELAYS, defer_decision, valid_retry  # noqa: E402
from core.visits.outings import begin_activity  # noqa: E402
from tests import test_visits_model_retry as retry_fixture  # noqa: E402
from tests import test_visit_shared_activities_20261113 as action_fixture  # noqa: E402


class OutageRecoveryTests(unittest.IsolatedAsyncioTestCase):
    offered = retry_fixture.VisitModelRetryTests.offered
    host_turn = retry_fixture.VisitModelRetryTests.host_turn
    fail_then_resume = retry_fixture.VisitModelRetryTests.fail_then_resume

    async def asyncSetUp(self):
        await retry_fixture.VisitModelRetryTests.asyncSetUp(self)
        for service in (self.a, self.b):
            service.config = replace(service.config, outings_enabled=True)

    async def test_four_failed_requests_then_reload_recovers_the_same_turn_once(self):
        visit_id, visit = await self.host_turn()
        before = copy.deepcopy(visit["log"])
        expires = visit["expires_at"]
        calls = visit["calls"][visit["host_id"]]
        attempts = self.fail_then_resume(self.b_actor, "provider", failures=4, phase="turn")
        for expected_delay in ACTIVE_DELAYS[:4]:
            failed_at = self.now
            await self.b._advance(visit_id)
            self.assertEqual(visit["status"], "active")
            self.assertEqual(visit["log"], before)
            self.assertEqual(visit["expires_at"], expires)
            self.assertEqual(visit["model_retry"]["next_at"], failed_at + expected_delay)
            self.assertTrue(valid_retry(visit, self.b._decision_cursor(visit, "turn")))
            self.now = failed_at + expected_delay - 1
            await self.b._advance(visit_id)
            self.assertEqual(visit["log"], before)
            self.now += 1
        await self.b.close()
        resumed = VisitService(self.b_db, self.b.config, self.b_actor, clock=lambda: self.now, rng=lambda: .9)
        self.addAsyncCleanup(resumed.close)
        await resumed._load()
        await resumed._advance(visit_id)
        current = resumed._state["visits"][visit_id]
        self.assertEqual(len(attempts), 5)
        self.assertEqual(current["status"], "active")
        self.assertEqual(current["calls"][current["host_id"]], calls + 5)
        self.assertEqual(current["expires_at"], expires)
        self.assertEqual(current["log"][:len(before)], before)
        self.assertEqual(sum(row["text"] == "b：turn" for row in current["log"]), 1)
        await resumed._advance(visit_id)
        self.assertEqual(len(attempts), 5)

    async def test_persistent_timeout_has_a_finite_limit_without_new_facts(self):
        visit_id, visit = await self.host_turn()
        before = copy.deepcopy(visit["log"])
        attempts = self.fail_then_resume(self.b_actor, "timeout", failures=10, phase="turn")
        for delay in ACTIVE_DELAYS:
            failed_at = self.now
            await self.b._advance(visit_id)
            self.assertEqual(visit["status"], "active")
            self.assertEqual(visit["log"], before)
            self.now = failed_at + delay
        await self.b._advance(visit_id)
        self.assertEqual(visit["status"], "interrupted")
        self.assertEqual(len(attempts), 6)
        self.assertEqual(visit["log"], before)
        self.assertNotIn("model_retry", visit)

    async def test_invalid_choice_and_invitation_keep_the_short_limit(self):
        visit_id, visit = await self.host_turn()
        self.fail_then_resume(self.b_actor, "format", failures=10, phase="turn")
        for delay in (30, 60):
            await self.b._advance(visit_id)
            self.now += delay
        await self.b._advance(visit_id)
        self.assertEqual(visit["status"], "interrupted")
        await self.b._notify_end(visit_id)
        await self.b._observe_memory(visit_id)
        pending_id, offered = await self.offered()
        self.fail_then_resume(self.b_actor, "provider", failures=10, phase="receive")
        for delay in (30, 60):
            await self.b._advance(pending_id)
            self.now += delay
        await self.b._advance(pending_id)
        self.assertEqual(offered["status"], "interrupted")
        self.assertNotIn("arrived_at", offered)

    async def test_recovery_near_expiry_waits_only_until_the_existing_lease(self):
        _, visit = await self.host_turn()
        visit["expires_at"] = self.now + 15
        cursor = self.b._decision_cursor(visit, "turn")
        before = copy.deepcopy(visit["log"])
        self.assertTrue(defer_decision(visit, cursor, category="provider", now=self.now))
        self.assertEqual(visit["model_retry"]["next_at"], visit["expires_at"])
        self.assertEqual(visit["log"], before)
        self.assertFalse(defer_decision(visit, cursor, category="provider", now=visit["expires_at"]))

    async def test_actual_decision_capacity_failure_saves_exact_issue_and_can_correct(self):
        visit_id, visit = await self.host_turn()
        begin_activity(visit, {"place": "书房", "activity": "自主读书", "minutes": 30}, self.now - 20)
        for index in range(6):
            self.assertTrue(record_action(visit, visit["host_id"], action_fixture.step("自主动作", 1),
                                          self.now - 18 + index * 2))
        before = copy.deepcopy(visit["log"])
        traces = []
        self.b_actor.record_decision_issue = lambda *args: traces.append(args)

        async def invalid(scope, phase, current):
            return {"say": "尚未执行的原话", "action_step": action_fixture.step("下一步", 1)}

        self.b_actor.decide = invalid
        self.assertIsNone(await self.b._decide(visit_id, "turn"))
        self.assertEqual(visit["model_retry"]["format_issue"], "action_capacity")
        self.assertEqual(traces[0][1], "turn")
        self.assertEqual(traces[0][-1], "action_capacity")
        self.assertEqual(visit["log"], before)
        first = visit["shared_activity"]["action_steps"][visit["host_id"]][0]["id"]

        async def corrected(scope, phase, current):
            return {"action_control": {"action_id": first, "operation": "cancel"},
                    "action_step": action_fixture.step("下一步", 1)}

        self.b_actor.decide = corrected
        self.now = visit["model_retry"]["next_at"]
        self.assertIsNotNone(await self.b._decide(visit_id, "turn"))
        self.assertNotIn("model_retry", visit)
        self.assertEqual(visit["log"], before)


class ActionIssueTests(unittest.TestCase):
    def full(self):
        visit = action_fixture.visit()
        visit["role"] = "host"
        for index in range(6):
            self.assertTrue(record_action(visit, action_fixture.HOST,
                action_fixture.step("自主动作", 1), action_fixture.NOW + index * 2))
        return visit

    def test_full_unsettled_actions_get_exact_issue_without_altering_facts(self):
        visit = self.full()
        before = copy.deepcopy(visit)
        result = {"action_step": action_fixture.step("下一步", 1)}
        self.assertEqual(VisitFlow._shared_choice_issue(visit, result, action_fixture.NOW + 15), "action_capacity")
        self.assertFalse(VisitFlow._shared_choice_executable(visit, result, action_fixture.NOW + 15))
        self.assertEqual(visit, before)

    def test_cancelling_one_action_allows_a_new_choice_in_the_same_turn(self):
        visit = self.full()
        first = visit["shared_activity"]["action_steps"][action_fixture.HOST][0]["id"]
        result = {"action_control": {"action_id": first, "operation": "cancel"},
                  "action_step": action_fixture.step("下一步", 1)}
        self.assertEqual(VisitFlow._shared_choice_issue(visit, result, action_fixture.NOW + 15), "")


if __name__ == "__main__":
    unittest.main()
