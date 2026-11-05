"""A delayed delivery keeps the original authenticated message and receipt."""
from __future__ import annotations

import copy
import hashlib
import hmac
from unittest.mock import patch
import unittest

from tests import test_visits_protocol as fixture
from core.visits.protocol import canonical, digest, envelope, original_envelope, renew_delivery, verify
from core.visits.transport import VisitTransportError


class DeliveryProofTests(unittest.TestCase):
    def setUp(self):
        self.original = envelope("key", "sender", "target", {"op": "status", "id": "a" * 32}, 1000)

    def test_proof_renews_freshness_without_changing_original_identity_or_contents(self):
        with self.assertRaises(ValueError):
            verify(self.original, "key", 1400)
        renewed = renew_delivery(self.original, "key", 1400)
        self.assertEqual(original_envelope(renewed), self.original)
        self.assertEqual(verify(renewed, "key", 1400), self.original["payload"])
        self.assertEqual(renewed["delivery_retry"]["message_digest"], digest(self.original))

    def test_proof_cannot_change_packet_target_payload_or_timestamp(self):
        renewed = renew_delivery(self.original, "key", 1400)
        for field, value in (("target", "other"), ("payload", {"op": "end"}), ("created_at", 1100)):
            with self.subTest(field=field):
                bad = copy.deepcopy(renewed)
                bad[field] = value
                with self.assertRaises(ValueError):
                    verify(bad, "key", 1400)

    def test_proof_requires_correct_signing_key_and_a_current_finite_clock(self):
        renewed = renew_delivery(self.original, "key", 1400)
        for field, value in (("signature", "0" * 64), ("at", 999), ("at", float("nan")),
                             ("message_digest", "0" * 64)):
            with self.subTest(field=field, value=value):
                bad = copy.deepcopy(renewed)
                bad["delivery_retry"][field] = value
                with self.assertRaises(ValueError):
                    verify(bad, "key", 1400)
        with self.assertRaises(ValueError):
            verify(renewed, "key", 1801)
        with self.assertRaises(ValueError):
            verify(renewed, "other-key", 1400)

    def test_valid_retry_proof_does_not_replace_the_original_signature(self):
        bad = copy.deepcopy(self.original)
        bad["signature"] = "0" * 64
        proof = {"at": 1400, "message_digest": digest(bad)}
        proof["signature"] = hmac.new(b"key", canonical(proof), hashlib.sha256).hexdigest()
        with self.assertRaises(ValueError):
            verify({**bad, "delivery_retry": proof}, "key", 1400)


class VisitDeliveryRenewalTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.VisitProtocolTests.asyncSetUp

    async def interrupted_offer(self, *, delivered=False):
        original = self.a.transport.request
        recorded = []

        async def lose_offer(url, body):
            if body["payload"]["op"] == "offer":
                recorded.append(copy.deepcopy(body))
                if delivered:
                    await original(url, body)
                raise VisitTransportError("temporary test interruption", category="channel")
            return await original(url, body)

        with patch.object(self.a.transport, "request", side_effect=lose_offer):
            result = await self.a.invite("life-a", self.peer)
        visit = self.a._state["visits"][result["id"]]
        self.assertEqual(visit["outbox"], recorded[0])
        return visit, recorded[0]

    async def test_undelivered_old_offer_recovers_without_another_model_or_message_id(self):
        visit, old = await self.interrupted_offer()
        self.now += 400
        original = self.a.transport.request
        sent = []

        async def capture(url, body):
            sent.append(copy.deepcopy(body))
            return await original(url, body)

        with patch.object(self.a.transport, "request", side_effect=capture):
            reply = await self.a._send(visit["id"], {"op": "status"})
        self.assertEqual(sent[0], old)
        self.assertEqual(original_envelope(sent[1]), old)
        self.assertEqual(reply["delivered_op"], "offer")
        self.assertEqual(len(self.b._state["visits"]), 1)
        self.assertEqual(len(self.a_actor.calls), 1)
        self.assertEqual(self.b_actor.calls, [])
        self.assertNotIn("outbox", visit)
        stored = self.b._state["receipts"][old["sender"] + "/" + old["message_id"]]
        self.assertEqual(stored["request_digest"], digest(old))

    async def test_received_old_offer_prefers_exact_receipt_and_never_needs_new_proof(self):
        visit, old = await self.interrupted_offer(delivered=True)
        before = copy.deepcopy(self.b._state["visits"])
        self.now += 400
        original = self.a.transport.request
        sent = []

        async def capture(url, body):
            sent.append(copy.deepcopy(body))
            return await original(url, body)

        with patch.object(self.a.transport, "request", side_effect=capture):
            await self.a._send(visit["id"], {"op": "status"})
        self.assertEqual(sent, [old])
        self.assertEqual(self.b._state["visits"], before)
        self.assertEqual(len(self.a_actor.calls), 1)
        self.assertEqual(self.b_actor.calls, [])

    async def test_renewed_retry_matches_a_receipt_written_before_this_protocol_extension(self):
        _, old = await self.interrupted_offer(delivered=True)
        cached = copy.deepcopy(self.b._state["receipts"][old["sender"] + "/" + old["message_id"]])
        self.now += 400
        before = copy.deepcopy(self.b._state["visits"])
        peer = self.a._state["peers"][self.peer]
        first = await self.b.receive(renew_delivery(old, peer["secret"], self.now))
        second = await self.b.receive(old)
        self.assertEqual(first, cached["response"])
        self.assertEqual(first, second)
        self.assertEqual(self.b._state["visits"], before)
        self.assertEqual(self.b_actor.calls, [])

    async def test_renewed_turn_uses_the_original_receipt_and_cannot_bypass_sequence_checks(self):
        result = await self.a.invite("life-a", self.peer)
        for _ in range(2):
            self.now += 65
            await self.b.tick()
        host = self.b._state["visits"][result["id"]]
        self.assertEqual(host["next_actor"], host["guest_id"])
        peer = self.a._state["peers"][self.peer]
        packet = envelope(peer["secret"], host["guest_id"], host["host_id"],
                          {"op": "step", "id": host["id"], "seq": len(host["log"]), "calls": 1,
                           "decision": {"say": "我接着听。", "action": "", "leave": False}}, self.now)
        first = await self.b.receive(packet)
        before = copy.deepcopy(host)
        model_calls = list(self.b_actor.calls)
        self.now += 400
        self.assertEqual(await self.b.receive(renew_delivery(packet, peer["secret"], self.now)), first)
        self.assertEqual(host, before)
        self.assertEqual(self.b_actor.calls, model_calls)
        other_id = envelope(peer["secret"], packet["sender"], packet["target"], packet["payload"],
                            packet["created_at"])
        with self.assertRaises(ValueError):
            await self.b.receive(renew_delivery(other_id, peer["secret"], self.now))
        self.assertEqual(host, before)

    async def test_renewal_cannot_revive_a_locally_expired_or_finished_invitation(self):
        for status in ("expired", "interrupted"):
            with self.subTest(status=status):
                visit, _ = await self.interrupted_offer()
                self.now += 400
                await self.a._finish(visit, status, "test ended", notify=False)
                sent = []

                async def no_send(url, body):
                    sent.append(body)
                    raise AssertionError("ended invitation must not reach transport")

                with patch.object(self.a.transport, "request", side_effect=no_send):
                    with self.assertRaises(ValueError):
                        await self.a._send(visit["id"], {"op": "status"})
                self.assertEqual(sent, [])
                self.assertEqual(visit["status"], status)
        self.assertEqual(self.b._state["visits"], {})

    async def test_retry_rechecks_expiry_after_the_first_rejection(self):
        visit, _ = await self.interrupted_offer()
        self.now += 400
        sent = []

        async def expired_while_sending(url, body):
            sent.append(body)
            self.now = visit["expires_at"] + 1
            raise VisitTransportError("old packet", category="channel", http_status=400)

        with patch.object(self.a.transport, "request", side_effect=expired_while_sending):
            with self.assertRaises(ValueError):
                await self.a._send(visit["id"], {"op": "status"})
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.b._state["visits"], {})

    async def test_current_or_unsupported_packet_errors_stay_bounded_without_extra_model_work(self):
        visit, _ = await self.interrupted_offer()
        calls = []

        async def rejected(url, body):
            calls.append(copy.deepcopy(body))
            raise VisitTransportError("test HTTP 400", category="channel", http_status=400)

        with patch.object(self.a.transport, "request", side_effect=rejected):
            with self.assertRaises(VisitTransportError):
                await self.a._send(visit["id"], {"op": "status"})
            self.assertEqual(len(calls), 1)
            self.now += 400
            with self.assertRaises(VisitTransportError):
                await self.a._send(visit["id"], {"op": "status"})
        self.assertEqual(len(calls), 3)
        self.assertEqual(original_envelope(calls[-1]), calls[-2])
        self.assertEqual(len(self.a_actor.calls), 1)
        self.assertEqual(self.b_actor.calls, [])
