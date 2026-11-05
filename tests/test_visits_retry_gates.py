"""Saved retry candidates keep current invitation gates and request accounting."""
from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
import unittest

from core.visits.decisions import VisitDecisionError
from tests import test_visits_model_retry as fixture


class VisitRetryGateTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.VisitModelRetryTests.asyncSetUp
    offered = fixture.VisitModelRetryTests.offered
    host_turn = fixture.VisitModelRetryTests.host_turn
    reload_service = fixture.VisitModelRetryTests.reload_service

    def hold_candidate(self, actor, phase):
        original = actor.decide

        async def decide(scope, current_phase, visit):
            result = await original(scope, current_phase, visit)
            if current_phase == phase:
                raise VisitDecisionError("life_busy", candidate=result)
            return result

        actor.decide = decide

    def change_card(self, actor, **changes):
        original = actor.describe

        async def describe(scope):
            return {**await original(scope), **changes}

        actor.describe = describe

    async def assert_receive_gate(self, changes, reason):
        visit_id, visit = await self.offered()
        self.hold_candidate(self.b_actor, "receive")
        await self.b._advance(visit_id)
        self.assertIn("candidate", visit["model_retry"])
        calls = copy.deepcopy(visit["calls"])
        resumed = await self.reload_service(self.b)
        self.change_card(self.b_actor, **changes)
        self.now += 30
        await resumed._advance(visit_id)
        current = resumed._state["visits"][visit_id]
        self.assertEqual(current["status"], "declined")
        self.assertEqual(current["end_reason"], reason)
        self.assertEqual(current["log"][-1]["kind"], "contact_status")
        self.assertEqual(current["calls"], calls)
        self.assertEqual(len(self.b_actor.calls), 1)
        self.assertNotIn("arrived_at", current)
        self.assertNotIn("model_retry", current)
        self.assertFalse(self.b_actor.memories)

    async def test_receive_candidate_after_reload_obeys_reception_window(self):
        visit_id, visit = await self.offered()
        self.hold_candidate(self.b_actor, "receive")
        await self.b._advance(visit_id)
        calls = copy.deepcopy(visit["calls"])
        resumed = await self.reload_service(self.b)
        self.change_card(self.b_actor, reception_open=False)
        self.now += 30
        await resumed._advance(visit_id)
        current = resumed._state["visits"][visit_id]
        self.assertEqual(current["status"], "active")
        self.assertEqual(current["calls"], calls)
        self.assertEqual(len(self.b_actor.calls), 1)
        self.assertNotIn("model_retry", current)

    async def test_receive_candidate_after_reload_obeys_human_chat_gate(self):
        visit_id, visit = await self.offered()
        self.hold_candidate(self.b_actor, "receive")
        await self.b._advance(visit_id)
        calls = copy.deepcopy(visit["calls"])
        resumed = await self.reload_service(self.b)
        original = self.b_actor.describe

        async def describe(scope):
            return {**await original(scope), "chat_busy": True}

        self.b_actor.describe = describe
        self.now += 30
        current = resumed._state["visits"][visit_id]
        await resumed._advance(visit_id)
        self.assertEqual(current["status"], "offered")
        self.assertEqual(current["model_retry"]["category"], "life_busy")
        self.assertEqual(current["calls"], calls)
        self.assertNotIn("arrived_at", current)
        self.b_actor.describe = original
        self.now = current["model_retry"]["next_at"]
        await resumed._advance(visit_id)
        self.assertEqual(current["status"], "active")
        self.assertEqual(current["calls"], calls)
        self.assertEqual(len(self.b_actor.calls), 1)

    async def test_proposal_candidate_after_reload_cannot_call_outside_reception(self):
        self.hold_candidate(self.a_actor, "propose")
        pending = await self.a.invite("life-a", self.peer)
        visit_id = pending["id"]
        calls = copy.deepcopy(self.a._state["visits"][visit_id]["calls"])
        resumed = await self.reload_service(self.a)
        self.change_card(self.a_actor, reception_open=False)
        self.now += 30
        await resumed._advance(visit_id)
        current = resumed._state["visits"][visit_id]
        self.assertEqual(current["status"], "declined")
        self.assertEqual(current["end_reason"], "现在不在接待时间，稍后再约")
        self.assertEqual(current["log"][-1]["kind"], "contact_status")
        self.assertEqual(current["calls"], calls)
        self.assertEqual(len(self.a_actor.calls), 1)
        self.assertFalse(self.b._state["visits"])

    async def test_confirmed_turn_candidate_can_resume_at_last_call_after_reload(self):
        visit_id, visit = await self.host_turn()
        self.hold_candidate(self.b_actor, "turn")
        await self.b._advance(visit_id)
        candidate = copy.deepcopy(visit["model_retry"]["candidate"])
        before = len(self.b_actor.calls)
        own = visit["host_id"]
        visit["calls"][own] = self.b._allocation(visit, own)
        await self.b._save()
        resumed = await self.reload_service(self.b)
        self.change_card(self.b_actor, reception_open=False, chat_busy=True)
        self.now += 30
        await resumed._advance(visit_id)
        current = resumed._state["visits"][visit_id]
        self.assertEqual(current["status"], "active")
        self.assertNotIn("model_retry", current)
        self.assertEqual(current["calls"][own], resumed._allocation(current, own))
        self.assertEqual(len(self.b_actor.calls), before)
        self.assertEqual(sum(row["text"] == candidate["say"] for row in current["log"]), 1)

    async def test_service_timeout_is_reported_as_timeout_while_waiting(self):
        visit_id, visit = await self.offered()
        self.b.config = replace(self.b.config, timeout_seconds=.01)

        async def hanging(*args):
            await asyncio.Future()

        self.b_actor.decide = hanging
        await self.b._advance(visit_id)
        self.assertEqual(visit["status"], "offered")
        self.assertEqual(visit["model_retry"]["category"], "timeout")
        self.assertIn("回应超时", self.b._visible(visit)["model_wait"]["reason"])
