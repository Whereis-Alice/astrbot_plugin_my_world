"""An occupied friend's phone answers safely through authenticated binding and offers."""
from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path
import unittest

from tests import test_visits_global_pairing as fixture
from core.db import AsyncDatabase
from core.visits import VisitService
from core.visits.protocol import envelope, public_actor, verify
from core.visits.transport import VisitTransportError


class VisitOccupiedContactReviewTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.GlobalPairingTests.asyncSetUp

    async def third_friend(self):
        actor = fixture.LivesActor("c")
        scope = next(iter(actor.rows))
        db = AsyncDatabase(Path(self.directory.name) / "c.db")
        self.addAsyncCleanup(db.close)
        service = VisitService(db, self.a.config, actor, clock=lambda: self.now, rng=lambda: .9)
        await service.start()
        service.config = replace(service.config,
            public_url=f"http://127.0.0.1:{service.transport.bound_port}")
        self.addAsyncCleanup(service.close)
        await service.set_enabled_scopes([scope])
        code = await self.b.create_pair_code()
        await service.pair_world(self.b.config.public_url, code["code"])
        return service, actor, scope

    async def occupy(self, *, arrived):
        await self.a._prepare_peer(self.a1, self.peer)
        third, actor, scope = await self.third_friend()
        invitation = await third.invite(scope, self.peer)
        if arrived:
            self.now += 65
            await self.b.tick()
        current = self.b._state["visits"][invitation["id"]]
        self.assertEqual(current["status"], "active" if arrived else "offered")
        return third, actor, scope, copy.deepcopy(current)

    async def test_existing_friend_gets_signed_busy_reply_in_both_directions(self):
        _, _, _, occupied = await self.occupy(arrived=True)
        before_calls = list(self.b_actor.calls)
        for direction in ("outgoing", "incoming"):
            with self.subTest(direction=direction):
                visit = await self.a.invite(self.a1, self.peer, direction=direction)
                self.assertEqual(visit["status"], "declined")
                self.assertEqual(visit["end_reason"], "我现在正和友邻相处，稍后再约")
                self.assertEqual(visit["contact_error"], "")
                self.assertNotIn("arrived_at", visit)
                self.assertEqual(visit["calls_used"], 1)
                self.assertEqual(visit["log"][-1]["kind"], "contact_status")
                self.assertEqual(visit["log"][-1]["actor_id"], self.peer)
                self.assertEqual(visit["log"][-1]["text"], visit["end_reason"])
                self.assertEqual(self.b.active(self.b1)["id"], occupied["id"])
                self.assertEqual(self.b._state["visits"][occupied["id"]], occupied)
        self.assertEqual(self.b_actor.calls, before_calls)

    async def test_pending_friend_contact_refuses_without_spending_automatic_success(self):
        _, _, _, occupied = await self.occupy(arrived=False)
        self.a.config = replace(self.a.config, auto_enabled=True, auto_probability=1)
        visit = await self.a.invite(self.a1, self.peer, automatic=True)
        self.assertEqual(visit["status"], "declined")
        self.assertEqual(visit["end_reason"], "我还有一通联系在等回应，稍后再约")
        self.assertEqual(visit["contact_error"], "")
        row = self.a._state["visits"][visit["id"]]
        self.assertEqual(row.get("network_failures"), 0)
        day = self.a._state["draws"][self.a1]["peer_days"][self.connection["id"]]["days"]["2026-11-01"]
        self.assertEqual(day["successes"], 0)
        self.assertEqual(self.b._state["visits"][occupied["id"]], occupied)
        self.assertEqual(self.b_actor.calls, [])

    async def test_occupied_world_revalidates_same_binding_but_rejects_changed_binding(self):
        _, _, _, occupied = await self.occupy(arrived=True)
        binding = self.last_request("world_bind")
        own = public_actor(await self.a_actor.describe(self.a1), self.a._state["instance_id"])["id"]
        before = copy.deepcopy(self.b._find_peer(self.b1, own))
        anchor = self.a._state["connections"][self.connection["id"]]["anchors"][0]
        repeat = envelope(anchor["secret"], binding["sender"], binding["target"], binding["payload"], self.now)
        response = await self.a.transport.request(self.b.config.public_url, repeat)
        self.assertEqual(verify(response, anchor["secret"], self.now)["actor"]["id"], self.peer)
        self.assertEqual(self.b._find_peer(self.b1, own), before)
        changed = envelope(anchor["secret"], binding["sender"], binding["target"],
            {**binding["payload"], "actor_token": "0" * 64}, self.now)
        with self.assertRaises(VisitTransportError):
            await self.a.transport.request(self.b.config.public_url, changed)
        self.assertEqual(self.b._find_peer(self.b1, own), before)
        self.assertEqual(self.b._state["visits"][occupied["id"]], occupied)

    async def test_occupied_world_cannot_create_a_new_life_binding(self):
        _, _, _, occupied = await self.occupy(arrived=True)
        own = public_actor(await self.a_actor.describe(self.a2), self.a._state["instance_id"])["id"]
        self.assertIsNone(self.b._find_peer(self.b1, own))
        with self.assertRaises(VisitTransportError):
            await self.a._prepare_peer(self.a2, self.peer)
        self.assertIsNone(self.b._find_peer(self.b1, own))
        self.assertEqual(self.b._state["visits"][occupied["id"]], occupied)
        self.assertEqual(self.a_actor.calls, [])

    async def test_signed_busy_reply_does_not_send_goodbyes_for_an_unknown_trip(self):
        await self.occupy(arrived=True)
        visit = await self.a.invite(self.a1, self.peer)
        self.assertEqual(visit["status"], "declined")
        sent = len(self.messages)
        for _ in range(3):
            self.now += 601
            await self.a.tick()
        self.assertEqual(self.messages[sent:], [])
        self.assertNotIn("end_pending", self.a._state["visits"][visit["id"]])
        self.assertEqual(len(self.a_actor.calls), 1)

    last_request = fixture.GlobalPairingTests.last_request

