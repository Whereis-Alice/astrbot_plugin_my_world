"""A stale default role can recover without resetting its world's daily invitation quota."""
from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
import unittest
from unittest.mock import patch

from tests import test_visits_global_pairing as fixture

from core.visits.ports import VisitStateError
from core.visits.protocol import envelope, public_actor
from core.visits.transport import VisitTransportError


class VisitRhythmReviewTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.GlobalPairingTests.asyncSetUp
    advance = fixture.GlobalPairingTests.advance
    completed = fixture.GlobalPairingTests.completed
    last_request = fixture.GlobalPairingTests.last_request

    def requests(self, operation):
        return [body for name, body in self.messages
                if name == "a" and body["payload"]["op"] == operation]

    def current_connection(self):
        return self.a._state["connections"][self.b._state["instance_id"]]

    def entry(self):
        return self.a._state["draws"][self.a1]["peer_days"][self.b._state["instance_id"]]["days"]["2026-11-01"]

    async def test_automatic_contact_recovers_when_only_another_remote_role_stays_open(self):
        await self.a.set_enabled_scopes([self.a1])
        await self.b.set_enabled_scopes([self.b2])
        self.a.config = replace(self.a.config, auto_enabled=True, auto_probability=1, auto_interval_minutes=0)
        self.a._random = lambda: 0
        anchors = copy.deepcopy(self.current_connection()["anchors"])
        await self.a.tick()
        self.assertEqual(len(self.requests("world_bind")), 1)
        self.assertEqual(len(self.requests("world_catalog")), 1)
        self.assertEqual(self.a_actor.calls, [])
        self.assertFalse(self.a._state["visits"])
        self.assertFalse(self.a._state["draws"][self.a1]["peer_days"][self.b._state["instance_id"]]["days"])
        next_role = public_actor(await self.b_actor.describe(self.b2), self.b._state["instance_id"])["id"]
        self.assertEqual(self.current_connection()["default_actor"], next_role)
        self.assertEqual(self.current_connection()["anchors"], anchors)
        self.now += 61
        await self.a.tick()
        self.assertEqual(self.requests("world_bind")[-1]["payload"]["target_actor"], next_role)
        visit = next(iter(self.a._state["visits"].values()))
        self.assertEqual(visit["calls"][self.a._local_id(visit)], 1)
        self.assertEqual(self.entry()["successes"], 0)
        await self.advance()
        self.assertTrue(visit.get("arrived_at"))
        self.assertEqual(self.entry()["successes"], 1)
        self.assertEqual(sum(phase == "propose" for _, phase, _ in self.a_actor.calls), 1)
        self.assertEqual(len(self.requests("pair_world")), 1)

    async def test_explicit_old_role_request_refreshes_but_never_substitutes_another_role(self):
        await self.b.set_enabled_scopes([self.b2])
        with self.assertRaises(VisitTransportError) as error:
            await self.a.invite(self.a1, self.peer)
        self.assertEqual(error.exception.http_status, 400)
        self.assertEqual(len(self.requests("world_catalog")), 1)
        self.assertEqual(self.requests("world_bind")[-1]["payload"]["target_actor"], self.peer)
        self.assertFalse(self.requests("offer"))
        self.assertFalse(self.a._state["visits"])
        self.assertEqual(self.a_actor.calls, [])
        self.assertNotEqual(self.current_connection()["default_actor"], self.peer)
        with self.assertRaises(ValueError):
            await self.a.invite(self.a1, self.peer)
        self.assertEqual(len(self.requests("world_bind")), 1)
        self.assertEqual(len(self.requests("world_catalog")), 1)

    async def test_multiple_lives_share_catalog_cooldown_when_the_directory_has_not_changed(self):
        original = self.b._receive_control
        anchors, actors = copy.deepcopy(self.current_connection()["anchors"]), copy.deepcopy(self.current_connection()["actors"])

        async def reject_binding(body, receipt_key):
            if body["payload"]["op"] == "world_bind":
                raise ValueError("isolated binding rejection")
            return await original(body, receipt_key)

        with patch.object(self.b, "_receive_control", side_effect=reject_binding):
            results = await asyncio.gather(self.a._prepare_peer(self.a1, self.peer),
                                           self.a._prepare_peer(self.a2, self.peer), return_exceptions=True)
            self.assertTrue(all(isinstance(result, VisitTransportError) for result in results))
            self.assertEqual(len(self.requests("world_bind")), 2)
            self.assertEqual(len(self.requests("world_catalog")), 1)
            with self.assertRaises(VisitTransportError):
                await self.a._prepare_peer(self.a1, self.peer)
            self.assertEqual(len(self.requests("world_catalog")), 1)
            self.now += 3601
            with self.assertRaises(VisitTransportError):
                await self.a._prepare_peer(self.a1, self.peer)
        self.assertEqual(len(self.requests("world_catalog")), 2)
        self.assertEqual(self.current_connection()["anchors"], anchors)
        self.assertEqual(self.current_connection()["actors"], actors)
        self.assertFalse(self.a._state["visits"])
        self.assertEqual(self.a_actor.calls, [])

    async def test_network_and_local_state_failures_do_not_request_a_catalog(self):
        original = self.b._receive_control

        async def unavailable(body, receipt_key):
            if body["payload"]["op"] == "world_bind":
                raise VisitStateError()
            return await original(body, receipt_key)

        with patch.object(self.b, "_receive_control", side_effect=unavailable):
            with self.assertRaises(VisitTransportError) as error:
                await self.a._prepare_peer(self.a1, self.peer)
        self.assertEqual(error.exception.http_status, 503)
        with patch.object(self.a.transport, "request", side_effect=VisitTransportError("isolated network failure", category="channel")):
            with self.assertRaises(VisitTransportError):
                await self.a._prepare_peer(self.a1, self.peer)
        before = len(self.messages)
        with patch.object(self.a_actor, "describe", side_effect=VisitStateError()):
            with self.assertRaises(VisitStateError):
                await self.a._prepare_peer(self.a1, self.peer)
        self.assertEqual(len(self.messages), before)
        self.assertFalse(self.requests("world_catalog"))
        self.assertNotIn("next_catalog_at", self.current_connection())
        self.assertEqual(self.a_actor.calls, [])

    async def test_authenticated_wrong_binding_identity_is_rejected_and_catalog_is_refreshed(self):
        original = self.b._receive_control

        async def wrong_actor(body, receipt_key):
            reply = await original(body, receipt_key)
            if body["payload"]["op"] != "world_bind":
                return reply
            _, anchor = self.b._anchor_for(body)
            actor = public_actor(await self.b_actor.describe(self.b2), self.b._state["instance_id"])
            return envelope(anchor["secret"], body["target"], body["sender"],
                            {**reply["payload"], "actor": actor}, self.now,
                            message_id=reply["message_id"])

        with patch.object(self.b, "_receive_control", side_effect=wrong_actor):
            with self.assertRaisesRegex(ValueError, "其他角色"):
                await self.a._prepare_peer(self.a1, self.peer)
        self.assertEqual(len(self.requests("world_catalog")), 1)
        self.assertIsNone(self.a._find_peer(self.a1, self.peer))
        self.assertFalse(self.a._state["visits"])
        self.assertEqual(self.a_actor.calls, [])

    async def test_local_life_closing_during_bind_rejection_does_not_refresh_or_reenable_it(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.b._receive_control

        async def blocked_rejection(body, receipt_key):
            if body["payload"]["op"] == "world_bind":
                entered.set()
                await release.wait()
                raise ValueError("isolated binding rejection")
            return await original(body, receipt_key)

        with patch.object(self.b, "_receive_control", side_effect=blocked_rejection):
            preparing = asyncio.create_task(self.a._prepare_peer(self.a1, self.peer))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                await self.a.set_enabled_scopes([self.a2])
            finally:
                release.set()
            with self.assertRaises(VisitTransportError):
                await preparing
        self.assertFalse(self.requests("world_catalog"))
        self.assertEqual(self.a._participating(), [self.a2])
        self.assertFalse(self.a._state["visits"])
        self.assertEqual(self.a_actor.calls, [])

    async def test_failed_refresh_preserves_pairing_and_original_bind_rejection(self):
        await self.b.set_enabled_scopes([self.b2])
        before = copy.deepcopy(self.current_connection())
        original = self.b._receive_control

        async def unavailable_catalog(body, receipt_key):
            if body["payload"]["op"] == "world_catalog":
                raise VisitStateError()
            return await original(body, receipt_key)

        with patch.object(self.b, "_receive_control", side_effect=unavailable_catalog):
            with self.assertRaises(VisitTransportError) as error:
                await self.a._prepare_peer(self.a1, self.peer)
        self.assertEqual(error.exception.http_status, 400)
        self.assertEqual(self.current_connection()["actors"], before["actors"])
        self.assertEqual(self.current_connection()["anchors"], before["anchors"])
        self.assertEqual(self.current_connection()["default_actor"], before["default_actor"])
        self.assertIn("HTTP 503", self.current_connection()["catalog_error"])
        self.assertGreater(self.current_connection()["next_catalog_at"], self.now)
        self.assertEqual(self.a_actor.calls, [])

    async def test_catalog_recovery_does_not_reset_world_success_already_used_by_another_role(self):
        await self.a.set_enabled_scopes([self.a1])
        self.a.config = replace(self.a.config, auto_enabled=True, auto_probability=1, auto_interval_minutes=0)
        self.a._random = lambda: 0
        await self.completed(self.a, self.a1, self.peer, self.b, self.b1, automatic=True)
        self.assertEqual(self.entry()["successes"], 1)
        await self.b.set_enabled_scopes([self.b2])
        self.now += 3601
        before = len(self.a_actor.calls)
        with self.assertRaises(VisitTransportError):
            await self.a._prepare_peer(self.a1, self.peer)
        self.assertNotEqual(self.current_connection()["default_actor"], self.peer)
        await self.a.tick()
        self.assertEqual(len(self.a_actor.calls), before)
        self.assertEqual(self.entry()["successes"], 1)
        self.assertEqual(set(self.a._state["draws"][self.a1]["peer_days"]), {self.b._state["instance_id"]})
        self.assertEqual(len(self.requests("pair_world")), 1)
