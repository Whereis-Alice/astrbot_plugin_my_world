"""Changing visit limits releases only authenticated, obsolete budget waits."""
from __future__ import annotations

import copy
from dataclasses import replace
import unittest
from unittest.mock import AsyncMock

from tests import fake_astrbot
fake_astrbot.install()

from core.visits import VisitConfig, VisitService  # noqa: E402
from core.visits.protocol import envelope  # noqa: E402


class MemoryKV:
    def __init__(self):
        self.value = {}

    async def call(self, operation, key, value=None):
        if operation == "kv_get":
            return copy.deepcopy(self.value)
        if operation == "kv_set":
            self.value = copy.deepcopy(value)
            return None
        raise AssertionError(operation)


class VisitBudgetReleaseTests(unittest.IsolatedAsyncioTestCase):
    def make_service(self, role="guest", *, daily_calls=0):
        self.now = 1793712000.0
        self.scope = "life"
        local = {"actor_id": "1" * 32, "persona_key": "persona", "name": "local",
                 "world": "home", "ready": True, "chat_busy": False,
                 "reception_open": True, "local_date": "2026-11-03"}
        remote = {"id": "b" * 32 + ":" + "2" * 32, "instance_id": "b" * 32,
                  "actor_id": "2" * 32, "name": "friend", "world": "neighbour"}
        port = type("Port", (), {})()
        port.canonical_scope = lambda scope: scope
        port.describe = AsyncMock(return_value=local)
        port.decide = AsyncMock(side_effect=AssertionError("This regression never calls a model"))
        config = VisitConfig(enabled=True, auto_enabled=False, outings_enabled=True,
                             daily_calls=daily_calls, daily_tokens=0)
        service = VisitService(MemoryKV(), config, port, clock=lambda: self.now)
        self.addAsyncCleanup(service.close)
        service._loaded = True
        service._state = {"instance_id": "a" * 32, "world_pairing_version": 1,
                          "enabled_scopes": [self.scope], "visits": {}, "peers": {},
                          "connections": {}, "codes": {}, "receipts": {}, "draws": {},
                          "presence_epochs": {}}
        peer = {"id": remote["id"], "actor": remote, "local": local, "scope": self.scope}
        peer.update(url="http://unused.invalid", secret="offline-test-key", enabled=True,
                    connection_id=remote["instance_id"])
        service._state["peers"][peer["id"]] = peer
        service._state["connections"][remote["instance_id"]] = {
            "id": remote["instance_id"], "enabled": True, "actors": [remote],
            "anchors": [{"secret": peer["secret"]}]}
        visit = service._new_visit(self.scope, peer, local, "same-visit", role, "一起聊天", True)
        visit.update(status="active", arrived_at=self.now - 600, outing_version=1,
                     next_at=self.now + 3600, expires_at=self.now + 7200,
                     automatic_counted=True)
        visit["log"] = [{"seq": 1, "actor_id": visit["host_id"], "kind": "say",
                         "text": "这句话已经确认。", "at": self.now - 500}]
        service._state["visits"][visit["id"]] = visit
        old_snapshot = ["old-ledger", 24, 0, 24, 0, 0, 0]
        service._state["draws"][self.scope] = {
            "rhythm_version": 3, "peer_days": {}, "budget_hold": old_snapshot}

        async def budget(scope):
            limit = service.config.daily_calls
            return {"allowed": limit == 0 or 24 < limit,
                    "snapshot": ["old-ledger", limit, 0, 24, 0, 0, 0],
                    "day": "2026-11-03", "used": 24, "limit": limit,
                    "reset_at": self.now + 3600}

        port.automatic_budget = AsyncMock(side_effect=budget)
        service._advance = AsyncMock()
        service._automatic = AsyncMock()
        service._drain_manual_invites = AsyncMock()
        return service, visit, old_snapshot

    def add_hold(self, visit, actor):
        visit.setdefault("budget_holds", {})[actor] = {
            "used": 24, "limit": 24, "day": "2026-11-03",
            "reset_at": self.now + 3600, "reason": "old allowance exhausted"}
        visit["outing_budget_hold"] = True

    async def test_unlimited_configuration_releases_local_hold_before_future_next_at_gate(self):
        service, visit, _ = self.make_service()
        own = service._local_id(visit)
        self.add_hold(visit, own)
        prior = copy.deepcopy(visit["log"])
        actor = visit["next_actor"]
        await service.tick()
        self.assertNotIn(own, visit.get("budget_holds", {}))
        self.assertNotIn("budget_hold", service._state["draws"][self.scope])
        self.assertLessEqual(visit["next_at"], self.now)
        service._advance.assert_awaited_once_with(visit["id"])
        self.assertEqual(visit["status"], "active")
        self.assertEqual(visit["next_actor"], actor)
        self.assertEqual(visit["log"], prior)
        service.actor_port.decide.assert_not_awaited()

    async def test_authenticated_guest_withdrawal_reschedules_host_without_changing_turn(self):
        service, visit, _ = self.make_service("host")
        self.add_hold(visit, visit["guest_id"])
        before = copy.deepcopy(visit["log"])
        actor = visit["next_actor"]
        remote = copy.deepcopy(visit)
        remote["budget_holds"] = {}
        service._synchronize(visit, remote, visit["guest_id"])
        self.assertNotIn(visit["guest_id"], visit.get("budget_holds", {}))
        self.assertLessEqual(visit["next_at"], self.now)
        self.assertEqual(visit["next_actor"], actor)
        self.assertEqual(visit["log"], before)
        self.assertEqual(visit["status"], "active")

    async def test_still_exhausted_finite_allowance_keeps_its_future_wait(self):
        service, visit, snapshot = self.make_service(daily_calls=24)
        own = service._local_id(visit)
        self.add_hold(visit, own)
        reset = visit["next_at"]
        await service.tick()
        self.assertIn(own, visit.get("budget_holds", {}))
        self.assertEqual(visit["next_at"], reset)
        self.assertEqual(service._state["draws"][self.scope]["budget_hold"], snapshot)
        service._advance.assert_not_awaited()
        service.actor_port.decide.assert_not_awaited()

    async def test_unchanged_reservation_shortage_is_not_mistaken_for_a_configuration_change(self):
        service, visit, _ = self.make_service(daily_calls=24)
        service.config = replace(service.config, daily_tokens=10000)
        snapshot = ["old-ledger", 24, 10000, 10, 0, 9900, 0]
        service._state["draws"][self.scope]["budget_hold"] = snapshot
        own = service._local_id(visit)
        self.add_hold(visit, own)
        visit["budget_holds"][own]["used"] = 10
        service.actor_port.automatic_budget = AsyncMock(return_value={
            "allowed": True, "snapshot": snapshot, "used": 10, "limit": 24,
            "reset_at": self.now + 3600})
        await service.tick()
        self.assertIn(own, visit.get("budget_holds", {}))
        self.assertEqual(service._state["draws"][self.scope]["budget_hold"], snapshot)
        service._advance.assert_not_awaited()
        service.actor_port.decide.assert_not_awaited()

    async def test_unlimited_calls_do_not_override_a_still_exhausted_token_limit(self):
        service, visit, _ = self.make_service()
        service.config = replace(service.config, daily_tokens=500)
        own = service._local_id(visit)
        self.add_hold(visit, own)
        service.actor_port.automatic_budget = AsyncMock(return_value={
            "allowed": False, "snapshot": ["current", 0, 500, 24, 500, 0, 0],
            "used": 24, "limit": 0, "reset_at": self.now + 3600})
        await service.tick()
        self.assertIn(own, visit.get("budget_holds", {}))
        service._advance.assert_not_awaited()
        service.actor_port.decide.assert_not_awaited()

    async def test_explicit_unlimited_status_releases_an_opaque_unchanged_old_snapshot(self):
        service, visit, snapshot = self.make_service()
        own = service._local_id(visit)
        self.add_hold(visit, own)
        service.actor_port.automatic_budget = AsyncMock(return_value={
            "allowed": True, "unlimited": True, "snapshot": snapshot,
            "used": 24, "limit": 0, "reset_at": self.now + 3600})
        await service.tick()
        self.assertNotIn(own, visit.get("budget_holds", {}))
        self.assertNotIn("budget_hold", service._state["draws"][self.scope])
        service._advance.assert_awaited_once_with(visit["id"])
        service.actor_port.decide.assert_not_awaited()

    async def test_local_unlimited_setting_cannot_withdraw_the_neighbours_hold(self):
        service, visit, _ = self.make_service("host")
        self.add_hold(visit, visit["guest_id"])
        before = copy.deepcopy(visit["budget_holds"])
        await service.tick()
        self.assertEqual(visit["budget_holds"], before)
        self.assertGreater(visit["next_at"], self.now)
        service.actor_port.automatic_budget.assert_not_awaited()
        service._advance.assert_not_awaited()
        service.actor_port.decide.assert_not_awaited()

    async def test_own_withdrawal_is_published_even_while_a_cached_remote_hold_remains(self):
        service, visit, _ = self.make_service()
        own = service._local_id(visit)
        self.add_hold(visit, own)
        self.add_hold(visit, visit["host_id"])
        remote_hold = copy.deepcopy(visit["budget_holds"][visit["host_id"]])
        await service.tick()
        self.assertNotIn(own, visit.get("budget_holds", {}))
        self.assertEqual(visit["budget_holds"][visit["host_id"]], remote_hold)
        self.assertLessEqual(visit["next_at"], self.now)
        service._advance.assert_awaited_once_with(visit["id"])
        service.actor_port.decide.assert_not_awaited()

    def pending_sync(self, service, visit):
        own = service._local_id(visit)
        packet = envelope("offline-test-key", own, visit["remote"]["id"],
                          {"op": "sync", "id": visit["id"], "visit": service._wire(visit)}, self.now)
        visit["outbox"] = copy.deepcopy(packet)
        return packet

    async def test_old_signed_packet_and_hold_are_preserved_until_its_receipt_is_settled(self):
        service, visit, snapshot = self.make_service()
        own = service._local_id(visit)
        self.add_hold(visit, own)
        packet = self.pending_sync(service, visit)
        await service.tick()
        self.assertEqual(visit["outbox"], packet)
        self.assertIn(own, visit["budget_holds"])
        self.assertEqual(service._state["draws"][self.scope]["budget_hold"], snapshot)
        service._advance.assert_awaited_once_with(visit["id"])
        service.actor_port.decide.assert_not_awaited()

    async def test_real_signed_old_receipt_settles_then_a_new_packet_withdraws_own_hold(self):
        service, visit, _ = self.make_service()
        own = service._local_id(visit)
        self.add_hold(visit, own)
        packet = self.pending_sync(service, visit)
        sent = []

        async def reply(url, outgoing):
            sent.append(copy.deepcopy(outgoing))
            remote = copy.deepcopy(outgoing["payload"]["visit"])
            return envelope("offline-test-key", visit["remote"]["id"], own,
                            {"reply_to": outgoing["message_id"], "visit": remote}, self.now)

        service.transport.request = reply
        await service.tick()
        await service._publish_outing_hold(visit["id"])
        self.assertEqual(sent, [packet])
        self.assertNotIn("outbox", visit)
        self.assertIn(own, visit["budget_holds"])
        await service.tick()
        self.assertNotIn(own, visit.get("budget_holds", {}))
        await service._publish_outing_hold(visit["id"])
        self.assertEqual(len(sent), 2)
        self.assertNotEqual(sent[1]["message_id"], packet["message_id"])
        self.assertNotIn(own, sent[1]["payload"]["visit"].get("budget_holds", {}))
        self.assertEqual(visit["status"], "active")
        service.actor_port.decide.assert_not_awaited()

    async def test_an_already_scheduled_packet_retry_keeps_network_backoff(self):
        service, visit, _ = self.make_service()
        own = service._local_id(visit)
        self.add_hold(visit, own)
        packet = self.pending_sync(service, visit)
        visit["budget_notice_pending"] = True
        retry_at = self.now + 120
        visit["next_at"] = retry_at
        await service.tick()
        self.assertEqual(visit["outbox"], packet)
        self.assertEqual(visit["next_at"], retry_at)
        self.assertIn(own, visit["budget_holds"])
        service._advance.assert_not_awaited()
        service.actor_port.decide.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
