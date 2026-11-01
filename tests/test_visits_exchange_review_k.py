"""Exchange state reads recover without losing paid output or blocking other lives."""
from __future__ import annotations

import asyncio
import copy
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from core.visits.ports import VisitStateError
from core.visits import VisitService
from tests import test_visits_lifecycle_races as races
from tests import test_visits_protocol as protocol


class ExchangeStateReadTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = races.VisitLifecycleRaceTests.asyncSetUp
    pair = races.VisitLifecycleRaceTests.pair
    exchange = races.VisitLifecycleRaceTests.exchange
    successes = races.VisitLifecycleRaceTests.successes

    async def test_repeated_pre_and_post_exchange_failures_keep_their_streak(self):
        for checkpoint in ("before", "after"):
            with self.subTest(checkpoint=checkpoint):
                visit, _ = self.pair(automatic=True)
                reads = 0
                description = self.a_actor.describe
                async def describe(scope):
                    nonlocal reads
                    reads += 1
                    if reads % (2 if checkpoint == "before" else 3) == 0:
                        raise OSError("private state read details")
                    return await description(scope)

                calls, log = copy.deepcopy(visit["calls"]), copy.deepcopy(visit["log"])
                with patch.object(self.a_actor, "describe", side_effect=describe), \
                     patch.object(self.a, "_automatic", new=AsyncMock()) as automatic:
                    for attempt, wait in ((1, 30), (2, 60), (3, 0)):
                        await self.a.tick()
                        automatic.assert_awaited()
                        self.assertEqual(visit["calls"], calls)
                        self.assertEqual(visit["log"], log)
                        self.assertEqual(self.successes(visit), 0)
                        if attempt < 3:
                            retry = visit["state_retry"]
                            self.assertEqual(retry["attempts"], attempt)
                            self.assertEqual(retry["checkpoint"], "exchange")
                            self.assertEqual(retry["next_at"], self.now + wait)
                            self.assertNotIn("network_failures", visit)
                            before = reads
                            await self.a.tick()
                            self.assertEqual(reads, before)
                            self.now += wait
                    self.assertEqual(visit["status"], "interrupted")
                    self.assertIn("生活状态", visit["end_reason"])
                    self.assertNotIn("private state read details", str(self.a._visible(visit)))
                # Allow another independent case's automatic admission.
                self.now += 4000

    async def test_failed_reply_recheck_preserves_outbox_until_actual_recovery(self):
        visit, _ = self.pair()
        reader = self.a_actor.describe
        reads, requests = 0, []
        async def describe(scope):
            nonlocal reads
            reads += 1
            if reads == 3:
                raise OSError("temporary state failure")
            return await reader(scope)

        async def request(url, body):
            requests.append(copy.deepcopy(body))
            return await self.exchange(url, body)

        self.a.transport.request = request
        with patch.object(self.a_actor, "describe", side_effect=describe), \
             patch.object(self.a, "_automatic", new=AsyncMock()):
            await self.a.tick()
            saved = copy.deepcopy(visit["outbox"])
            self.assertEqual(visit["status"], "inviting")
            self.now += 30
            await self.a.tick()
        self.assertEqual(requests[0], saved)
        self.assertEqual(requests[1], saved)
        self.assertEqual(visit["status"], "active")
        self.assertNotIn("state_retry", visit)
        self.assertNotIn("outbox", visit)
        self.assertEqual(self.a_actor.calls, [])

    async def test_exchange_streak_survives_alternating_tick_failures_and_reload(self):
        for boundary in ("before", "after"):
            with self.subTest(boundary=boundary):
                visit, _ = self.pair(automatic=True)
                reader, requests = self.a_actor.describe, []
                calls, log = copy.deepcopy(visit["calls"]), copy.deepcopy(visit["log"])
                async def request(url, body):
                    requests.append(copy.deepcopy(body))
                    return await self.exchange(url, body)
                self.a.transport.request = request
                for index, failure in enumerate(("exchange", "tick", "exchange", "tick", "exchange")):
                    reads = 0
                    target = 1 if failure == "tick" else 2 if boundary == "before" else 3
                    async def describe(scope):
                        nonlocal reads
                        reads += 1
                        if reads == target:
                            raise OSError("temporary state read failure")
                        return await reader(scope)
                    with patch.object(self.a_actor, "describe", side_effect=describe), \
                         patch.object(self.a, "_automatic", new=AsyncMock()):
                        await self.a.tick()
                        self.assertEqual(visit["calls"], calls)
                        self.assertEqual(visit["log"], log)
                        self.assertEqual(self.successes(visit), 0)
                        if index == 4:
                            self.assertEqual(visit["status"], "interrupted")
                            self.assertIn("生活状态", visit["end_reason"])
                            self.assertNotIn("state_retry", visit)
                            break
                        retry = visit["state_retry"]
                        exchange_attempts = 1 if index < 2 else 2
                        self.assertEqual(retry["checkpoints"]["exchange"]["attempts"], exchange_attempts)
                        self.assertEqual("tick" in retry["checkpoints"], failure == "tick")
                        wait = 60 if failure == "exchange" and index == 2 else 30
                        self.assertEqual(retry["next_at"], self.now + wait)
                        self.assertEqual(self.a._visible(visit)["model_wait"]["next_at"], self.now + wait)
                        await self.a.tick()
                        self.assertEqual(reads, target)
                        self.now += wait
                    if index == 1:
                        retry, outbox = copy.deepcopy(visit["state_retry"]), copy.deepcopy(visit.get("outbox"))
                        await self.a.close()
                        self.a = VisitService(self.a.db, self.a.config, self.a_actor,
                                              clock=lambda: self.now, rng=lambda: .9)
                        self.addAsyncCleanup(self.a.close)
                        await self.a._load()
                        self.a.transport.request = request
                        visit = self.a._state["visits"][visit["id"]]
                        self.assertEqual(visit["state_retry"], retry)
                        self.assertEqual(visit.get("outbox"), outbox)
                if boundary == "after":
                    self.assertEqual(len(requests), 3)
                    self.assertEqual(len({item["message_id"] for item in requests}), 1)
                else:
                    self.assertEqual(requests, [])
                self.assertEqual(self.a_actor.calls, [])
                self.now += 4000

    async def test_legacy_step_waits_survive_a_failed_tick_and_clear_after_recovery(self):
        for legacy in ({"attempts": 1, "next_at": self.now},
                       {"attempts": 1, "next_at": self.now, "checkpoint": "exchange"}):
            with self.subTest(legacy=legacy):
                visit, _ = self.pair()
                visit["state_retry"] = {**legacy, "next_at": self.now}
                own = self.a._local_id(visit)
                calls = visit["calls"][own]
                with patch.object(self.a_actor, "describe", side_effect=OSError("not ready")), \
                     patch.object(self.a, "_automatic", new=AsyncMock()):
                    await self.a.tick()
                retry = visit["state_retry"]
                if "checkpoint" in legacy:
                    self.assertEqual(retry["checkpoints"]["exchange"]["attempts"], 1)
                    self.assertEqual(retry["attempts"], 1)
                else:
                    self.assertEqual(retry["attempts"], 2)
                self.now = retry["next_at"]
                with patch.object(self.a, "_automatic", new=AsyncMock()):
                    await self.a.tick()
                self.assertEqual(visit["status"], "active")
                self.assertNotIn("state_retry", visit)
                self.assertEqual(visit["calls"][own], calls)
                self.assertEqual(self.a_actor.calls, [])
                self.now += 4000

    async def test_direct_exchange_clears_only_its_own_checkpoint(self):
        visit, _ = self.pair()
        visit["state_retry"] = {
            "attempts": 1, "next_at": self.now, "checkpoint": "exchange", "checkpoints": {
                "tick": {"attempts": 2, "next_at": self.now},
                "exchange": {"attempts": 1, "next_at": self.now}}}
        await self.a._send(visit["id"], {"op": "status"})
        self.assertEqual(visit["state_retry"]["checkpoint"], "tick")
        self.assertEqual(visit["state_retry"]["attempts"], 2)
        self.assertNotIn("exchange", visit["state_retry"]["checkpoints"])
        self.assertNotIn("outbox", visit)
        self.assertEqual(self.a_actor.calls, [])

    async def test_each_exchange_read_has_a_deadline_and_releases_the_lock(self):
        for checkpoint in ("before", "after"):
            with self.subTest(checkpoint=checkpoint):
                visit, _ = self.pair()
                reader, reads = self.a_actor.describe, 0
                async def describe(scope):
                    nonlocal reads
                    reads += 1
                    if reads == (1 if checkpoint == "before" else 2):
                        await asyncio.Future()
                    return await reader(scope)

                with patch.object(self.a_actor, "describe", side_effect=describe), \
                     patch.object(self.a, "config", replace(self.a.config, timeout_seconds=.01)):
                    with self.assertRaises(VisitStateError):
                        await asyncio.wait_for(self.a._send(visit["id"], {"op": "status"}), .2)
                self.assertFalse(self.a._lock.locked())
                self.assertEqual(visit["status"], "inviting")

    async def test_exchange_cancellation_propagates_without_changing_retry(self):
        visit, _ = self.pair()
        with patch.object(self.a_actor, "describe", side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await self.a._send(visit["id"], {"op": "status"})
        self.assertFalse(self.a._lock.locked())
        self.assertNotIn("state_retry", visit)


class ProposalExchangeReadTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = protocol.VisitProtocolTests.asyncSetUp

    async def test_initial_send_failure_waits_and_reuses_the_saved_invitation(self):
        sender = self.a._send
        first = True
        async def send(visit_id, payload, *args, **kwargs):
            nonlocal first
            if first:
                first = False
                with patch.object(self.a_actor, "describe", side_effect=OSError("unavailable")):
                    return await sender(visit_id, payload, *args, **kwargs)
            return await sender(visit_id, payload, *args, **kwargs)

        with patch.object(self.a, "_send", side_effect=send):
            result = await self.a.invite("life-a", self.peer)
            visit = self.a._state["visits"][result["id"]]
            log, calls = copy.deepcopy(visit["log"]), copy.deepcopy(visit["calls"])
            self.assertEqual(visit["proposal_stage"], "prepared")
            self.assertEqual(visit["state_retry"]["next_at"], self.now + 30)
            self.assertEqual(self.b_actor.calls, [])
            self.now += 30
            await self.a.tick()
        self.assertEqual(visit["proposal_stage"], "sent")
        self.assertEqual(visit["log"], log)
        self.assertEqual(visit["calls"], calls)
        self.assertNotIn("state_retry", visit)
        self.assertEqual(len(self.a_actor.calls), 1)

    async def test_repeated_prepared_send_failure_finishes_after_two_cooldowns(self):
        sender = self.a._send
        async def send(visit_id, payload, *args, **kwargs):
            with patch.object(self.a_actor, "describe", side_effect=OSError("unavailable")):
                return await sender(visit_id, payload, *args, **kwargs)

        with patch.object(self.a, "_send", side_effect=send):
            result = await self.a.invite("life-a", self.peer)
            visit = self.a._state["visits"][result["id"]]
            for wait in (30, 60):
                self.now += wait
                await self.a.tick()
        self.assertEqual(visit["status"], "interrupted")
        self.assertEqual(len(self.a_actor.calls), 1)
        self.assertEqual(self.b_actor.calls, [])
        self.assertEqual(self.b._state["visits"], {})
