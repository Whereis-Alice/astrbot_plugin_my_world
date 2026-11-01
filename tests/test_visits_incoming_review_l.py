"""Incoming state reads are bounded and use the final availability snapshot."""
from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
import unittest
from unittest.mock import AsyncMock, patch

from core.visits import VisitConfig, VisitService
from core.visits.ports import VisitStateError
from core.visits.protocol import code_id, envelope, public_actor, verify
from tests import test_visits_global_pairing as global_fixture
from tests import test_visits_lifecycle_races as fixture


class IncomingStateReadTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.VisitLifecycleRaceTests.asyncSetUp
    exchange = fixture.VisitLifecycleRaceTests.exchange

    def offer(self, *, role="host", visit_id="a" * 32):
        return envelope("test-key", self.reverse["id"], self.peer["id"], {
            "op": "offer", "id": visit_id, "role": role, "reason": "喝茶",
            "say": "来喝茶吗", "max_calls": 12, "turn_limit": 3, "duration": 1800,
            "calls": 1,
        }, self.now)

    async def test_final_availability_refuses_both_directions_without_creating_a_journey(self):
        original = self.b_actor.describe
        changes = (
            ({"ready": False, "ready_reason": "现在需要休息"}, "现在需要休息"),
            ({"chat_busy": True}, "眼前还有交谈，稍后再约"),
            ({"reception_open": False}, "现在不在接待时间，稍后再约"),
        )
        for role in ("host", "guest"):
            for change, reason in changes:
                with self.subTest(role=role, change=change):
                    reads = 0
                    async def describe(scope):
                        nonlocal reads
                        reads += 1
                        local = await original(scope)
                        return {**local, **change} if reads >= 2 else local

                    before = copy.deepcopy(self.b._state["visits"])
                    body = self.offer(role=role)
                    with patch.object(self.b_actor, "describe", side_effect=describe):
                        response = await self.b.receive(body)
                        repeated = await self.b.receive(body)
                    reply = verify(response, "test-key", self.now)
                    self.assertEqual(reply["declined"], True)
                    self.assertEqual(reply["reason"], reason)
                    self.assertEqual(reply["contact"], {"kind": "contact_status", "text": reason})
                    self.assertEqual(response, repeated)
                    self.assertEqual(self.b._state["visits"], before)
                    self.assertEqual(self.b_actor.calls, [])
                    self.assertEqual(self.b_actor.memories, {})

    async def test_final_identity_change_still_rejects_instead_of_answering_as_another_actor(self):
        original, reads = self.b_actor.describe, 0
        async def describe(scope):
            nonlocal reads
            reads += 1
            local = await original(scope)
            return {**local, "persona_key": "changed"} if reads >= 2 else local

        with patch.object(self.b_actor, "describe", side_effect=describe):
            with self.assertRaisesRegex(ValueError, "身份"):
                await self.b.receive(self.offer())
        self.assertEqual(self.b._state["visits"], {})
        self.assertEqual(self.b._state["receipts"], {})
        self.assertEqual(self.b_actor.calls, [])

    async def assert_read_deadline(self, boundary):
        body = self.offer()
        if boundary == "cached":
            await self.b.receive(body)
        before = copy.deepcopy(self.b._state)
        original, entered, reads = self.b_actor.describe, asyncio.Event(), 0
        async def describe(scope):
            nonlocal reads
            reads += 1
            target = 1 if boundary in {"first", "cached"} else 2
            if reads == target:
                entered.set()
                await asyncio.Future()
            return await original(scope)

        with patch.object(self.b_actor, "describe", side_effect=describe), \
             patch.object(self.b, "config", replace(self.b.config, timeout_seconds=.02)):
            incoming = asyncio.create_task(self.b.receive(body))
            await asyncio.wait_for(entered.wait(), .3)
            with self.assertRaisesRegex(VisitStateError, "生活状态"):
                await asyncio.wait_for(incoming, .3)
            other = await asyncio.wait_for(self.b.overview("other-life"), .3)
        self.assertEqual(other["active"], None)
        self.assertFalse(self.b._lock.locked())
        self.assertEqual(self.b._state, before)
        self.assertEqual(self.b_actor.calls, [])
        # A timeout retains credentials and receipts; retry can reuse them.
        if boundary == "cached":
            response = await self.b.receive(body)
            self.assertEqual(response, next(iter(before["receipts"].values()))["response"])
        else:
            self.assertEqual(verify(await self.b.receive(body), "test-key", self.now)["visit"]["status"], "offered")

    async def test_first_incoming_read_has_a_deadline(self):
        await self.assert_read_deadline("first")

    async def test_final_incoming_read_has_a_deadline_and_releases_the_shared_lock(self):
        await self.assert_read_deadline("final")

    async def test_cached_identity_read_has_a_deadline_and_preserves_the_receipt(self):
        await self.assert_read_deadline("cached")

    async def test_cached_read_cancellation_propagates_without_editing_state(self):
        body = self.offer()
        await self.b.receive(body)
        before = copy.deepcopy(self.b._state)
        with patch.object(self.b_actor, "describe", side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await self.b.receive(body)
        self.assertFalse(self.b._lock.locked())
        self.assertEqual(self.b._state, before)

    async def test_legacy_pair_preparation_and_receiving_keep_their_code_after_a_read_timeout(self):
        code = await self.b.create_pair_code("life-b")
        remote = public_actor(self.local, self.a._state["instance_id"])
        body = envelope(code["code"], remote["id"], "pair:" + code_id(code["code"]),
                        {"op": "pair", "actor": remote, "url": ""}, self.now)
        before = copy.deepcopy(self.b._state)
        async def unavailable(scope):
            await asyncio.Future()

        with patch.object(self.b_actor, "describe", side_effect=unavailable), \
             patch.object(self.b, "config", replace(self.b.config, timeout_seconds=.01)):
            with self.assertRaises(VisitStateError):
                await asyncio.wait_for(self.b.create_pair_code("life-b"), .3)
            with self.assertRaises(VisitStateError):
                await asyncio.wait_for(self.b.receive(body), .3)
            with patch.object(self.b.transport, "request", new=AsyncMock()) as request:
                with self.assertRaises(VisitStateError):
                    await asyncio.wait_for(self.b.pair("life-b", "https://unused.example", code["code"]), .3)
                request.assert_not_awaited()
        self.assertEqual(self.b._state, before)
        self.assertFalse(self.b._lock.locked())
        self.assertEqual(self.b_actor.calls, [])


class DirectoryStateReadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = 1793505600.0
        self.actor = global_fixture.LivesActor("local")
        self.service = VisitService(fixture.MemoryDb(), VisitConfig(enabled=True, auto_enabled=False),
                                    self.actor, clock=lambda: self.now)
        self.addAsyncCleanup(self.service.close)
        await self.service._load()
        self.scopes = list(self.actor.rows)
        await self.service.set_enabled_scopes(self.scopes)
        self.remote = "f" * 32
        await self.service._store_world(self.remote, "https://unused.example", "test-key", [])
        await self.service._save()

    def request(self):
        return envelope("test-key", self.remote, self.service._state["instance_id"],
                        {"op": "world_catalog"}, self.now)

    async def test_complete_directory_has_one_deadline_not_one_allowance_per_life(self):
        original, reads = self.actor.describe, []
        async def delayed(scope):
            reads.append(scope)
            await asyncio.sleep(.018)
            return await original(scope)

        before = copy.deepcopy(self.service._state)
        with patch.object(self.actor, "describe", side_effect=delayed), \
             patch.object(self.service, "config", replace(self.service.config, timeout_seconds=.03)):
            with self.assertRaises(VisitStateError):
                await asyncio.wait_for(self.service._catalog_snapshot(), .3)
        self.assertLess(len(reads), len(self.scopes))
        self.assertEqual(self.service._state, before)
        self.assertEqual(self.actor.calls, [])

    async def test_broken_life_does_not_publish_a_partial_directory(self):
        original = self.actor.describe
        async def describe(scope):
            if scope == self.scopes[1]:
                raise OSError("private state diagnostic")
            return await original(scope)

        before = copy.deepcopy(self.service._state)
        with patch.object(self.actor, "describe", side_effect=describe):
            with self.assertRaises(VisitStateError) as caught:
                await self.service._catalog_snapshot()
        self.assertNotIn("private state diagnostic", str(caught.exception))
        self.assertEqual(self.service._state, before)

    async def test_control_directory_recheck_releases_lock_without_caching_a_failed_response(self):
        original, reads, entered = self.actor.describe, 0, asyncio.Event()
        async def describe(scope):
            nonlocal reads
            reads += 1
            if reads == len(self.scopes) + 1:
                entered.set()
                await asyncio.Future()
            return await original(scope)

        before = copy.deepcopy(self.service._state)
        with patch.object(self.actor, "describe", side_effect=describe), \
             patch.object(self.service, "config", replace(self.service.config, timeout_seconds=.02)):
            request = asyncio.create_task(self.service.receive(self.request()))
            await asyncio.wait_for(entered.wait(), .3)
            with self.assertRaises(VisitStateError):
                await asyncio.wait_for(request, .3)
            await asyncio.wait_for(self.service.overview("other-life"), .3)
        self.assertFalse(self.service._lock.locked())
        self.assertEqual(self.service._state, before)
        self.assertEqual(self.actor.calls, [])

    async def test_directory_cancellation_propagates(self):
        before = copy.deepcopy(self.service._state)
        with patch.object(self.actor, "describe", side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await self.service._catalog_snapshot()
        self.assertEqual(self.service._state, before)

    async def test_changed_participation_remains_an_identity_rejection(self):
        original = self.actor.describe
        async def describe(scope):
            local = await original(scope)
            if scope == self.scopes[0]:
                self.actor.owners[scope] = self.scopes[1]
            return local

        with patch.object(self.actor, "describe", side_effect=describe):
            with self.assertRaisesRegex(ValueError, "参与状态") as caught:
                await self.service._catalog_snapshot()
        self.assertNotIsInstance(caught.exception, VisitStateError)
