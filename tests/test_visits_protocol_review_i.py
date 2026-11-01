"""Invitation retries remain accounted for when the peer negotiates a budget."""
from __future__ import annotations

from dataclasses import replace
import copy
import unittest

from tests import test_visits_protocol as fixture
from core.visits.decisions import VisitDecisionError
from core.visits.protocol import RESERVED, envelope, new_id


class VisitInvitationAccountingReviewTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.VisitProtocolTests.asyncSetUp
    advance = fixture.VisitProtocolTests.advance

    async def repeated_proposal(self, *, direction="outgoing", automatic=False):
        original = self.a_actor.decide
        failures = 0

        async def decide(scope, phase, visit):
            nonlocal failures
            if phase == "propose" and failures < 2:
                failures += 1
                self.a_actor.calls.append((phase, visit["id"]))
                raise VisitDecisionError("provider", request_started=True)
            return await original(scope, phase, visit)

        self.a_actor.decide = decide
        pending = await self.a.invite("life-a", self.peer, direction=direction, automatic=automatic)
        self.now += 30
        await self.a.tick()
        self.now += 60
        await self.a.tick()
        visit_id = pending["id"]
        self.assertEqual(sum(phase == "propose" for phase, _ in self.a_actor.calls), 3)
        return self.a._state["visits"][visit_id], self.b._state["visits"].get(visit_id)

    async def test_offer_keeps_failed_dispatched_proposal_calls_on_both_servers(self):
        caller, receiver = await self.repeated_proposal()
        self.assertIsNotNone(receiver)
        own = self.a._local_id(caller)
        self.assertEqual(caller["calls"][own], 3)
        self.assertEqual(receiver["calls"][own], caller["calls"][own])

    async def test_smaller_peer_allocation_declines_before_acceptance_or_network_retry(self):
        self.b.config = replace(self.b.config, max_calls=4)
        caller, receiver = await self.repeated_proposal()
        self.assertEqual(caller["status"], "declined")
        self.assertEqual(caller.get("network_failures", 0), 0)
        self.assertFalse(caller.get("arrived_at"))
        self.assertFalse(self.b_actor.calls)
        self.assertTrue(receiver is None or not receiver.get("arrived_at"))

    async def test_smaller_but_sufficient_budget_keeps_honest_invitation_count(self):
        self.b.config = replace(self.b.config, max_calls=6)
        caller, receiver = await self.repeated_proposal(direction="incoming")
        self.assertEqual(caller["max_calls"], 6)
        self.assertEqual(caller["status"], "inviting")
        self.assertEqual(receiver["status"], "offered")
        self.assertEqual(receiver["calls"][caller["host_id"]], 3)

    async def test_odd_negotiated_budget_can_fit_three_host_invitation_calls(self):
        self.b.config = replace(self.b.config, max_calls=5)
        caller, receiver = await self.repeated_proposal(direction="incoming")
        self.assertEqual(caller["max_calls"], 5)
        self.assertEqual(caller["status"], "inviting")
        self.assertEqual(receiver["calls"][caller["host_id"]], 3)

    async def test_odd_negotiated_budget_declines_three_guest_invitation_calls(self):
        self.b.config = replace(self.b.config, max_calls=5)
        caller, _ = await self.repeated_proposal()
        self.assertEqual(caller["status"], "declined")
        self.assertFalse(self.b_actor.calls)

    async def test_old_offer_without_call_count_retains_one_invitation_call(self):
        request = self.a.transport.request

        async def old_request(url, body):
            payload = copy.deepcopy(body["payload"])
            payload.pop("calls", None)
            key = self.a._find_peer("life-a", body["target"])["secret"]
            legacy = envelope(key, body["sender"], body["target"], payload, self.now,
                              message_id=body["message_id"])
            return await request(url, legacy)

        self.a.transport.request = old_request
        pending = await self.a.invite("life-a", self.peer)
        receiver = self.b._state["visits"].get(pending["id"])
        self.assertIsNotNone(receiver, pending)
        self.assertEqual(receiver["calls"][receiver["guest_id"]], 1)

    async def test_invalid_offer_call_count_cannot_create_a_journey(self):
        pending = await self.a.invite("life-a", self.peer)
        peer = self.b._state["peers"][self.a._local_id(self.a._state["visits"][pending["id"]])]
        local = await self.b_actor.describe("life-b")
        template = {"op": "offer", "role": "host", "max_calls": 12,
                    "turn_limit": 3, "duration": 1800, "reason": "喝茶"}
        for calls in (None, True, -1, "3", 21):
            with self.subTest(calls=calls):
                async with self.b._lock:
                    for prior in list(self.b._state["visits"].values()):
                        if prior["status"] in RESERVED:
                            await self.b._finish(prior, "declined", "准备下一次测试", notify=False)
                visit_id = new_id()
                with self.assertRaises(ValueError):
                    await self.b._inbox(peer, local, {**template, "id": visit_id, "calls": calls})
                self.assertNotIn(visit_id, self.b._state["visits"])

    async def test_old_peer_cannot_lower_allocation_below_already_dispatched_calls(self):
        peer = self.a._state["peers"][self.peer]
        local = await self.a_actor.describe("life-a")
        caller = self.a._new_visit("life-a", peer, local, new_id(), "guest", "喝茶", False)
        caller["calls"][self.a._local_id(caller)] = 3
        self.a._state["visits"][caller["id"]] = caller

        async def undersized_reply(url, body):
            remote = self.a._wire(caller)
            remote.update(status="offered", max_calls=4)
            remote["calls"][caller["guest_id"]] = 1  # Older receivers forgot proposal failures.
            return envelope(peer["secret"], peer["id"], body["sender"],
                            {"reply_to": body["message_id"], "visit": remote}, self.now)

        self.a.transport.request = undersized_reply
        with self.assertRaises(ValueError):
            await self.a._send(caller["id"], {"op": "status"})
        self.assertEqual(caller["max_calls"], 12)
        self.assertEqual(caller["calls"][caller["guest_id"]], 3)
        self.assertIn("outbox", caller)

    async def test_retried_offer_completes_a_smaller_budget_trip_with_matching_logs(self):
        self.b.config = replace(self.b.config, max_calls=10)
        caller, receiver = await self.repeated_proposal()
        await self.advance()
        self.assertEqual(caller["status"], "completed")
        self.assertEqual(receiver["status"], "completed")
        self.assertEqual(caller["log"], receiver["log"])
        self.assertEqual(caller["calls"], receiver["calls"])
        self.assertLessEqual(len(self.a_actor.calls) + len(self.b_actor.calls), 10)
        self.assertEqual(len(self.a_actor.memories), 1)
        self.assertEqual(len(self.b_actor.memories), 1)

    async def test_insufficient_negotiation_does_not_spend_daily_successful_invitation(self):
        self.a.config = replace(self.a.config, auto_enabled=True, auto_probability=1)
        self.b.config = replace(self.b.config, max_calls=4)
        caller, receiver = await self.repeated_proposal(automatic=True)
        self.assertEqual(caller["status"], "declined")
        self.assertIsNone(receiver)
        record = self.a._state["draws"]["life-a"]
        self.assertEqual(sum(day.get("successes", 0) for peer in record["peer_days"].values()
                             for day in peer["days"].values()), 0)
        self.assertFalse(self.b_actor.calls)
        self.assertFalse(self.a_actor.memories)
        self.assertFalse(self.b_actor.memories)


if __name__ == "__main__":
    unittest.main()
