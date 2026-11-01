"""Rejected or late protocol responses must not partly change a live journey."""
from __future__ import annotations

import asyncio
import copy
import unittest
from dataclasses import replace

from core.visits.protocol import envelope
from tests import test_visits_protocol as fixture


class VisitProtocolBoundaryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.VisitProtocolTests.asyncSetUp
    advance = fixture.VisitProtocolTests.advance

    async def arrived(self):
        await self.a.invite("life-a", self.peer)
        await self.advance(4)
        guest = self.a._state["visits"][self.a.active("life-a")["id"]]
        return guest, self.b._state["visits"][guest["id"]]

    async def test_rejected_step_or_operation_cannot_consume_peer_calls(self):
        guest, host = await self.arrived()
        peer = self.b._state["peers"][guest["guest_id"]]
        local = await self.b_actor.describe("life-b")
        host["calls"][guest["guest_id"]] = 1
        before = copy.deepcopy(host)
        for payload in ({"op": "step", "seq": -1, "decision": {}}, {"op": "unknown"}):
            with self.subTest(op=payload["op"]):
                with self.assertRaises(ValueError):
                    await self.b._inbox(peer, local, {**payload, "id": host["id"], "calls": 2})
                self.assertEqual(host, before)

    async def test_invalid_reply_budget_preserves_outbox_and_all_previous_fields(self):
        guest, host = await self.arrived()
        peer = self.a._state["peers"][self.peer]
        admitted = []
        guest["network_failures"] = 2

        async def bad_response(url, body):
            admitted.append(copy.deepcopy(guest))
            remote = self.b._wire(host)
            remote.update(max_calls=guest["max_calls"] - 2, turn_limit="broken")
            return envelope(peer["secret"], peer["id"], body["sender"],
                            {"reply_to": body["message_id"], "visit": remote}, self.now)

        self.a.transport.request = bad_response
        with self.assertRaises(ValueError):
            await self.a._send(guest["id"], {"op": "status"})
        self.assertEqual(guest, admitted[0])

    async def test_invalid_snapshot_fields_are_rejected_before_state_commit(self):
        guest, host = await self.arrived()
        before = copy.deepcopy(guest)
        changes = [("next_at", "later"), ("next_at", float("nan")), ("next_actor", "stranger"),
                   ("turns", None), ("turns", {host["host_id"]: -1, host["guest_id"]: 0}),
                   ("scene", []), ("scene", {"place": []}), ("arrived_at", "yesterday"),
                   ("event_stage", []), ("status", []), ("calls", {host["host_id"]: "one"}),
                   ("intro", {}), ("end_reason", []), ("max_calls", True), ("turn_limit", "three")]
        for field, value in changes:
            with self.subTest(field=field, value=value):
                remote = self.b._wire(host)
                remote[field] = value
                with self.assertRaises(ValueError):
                    self.a._synchronize(guest, remote, host["host_id"])
                self.assertEqual(guest, before)

    async def test_closed_service_cannot_commit_a_description_that_was_already_waiting(self):
        entered, release = asyncio.Event(), asyncio.Event()
        describe = self.a_actor.describe

        async def delayed(scope):
            entered.set()
            await release.wait()
            return await describe(scope)

        self.a_actor.describe = delayed
        pending = asyncio.create_task(self.a.invite("life-a", self.peer))
        await asyncio.wait_for(entered.wait(), 2)
        await self.a.close()
        before = copy.deepcopy(self.a._state)
        release.set()
        with self.assertRaises(ValueError):
            await asyncio.wait_for(pending, 2)
        self.assertEqual(self.a._state, before)

    async def test_closed_service_cannot_apply_a_late_network_reply(self):
        guest, host = await self.arrived()
        entered, release = asyncio.Event(), asyncio.Event()
        peer = self.a._state["peers"][self.peer]

        async def delayed(url, body):
            entered.set()
            await release.wait()
            return envelope(peer["secret"], peer["id"], body["sender"],
                            {"reply_to": body["message_id"], "visit": self.b._wire(host)}, self.now)

        self.a.transport.request = delayed
        pending = asyncio.create_task(self.a._send(guest["id"], {"op": "status"}))
        await asyncio.wait_for(entered.wait(), 2)
        await self.a.close()
        before = copy.deepcopy(guest)
        release.set()
        with self.assertRaises(ValueError):
            await asyncio.wait_for(pending, 2)
        self.assertEqual(guest, before)

    async def test_invalid_confirmed_log_cannot_acknowledge_outbox(self):
        guest, host = await self.arrived()
        peer = self.a._state["peers"][self.peer]
        admitted = []

        async def rewritten(url, body):
            admitted.append(copy.deepcopy(guest))
            remote = self.b._wire(host)
            remote["log"][0]["text"] = "另一份被改写的邀请"
            return envelope(peer["secret"], peer["id"], body["sender"],
                            {"reply_to": body["message_id"], "visit": remote}, self.now)

        self.a.transport.request = rewritten
        with self.assertRaises(ValueError):
            await self.a._send(guest["id"], {"op": "status"})
        self.assertEqual(guest, admitted[0])

    async def test_reply_without_journey_cannot_acknowledge_outbox(self):
        guest, _ = await self.arrived()
        peer = self.a._state["peers"][self.peer]
        admitted = []

        async def empty(url, body):
            admitted.append(copy.deepcopy(guest))
            return envelope(peer["secret"], peer["id"], body["sender"],
                            {"reply_to": body["message_id"]}, self.now)

        self.a.transport.request = empty
        with self.assertRaises(ValueError):
            await self.a._send(guest["id"], {"op": "status"})
        self.assertEqual(guest, admitted[0])

    async def test_host_rejects_invalid_goodbye_before_ending_journey(self):
        guest, host = await self.arrived()
        peer = self.b._state["peers"][guest["guest_id"]]
        local = await self.b_actor.describe("life-b")
        remote = self.a._wire(guest)
        remote["status"] = []
        before = copy.deepcopy(host)
        with self.assertRaises(ValueError):
            await self.b._inbox(peer, local, {"op": "end", "id": host["id"], "visit": remote})
        self.assertEqual(host, before)

    async def test_invitation_does_not_reschedule_after_close_during_delivery(self):
        entered, release = asyncio.Event(), asyncio.Event()
        request = self.a.transport.request

        async def delayed(url, body):
            response = await request(url, body)
            if body["payload"].get("op") == "offer":
                entered.set()
                await release.wait()
            return response

        self.a.transport.request = delayed
        pending = asyncio.create_task(self.a.invite("life-a", self.peer))
        await asyncio.wait_for(entered.wait(), 2)
        await self.a.close()
        before = copy.deepcopy(self.a._state)
        self.now += 300
        release.set()
        await asyncio.wait_for(pending, 2)
        self.assertEqual(self.a._state, before)

    async def test_disabled_neighbor_cannot_start_an_invitation(self):
        self.a._state["peers"][self.peer]["enabled"] = False
        before = copy.deepcopy(self.a._state)
        with self.assertRaises(ValueError):
            await self.a.invite("life-a", self.peer)
        self.assertEqual(self.a._state, before)
        self.assertEqual(self.a_actor.calls, [])

    async def test_arrival_cursor_survives_retries_history_trim_and_restart(self):
        self.assertEqual(self.a.presence_epoch("life-a"), 0)
        await self.a.invite("life-a", self.peer)
        self.assertEqual(self.a.presence_epoch("life-a"), 0)
        await self.advance()
        self.assertEqual(self.a.presence_epoch("life-a"), 1)
        self.assertEqual(self.b.presence_epoch("life-b"), 1)
        await self.advance(3)
        self.assertEqual(self.a.presence_epoch("life-a"), 1)
        await self.a.invite("life-a", self.peer)
        await self.advance()
        self.assertEqual(self.a.presence_epoch("life-a"), 2)
        self.a.config = replace(self.a.config, history_keep=1)
        await self.a._save()
        self.assertEqual(len(self.a._state["visits"]), 1)
        self.a._loaded = False
        await self.a._load()
        self.assertEqual(self.a.presence_epoch("life-a"), 2)
        self.assertEqual(self.a.presence_epoch("another-life"), 0)
