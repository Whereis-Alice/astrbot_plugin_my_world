"""Paid saved turns and events resume their original stage through real ticks."""
from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
import unittest
from unittest.mock import AsyncMock, patch

from core.visits import VisitService
from core.visits.protocol import clean_decision
from tests import test_visits_lifecycle_races as fixture


class SavedVisitPhaseTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.VisitLifecycleRaceTests.asyncSetUp
    pair = fixture.VisitLifecycleRaceTests.pair
    exchange = fixture.VisitLifecycleRaceTests.exchange

    async def reload_service(self, service):
        await service.close()
        resumed = VisitService(service.db, service.config, service.actor_port,
                               clock=lambda: self.now, rng=lambda: .9)
        self.addAsyncCleanup(resumed.close)
        await resumed._load()
        if service is self.a:
            resumed.transport.request = self.exchange
            self.a = resumed
        else:
            self.b = resumed
        return resumed

    async def tick(self, service):
        with patch.object(service, "_automatic", new=AsyncMock()), \
             patch.object(service, "_notify_end", new=AsyncMock()), \
             patch.object(service, "_remember", new=AsyncMock()):
            await service.tick()

    async def saved_host(self, phase="turn", *, stage="event_offer", final_call=False):
        _, visit = self.pair(arrived=True)
        self.b._record_turn(visit, visit["guest_id"], clean_decision({"say": "访客已发言"}))
        if phase == "scene":
            self.b._record_turn(visit, visit["host_id"], clean_decision({"say": "主人已发言"}))
            visit["scene_stage"] = stage
            if stage == "event_resolve":
                visit["event_stage"] = "offered"
                visit["scene"]["event"] = "茶叶不够了"
                visit["action_plans"] = {visit["host_id"]: "找茶", visit["guest_id"]: "烧水"}
        if final_call:
            visit["max_calls"] = 4
        result = await self.b._decide(visit["id"], phase)
        resumed = await self.reload_service(self.b)
        return resumed, resumed._state["visits"][visit["id"]], result

    async def test_saved_host_turn_waits_for_current_readiness_then_applies_once(self):
        service, visit, result = await self.saved_host()
        calls, log = copy.deepcopy(visit["calls"]), copy.deepcopy(visit["log"])
        model_calls = list(self.b_actor.calls)
        self.b_actor.ready = False
        self.now += 65
        await self.tick(service)
        self.assertEqual(visit["log"], log)
        self.assertEqual(visit["model_retry"]["category"], "life_busy")
        self.assertEqual(visit["calls"], calls)
        self.b_actor.ready = True
        self.now += 29
        await self.tick(service)
        self.assertEqual(visit["log"], log)
        self.now += 1
        await self.tick(service)
        self.now += 65
        await self.tick(service)
        self.assertEqual(sum(row["text"] == result["say"] for row in visit["log"]), 1)
        self.assertEqual(visit["turns"][visit["host_id"]], 1)
        self.assertEqual(self.b_actor.calls, model_calls)
        self.assertEqual(visit["calls"], calls)

    async def test_busy_saved_turn_ends_after_two_cooldowns_without_regeneration(self):
        service, visit, _ = await self.saved_host()
        calls, log = copy.deepcopy(visit["calls"]), copy.deepcopy(visit["log"])
        self.b_actor.ready = False
        self.now += 65
        for attempt, wait in ((1, 30), (2, 60), (3, 0)):
            await self.tick(service)
            if attempt < 3:
                self.assertEqual(visit["model_retry"]["attempts"], attempt)
                self.assertEqual(visit["model_retry"]["next_at"], self.now + wait)
                self.now += wait
        self.assertEqual(visit["status"], "interrupted")
        self.assertEqual(visit["calls"], calls)
        self.assertEqual(visit["log"], log)
        self.assertEqual(len(self.b_actor.calls), 1)

    async def test_saved_event_offer_resumes_before_the_guest_turn_at_final_allocation(self):
        service, visit, result = await self.saved_host("scene", final_call=True)
        calls, model_calls = copy.deepcopy(visit["calls"]), list(self.b_actor.calls)
        self.assertEqual(visit["next_actor"], visit["guest_id"])
        self.assertEqual(visit["calls"][visit["host_id"]], service._allocation(visit, visit["host_id"]))
        self.now += 65
        await self.tick(service)
        self.assertEqual(visit["event_stage"], "offered")
        self.assertEqual(visit["scene"]["event"], result["scene"]["event"])
        self.now += 65
        await self.tick(service)
        self.assertEqual(sum(row["kind"] == "event" for row in visit["log"]), 1)
        self.assertEqual(visit["calls"], calls)
        self.assertEqual(self.b_actor.calls, model_calls)

    async def test_saved_event_wait_survives_reload_and_is_not_applied_while_busy(self):
        service, visit, _ = await self.saved_host("scene")
        calls, log = copy.deepcopy(visit["calls"]), copy.deepcopy(visit["log"])
        self.b_actor.ready = False
        self.now += 65
        await self.tick(service)
        self.assertEqual(visit["log"], log)
        self.assertEqual(visit["event_stage"], "none")
        retry = copy.deepcopy(visit["model_retry"])
        service = await self.reload_service(service)
        visit = service._state["visits"][visit["id"]]
        self.assertEqual(visit["model_retry"], retry)
        self.b_actor.ready = True
        self.now += 30
        await self.tick(service)
        self.assertEqual(visit["event_stage"], "offered")
        self.assertEqual(sum(row["kind"] == "event" for row in visit["log"]), 1)
        self.assertEqual(visit["calls"], calls)
        self.assertEqual(len(self.b_actor.calls), 1)

    async def test_saved_event_resolution_is_consumed_once_by_the_normal_tick(self):
        service, visit, result = await self.saved_host("scene", stage="event_resolve")
        calls = copy.deepcopy(visit["calls"])
        self.now += 65
        await self.tick(service)
        self.assertEqual(visit["event_stage"], "resolved")
        self.now += 65
        await self.tick(service)
        self.assertEqual(sum(row["kind"] == "result" and row["text"] == result["outcome"]
                             for row in visit["log"]), 1)
        self.assertEqual(visit["calls"], calls)
        self.assertEqual(len(self.b_actor.calls), 1)

    async def test_saved_turn_final_state_read_has_bounded_recovery(self):
        service, visit, _ = await self.saved_host()
        service.config = replace(service.config, timeout_seconds=.01)
        original, reads = self.b_actor.describe, 0
        async def describe(scope):
            nonlocal reads
            reads += 1
            if reads % 2 == 0:
                await asyncio.Future()
            return await original(scope)
        calls = copy.deepcopy(visit["calls"])
        self.now += 65
        with patch.object(self.b_actor, "describe", side_effect=describe):
            for attempt, wait in ((1, 30), (2, 60), (3, 0)):
                await self.tick(service)
                if attempt < 3:
                    self.assertEqual(visit["model_retry"]["category"], "state")
                    self.assertEqual(visit["model_retry"]["attempts"], attempt)
                    self.now += wait
        self.assertEqual(visit["status"], "interrupted")
        self.assertEqual(visit["calls"], calls)
        self.assertEqual(len(self.b_actor.calls), 1)

    async def test_saved_event_cannot_cross_a_persona_change_during_final_recheck(self):
        service, visit, _ = await self.saved_host("scene")
        original, reads = self.b_actor.describe, 0
        async def describe(scope):
            nonlocal reads
            reads += 1
            local = await original(scope)
            if reads == 2:
                local["persona_key"] = "changed-persona"
            return local
        log, calls = copy.deepcopy(visit["log"]), copy.deepcopy(visit["calls"])
        self.now += 65
        with patch.object(self.b_actor, "describe", side_effect=describe):
            await self.tick(service)
        self.assertEqual(visit["status"], "interrupted")
        self.assertIn("身份", visit["end_reason"])
        self.assertEqual(visit["log"], log)
        self.assertEqual(visit["calls"], calls)

    async def test_saved_turn_cancel_during_recheck_does_not_turn_into_failure(self):
        service, visit, _ = await self.saved_host()
        async def cancelled(scope):
            raise asyncio.CancelledError()
        with patch.object(self.b_actor, "describe", side_effect=cancelled):
            with self.assertRaises(asyncio.CancelledError):
                await service._decide(visit["id"], "turn")
        self.assertEqual(visit["status"], "active")
        self.assertNotIn("model_retry", visit)
        self.assertEqual(visit["turns"][visit["host_id"]], 0)

    async def saved_guest(self):
        visit, _ = self.pair(arrived=True)
        result = await self.a._decide(visit["id"], "turn")
        visit["unsent_turn"] = True
        await self.a._save()
        service = await self.reload_service(self.a)
        return service, service._state["visits"][visit["id"]], result

    async def test_guest_unsent_turn_rechecks_readiness_without_a_second_generation(self):
        service, visit, result = await self.saved_guest()
        model_calls = list(self.a_actor.calls)
        self.a_actor.ready = False
        self.now += 65
        await self.tick(service)
        self.assertEqual(visit["turns"][visit["guest_id"]], 0)
        self.assertTrue(visit["unsent_turn"])
        self.assertEqual(visit["model_retry"]["category"], "life_busy")
        self.a_actor.ready = True
        self.now += 30
        await self.tick(service)
        self.assertEqual(visit["turns"][visit["guest_id"]], 1)
        self.assertEqual(sum(row["text"] == result["say"] for row in visit["log"]), 1)
        self.assertEqual(self.a_actor.calls, model_calls)
        self.assertNotIn("unsent_turn", visit)

    async def test_unsent_turn_without_original_phase_proof_is_not_regenerated(self):
        service, visit, result = await self.saved_guest()
        visit.pop("decision_cursor")
        await service._save()
        model_calls = list(self.a_actor.calls)
        self.now += 65
        await self.tick(service)
        self.assertEqual(visit["status"], "interrupted")
        self.assertNotIn(result["say"], str(visit["log"]))
        self.assertEqual(self.a_actor.calls, model_calls)

    async def test_already_delivered_guest_turn_keeps_its_receipt_when_she_becomes_busy(self):
        service, visit, _ = await self.saved_guest()
        original = self.a_actor.describe
        fail_next, requests = False, []
        async def describe(scope):
            nonlocal fail_next
            if fail_next:
                fail_next = False
                raise OSError("temporary receipt recheck failure")
            return await original(scope)
        async def request(url, body):
            nonlocal fail_next
            requests.append(copy.deepcopy(body))
            response = await self.b.receive(body)
            if body["payload"]["op"] == "step" and len([r for r in requests if r["payload"]["op"] == "step"]) == 1:
                fail_next = True
            return response
        service.transport.request = request
        model_calls = list(self.a_actor.calls)
        self.now += 65
        with patch.object(self.a_actor, "describe", side_effect=describe):
            await self.tick(service)
            self.assertTrue(visit["outbox"])
            self.a_actor.ready = False
            self.now += 30
            await self.tick(service)
        steps = [row for row in requests if row["payload"]["op"] == "step"]
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0], steps[1])
        self.assertEqual(visit["turns"][visit["guest_id"]], 1)
        self.assertEqual(self.b._state["visits"][visit["id"]]["turns"][visit["guest_id"]], 1)
        self.assertEqual(self.a_actor.calls, model_calls)
        self.assertNotIn("unsent_turn", visit)
        self.assertNotIn("outbox", visit)
