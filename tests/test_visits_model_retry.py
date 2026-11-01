"""Transient decisions resume one durable phase across two independent worlds."""
from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
from types import SimpleNamespace
import unittest

from core.visits import VisitService
from core.visits.decisions import VisitDecisionError
from tests import test_visits_protocol as fixture


class VisitModelRetryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.VisitProtocolTests.asyncSetUp

    async def offered(self, *, automatic=False):
        if automatic:
            self.a.config = replace(self.a.config, auto_enabled=True)
        pending = await self.a.invite("life-a", self.peer, automatic=automatic)
        return pending["id"], self.b._state["visits"][pending["id"]]

    async def host_turn(self, *, automatic=False):
        visit_id, visit = await self.offered(automatic=automatic)
        await self.b._advance(visit_id)
        await self.a._advance(visit_id)  # Confirm arrival through the signed status exchange.
        self.now = max(self.now, self.a._state["visits"][visit_id]["next_at"])
        await self.a._advance(visit_id)  # Deliver the guest's first confirmed turn.
        self.assertEqual(visit["status"], "active")
        self.assertEqual(visit["next_actor"], visit["host_id"])
        self.assertEqual(visit["turns"][visit["guest_id"]], 1)
        return visit_id, visit

    def fail_then_resume(self, actor, category, *, failures=1, phase=None, request_started=True):
        original = actor.decide
        attempts = []

        async def decide(scope, current_phase, visit):
            if phase is None or current_phase == phase:
                attempts.append((current_phase, visit["id"]))
                if len(attempts) <= failures:
                    actor.calls.append((current_phase, visit["id"]))
                    raise VisitDecisionError(category, request_started=request_started)
            return await original(scope, current_phase, visit)

        actor.decide = decide
        return attempts

    def assert_waiting(self, service, visit, phase, category, attempts, next_at):
        retry = visit["model_retry"]
        self.assertEqual(retry["phase"], phase)
        self.assertEqual(retry["cursor"], service._decision_cursor(visit, phase))
        self.assertEqual(retry["category"], category)
        self.assertEqual(retry["attempts"], attempts)
        self.assertEqual(retry["next_at"], next_at)
        self.assertNotIn("model_pending", visit)
        self.assertNotIn("ended_at", visit)
        self.assertNotIn("end_pending", visit)
        self.assertNotIn("remembered", visit)
        self.assertIsNotNone(service.active(visit["scope"]))

    async def reload_service(self, service):
        await service.close()
        resumed = VisitService(service.db, service.config, service.actor_port,
                               clock=lambda: self.now, rng=lambda: .9)
        self.addAsyncCleanup(resumed.close)
        await resumed._load()
        return resumed

    async def assert_turn_recovers(self, category):
        visit_id, visit = await self.host_turn()
        log = copy.deepcopy(visit["log"])
        turns = copy.deepcopy(visit["turns"])
        calls = visit["calls"][visit["host_id"]]
        attempts = self.fail_then_resume(self.b_actor, category, phase="turn")
        failed_at = self.now
        await self.b._advance(visit_id)
        self.assertEqual(visit["status"], "active")
        self.assert_waiting(self.b, visit, "turn", category, 1, failed_at + 30)
        self.assertEqual(visit["log"], log)
        self.assertEqual(visit["turns"], turns)
        self.assertEqual(visit["calls"][visit["host_id"]], calls + 1)
        self.assertFalse(self.b_actor.memories)
        self.now = failed_at + 29
        await self.b._advance(visit_id)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(visit["log"], log)
        self.now = failed_at + 30
        await self.b._advance(visit_id)
        self.assertEqual(visit["status"], "active")
        self.assertNotIn("model_retry", visit)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(visit["calls"][visit["host_id"]], calls + 2)
        self.assertEqual(visit["log"][:len(log)], log)
        self.assertEqual(visit["turns"][visit["host_id"]], turns[visit["host_id"]] + 1)
        self.assertEqual(sum(row["text"] == "b：turn" for row in visit["log"]), 1)
        await self.b._advance(visit_id)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(sum(row["text"] == "b：turn" for row in visit["log"]), 1)

    async def test_turn_provider_failure_waits_then_recovers_once(self):
        await self.assert_turn_recovers("provider")

    async def test_turn_timeout_waits_then_recovers_once(self):
        await self.assert_turn_recovers("timeout")

    async def test_turn_format_failure_waits_then_recovers_once(self):
        await self.assert_turn_recovers("format")

    async def test_two_recoveries_are_the_limit_and_never_replay_confirmed_turns(self):
        visit_id, visit = await self.host_turn()
        log = copy.deepcopy(visit["log"])
        calls = visit["calls"][visit["host_id"]]
        attempts = self.fail_then_resume(self.b_actor, "provider", failures=10, phase="turn")
        first = self.now
        await self.b._advance(visit_id)
        self.assert_waiting(self.b, visit, "turn", "provider", 1, first + 30)
        self.now = first + 30
        await self.b._advance(visit_id)
        self.assert_waiting(self.b, visit, "turn", "provider", 2, first + 90)
        self.now = first + 89
        await self.b._advance(visit_id)
        self.assertEqual(len(attempts), 2)
        self.now = first + 90
        await self.b._advance(visit_id)
        self.assertEqual(visit["status"], "interrupted")
        self.assertNotIn("model_retry", visit)
        self.assertEqual(visit["calls"][visit["host_id"]], calls + 3)
        self.assertEqual(visit["log"], log)
        self.now += 600
        await self.b._advance(visit_id)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(visit["log"], log)

    async def test_busy_without_dispatch_does_not_spend_trip_calls(self):
        visit_id, visit = await self.host_turn()
        calls = visit["calls"][visit["host_id"]]
        log = copy.deepcopy(visit["log"])
        attempts = self.fail_then_resume(self.b_actor, "busy", failures=2,
                                         phase="turn", request_started=False)
        first = self.now
        await self.b._advance(visit_id)
        self.assert_waiting(self.b, visit, "turn", "busy", 1, first + 30)
        self.assertEqual(visit["calls"][visit["host_id"]], calls)
        self.now = first + 30
        await self.b._advance(visit_id)
        self.assert_waiting(self.b, visit, "turn", "busy", 2, first + 90)
        self.assertEqual(visit["calls"][visit["host_id"]], calls)
        self.assertEqual(visit["log"], log)
        self.now = first + 90
        await self.b._advance(visit_id)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(visit["status"], "active")
        self.assertEqual(visit["calls"][visit["host_id"]], calls + 1)
        self.assertNotIn("model_retry", visit)

    async def assert_scene_recovers(self, category):
        visit_id, visit = await self.host_turn()
        self.b._random = lambda: .1
        attempts = self.fail_then_resume(self.b_actor, category, phase="scene")
        first = self.now
        await self.b._advance(visit_id)  # The first host turn offers the optional event.
        log = copy.deepcopy(visit["log"])
        calls = visit["calls"][visit["host_id"]]
        self.assertEqual(visit["next_actor"], visit["guest_id"])
        self.assertEqual(visit["scene_stage"], "event_offer")
        self.assert_waiting(self.b, visit, "scene", category, 1, first + 30)
        self.assertEqual(visit["event_stage"], "none")
        self.now = first + 29
        await self.b._advance(visit_id)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(visit["log"], log)
        self.now = first + 30
        await self.b._advance(visit_id)
        self.assertEqual(visit["status"], "active")
        self.assertNotIn("model_retry", visit)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(visit["calls"][visit["host_id"]], calls + 1)
        self.assertEqual(visit["event_stage"], "offered")
        self.assertEqual(visit["log"][:len(log)], log)
        self.assertEqual(sum(row["kind"] == "event" for row in visit["log"]), 1)
        self.assertEqual(sum(row["text"] == "b：turn" for row in visit["log"]), 1)
        await self.b._advance(visit_id)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(sum(row["kind"] == "event" for row in visit["log"]), 1)

    async def test_scene_provider_failure_resumes_before_next_actor_gate(self):
        await self.assert_scene_recovers("provider")

    async def test_scene_timeout_resumes_before_next_actor_gate(self):
        await self.assert_scene_recovers("timeout")

    async def test_scene_format_failure_resumes_before_next_actor_gate(self):
        await self.assert_scene_recovers("format")

    async def test_receive_unavailable_result_stays_offered_and_can_accept_later(self):
        visit_id, visit = await self.offered()
        log = copy.deepcopy(visit["log"])
        attempts = self.fail_then_resume(self.b_actor, "format", phase="receive")
        first = self.now
        await self.b._advance(visit_id)
        self.assertEqual(visit["status"], "offered")
        self.assert_waiting(self.b, visit, "receive", "format", 1, first + 30)
        self.assertNotIn("arrived_at", visit)
        self.assertEqual(visit["log"], log)
        self.now = first + 29
        await self.b._advance(visit_id)
        self.assertEqual(len(attempts), 1)
        self.now = first + 30
        await self.b._advance(visit_id)
        self.assertEqual(visit["status"], "active")
        self.assertIn("arrived_at", visit)
        self.assertEqual(len(attempts), 2)
        self.assertNotIn("model_retry", visit)
        self.assertEqual(sum(row["text"] == "b：receive" for row in visit["log"]), 1)

    async def test_proposal_pending_retry_survives_sqlite_reload_before_contact(self):
        attempts = self.fail_then_resume(self.a_actor, "provider", phase="propose")
        first = self.now
        pending = await self.a.invite("life-a", self.peer)
        visit_id = pending["id"]
        visit = self.a._state["visits"][visit_id]
        self.assertEqual(visit["status"], "inviting")
        self.assertEqual(visit["proposal_stage"], "pending")
        self.assert_waiting(self.a, visit, "propose", "provider", 1, first + 30)
        saved = copy.deepcopy(visit["model_retry"])
        self.assertFalse(self.b._state["visits"])
        resumed = await self.reload_service(self.a)
        current = resumed._state["visits"][visit_id]
        self.assertEqual(current["model_retry"], saved)
        self.now = first + 29
        await resumed._advance(visit_id)
        self.assertEqual(len(attempts), 1)
        self.assertFalse(self.b._state["visits"])
        self.now = first + 30
        await resumed._advance(visit_id)
        self.assertEqual(len(attempts), 2)
        self.assertNotIn("model_retry", current)
        self.assertEqual(current["calls"][current["guest_id"]], 2)
        self.assertEqual(len(self.b._state["visits"]), 1)
        self.assertEqual(sum(row["kind"] == "invitation" for row in current["log"]), 1)
        self.assertNotIn("arrived_at", current)

    async def test_unknown_inflight_request_is_not_replayed_after_reload(self):
        visit_id, visit = await self.offered()
        visit["model_pending"] = "unknown-request-on-process-exit"
        visit["model_retry"] = {"phase": "receive", "cursor": self.b._decision_cursor(visit, "receive"),
                                "attempts": 1, "next_at": self.now + 30, "category": "provider"}
        calls = copy.deepcopy(visit["calls"])
        await self.b._save()
        resumed = await self.reload_service(self.b)
        current = resumed._state["visits"][visit_id]
        self.assertEqual(current["status"], "interrupted")
        self.assertNotIn("model_pending", current)
        self.assertNotIn("model_retry", current)
        self.assertNotIn("arrived_at", current)
        self.assertEqual(current["calls"], calls)
        self.assertFalse(self.b_actor.calls)

    async def test_real_model_candidate_survives_soft_busy_and_is_applied_once(self):
        from tests import test_visits_actor as actor_fixture
        from tests import test_engine as engine_fixture

        visit_id, visit = await self.host_turn()
        log = copy.deepcopy(visit["log"])
        calls = visit["calls"][visit["host_id"]]
        private = copy.deepcopy(visit["private"])
        self.node_id = "study"
        await actor_fixture.VisitActorTests.asyncSetUp(self)
        self.llm.default_reply = '{"say":"茶还温着，我们接着聊。","thought":"想听她说完。"}'
        describe = self.b_actor.describe
        actual_decide = self.port.decide
        generate = self.engine._budgeted_generate
        model_attempts = []

        async def describe_model(scope):
            card = await describe(scope)
            return {**card, "place": "客厅", "reception_open": True}

        async def model_for_service(scope, phase, snapshot):
            model_attempts.append(phase)
            return await actual_decide(engine_fixture.SESSION, phase, snapshot)

        async def model_then_busy(*args, **kwargs):
            reply = await generate(*args, **kwargs)
            self.b_actor.ready = False
            return reply

        self.port.describe = describe_model
        self.engine._budgeted_generate = model_then_busy
        self.b.actor_port = SimpleNamespace(describe=describe_model, decide=model_for_service,
                                           remember=self.b_actor.remember)
        first = self.now
        await self.b._advance(visit_id)
        self.assert_waiting(self.b, visit, "turn", "life_busy", 1, first + 30)
        candidate = copy.deepcopy(visit["model_retry"]["candidate"])
        self.assertEqual(candidate["say"], "茶还温着，我们接着聊。")
        self.assertEqual(visit["calls"][visit["host_id"]], calls + 1)
        self.assertEqual(visit["log"], log)
        self.assertEqual(visit["private"], private)
        stored = await self.b_db.call("kv_get", "world_visits.v1", {})
        self.assertEqual(stored["visits"][visit_id]["model_retry"]["candidate"], candidate)
        self.assertNotIn("candidate", self.b._wire(visit))
        self.assertEqual(len(self.llm.calls), 1)
        self.now = first + 30
        await self.b._advance(visit_id)
        self.assert_waiting(self.b, visit, "turn", "life_busy", 2, first + 90)
        self.assertEqual(visit["model_retry"]["candidate"], candidate)
        self.assertEqual(visit["calls"][visit["host_id"]], calls + 1)
        self.assertEqual(len(self.llm.calls), 1)
        self.b_actor.ready = True
        self.now = first + 90
        await self.b._advance(visit_id)
        self.assertEqual(visit["status"], "active")
        self.assertNotIn("model_retry", visit)
        self.assertEqual(model_attempts, ["turn"])
        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual(visit["calls"][visit["host_id"]], calls + 1)
        self.assertEqual(visit["log"][:len(log)], log)
        self.assertEqual(sum(row["text"] == candidate["say"] for row in visit["log"]), 1)
        await self.b._advance(visit_id)
        self.assertEqual(model_attempts, ["turn"])
        self.assertEqual(sum(row["text"] == candidate["say"] for row in visit["log"]), 1)

    async def test_expired_wait_never_starts_a_recovery_request(self):
        visit_id, visit = await self.host_turn()
        attempts = self.fail_then_resume(self.b_actor, "provider", phase="turn")
        await self.b._advance(visit_id)
        calls = copy.deepcopy(visit["calls"])
        log = copy.deepcopy(visit["log"])
        self.now = visit["expires_at"] + 1
        await self.b.tick()
        self.assertEqual(visit["status"], "expired")
        self.assertNotIn("model_retry", visit)
        self.assertEqual(visit["calls"], calls)
        self.assertEqual(visit["log"], log)
        self.assertEqual(len(attempts), 1)

    async def test_late_failure_cannot_reopen_a_confirmed_goodbye(self):
        visit_id, visit = await self.host_turn()
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed(*args):
            entered.set()
            await release.wait()
            raise VisitDecisionError("provider")

        self.b_actor.decide = delayed
        task = asyncio.create_task(self.b._advance(visit_id))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            await self.b.return_home("life-b")
            ended = copy.deepcopy(visit)
            self.assertIn(ended["status"], {"completed", "interrupted"})
            release.set()
            await asyncio.wait_for(task, 3)
            self.assertEqual(visit, ended)
            self.assertNotIn("model_retry", visit)
            self.assertNotIn("model_pending", visit)
            self.assertIsNone(self.b.active("life-b"))
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class VisitActorFormatTests(unittest.TestCase):
    def test_empty_object_is_not_a_valid_turn(self):
        from tests.test_visits_actor import VisitActorPort

        with self.assertRaises(VisitDecisionError) as error:
            VisitActorPort._parse_decision("turn", "{}")
        self.assertEqual(error.exception.category, "format")
