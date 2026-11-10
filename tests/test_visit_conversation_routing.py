"""Signed world exchanges select the matching conversation before any inference."""
from __future__ import annotations

import copy
from dataclasses import replace
import json
import unittest

from tests import test_visits_global_pairing as fixture
from core.visits.protocol import envelope, public_actor


class ConversationRoutingTests(unittest.IsolatedAsyncioTestCase):
    advance = fixture.GlobalPairingTests.advance
    completed = fixture.GlobalPairingTests.completed
    last_request = fixture.GlobalPairingTests.last_request

    async def asyncSetUp(self):
        await fixture.GlobalPairingTests.asyncSetUp(self)
        self.a_contexts = {scope: [f"qq|group|private-group-{index}"]
                           for index, scope in enumerate(self.a_actor.rows, 1)}
        self.b_contexts = {scope: [f"qq|group|private-group-{index}"]
                           for index, scope in enumerate(self.b_actor.rows, 1)}
        self.install_routes()
        await self.a.set_enabled_scopes([self.a1, self.a2, self.a3])
        await self.b.set_enabled_scopes([self.b3, self.b1, self.b2])

    def install_routes(self):
        self.a_actor.routing_contexts = lambda scope: list(self.a_contexts.get(scope, []))
        self.b_actor.routing_contexts = lambda scope: list(self.b_contexts.get(scope, []))

    def remote_id(self, service, port, scope):
        return public_actor(port.rows[scope], service._state["instance_id"])["id"]

    async def test_shuffled_three_same_name_lives_route_both_records_to_matching_group(self):
        for port in (self.a_actor, self.b_actor):
            for row in port.rows.values():
                row["name"] = "同名角色"
        connection = self.a._state["connections"][self.connection["id"]]
        connection["default_actor"] = self.remote_id(self.b, self.b_actor, self.b3)
        await self.a.refresh_connection(connection["id"])
        for own, away in ((self.a1, self.b1), (self.a2, self.b2), (self.a3, self.b3)):
            target = self.remote_id(self.b, self.b_actor, away)
            self.assertEqual([row["id"] for row in self.a._scoped_candidates(own)], [target])
            self.assertEqual([row["id"] for row in self.a._automatic_peers(own)], [target])
            visit_id = await self.completed(self.a, own, connection["id"], self.b, away)
            self.assertEqual(self.a._state["visits"][visit_id]["scope"], own)
            self.assertEqual(self.b._state["visits"][visit_id]["scope"], away)
            self.assertEqual(len((await self.a.global_overview(own))["history"]), 1)
        payloads = json.dumps(self.messages)
        self.assertNotIn("private-group", payloads)
        self.assertNotIn("private-life", payloads)
        self.assertTrue((await self.a.global_overview(self.a1))["scope_routing"])
        for row in connection["actors"]:
            self.assertEqual(set(row), {"id", "actor_id", "instance_id", "name", "world"})

    async def test_no_match_ambiguity_or_budget_hold_never_falls_back_to_other_group(self):
        cid = self.connection["id"]
        await self.a.refresh_connection(cid)
        original = copy.deepcopy(self.b_contexts)
        self.b_contexts[self.b1] = ["unrelated-group"]
        await self.a.refresh_connection(cid)
        self.assertEqual(self.a._automatic_peers(self.a1), [])
        with self.assertRaises(ValueError):
            await self.a.invite(self.a1, cid, manual=True)
        self.b_contexts = copy.deepcopy(original)
        self.b_contexts[self.b3] = list(self.b_contexts[self.b1])
        await self.a.refresh_connection(cid)
        self.assertEqual(self.a._automatic_peers(self.a1), [])
        with self.assertRaises(ValueError):
            await self.a.invite(self.a1, cid, automatic=True)
        self.b_contexts = original
        await self.a.refresh_connection(cid)
        await self.a._prepare_peer(self.a1, self.peer)
        self.a._state["connections"][cid]["availability"] = {
            self.peer: {"allowed": False, "reset_at": self.now + 3600}}
        self.assertEqual(self.a._automatic_peers(self.a1), [])
        with self.assertRaises(ValueError):
            await self.a.invite(self.a1, self.peer, automatic=True)
        self.assertEqual(self.a_actor.calls + self.b_actor.calls, [])
        self.assertEqual(self.a._state["visits"], {})

    async def test_manual_wrong_actor_and_signed_cross_group_bind_are_rejected(self):
        other = self.remote_id(self.b, self.b_actor, self.b2)
        with self.assertRaises(ValueError):
            await self.a.invite(self.a1, other, manual=True)
        connection = self.a._state["connections"][self.connection["id"]]
        anchor = connection["anchors"][0]
        local = await self.a_actor.describe(self.a1)
        body = envelope(anchor["secret"], anchor["local_id"], anchor["remote_id"],
            {"op": "world_bind", "actor": public_actor(local, self.a._state["instance_id"]),
             "actor_token": self.a._binding_token(self.a1, local, anchor), "target_actor": other,
             "routing": self.a._routing_proof(self.a1, anchor)}, self.now)
        with self.assertRaises(ValueError):
            await self.b.receive(body)
        self.assertEqual(self.a_actor.calls + self.b_actor.calls, [])
        self.assertEqual(self.b._state["visits"], {})

    async def test_unknown_metadata_is_discovered_even_when_old_actor_catalog_is_populated(self):
        cid = self.connection["id"]
        self.assertTrue(self.a._state["connections"][cid]["actors"])
        self.assertNotIn("routing", self.a._state["connections"][cid])
        self.a._state["connections"][cid]["next_catalog_at"] = self.now + 3600
        self.a.config = replace(self.a.config, auto_enabled=True)
        for row in self.a_actor.rows.values():
            row["ready"] = False
        await self.a._automatic()
        self.assertTrue(self.a._routing_catalog_current(self.a._state["connections"][cid]))
        count = sum(body["payload"]["op"] == "world_catalog" for _, body in self.messages)
        await self.a._automatic()
        self.assertEqual(sum(body["payload"]["op"] == "world_catalog" for _, body in self.messages), count)
        self.assertEqual(self.a_actor.calls, [])

    async def test_contact_discovers_membership_once_despite_pre_upgrade_catalog_cooldown(self):
        cid = self.connection["id"]
        self.a._state["connections"][cid]["next_catalog_at"] = self.now + 3600
        del self.b_actor.routing_contexts
        before = len(self.messages)
        for _ in range(2):
            with self.assertRaises(ValueError):
                await self.a.invite(self.a1, self.peer, manual=True)
        requests = [body for _, body in self.messages[before:] if body["payload"]["op"] == "world_catalog"]
        self.assertEqual(len(requests), 1)
        self.assertEqual(self.a_actor.calls + self.b_actor.calls, [])

    async def test_exhausted_local_budget_skips_even_first_unknown_routing_discovery(self):
        self.a.config = replace(self.a.config, auto_enabled=True)
        self.a._state["connections"][self.connection["id"]]["next_catalog_at"] = self.now + 3600

        async def exhausted(scope):
            return False

        self.a._automatic_budget_available = exhausted
        before = copy.deepcopy(self.messages)
        await self.a._automatic()
        self.assertEqual(self.messages, before)
        self.assertEqual(self.a_actor.calls, [])
        self.assertEqual(self.a._state["draws"], {})

    async def test_rolling_upgrade_preserves_pairing_but_waits_for_verified_membership(self):
        del self.b_actor.routing_contexts
        with self.assertRaises(ValueError):
            await self.a.invite(self.a1, self.peer)
        self.assertIn(self.connection["id"], self.a._state["connections"])
        self.assertEqual(self.a._scoped_candidates(self.a1), [])
        own = self.remote_id(self.a, self.a_actor, self.a1)
        with self.assertRaises(ValueError):
            await self.b.invite(self.b1, own)
        self.install_routes()
        await self.a.refresh_connection(self.connection["id"])
        await self.completed(self.a, self.a1, self.peer, self.b, self.b1)

    async def test_unverified_old_binding_cannot_create_new_inbound_offer(self):
        del self.a_actor.routing_contexts
        del self.b_actor.routing_contexts
        other = self.remote_id(self.b, self.b_actor, self.b2)
        await self.a._prepare_peer(self.a1, other)
        peer = self.a._find_peer(self.a1, other)
        before = copy.deepcopy(peer)
        self.install_routes()
        body = envelope(peer["secret"], self.remote_id(self.a, self.a_actor, self.a1), other,
                        {"op": "offer", "id": "1" * 32, "role": "host"}, self.now)
        with self.assertRaises(ValueError):
            await self.b.receive(body)
        self.assertEqual(self.a._find_peer(self.a1, other), before)
        self.assertEqual(self.b._state["visits"], {})

    async def test_legacy_pairing_establishes_trust_but_requires_verified_bind_before_offer(self):
        code = await self.b.create_pair_code(self.b1)
        target = (await self.a.pair(self.a1, self.b.config.public_url, code["code"]))["id"]
        peer = self.a._find_peer(self.a1, target)
        self.assertFalse(self.a._routing_binding_current(peer))
        body = envelope(peer["secret"], self.remote_id(self.a, self.a_actor, self.a1), target,
            {"op": "offer", "id": "f" * 32, "role": "host"}, self.now)
        with self.assertRaises(ValueError):
            await self.b.receive(body)
        await self.a._prepare_peer(self.a1, target)
        self.assertTrue(self.a._routing_binding_current(self.a._find_peer(self.a1, target)))
        self.assertEqual(self.b._state["visits"], {})

    async def test_active_legacy_encounter_finishes_with_original_keys_and_history_owners(self):
        del self.a_actor.routing_contexts
        del self.b_actor.routing_contexts
        other = self.remote_id(self.b, self.b_actor, self.b2)
        record = await self.a.invite(self.a1, other)
        await self.advance(2)
        visit = self.a._state["visits"][record["id"]]
        self.assertTrue(visit.get("arrived_at"))
        key = self.a._find_peer(self.a1, other)["secret"]
        self.install_routes()
        await self.advance()
        self.assertEqual(self.a._find_peer(self.a1, other)["secret"], key)
        self.assertEqual(self.a._state["visits"][record["id"]]["status"], "completed")
        self.assertEqual(self.b._state["visits"][record["id"]]["status"], "completed")
        self.assertEqual(self.a._state["visits"][record["id"]]["scope"], self.a1)
        self.assertEqual(self.b._state["visits"][record["id"]]["scope"], self.b2)
        self.assertEqual((await self.a.overview(self.a2))["visits"], [])

    async def test_membership_change_during_catalog_or_bind_wait_discards_preparation(self):
        for phase in ("world_catalog", "world_bind"):
            with self.subTest(phase=phase):
                self.a_contexts[self.a1] = ["qq|group|private-group-1"]
                connection = self.a._state["connections"][self.connection["id"]]
                connection["next_catalog_at"] = 0
                original = self.a.transport.request

                async def change_after_request(url, body):
                    result = await original(url, body)
                    if body["payload"].get("op") == phase:
                        self.a_contexts[self.a1] = ["qq|group|another-membership"]
                    return result

                self.a.transport.request = change_after_request
                try:
                    with self.assertRaises(ValueError):
                        await self.a.invite(self.a1, self.peer, manual=True)
                finally:
                    self.a.transport.request = original
                self.assertEqual(self.a_actor.calls, [])
                self.assertEqual(self.a._state["visits"], {})

    async def test_cached_bind_and_catalog_reject_membership_changes(self):
        await self.a._prepare_peer(self.a1, self.peer)
        binding = self.last_request("world_bind")
        catalog = self.last_request("world_catalog")
        self.b_contexts[self.b1] = ["qq|group|replacement-group"]
        for request in (binding, catalog):
            with self.subTest(op=request["payload"]["op"]), self.assertRaises(ValueError):
                await self.b.receive(request)
        with self.assertRaises(ValueError):
            await self.a.invite(self.a1, self.peer)
        self.assertEqual(self.a_actor.calls, [])

    async def test_shared_remote_life_keeps_member_match_without_exposing_other_member_context(self):
        self.b_contexts[self.b1].append(self.b_contexts[self.b2][0])
        await self.b.set_enabled_scopes([self.b1, self.b3])
        await self.a.refresh_connection(self.connection["id"])
        await self.completed(self.a, self.a2, self.connection["id"], self.b, self.b1)
        self.assertEqual((await self.a.overview(self.a1))["visits"], [])

    async def test_new_inbound_offer_rechecks_membership_after_final_description_or_budget_wait(self):
        await self.a._prepare_peer(self.a1, self.peer)
        peer = self.a._find_peer(self.a1, self.peer)
        original_description = self.b_actor.describe
        original_budget = self.b._local_automatic_availability
        for phase in ("description", "budget"):
            with self.subTest(phase=phase):
                self.b_contexts[self.b1] = ["qq|group|private-group-1"]
                reads = 0

                async def change_during_description(scope):
                    nonlocal reads
                    result = await original_description(scope)
                    reads += 1
                    if phase == "description" and reads == 2:
                        self.b_contexts[self.b1] = ["qq|group|replacement-group"]
                    return result

                async def change_during_budget(scope, local=None):
                    result = await original_budget(scope, local)
                    if phase == "budget":
                        self.b_contexts[self.b1] = ["qq|group|replacement-group"]
                    return result

                self.b_actor.describe = change_during_description
                self.b._local_automatic_availability = change_during_budget
                self.b.config = replace(self.b.config, outings_enabled=True)
                body = envelope(peer["secret"], self.remote_id(self.a, self.a_actor, self.a1), self.peer,
                    {"op": "offer", "id": ("d" if phase == "description" else "e") * 32,
                     "role": "host", "manual": phase == "description", "max_calls": 6,
                     "turn_limit": 2, "duration": 600, "calls": 1}, self.now)
                try:
                    with self.assertRaises(ValueError):
                        await self.b.receive(body)
                finally:
                    self.b_actor.describe = original_description
                    self.b._local_automatic_availability = original_budget
                self.assertEqual(self.b._state["visits"], {})
                self.assertEqual(self.a_actor.calls + self.b_actor.calls, [])

    async def test_new_outbound_offer_rechecks_membership_after_description_or_durable_save(self):
        await self.a._prepare_peer(self.a1, self.peer)
        peer = self.a._find_peer(self.a1, self.peer)
        original_description, original_save = self.a_actor.describe, self.a._save
        for phase in ("description", "save"):
            with self.subTest(phase=phase):
                self.a_contexts[self.a1] = ["qq|group|private-group-1"]
                local = await original_description(self.a1)
                visit_id = ("7" if phase == "description" else "8") * 32
                self.a._state["visits"][visit_id] = self.a._new_visit(self.a1, peer, local, visit_id,
                                                                       "guest", "一起喝茶", False)

                async def change_during_description(scope):
                    result = await original_description(scope)
                    if phase == "description":
                        self.a_contexts[self.a1] = ["qq|group|replacement-group"]
                    return result

                async def change_during_save():
                    await original_save()
                    if phase == "save":
                        self.a_contexts[self.a1] = ["qq|group|replacement-group"]

                self.a_actor.describe, self.a._save = change_during_description, change_during_save
                before = len(self.messages)
                try:
                    with self.assertRaises(ValueError):
                        await self.a._send(visit_id, {"op": "offer", "role": "host", "manual": True,
                            "max_calls": 6, "turn_limit": 2, "duration": 600, "calls": 1})
                finally:
                    self.a_actor.describe, self.a._save = original_description, original_save
                self.assertEqual(len(self.messages), before)
                self.assertEqual(self.b._state["visits"], {})
