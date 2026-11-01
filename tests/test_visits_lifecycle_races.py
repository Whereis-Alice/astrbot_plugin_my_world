"""Expiry and queued goodbyes cannot manufacture or overwrite a confirmed visit."""
from __future__ import annotations

import asyncio
import copy
import unittest
from unittest.mock import AsyncMock, patch

from tests import test_visits_protocol as fixture
from core.visits import VisitConfig, VisitService
from core.visits.protocol import envelope, public_actor


class MemoryDb:
    def __init__(self):
        self.state = {}

    async def call(self, operation, key, value):
        if operation == "kv_get":
            return copy.deepcopy(self.state.get(key, value))
        self.state[key] = copy.deepcopy(value)


class VisitLifecycleRaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = 1793505600.0
        self.a_actor, self.b_actor = fixture.Actor("guest"), fixture.Actor("host")
        config = VisitConfig(enabled=True, auto_enabled=True)
        self.a = VisitService(MemoryDb(), config, self.a_actor, clock=lambda: self.now, rng=lambda: .9)
        self.b = VisitService(MemoryDb(), config, self.b_actor, clock=lambda: self.now, rng=lambda: .9)
        await self.a._load()
        await self.b._load()
        self.addAsyncCleanup(self.a.close)
        self.addAsyncCleanup(self.b.close)
        self.local = await self.a_actor.describe("life-a")
        self.remote = await self.b_actor.describe("life-b")
        self.peer = self.a._add_peer("life-a", self.local,
                                     public_actor(self.remote, self.b._state["instance_id"]),
                                     "http://not-contacted.example", "test-key")
        self.reverse = self.b._add_peer("life-b", self.remote,
                                        public_actor(self.local, self.a._state["instance_id"]),
                                        "http://not-contacted.example", "test-key")
        self.a.transport.request = self.exchange

    async def exchange(self, url, outgoing):
        async with self.b._lock:
            response = await self.b._inbox(self.reverse, self.remote, outgoing["payload"])
        return envelope("test-key", self.peer["id"], outgoing["sender"],
                        {**response, "reply_to": outgoing["message_id"]}, self.now)

    def pair(self, *, arrived=False, automatic=False):
        if automatic:
            self.assertTrue(self.a._admit_automatic("life-a", self.peer, self.local))
        visit = self.a._new_visit("life-a", self.peer, self.local, "a" * 32, "guest", "喝茶", automatic)
        host = self.b._new_visit("life-b", self.reverse, self.remote, visit["id"], "host", "喝茶", False)
        visit["calls"][visit["guest_id"]] = 1
        host["calls"][host["host_id"]] = 1
        self.a._append(visit, visit["guest_id"], "invitation", "来喝茶吗")
        host["log"] = copy.deepcopy(visit["log"])
        host["scene"] = {"place": "客厅", "activity": "喝茶", "event": ""}
        self.a._state["visits"][visit["id"]] = visit
        self.b._state["visits"][visit["id"]] = host
        self.b._activate(host)
        if arrived:
            self.a._synchronize(visit, self.b._wire(host), visit["host_id"])
        return visit, host

    def successes(self, visit):
        peer = self.a._rhythm_peer_id(visit)
        return self.a._state["draws"]["life-a"]["peer_days"][peer]["days"]["2026-11-01"]["successes"]

    async def test_queued_return_home_preserves_an_incoming_completed_goodbye(self):
        visit, host = self.pair(arrived=True)
        confirmed = self.a._wire(visit)
        confirmed.update(status="completed", end_reason="访客已告别归家")
        observed = {}

        async def incoming():
            async with self.b._lock:
                await self.b._inbox(self.reverse, self.remote,
                                    {"op": "end", "id": host["id"], "visit": confirmed})
                observed["reason"] = host["end_reason"]

        with patch.object(self.b, "_notify_end", new=AsyncMock()), patch.object(self.b, "_remember", new=AsyncMock()):
            await self.b._lock.acquire()
            returning = asyncio.create_task(self.b.return_home("life-b"))
            await asyncio.sleep(0)  # Queue return_home's _load, then the incoming goodbye.
            incoming_task = asyncio.create_task(incoming())
            await asyncio.sleep(0)
            self.b._lock.release()
            result, _ = await asyncio.gather(returning, incoming_task)
        self.assertEqual(host["status"], "completed")
        self.assertEqual(host["end_reason"], observed["reason"])
        self.assertEqual(result["status"], "completed")
        self.assertNotIn("end_pending", host)

    async def test_reply_crossing_expiry_cannot_arrive_or_consume_daily_success(self):
        visit, _ = self.pair(automatic=True)

        async def delayed(url, outgoing):
            response = await self.exchange(url, outgoing)
            if outgoing["payload"]["op"] == "status":
                self.now = visit["expires_at"] + 1
            return response

        self.a.transport.request = delayed
        with patch.object(self.a, "_automatic", new=AsyncMock()):
            await self.a.tick()
        self.assertEqual(visit["status"], "expired")
        self.assertNotIn("arrived_at", visit)
        self.assertEqual(self.a.presence_epoch("life-a"), 0)
        self.assertEqual(self.successes(visit), 0)
        # A final acknowledgement also cannot turn this expired invitation into a first arrival.
        await self.a._notify_end(visit["id"])
        await self.a._remember(visit["id"])
        self.assertEqual(visit["status"], "expired")
        self.assertNotIn("arrived_at", visit)
        self.assertEqual(self.successes(visit), 0)
        self.assertEqual(self.a_actor.memories, {})

    async def test_send_rechecks_expiry_after_waiting_for_local_description(self):
        visit, _ = self.pair()
        original = self.a_actor.describe

        async def delayed(scope):
            local = await original(scope)
            self.now = visit["expires_at"] + 1
            return local

        with patch.object(self.a_actor, "describe", side_effect=delayed), patch.object(self.a.transport, "request", new=AsyncMock()) as request:
            with self.assertRaisesRegex(ValueError, "到期"):
                await self.a._send(visit["id"], {"op": "status"})
            request.assert_not_awaited()
        self.assertEqual(visit["status"], "expired")
        self.assertNotIn("outbox", visit)

    async def test_late_incoming_goodbye_cannot_confirm_first_arrival_after_expiry(self):
        visit, host = self.pair(automatic=True)
        remote = self.b._wire(host)
        remote.update(status="completed", ended_at=self.now)
        self.now = visit["expires_at"] + 1
        before = copy.deepcopy(visit)
        async with self.a._lock:
            with self.assertRaisesRegex(ValueError, "到访"):
                await self.a._inbox(self.peer, self.local,
                                    {"op": "end", "id": visit["id"], "visit": remote})
        self.assertEqual(visit, before)
        self.assertEqual(self.successes(visit), 0)

    async def test_existing_arrival_can_receive_final_facts_after_expiry(self):
        visit, host = self.pair(arrived=True, automatic=True)
        self.assertEqual(self.successes(visit), 1)
        self.b._append(host, host["host_id"], "say", "记得把茶带回去")
        await self.b._finish(host, "completed", "告别归家", notify=False)
        self.now = visit["expires_at"] + 1
        await self.a._finish(visit, "expired", "等待最终经过")
        await self.a._notify_end(visit["id"])
        self.assertEqual(visit["status"], "completed")
        self.assertEqual(visit["log"], host["log"])
        self.assertNotIn("end_pending", visit)
        self.assertEqual(self.successes(visit), 1)
