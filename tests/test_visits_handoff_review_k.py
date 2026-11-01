"""Saved invitation choices survive bounded failures in their final state check."""
from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
import unittest
from unittest.mock import patch

from core.visits.protocol import TERMINAL
from tests import test_visits_protocol as protocol
from tests import test_visits_proposal_resume as proposals
from tests import test_visits_decision_resume as decisions


class VisitHandoffReviewTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = protocol.VisitProtocolTests.asyncSetUp
    suspend = proposals.VisitProposalResumeTests.suspend
    reload_a = proposals.VisitProposalResumeTests.reload_a
    reload_service = decisions.VisitDecisionResumeTests.reload_service
    offered = decisions.VisitDecisionResumeTests.offered

    def fail_checks(self, actor, *, always=False, hanging=False):
        original = actor.describe
        reads = []
        async def describe(scope):
            reads.append(scope)
            if len(reads) % 2 == 0 and (always or len(reads) == 2):
                if hanging:
                    await asyncio.Future()
                raise OSError("synthetic private state read details")
            return await original(scope)
        actor.describe = describe
        return reads

    async def saved_proposal(self, boundary):
        visit_id = await self.suspend(boundary)
        await self.reload_a()
        return visit_id, self.a._state["visits"][visit_id]

    async def assert_proposal_recovers(self, boundary):
        visit_id, visit = await self.saved_proposal(boundary)
        before = copy.deepcopy(visit["calls"])
        reads = self.fail_checks(self.a_actor)
        await self.a.tick()
        self.assertEqual(visit["status"], "inviting")
        self.assertEqual(visit["model_retry"]["category"], "state")
        self.assertEqual(visit["model_retry"]["attempts"], 1)
        self.assertEqual(visit["calls"], before)
        self.assertNotIn("arrived_at", visit)
        self.assertNotIn(visit_id, self.b._state["visits"])
        self.assertIn("状态", self.a._visible(visit)["model_wait"]["reason"])
        self.assertNotIn("synthetic private", str(self.a._visible(visit)))
        self.now += 29
        await self.a.tick()
        self.assertEqual(len(reads), 2)
        self.now += 1
        await self.a.tick()
        self.assertIn(visit_id, self.b._state["visits"])
        self.assertEqual(visit["proposal_stage"], "sent")
        self.assertEqual(visit["calls"], before)
        self.assertEqual(sum(phase == "propose" for phase, _ in self.a_actor.calls), 1)
        self.assertEqual(sum(row["kind"] == "invitation" for row in visit["log"]), 1)
        self.assertNotIn("model_retry", visit)

    async def test_saved_pending_proposal_recovers_after_one_final_read_failure(self):
        await self.assert_proposal_recovers("decision")

    async def test_saved_prepared_proposal_recovers_after_one_final_read_failure(self):
        await self.assert_proposal_recovers("prepared")

    async def test_saved_receive_waits_without_arriving_then_reuses_its_choice(self):
        visit_id, _ = await self.offered()
        await self.b._decide(visit_id, "receive")
        resumed = await self.reload_service(self.b)
        visit = resumed._state["visits"][visit_id]
        calls = copy.deepcopy(visit["calls"])
        self.fail_checks(self.b_actor)
        await resumed.tick()
        self.assertEqual(visit["status"], "offered")
        self.assertEqual(visit["model_retry"]["category"], "state")
        self.assertNotIn("arrived_at", visit)
        self.assertFalse(self.b_actor.memories)
        self.now += 30
        await resumed.tick()
        self.assertEqual(visit["status"], "active")
        self.assertEqual(visit["calls"], calls)
        self.assertEqual(len(self.b_actor.calls), 1)
        self.assertEqual(sum(row["text"] == "b：receive" for row in visit["log"]), 1)
        self.assertNotIn("model_retry", visit)

    async def test_three_failed_checks_end_without_reset_by_successful_tick_reads(self):
        visit_id, visit = await self.saved_proposal("prepared")
        before = copy.deepcopy(visit["calls"])
        self.fail_checks(self.a_actor, always=True)
        await self.a.tick()
        self.assertEqual(visit["model_retry"]["next_at"], self.now + 30)
        self.now += 30
        await self.a.tick()
        self.assertEqual(visit["model_retry"]["attempts"], 2)
        self.assertEqual(visit["model_retry"]["next_at"], self.now + 60)
        self.now += 60
        await self.a.tick()
        self.assertEqual(visit["status"], "interrupted")
        self.assertIn("接待状态", visit["end_reason"])
        self.assertNotIn("model_retry", visit)
        self.assertEqual(visit["calls"], before)
        self.assertNotIn(visit_id, self.b._state["visits"])

    async def test_waiting_prepared_choice_survives_reload_at_last_allocation(self):
        visit_id, visit = await self.saved_proposal("prepared")
        self.fail_checks(self.a_actor)
        await self.a.tick()
        own = self.a._local_id(visit)
        visit["calls"][own] = self.a._allocation(visit, own)
        await self.a._save()
        calls = copy.deepcopy(visit["calls"])
        await self.reload_a()
        current = self.a._state["visits"][visit_id]
        self.assertEqual(current["model_retry"]["attempts"], 1)
        self.now += 30
        await self.a.tick()
        self.assertEqual(current["proposal_stage"], "sent")
        self.assertEqual(current["calls"], calls)
        self.assertEqual(sum(phase == "propose" for phase, _ in self.a_actor.calls), 1)
        self.assertNotIn("model_retry", current)

    async def test_direct_handoff_obeys_cooldown_without_reloading_state(self):
        visit_id, visit = await self.saved_proposal("prepared")
        original = self.a_actor.describe
        reads = []
        async def unavailable(scope):
            reads.append(scope)
            raise OSError("not available")
        self.a_actor.describe = unavailable
        await self.a._advance(visit_id)
        await self.a._advance(visit_id)
        self.assertEqual(reads, ["life-a"])
        self.assertEqual(visit["model_retry"]["attempts"], 1)
        self.a_actor.describe = original

    async def test_final_state_check_uses_the_configured_deadline(self):
        _, visit = await self.saved_proposal("prepared")
        self.a.config = replace(self.a.config, timeout_seconds=.01)
        self.fail_checks(self.a_actor, hanging=True)
        await self.a.tick()
        self.assertEqual(visit["status"], "inviting")
        self.assertEqual(visit["model_retry"]["category"], "state")
        self.assertEqual(visit["model_retry"]["attempts"], 1)

    async def test_cancellation_propagates_without_creating_a_failed_check(self):
        visit_id, visit = await self.saved_proposal("prepared")
        async def cancelled(scope):
            raise asyncio.CancelledError
        with patch.object(self.a_actor, "describe", side_effect=cancelled):
            with self.assertRaises(asyncio.CancelledError):
                await self.a._advance(visit_id)
        self.assertNotIn("model_retry", visit)
        self.assertNotIn(visit["status"], TERMINAL)

    def change_card(self, actor, **changes):
        original = actor.describe
        async def changed(scope):
            return {**await original(scope), **changes}
        actor.describe = changed

    async def assert_waiting_proposal_gate(self, changes, status, reason):
        visit_id, visit = await self.saved_proposal("prepared")
        calls = copy.deepcopy(visit["calls"])
        self.fail_checks(self.a_actor)
        await self.a.tick()
        self.assertEqual(visit["model_retry"]["attempts"], 1)
        self.change_card(self.a_actor, **changes)
        self.now += 30
        await self.a.tick()
        self.assertEqual(visit["status"], status)
        self.assertEqual(visit["end_reason"], reason)
        self.assertNotIn("model_retry", visit)
        self.assertNotIn(visit_id, self.b._state["visits"])
        self.assertEqual(visit["calls"], calls)
        self.assertEqual(sum(phase == "propose" for phase, _ in self.a_actor.calls), 1)

    async def test_waiting_proposal_still_obeys_a_closed_reception_window(self):
        await self.assert_waiting_proposal_gate({"reception_open": False}, "declined",
                                               "现在不在接待时间，稍后再约")

    async def test_waiting_proposal_cannot_survive_a_persona_change(self):
        await self.assert_waiting_proposal_gate({"persona_key": "different-persona"}, "interrupted",
                                               "生活身份变化，本次联系结束")

    async def test_waiting_receive_yields_to_a_person_without_another_model_call(self):
        visit_id, _ = await self.offered()
        await self.b._decide(visit_id, "receive")
        resumed = await self.reload_service(self.b)
        visit = resumed._state["visits"][visit_id]
        calls = copy.deepcopy(visit["calls"])
        self.fail_checks(self.b_actor)
        await resumed.tick()
        self.change_card(self.b_actor, chat_busy=True)
        self.now += 30
        await resumed.tick()
        self.assertEqual(visit["status"], "declined")
        self.assertEqual(visit["end_reason"], "正在接眼前的交谈，稍后再约")
        self.assertNotIn("arrived_at", visit)
        self.assertNotIn("model_retry", visit)
        self.assertEqual(visit["calls"], calls)
        self.assertEqual(len(self.b_actor.calls), 1)
