"""A paid candidate survives temporary failures while verifying its owner."""
from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
from types import SimpleNamespace
import unittest

from core.visits.decisions import VisitDecisionError
from tests import test_visits_model_retry as fixture


class VisitRetryReviewTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.VisitModelRetryTests.asyncSetUp
    offered = fixture.VisitModelRetryTests.offered
    host_turn = fixture.VisitModelRetryTests.host_turn
    reload_service = fixture.VisitModelRetryTests.reload_service

    async def paid_candidate(self):
        visit_id, visit = await self.host_turn()
        original = self.b_actor.decide

        async def hold_once(scope, phase, current):
            result = await original(scope, phase, current)
            if phase == "turn":
                self.b_actor.decide = original
                raise VisitDecisionError("life_busy", candidate=result)
            return result

        self.b_actor.decide = hold_once
        await self.b._advance(visit_id)
        self.assertIn("candidate", visit["model_retry"])
        return visit_id, visit, copy.deepcopy(visit["model_retry"]["candidate"])

    def fail_one_status_read(self, error):
        original = self.b_actor.describe

        async def flaky(scope):
            self.b_actor.describe = original
            raise error

        self.b_actor.describe = flaky

    async def test_candidate_survives_failed_status_read_and_reload(self):
        visit_id, visit, candidate = await self.paid_candidate()
        calls = copy.deepcopy(visit["calls"])
        model_calls = len(self.b_actor.calls)
        log = copy.deepcopy(visit["log"])
        self.fail_one_status_read(OSError("temporary state read failure"))
        self.now += 30
        await self.b._advance(visit_id)
        self.assertEqual(visit["status"], "active")
        self.assertEqual(visit["calls"], calls)
        self.assertEqual(visit["model_retry"]["attempts"], 2)
        self.assertEqual(visit["model_retry"].get("candidate"), candidate)
        saved = await self.b_db.call("kv_get", "world_visits.v1", {})
        self.assertEqual(saved["visits"][visit_id]["model_retry"].get("candidate"), candidate)
        resumed = await self.reload_service(self.b)
        self.now += 60
        await resumed._advance(visit_id)
        current = resumed._state["visits"][visit_id]
        self.assertEqual(current["status"], "active")
        self.assertEqual(current["calls"], calls)
        self.assertEqual(len(self.b_actor.calls), model_calls)
        self.assertEqual(current["log"][:len(log)], log)
        self.assertEqual(sum(row["text"] == candidate["say"] for row in current["log"]), 1)
        self.assertNotIn("model_retry", current)

    async def test_status_timeout_keeps_paid_candidate_at_last_call(self):
        visit_id, visit, candidate = await self.paid_candidate()
        own = visit["host_id"]
        visit["calls"][own] = self.b._allocation(visit, own)
        calls = copy.deepcopy(visit["calls"])
        model_calls = len(self.b_actor.calls)
        await self.b._save()
        self.fail_one_status_read(TimeoutError("temporary status verification timeout"))
        self.now += 30
        await self.b._advance(visit_id)
        self.assertEqual(visit["model_retry"]["category"], "timeout")
        self.now += 60
        await self.b._advance(visit_id)
        self.assertEqual(visit["status"], "active")
        self.assertEqual(visit["calls"], calls)
        self.assertEqual(len(self.b_actor.calls), model_calls)
        self.assertEqual(sum(row["text"] == candidate["say"] for row in visit["log"]), 1)
        self.assertNotIn("model_retry", visit)

    async def test_unstarted_busy_proposal_does_not_spend_trip_call(self):
        from tests import test_visits_actor as actor_fixture
        from tests import test_engine as engine_fixture

        await actor_fixture.VisitActorTests.asyncSetUp(self)
        describe = self.a_actor.describe

        async def became_busy(scope):
            return {**await describe(scope), "ready": False, "ready_reason": "正在回应或处理手头的事"}

        async def actual_actor(scope, phase, snapshot):
            return await self.port.decide(engine_fixture.SESSION, phase, snapshot)

        self.port.describe = became_busy
        self.a.actor_port = SimpleNamespace(describe=describe, decide=actual_actor,
                                           remember=self.a_actor.remember)
        result = await self.a.invite("life-a", self.peer)
        visit = self.a._state["visits"][result["id"]]
        self.assertEqual(visit["status"], "declined")
        self.assertEqual(visit["log"][-1]["kind"], "contact_status")
        self.assertEqual(sum(visit["calls"].values()), 0)
        self.assertFalse(self.llm.calls)
        self.assertFalse(self.b._state["visits"])

    async def test_post_model_closed_reception_keeps_the_spent_trip_call(self):
        from tests import test_visits_actor as actor_fixture
        from tests import test_engine as engine_fixture

        await actor_fixture.VisitActorTests.asyncSetUp(self)
        describe = self.a_actor.describe
        reads = 0

        async def reception_closes(scope):
            nonlocal reads
            reads += 1
            return {**await describe(scope), "place": "客厅", "reception_open": reads == 1}

        async def actual_actor(scope, phase, snapshot):
            return await self.port.decide(engine_fixture.SESSION, phase, snapshot)

        self.port.describe = reception_closes
        self.a.actor_port = SimpleNamespace(describe=describe, decide=actual_actor,
                                           remember=self.a_actor.remember)
        result = await self.a.invite("life-a", self.peer)
        visit = self.a._state["visits"][result["id"]]
        self.assertEqual(visit["status"], "declined")
        self.assertEqual(visit["log"][-1]["kind"], "contact_status")
        self.assertEqual(sum(visit["calls"].values()), 1)
        self.assertEqual(len(self.llm.calls), 1)
        budget = await self.engine._background_budget_status(purpose="world_visit",
                                                            session_id=engine_fixture.SESSION)
        self.assertEqual(budget["calls"], 1)

    async def test_outer_service_deadline_preserves_parsed_actor_result(self):
        from tests import test_visits_actor as actor_fixture
        from tests import test_engine as engine_fixture

        visit_id, visit = await self.host_turn()
        calls = copy.deepcopy(visit["calls"])
        log = copy.deepcopy(visit["log"])
        await actor_fixture.VisitActorTests.asyncSetUp(self)
        self.llm.default_reply = '{"say":"茶还温着，我们继续聊。","thought":"想听她说完。"}'
        describe = self.b_actor.describe
        blocked = asyncio.Event()
        reads = 0
        model_attempts = 0

        async def checking(scope):
            nonlocal reads
            reads += 1
            if reads == 2:
                blocked.set()
                await asyncio.Future()
            return {**await describe(scope), "place": "客厅", "reception_open": True}

        async def actual_actor(scope, phase, snapshot):
            nonlocal model_attempts
            model_attempts += 1
            return await self.port.decide(engine_fixture.SESSION, phase, snapshot)

        self.port.describe = checking
        self.b.actor_port = SimpleNamespace(describe=checking, decide=actual_actor,
                                           remember=self.b_actor.remember)
        self.b.config = replace(self.b.config, timeout_seconds=2)
        await self.b._advance(visit_id)
        self.assertTrue(blocked.is_set(), "the deadline must cancel the final state read")
        self.assertEqual(visit["status"], "active")
        self.assertEqual(visit["model_retry"]["category"], "timeout")
        self.assertEqual(visit["model_retry"].get("candidate", {}).get("say"), "茶还温着，我们继续聊。")
        self.assertEqual(len(self.llm.calls), 1)
        own = visit["host_id"]
        self.assertEqual(visit["calls"][own], calls[own] + 1)
        paid_calls = copy.deepcopy(visit["calls"])
        self.now += 30
        await self.b._advance(visit_id)
        self.assertEqual(visit["status"], "active")
        self.assertEqual(visit["calls"], paid_calls)
        self.assertEqual(model_attempts, 1)
        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual(visit["log"][:len(log)], log)
        self.assertEqual(sum(row["text"] == "茶还温着，我们继续聊。" for row in visit["log"]), 1)

    async def test_reload_during_candidate_verification_keeps_paid_result(self):
        visit_id, visit, candidate = await self.paid_candidate()
        calls = copy.deepcopy(visit["calls"])
        log = copy.deepcopy(visit["log"])
        model_attempts = len(self.b_actor.calls)
        describe = self.b_actor.describe
        entered, release = asyncio.Event(), asyncio.Event()

        async def checking(scope):
            entered.set()
            await release.wait()
            return await describe(scope)

        self.b_actor.describe = checking
        self.now += 30
        task = asyncio.create_task(self.b._advance(visit_id))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            self.assertIn("model_pending", visit)
            await self.b.close()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.b_actor.describe = describe
            resumed = await self.reload_service(self.b)
            current = resumed._state["visits"][visit_id]
            self.assertEqual(current["status"], "active")
            self.assertNotIn("model_pending", current)
            self.assertEqual(current["model_retry"]["candidate"], candidate)
            self.assertEqual(current["calls"], calls)
            await resumed._advance(visit_id)
            self.assertEqual(current["status"], "active")
            self.assertEqual(current["calls"], calls)
            self.assertEqual(len(self.b_actor.calls), model_attempts)
            self.assertEqual(current["log"][:len(log)], log)
            self.assertEqual(sum(row["text"] == candidate["say"] for row in current["log"]), 1)
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class VisitActorVerificationReviewTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"

    async def asyncSetUp(self):
        from tests import test_visits_actor as actor_fixture

        await actor_fixture.VisitActorTests.asyncSetUp(self)

    async def assert_postread_failure(self, error, category):
        from tests import test_engine as engine_fixture

        self.llm.default_reply = '{"say":"茶还温着，我们继续聊。","thought":"想听她说完。"}'
        describe = self.port.describe
        reads = 0

        async def status_read(scope):
            nonlocal reads
            reads += 1
            if reads == 2:
                raise error
            return await describe(scope)

        self.port.describe = status_read
        with self.assertRaises(VisitDecisionError) as failed:
            await self.port.decide(engine_fixture.SESSION, "turn", self.visit)
        self.assertEqual(failed.exception.category, category)
        self.assertTrue(failed.exception.request_started)
        self.assertEqual(failed.exception.candidate["say"], "茶还温着，我们继续聊。")
        self.assertEqual(len(self.llm.calls), 1)
        budget = await self.engine._background_budget_status(purpose="world_visit",
                                                            session_id=engine_fixture.SESSION)
        self.assertEqual(budget["calls"], 1)

    async def test_paid_result_survives_post_model_status_read_failure(self):
        await self.assert_postread_failure(OSError("temporary post-model state read failure"), "provider")

    async def test_paid_result_survives_post_model_status_timeout(self):
        await self.assert_postread_failure(TimeoutError("temporary post-model status timeout"), "timeout")

    async def test_persona_switch_during_final_status_read_discards_result(self):
        from tests import test_engine as engine_fixture

        describe = self.port.describe
        reads = 0

        async def switched(scope):
            nonlocal reads
            reads += 1
            if reads == 2:
                self.persona.text = "这是另一个角色的人设。"
            return await describe(scope)

        self.port.describe = switched
        with self.assertRaises(VisitDecisionError) as failed:
            await self.port.decide(engine_fixture.SESSION, "turn", self.visit)
        self.assertEqual(failed.exception.category, "identity")
        self.assertIsNone(failed.exception.candidate)
        self.assertEqual(len(self.llm.calls), 1)

    async def test_cancellation_during_final_status_read_is_not_reclassified(self):
        from tests import test_engine as engine_fixture

        describe = self.port.describe
        reads = 0

        async def cancelled(scope):
            nonlocal reads
            reads += 1
            if reads == 2:
                raise asyncio.CancelledError()
            return await describe(scope)

        self.port.describe = cancelled
        with self.assertRaises(asyncio.CancelledError):
            await self.port.decide(engine_fixture.SESSION, "turn", self.visit)
        self.assertEqual(len(self.llm.calls), 1)
