"""Signed phone replies retain the real speaker and both invitation directions."""
from __future__ import annotations

import copy
import unittest

from tests import test_visits_protocol as fixture


class VisitContactTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.VisitProtocolTests.asyncSetUp
    advance = fixture.VisitProtocolTests.advance

    async def test_busy_neighbour_returns_its_status_without_calling_a_model(self):
        self.b_actor.ready = False
        for direction in ("outgoing", "incoming"):
            with self.subTest(direction=direction):
                visit = await self.a.invite("life-a", self.peer, direction=direction)
                self.assertEqual(visit["status"], "declined")
                self.assertEqual(visit["end_reason"], "正在休息")
                reply = visit["log"][-1]
                self.assertEqual((reply["actor_id"], reply["kind"], reply["text"]),
                                 (self.peer, "contact_status", "正在休息"))
                self.assertEqual(visit["calls_used"], 1)
                self.assertNotIn("arrived_at", visit)
        self.assertEqual(self.b_actor.calls, [])
        self.assertEqual(self.a_actor.memories, {})

    async def test_model_refusal_reaches_caller_verbatim_in_both_directions(self):
        self.b_actor.accept = False
        for direction in ("outgoing", "incoming"):
            with self.subTest(direction=direction):
                pending = await self.a.invite("life-a", self.peer, direction=direction)
                await self.advance(4)
                local = self.a._state["visits"][pending["id"]]
                remote = self.b._state["visits"][pending["id"]]
                self.assertEqual(local["status"], "declined")
                self.assertEqual(remote["status"], "declined")
                self.assertEqual(local["end_reason"], "b：receive")
                self.assertEqual(local["log"], remote["log"])
                self.assertEqual(local["log"][-1]["kind"], "contact_reply")
                self.assertEqual(local["log"][-1]["actor_id"], self.peer)
                self.assertEqual(local["log"][-1]["text"], "b：receive")
                self.assertNotIn("arrived_at", local)
        self.assertEqual(self.a_actor.memories, {})
        self.assertEqual(self.b_actor.memories, {})

    async def test_phone_decline_reply_retry_does_not_duplicate_the_transcript(self):
        self.b_actor.ready = False
        visit = await self.a.invite("life-a", self.peer)
        self.assertEqual(len(visit["log"]), 2)
        for _ in range(3):
            await self.advance(1)
        current = self.a._state["visits"][visit["id"]]
        self.assertEqual(current["log"], visit["log"])

    async def test_invalid_phone_reply_does_not_commit_or_impersonate_another_actor(self):
        original = self.a.transport.request

        async def tamper(url, body):
            response = await original(url, body)
            from core.visits.protocol import envelope
            peer = self.a._find_peer("life-a", self.peer)
            response["payload"]["contact"] = {"kind": "not-a-reply", "text": "forged"}
            return envelope(peer["secret"], response["sender"], response["target"],
                            copy.deepcopy(response["payload"]), self.now)

        self.b_actor.ready = False
        self.a.transport.request = tamper
        visit = await self.a.invite("life-a", self.peer)
        self.assertEqual(visit["status"], "inviting")
        self.assertEqual(len(visit["log"]), 1)
        self.assertNotIn("forged", str(visit))
