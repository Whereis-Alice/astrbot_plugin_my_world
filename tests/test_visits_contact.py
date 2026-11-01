"""Signed phone replies retain the real speaker and both invitation directions."""
from __future__ import annotations

import copy
import unittest

from tests import test_visits_protocol as fixture
from tests import test_visits_actor as actor_fixture


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

    async def test_empty_receiver_refusal_keeps_a_public_reason_in_both_directions(self):
        async def silent_refusal(scope, phase, visit):
            return {"accept": False, "say": "", "leave": True, "thought": "私有拒绝想法"}

        self.b_actor.decide = silent_refusal
        for direction in ("outgoing", "incoming"):
            with self.subTest(direction=direction):
                pending = await self.a.invite("life-a", self.peer, direction=direction)
                await self.advance(4)
                for service in (self.a, self.b):
                    visit = service._state["visits"][pending["id"]]
                    self.assertEqual(visit["status"], "declined")
                    self.assertEqual(visit["end_reason"], "现在不方便，稍后再约")
                    self.assertNotIn("私有拒绝想法", str(service._visible(visit)))
                self.assertEqual(self.a._state["visits"][pending["id"]]["log"],
                                 self.b._state["visits"][pending["id"]]["log"])

    async def test_guest_goodbye_keeps_its_words_and_reason_in_both_directions(self):
        originals = {actor: actor.decide for actor in (self.a_actor, self.b_actor)}
        for direction in ("outgoing", "incoming"):
            with self.subTest(direction=direction):
                for actor, decide in originals.items():
                    actor.decide = decide
                guest = self.a_actor if direction == "outgoing" else self.b_actor

                async def goodbye(scope, phase, visit):
                    result = await originals[guest](scope, phase, visit)
                    if phase == "turn":
                        result.update(say="我先回去收好书，下次再见。", leave=True)
                    return result

                guest.decide = goodbye
                pending = await self.a.invite("life-a", self.peer, direction=direction)
                await self.advance(4)
                local = self.a._state["visits"][pending["id"]]
                remote = self.b._state["visits"][pending["id"]]
                for visit in (local, remote):
                    self.assertEqual(visit["status"], "completed")
                    self.assertEqual(visit["end_reason"], "访客已告别归家")
                    self.assertEqual([row["text"] for row in visit["log"]
                                      if row["actor_id"] == visit["guest_id"] and row["kind"] == "say"][-1],
                                     "我先回去收好书，下次再见。")
                self.assertEqual(local["log"], remote["log"])

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


class VisitReceptionStatusTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = actor_fixture.VisitActorTests.asyncSetUp

    async def test_closed_reception_returns_public_status_without_a_model(self):
        from unittest.mock import patch
        from tests.test_engine import SESSION

        with patch.object(self.port, "_reception_open", return_value=False):
            for phase in ("propose", "receive"):
                with self.subTest(phase=phase):
                    result = await self.port.decide(SESSION, phase, self.visit)
                    self.assertFalse(result["accept"])
                    self.assertTrue(result["leave"])
                    self.assertEqual(result["say"], "现在不在接待时间，稍后再约")
                    self.assertEqual(result["contact_kind"], "contact_status")
        self.assertEqual(self.llm.calls, [])

    async def test_reception_closing_during_thought_discards_acceptance_with_public_status(self):
        from unittest.mock import patch
        from tests.test_engine import SESSION

        closed = False
        original = self.llm.generate

        async def close_after_generation(**kwargs):
            nonlocal closed
            result = await original(**kwargs)
            closed = True
            return result

        self.llm.generate = close_after_generation
        with patch.object(self.port, "_reception_open", side_effect=lambda: not closed):
            result = await self.port.decide(SESSION, "receive", self.visit)
        self.assertFalse(result["accept"])
        self.assertEqual(result["say"], "现在不在接待时间，稍后再约")
        self.assertEqual(result["contact_kind"], "contact_status")
        self.assertEqual(len(self.llm.calls), 1)
