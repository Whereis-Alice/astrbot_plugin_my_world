"""A late identity read cannot accept work after its service was closed."""
from __future__ import annotations

import asyncio
import copy
import unittest
from unittest.mock import AsyncMock, patch

from tests import fake_astrbot

fake_astrbot.install()

from core.visits import VisitService  # noqa: E402 - install the SDK fixture first
from core.visits.persistence import KEY  # noqa: E402
from tests import test_visits_incoming_review_l as incoming  # noqa: E402


class InboxCloseTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = incoming.IncomingStateReadTests.asyncSetUp
    exchange = incoming.IncomingStateReadTests.exchange
    offer = incoming.IncomingStateReadTests.offer

    async def assert_closed_read(self, *, cached=False, queued=False):
        body = self.offer()
        if cached:
            await self.b.receive(body)
        await self.b._save()
        before = copy.deepcopy(self.b._state)
        entered, release = asyncio.Event(), asyncio.Event()
        original, reads = self.b_actor.describe, 0

        async def describe(scope):
            nonlocal reads
            reads += 1
            if reads == (1 if cached else 2):
                entered.set()
                await release.wait()
            return await original(scope)

        with patch.object(self.b_actor, "describe", side_effect=describe):
            if queued:
                await self.b._lock.acquire()
            pending = asyncio.create_task(self.b.receive(body))
            try:
                if queued:
                    await asyncio.sleep(0)  # Queue _load behind the existing lock.
                    await self.b.close()
                    self.b._lock.release()
                else:
                    await asyncio.wait_for(entered.wait(), 1)
                    await self.b.close()
                release.set()
                with self.assertRaises(ValueError):
                    await asyncio.wait_for(pending, 1)
            finally:
                release.set()
                if not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                if self.b._lock.locked():
                    self.b._lock.release()
        self.assertEqual(self.b._state, before)
        self.assertEqual(await self.b.db.call("kv_get", KEY, {}), before)
        self.assertEqual(self.b_actor.calls, [])
        self.assertEqual(self.b_actor.memories, {})

        # The new instance may accept the same request, or reuse its real receipt.
        resumed = VisitService(self.b.db, self.b.config, self.b_actor, clock=lambda: self.now)
        self.addAsyncCleanup(resumed.close)
        await resumed._load()
        response = await resumed.receive(body)
        self.assertEqual(response["payload"]["visit"]["status"], "offered")
        self.assertEqual(len(resumed._state["visits"]), 1)
        self.assertEqual(len(resumed._state["receipts"]), 1)
        if cached:
            self.assertEqual(response, next(iter(before["receipts"].values()))["response"])

    async def test_close_during_final_incoming_read_cannot_accept_an_offer(self):
        await self.assert_closed_read()

    async def test_close_during_cached_identity_read_cannot_answer_as_the_old_service(self):
        await self.assert_closed_read(cached=True)

    async def test_cached_request_queued_before_close_cannot_answer_after_close(self):
        await self.assert_closed_read(cached=True, queued=True)

    async def test_close_during_legacy_pair_description_does_not_contact_the_neighbour(self):
        code = await self.a.create_pair_code("life-a")
        before = copy.deepcopy(self.b._state)
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.b_actor.describe

        async def describe(scope):
            entered.set()
            await release.wait()
            return await original(scope)

        with patch.object(self.b_actor, "describe", side_effect=describe), \
             patch.object(self.b.transport, "request", new=AsyncMock(side_effect=ValueError("late contact"))) as request:
            pending = asyncio.create_task(self.b.pair("life-b", "https://unused.example", code["code"]))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                await self.b.close()
                release.set()
                with self.assertRaises(ValueError):
                    await asyncio.wait_for(pending, 1)
                request.assert_not_awaited()
            finally:
                release.set()
                if not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
        self.assertEqual(self.b._state, before)
        self.assertEqual(self.b_actor.calls, [])
