"""Only real confirmed contact renews a visit, including signed in-flight catch-up."""
from __future__ import annotations

import asyncio
import copy
import unittest

from tests import fake_astrbot
fake_astrbot.install()

from core.visits.protocol import clean_decision, envelope  # noqa: E402
from core.visits.decisions import VisitDecisionError  # noqa: E402
from core.visits.snapshots import validate_snapshot  # noqa: E402
from tests import test_visit_shared_flow_20261113 as fixture  # noqa: E402


class ContactLeaseTests(unittest.IsolatedAsyncioTestCase):
    make_world = fixture.SharedVisitFlowTests.make_world
    exhaust = fixture.SharedVisitFlowTests.exhaust
    asyncSetUp = fixture.SharedVisitFlowTests.asyncSetUp
    advance = fixture.SharedVisitFlowTests.advance
    start_shared = fixture.SharedVisitFlowTests.start_shared

    @staticmethod
    def choice(text="这条新的线索，我们接着看。"):
        return clean_decision({"say": text, "continue_activity": True, "pause_minutes": 0})

    async def test_real_host_turn_renews_contact_without_changing_the_activity(self):
        visit_id, host, guest = await self.start_shared()
        self.assertIs(host.get("contact_lease_supported"), True)
        self.assertIs(guest.get("contact_lease_supported"), True)
        old_expiry, activity = host["expires_at"], copy.deepcopy(host["outing"])
        self.advance(old_expiry - self.b.clock.now() - 30)
        self.b.service._record_turn(host, host["host_id"], self.choice())
        self.assertEqual(host["expires_at"], self.b.clock.now() + 3600)
        self.assertGreater(host["expires_at"], old_expiry)
        self.assertEqual(host["outing"], activity)
        await self.b.service._save()
        await self.a.service._send(visit_id, {"op": "sync", "visit": self.a.service._wire(guest)})
        self.assertEqual(host["expires_at"], guest["expires_at"])
        self.assertEqual(host["log"], guest["log"])
        self.assertEqual(host["status"], "active")

    async def test_a_signed_legacy_peer_without_capability_keeps_the_original_contact_rule(self):
        visit_id, host, guest = await self.start_shared()
        self.assertIs(host.get("contact_lease_supported"), True)
        legacy = self.a.service._wire(guest)
        legacy.pop("contact_lease_version")
        peer = self.a.service._find_peer(self.scope, self.peer_id)
        packet = envelope(peer["secret"], guest["guest_id"], guest["host_id"],
            {"op": "sync", "id": visit_id, "visit": legacy}, self.a.clock.now())
        await self.b.service.receive(packet)
        self.assertIsNot(host.get("contact_lease_supported"), True)
        expiry = host["expires_at"]
        self.advance(expiry - self.b.clock.now() - 30)
        self.b.service._record_turn(host, host["host_id"], self.choice())
        self.assertEqual(host["expires_at"], expiry)
        self.assertEqual(host["status"], "active")
        self.assertEqual(host["log"][-1]["text"], self.choice()["say"])

    async def test_capability_requires_the_exact_integer_version_in_snapshots_and_signed_requests(self):
        visit_id, host, guest = await self.start_shared()
        peer = self.a.service._find_peer(self.scope, self.peer_id)
        expiry = host["expires_at"]
        for invalid in (True, "1", None, 0, 2):
            wire = self.b.service._wire(host)
            wire["contact_lease_version"] = invalid
            with self.assertRaises(ValueError):
                validate_snapshot(guest, wire, now=self.a.clock.now())
            packet = envelope(peer["secret"], guest["guest_id"], guest["host_id"],
                {"op": "status", "id": visit_id, "contact_lease_version": invalid}, self.a.clock.now())
            with self.assertRaises(ValueError):
                await self.b.service.receive(packet)
        self.assertEqual(host["expires_at"], expiry)

    async def test_repeated_signed_sync_and_failed_requests_do_not_renew_contact(self):
        visit_id, host, guest = await self.start_shared()
        expiry, logs = host["expires_at"], copy.deepcopy(host["log"])
        for _ in range(3):
            self.advance(20)
            await self.a.service._send(visit_id, {"op": "sync", "visit": self.a.service._wire(guest)})
        self.assertEqual(host["expires_at"], expiry)
        self.assertEqual(guest["expires_at"], expiry)

        self.assertEqual(host["log"], logs)
        async def invalid(scope, phase, current):
            raise VisitDecisionError("format", format_issue="json_object")

        self.b.service.actor_port.decide = invalid
        self.assertIsNone(await self.b.service._decide(visit_id, "turn"))
        self.assertEqual(host["expires_at"], expiry)
        fake = self.b.service._wire(host)
        fake["expires_at"] = expiry + 1
        fake["next_at"] += 60
        fake["calls"][host["host_id"]] += 3
        with self.assertRaises(ValueError):
            validate_snapshot(guest, fake, now=self.a.clock.now())
        self.assertEqual(guest["expires_at"], expiry)

    async def test_confirmed_public_experience_without_speech_also_renews_contact(self):
        visit_id, host, guest = await self.start_shared()
        expiry = host["expires_at"]
        self.advance(expiry - self.b.clock.now() - 30)
        self.b.service._record_turn(host, host["host_id"], clean_decision({
            "outcome": "一起看过这一页，记住了其中的具体线索。", "continue_activity": True}))
        self.assertEqual(host["log"][-1]["kind"], "experience")
        self.assertEqual(host["expires_at"], self.b.clock.now() + 3600)
        await self.b.service._save()
        await self.a.service._send(visit_id, {"op": "sync", "visit": self.a.service._wire(guest)})
        self.assertEqual(guest["expires_at"], host["expires_at"])
        self.assertEqual(guest["log"], host["log"])

    async def test_quiet_or_private_choices_and_invalid_actions_do_not_renew_contact(self):
        _, host, _ = await self.start_shared()
        expiry = host["expires_at"]
        self.advance(expiry - self.b.clock.now() - 30)
        for value in ({"continue_activity": True, "pause_minutes": 5}, {"thought": "只在心里考虑"}):
            self.b.service._record_turn(host, host["host_id"], clean_decision(value))
            self.assertEqual(host["expires_at"], expiry)
        previous = copy.deepcopy(host)
        bad = {"action_step": fixture.action("需要尚未答应的协作", 10,
                    requires_peer=True, peer_action_id="s0-a999")}
        with self.assertRaises(ValueError):
            self.b.service._record_turn(host, host["host_id"], clean_decision(bad))
        self.assertEqual(host, previous)

    async def test_agreed_return_is_not_extended_by_new_contact_or_a_late_turn(self):
        _, host, guest = await self.start_shared(return_after=True)
        expiry, until = host["expires_at"], host["outing"]["until"]
        self.advance(until - self.b.clock.now() - 1)
        self.b.service._record_turn(host, host["host_id"], self.choice())
        self.assertEqual(host["expires_at"], expiry)
        self.assertEqual(host["outing"]["until"], until)
        self.advance(1)
        before = copy.deepcopy(host)
        with self.assertRaises(ValueError):
            self.b.service._record_turn(host, host["host_id"], self.choice("过时的新台词"))
        self.assertEqual(host, before)
        self.assertEqual(guest["outing"]["until"], until)

    async def test_expired_contact_cannot_be_revived_by_late_or_future_dated_facts(self):
        _, host, guest = await self.start_shared()
        expiry = host["expires_at"]
        self.advance(expiry - self.b.clock.now() + 1)
        before = copy.deepcopy(host)
        with self.assertRaises(ValueError):
            self.b.service._record_turn(host, host["host_id"], self.choice())
        self.assertEqual(host, before)
        fake = self.b.service._wire(host)
        fake["log"].append({"seq": len(fake["log"]) + 1, "actor_id": host["host_id"],
            "kind": "say", "text": "失联之后才编出的新进展", "at": self.b.clock.now()})
        fake["expires_at"] = self.b.clock.now() + 3600
        with self.assertRaises(ValueError):
            self.a.service._synchronize(guest, fake, guest["host_id"])
        self.assertEqual(guest["expires_at"], expiry)
        self.assertEqual(guest["log"], before["log"])
        guest.update(status="completed", ended_at=self.a.clock.now())
        self.a.service._synchronize(guest, self.b.service._wire(host), guest["host_id"])
        self.assertEqual(guest["status"], "completed")
        self.assertEqual(guest["expires_at"], expiry)

    async def test_future_timestamp_does_not_prove_a_contact_renewal(self):
        _, host, guest = await self.start_shared()
        expiry = host["expires_at"]
        self.advance(expiry - self.b.clock.now() - 30)
        fake = self.b.service._wire(host)
        fake["log"].append({"seq": len(fake["log"]) + 1, "actor_id": host["host_id"],
            "kind": "say", "text": "未来尚未发生的进展", "at": self.b.clock.now() + 20})
        fake["expires_at"] = self.b.clock.now() + 3600
        with self.assertRaises(ValueError):
            validate_snapshot(guest, fake, now=self.a.clock.now())
        self.assertEqual(guest["expires_at"], expiry)

    async def test_signed_reply_in_flight_before_old_expiry_uses_confirmed_new_contact(self):
        visit_id, host, guest = await self.start_shared()
        old_expiry = host["expires_at"]
        self.advance(old_expiry - self.b.clock.now() - 1)
        original = self.a.service.transport.request

        async def delayed_reply(url, packet):
            self.b.service._record_turn(host, host["host_id"], self.choice())
            await self.b.service._save()
            response = await original(url, packet)
            self.advance(2)  # The signed actual progress predates the old lease.
            return response

        self.a.service.transport.request = delayed_reply
        await self.a.service._send(visit_id, {"op": "sync", "visit": self.a.service._wire(guest)})
        self.assertGreater(self.a.clock.now(), old_expiry)
        self.assertGreater(guest["expires_at"], self.a.clock.now())
        self.assertEqual(guest["expires_at"], host["expires_at"])
        self.assertEqual(guest["log"], host["log"])
        self.assertEqual(guest["status"], "active")

    async def test_model_crossing_old_expiry_keeps_new_signed_progress_and_discards_old_draft(self):
        visit_id, host, guest = await self.start_shared()
        self.b.service._record_turn(host, host["host_id"], self.choice("先看这一页。"))
        await self.b.service._save()
        await self.b.service._send(visit_id, {"op": "sync", "visit": self.b.service._wire(host)})
        old_expiry = guest["expires_at"]
        self.advance(old_expiry - self.a.clock.now() - 1)
        entered, release = asyncio.Event(), asyncio.Event()

        async def waiting_model(scope, phase, current):
            entered.set()
            await release.wait()
            return self.choice("这是旧场景下生成的草稿。")

        self.a.service.actor_port.decide = waiting_model
        task = asyncio.create_task(self.a.service._decide(visit_id, "turn"))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            self.b.service._record_turn(host, host["host_id"], self.choice("又确认了一条具体的新线索。"))
            await self.b.service._save()
            await self.b.service._send(visit_id, {"op": "sync", "visit": self.b.service._wire(host)})
            self.advance(2)
            release.set()
            self.assertIsNone(await asyncio.wait_for(task, 2))
            self.assertGreater(self.a.clock.now(), old_expiry)
            self.assertGreater(guest["expires_at"], self.a.clock.now())
            self.assertEqual(guest["status"], "active")
            self.assertEqual(guest["log"], host["log"])
            self.assertFalse(any("旧场景" in row["text"] for row in guest["log"]))
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_host_signed_sync_sent_before_old_expiry_arrives_after_the_old_deadline(self):
        visit_id, host, guest = await self.start_shared()
        old_expiry = host["expires_at"]
        self.advance(old_expiry - self.b.clock.now() - 1)
        self.b.service._record_turn(host, host["host_id"], self.choice())
        await self.b.service._save()
        original = self.b.service.transport.request

        async def delayed_delivery(url, packet):
            self.advance(2)
            return await original(url, packet)

        self.b.service.transport.request = delayed_delivery
        await self.b.service._send(visit_id, {"op": "sync", "visit": self.b.service._wire(host)})
        self.assertGreater(self.a.clock.now(), old_expiry)
        self.assertEqual(guest["status"], "active")
        self.assertEqual(guest["expires_at"], host["expires_at"])
        self.assertGreater(guest["expires_at"], self.a.clock.now())
        self.assertEqual(guest["log"], host["log"])


if __name__ == "__main__":
    unittest.main()
