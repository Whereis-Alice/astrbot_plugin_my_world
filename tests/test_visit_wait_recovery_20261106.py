"""A busy scheduler keeps one call alive without spending inference recovery."""
from __future__ import annotations

import copy
from types import SimpleNamespace
import unittest

from tests import fake_astrbot
fake_astrbot.install()

from core.visits.decisions import VisitDecisionError  # noqa: E402
from core.visits.model_retry import valid_retry  # noqa: E402
from tests import test_visits_model_retry as fixture  # noqa: E402


class VisitWaitRecoveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.VisitModelRetryTests.asyncSetUp
    offered = fixture.VisitModelRetryTests.offered
    host_turn = fixture.VisitModelRetryTests.host_turn
    reload_service = fixture.VisitModelRetryTests.reload_service

    async def test_repeated_unissued_backoff_reuses_proposal_and_daily_opportunity(self):
        original = self.a_actor.decide
        attempts = 0

        async def decide(scope, phase, visit):
            nonlocal attempts
            attempts += 1
            if attempts <= 6:
                raise VisitDecisionError("backoff", request_started=False)
            return await original(scope, phase, visit)

        self.a_actor.decide = decide
        pending = await self.a.invite("life-a", self.peer)
        visit = self.a._state["visits"][pending["id"]]
        expires = visit["expires_at"]
        for delay in (30, 60, 120, 300, 300, 300):
            self.assertEqual(visit["status"], "inviting")
            self.assertEqual(visit["model_retry"]["next_at"], self.now + delay)
            self.assertTrue(valid_retry(visit, self.a._decision_cursor(visit, "propose")))
            self.assertEqual(sum(visit["calls"].values()), 0)
            self.assertFalse(visit["log"])
            self.assertFalse(self.b._state["visits"])
            self.assertEqual(visit["expires_at"], expires)
            self.now = visit["model_retry"]["next_at"]
            await self.a._advance(visit["id"])
        self.assertEqual(len(self.a._state["visits"]), 1)
        self.assertEqual(sum(visit["calls"].values()), 1)
        self.assertEqual(len(self.a_actor.calls), 1)
        self.assertEqual(sum(row["kind"] == "invitation" for row in visit["log"]), 1)
        self.assertEqual(len(self.b._state["visits"]), 1)
        self.assertNotIn("model_retry", visit)

    async def test_unissued_wait_cannot_reset_real_provider_failure_recovery(self):
        events = iter((("provider", True), ("busy", False), ("provider", True),
                       ("backoff", False), ("provider", True)))

        async def decide(*args):
            category, started = next(events)
            raise VisitDecisionError(category, request_started=started)

        self.a_actor.decide = decide
        pending = await self.a.invite("life-a", self.peer)
        visit = self.a._state["visits"][pending["id"]]
        for _ in range(4):
            self.assertEqual(visit["status"], "inviting")
            self.now = visit["model_retry"]["next_at"]
            await self.a._advance(visit["id"])
        self.assertEqual(visit["status"], "interrupted")
        self.assertEqual(sum(visit["calls"].values()), 3)
        self.assertFalse(visit["log"])
        self.assertFalse(self.b._state["visits"])

    async def test_transient_receive_wait_keeps_the_original_invitation_until_ready(self):
        visit_id, visit = await self.offered()
        original = self.b_actor.decide
        attempts = 0
        calls = copy.deepcopy(visit["calls"])
        log = copy.deepcopy(visit["log"])

        async def decide(scope, phase, snapshot):
            nonlocal attempts
            attempts += 1
            if attempts <= 4:
                raise VisitDecisionError("life_busy", request_started=False)
            return await original(scope, phase, snapshot)

        self.b_actor.decide = decide
        for delay in (30, 60, 120, 300):
            await self.b._advance(visit_id)
            self.assertEqual(visit["status"], "offered")
            self.assertEqual(visit["model_retry"]["next_at"], self.now + delay)
            self.assertEqual(visit["calls"], calls)
            self.assertEqual(visit["log"], log)
            self.assertNotIn("arrived_at", visit)
            self.now += delay
        await self.b._advance(visit_id)
        self.assertEqual(visit["status"], "active")
        self.assertEqual(len(self.b_actor.calls), 1)
        self.assertEqual(visit["calls"][visit["host_id"]], calls[visit["host_id"]] + 1)
        self.assertEqual(sum(row["text"] == "b：receive" for row in visit["log"]), 1)

    async def test_paid_receive_choice_waits_for_transient_readiness_then_applies_once(self):
        visit_id, visit = await self.offered()
        result = await self.b._decide(visit_id, "receive")
        calls = copy.deepcopy(visit["calls"])
        log = copy.deepcopy(visit["log"])
        describe = self.b_actor.describe
        ready = False

        async def describing(scope):
            return {**await describe(scope), "ready": ready, "readiness_wait": not ready}

        self.b_actor.describe = describing
        for delay in (30, 60, 120, 300):
            await self.b._advance(visit_id)
            self.assertEqual(visit["status"], "offered")
            self.assertEqual(visit["model_retry"]["next_at"], self.now + delay)
            self.assertEqual(visit["model_retry"]["candidate"], result)
            self.assertEqual(visit["calls"], calls)
            self.assertEqual(visit["log"], log)
            self.assertNotIn("arrived_at", visit)
            self.now += delay
        ready = True
        await self.b._advance(visit_id)
        self.assertEqual(visit["status"], "active")
        self.assertEqual(visit["calls"], calls)
        self.assertEqual(len(self.b_actor.calls), 1)
        self.assertEqual(sum(row["text"] == result["say"] for row in visit["log"]), 1)

    async def test_extended_wait_survives_reload_without_a_new_request(self):
        attempts = fixture.VisitModelRetryTests.fail_then_resume(
            self, self.a_actor, "backoff", failures=4, phase="propose", request_started=False)
        pending = await self.a.invite("life-a", self.peer)
        visit = self.a._state["visits"][pending["id"]]
        for _ in range(2):
            self.now = visit["model_retry"]["next_at"]
            await self.a._advance(visit["id"])
        self.assertEqual(visit["model_retry"]["attempts"], 3)
        resumed = await self.reload_service(self.a)
        current = resumed._state["visits"][visit["id"]]
        self.assertEqual(current["model_retry"], visit["model_retry"])
        self.assertEqual(current["status"], "inviting")
        for _ in range(2):
            self.now = current["model_retry"]["next_at"]
            await resumed._advance(current["id"])
        self.assertEqual(len(attempts), 5)
        self.assertEqual(sum(current["calls"].values()), 1)
        self.assertEqual(len(self.b._state["visits"]), 1)

    async def test_wait_ends_at_existing_expiry_without_renewing_the_visit(self):
        async def unavailable(*args):
            raise VisitDecisionError("busy", request_started=False)

        self.a_actor.decide = unavailable
        pending = await self.a.invite("life-a", self.peer)
        visit = self.a._state["visits"][pending["id"]]
        expires = visit["expires_at"]
        self.now = expires
        await self.a.tick()
        self.assertEqual(visit["status"], "expired")
        self.assertEqual(visit["expires_at"], expires)
        self.assertEqual(sum(visit["calls"].values()), 0)
        self.assertNotIn("model_retry", visit)
        self.assertFalse(self.b._state["visits"])

    async def test_long_cooldown_does_not_end_early_when_lease_is_nearly_due(self):
        async def unavailable(*args):
            raise VisitDecisionError("busy", request_started=False)

        self.a_actor.decide = unavailable
        pending = await self.a.invite("life-a", self.peer)
        visit = self.a._state["visits"][pending["id"]]
        expires = visit["expires_at"]
        self.now = expires - 10
        await self.a._advance(visit["id"])
        self.assertEqual(visit["status"], "inviting")
        self.assertEqual(visit["model_retry"]["next_at"], expires)
        self.assertNotIn("ended_at", visit)
        self.assertEqual(sum(visit["calls"].values()), 0)

    async def test_ready_wait_marker_does_not_replace_the_models_refusal(self):
        visit_id, visit = await self.offered()
        self.b_actor.accept = False
        result = await self.b._decide(visit_id, "receive")
        describe = self.b_actor.describe

        async def describing(scope):
            return {**await describe(scope), "ready": False, "readiness_wait": True}

        self.b.actor_port = SimpleNamespace(describe=describing, decide=self.b_actor.decide,
                                           remember=self.b_actor.remember)
        await self.b._advance(visit_id)
        self.assertEqual(visit["status"], "declined")
        self.assertEqual(visit["end_reason"], result["say"])
        self.assertNotIn("model_retry", visit)
        self.assertEqual(len(self.b_actor.calls), 1)
