"""Real signed HTTP exchanges across independent servers and private local lives."""
from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from tests import fake_astrbot
fake_astrbot.install()
from core.db import AsyncDatabase  # noqa: E402 - install the SDK fixture before core imports
from core.visits import VisitConfig, VisitService  # noqa: E402
from core.visits.persistence import KEY  # noqa: E402
from core.visits.protocol import envelope, public_actor  # noqa: E402


class LivesActor:
    def __init__(self, prefix):
        self.prefix = prefix
        self.rows = {f"private-life-{prefix}-{n}": {
            "actor_id": f"{prefix}{n}", "persona_key": f"private-persona-{prefix}-{n}",
            "name": f"{prefix}{n}", "world": f"world-{prefix}", "ready": True,
            "chat_busy": False, "home_node": "home", "local_date": "2026-11-01"}
            for n in (1, 2, 3)}
        self.owners = {scope: scope for scope in self.rows}
        self.calls = []
        self.memories = {scope: {} for scope in self.rows}

    def canonical_scope(self, scope):
        return self.owners.get(scope, "")

    def lives(self):
        return [{"scope": scope, "label": row["name"]} for scope, row in self.rows.items()]

    async def describe(self, scope):
        return copy.deepcopy(self.rows[self.canonical_scope(scope)])

    async def decide(self, scope, phase, visit):
        self.calls.append((scope, phase, visit["id"]))
        return {"accept": True, "say": self.rows[scope]["name"] + ":" + phase,
                "action": "一起喝茶", "thought": "private-thought", "reflection": "private-reflection",
                "scene": {"place": "客厅", "activity": "一起喝茶"}, "outcome": "喝好了茶"}

    async def remember(self, scope, visit):
        if visit.get("arrived_at") and visit["status"] != "declined":
            self.memories[scope][visit["id"]] = copy.deepcopy(visit)


class GlobalPairingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 1793505600.0
        self.a_actor, self.b_actor = LivesActor("a"), LivesActor("b")
        self.a1, self.a2, self.a3 = tuple(self.a_actor.rows)
        self.b1, self.b2, self.b3 = tuple(self.b_actor.rows)
        config = VisitConfig(enabled=True, listen_host="127.0.0.1", listen_port=0, auto_enabled=False)
        self.a_db = AsyncDatabase(Path(self.directory.name) / "a.db")
        self.b_db = AsyncDatabase(Path(self.directory.name) / "b.db")
        self.addAsyncCleanup(self.a_db.close)
        self.addAsyncCleanup(self.b_db.close)
        self.a = VisitService(self.a_db, config, self.a_actor, clock=lambda: self.now, rng=lambda: .9)
        self.b = VisitService(self.b_db, config, self.b_actor, clock=lambda: self.now, rng=lambda: .9)
        await self.a.start()
        await self.b.start()
        self.a.config = replace(config, public_url=f"http://127.0.0.1:{self.a.transport.bound_port}")
        self.b.config = replace(config, public_url=f"http://127.0.0.1:{self.b.transport.bound_port}")
        self.addAsyncCleanup(self.a.close)
        self.addAsyncCleanup(self.b.close)
        self.messages = []
        for name, service in (("a", self.a), ("b", self.b)):
            original = service.transport.request
            async def capture(url, body, original=original, name=name):
                self.messages.append((name, copy.deepcopy(body)))
                return await original(url, body)
            service.transport.request = capture
        await self.a.set_enabled_scopes([self.a1, self.a2])
        await self.b.set_enabled_scopes([self.b1, self.b2])
        code = await self.b.create_pair_code()
        self.connection = await self.a.pair_world(self.b.config.public_url, code["code"], "邻居")
        self.peer = public_actor(await self.b_actor.describe(self.b1), self.b._state["instance_id"])["id"]

    async def advance(self, count=24):
        for _ in range(count):
            self.now += 65
            await self.a.tick()
            await self.b.tick()

    async def completed(self, sender, scope, peer, receiver, receiver_scope, **kwargs):
        visit = await sender.invite(scope, peer, **kwargs)
        await self.advance()
        local = sender._state["visits"][visit["id"]]
        remote = receiver._state["visits"][visit["id"]]
        self.assertEqual(local["status"], "completed", local)
        self.assertEqual(remote["status"], "completed", remote)
        self.assertEqual(local["log"], remote["log"])
        self.assertEqual(remote["scope"], receiver_scope)
        return visit["id"]

    def last_request(self, op, sender="a"):
        return next(body for name, body in reversed(self.messages) if name == sender and body["payload"]["op"] == op)

    async def test_one_global_pair_supports_two_lives_and_both_invitation_directions(self):
        first = await self.completed(self.a, self.a1, self.peer, self.b, self.b1)
        second = await self.completed(self.a, self.a2, self.peer, self.b, self.b1)
        a1_actor = public_actor(await self.a_actor.describe(self.a1), self.a._state["instance_id"])["id"]
        a2_actor = public_actor(await self.a_actor.describe(self.a2), self.a._state["instance_id"])["id"]
        third = await self.completed(self.b, self.b1, a1_actor, self.a, self.a1)
        fourth = await self.completed(self.b, self.b1, a2_actor, self.a, self.a2)
        self.assertEqual(set(self.a_actor.memories[self.a1]), {first, third})
        self.assertEqual(set(self.a_actor.memories[self.a2]), {second, fourth})
        self.assertEqual(set(self.b_actor.memories[self.b1]), {first, second, third, fourth})
        self.assertEqual(self.a_actor.memories[self.a3], {})
        self.assertEqual(self.b_actor.memories[self.b2], {})
        self.assertEqual(len(self.a._state["connections"]), 1)
        self.assertEqual(len(self.b._state["connections"]), 1)
        bindings = [p for p in self.a._state["peers"].values() if p["id"] == self.peer]
        self.assertEqual({p["scope"] for p in bindings}, {self.a1, self.a2})
        self.assertEqual(len({p["secret"] for p in bindings}), 2)
        self.assertEqual(sum(body["payload"]["op"] == "pair_world" for _, body in self.messages), 1)
        for scope, expected in ((self.a1, {first, third}), (self.a2, {second, fourth})):
            self.assertEqual({v["id"] for v in (await self.a.overview(scope))["visits"]}, expected)

    async def test_new_lives_stay_off_and_wire_and_overview_hide_private_identity(self):
        overview = await self.a.global_overview(self.a3)
        self.assertFalse(overview["scope_enabled"])
        self.assertEqual(overview["peers"], [])
        self.assertEqual({a["actor_id"] for a in self.connection["actors"]}, {"b1", "b2"})
        with self.assertRaises(ValueError):
            await self.a.invite(self.a3, self.peer)
        await self.completed(self.a, self.a1, self.peer, self.b, self.b1)
        wire = json.dumps(self.messages)
        self.assertNotIn("private-life-", wire)
        self.assertNotIn("private-persona-", wire)
        self.assertNotIn("private-thought", wire)
        self.assertNotIn("persona_key", wire)
        safe = json.dumps(await self.a.global_overview(self.a1))
        self.assertNotIn('"secret"', safe)
        self.assertNotIn('"anchors"', safe)
        for connection in self.a._state["connections"].values():
            for anchor in connection["anchors"]:
                self.assertNotIn(anchor["secret"], safe)
                self.assertNotIn(anchor["secret"], wire)

    async def test_daily_success_is_shared_by_remote_server_but_local_lives_are_independent(self):
        self.a.config = replace(self.a.config, auto_enabled=True, auto_probability=1, daily_peer_limit=1)
        self.a._random = lambda: 0
        # Admit the requested real exchange without also scheduling a2's automatic draw.
        await self.a.set_enabled_scopes([self.a1])
        await self.completed(self.a, self.a1, self.peer, self.b, self.b1, automatic=True)
        self.now += 3601
        other = public_actor(await self.b_actor.describe(self.b2), self.b._state["instance_id"])["id"]
        before = len(self.a_actor.calls)
        with self.assertRaises(ValueError):
            await self.a.invite(self.a1, other, automatic=True)
        self.assertEqual(len(self.a_actor.calls), before)
        record = self.a._state["draws"][self.a1]["peer_days"]
        self.assertEqual(set(record), {self.b._state["instance_id"]})
        self.assertEqual(next(iter(record.values()))["days"]["2026-11-01"]["successes"], 1)
        await self.a.set_enabled_scopes([self.a2])
        await self.completed(self.a, self.a2, self.peer, self.b, self.b1, automatic=True)
        self.assertEqual(self.a._state["draws"][self.a2]["peer_days"][self.b._state["instance_id"]]
                         ["days"]["2026-11-01"]["successes"], 1)

    async def test_closing_one_life_delivers_goodbye_and_rejects_offer_and_turn_replay(self):
        visit = await self.a.invite(self.a2, self.peer)
        offer = self.last_request("offer")
        await self.advance(2)
        await self.a.set_enabled_scopes([self.a1])
        self.assertIsNone(self.a.active(self.a2))
        await self.a.tick()
        self.assertIsNone(self.b.active(self.b1))
        self.assertNotIn("end_pending", self.a._state["visits"][visit["id"]])
        with self.assertRaises(ValueError):
            await self.a.invite(self.a2, self.peer)
        # The disabled receiving life rejects old cached offers as well.
        await self.b.set_enabled_scopes([self.b2])
        with self.assertRaises(ValueError):
            await self.b.receive(offer)
        peer = self.a._find_peer(self.a2, self.peer)
        turn = envelope(peer["secret"], self.a._local_id(self.a._state["visits"][visit["id"]]), self.peer,
                        {"op": "step", "id": visit["id"], "calls": 1}, self.now)
        with self.assertRaises(ValueError):
            await self.b.receive(turn)
        end = self.last_request("end")
        self.assertEqual(await self.b.receive(end), await self.b.receive(end))

    async def test_terminal_notifications_can_cross_when_both_lives_are_closed(self):
        visit = await self.a.invite(self.a2, self.peer)
        await self.advance(2)
        await self.a.set_enabled_scopes([self.a1])
        await self.b.set_enabled_scopes([self.b2])
        await self.a.tick()
        await self.b.tick()
        self.assertNotIn("end_pending", self.a._state["visits"][visit["id"]])
        self.assertNotIn("end_pending", self.b._state["visits"][visit["id"]])
        self.assertEqual(self.a._state["visits"][visit["id"]]["status"], "interrupted")
        self.assertEqual(self.b._state["visits"][visit["id"]]["status"], "interrupted")

    async def test_cached_requests_require_authentication_and_unchanged_target(self):
        await self.a._prepare_peer(self.a1, self.peer)
        request = self.last_request("world_bind")
        first = await self.b.receive(request)
        self.assertEqual(first, await self.b.receive(request))
        bad = copy.deepcopy(request)
        bad["signature"] = "0" * 64
        with self.assertRaises(ValueError):
            await self.b.receive(bad)
        anchor = self.a._state["connections"][self.connection["id"]]["anchors"][0]
        changed = envelope(anchor["secret"], request["sender"], "another-target", request["payload"], self.now,
                           message_id=request["message_id"])
        with self.assertRaises(ValueError):
            await self.b.receive(changed)

    async def test_persona_change_rejects_cached_offer_and_next_invitation_rebinds(self):
        first = await self.completed(self.a, self.a1, self.peer, self.b, self.b1)
        offer = self.last_request("offer")
        binding = self.last_request("world_bind")
        old_key = self.a._find_peer(self.a1, self.peer)["secret"]
        self.b_actor.rows[self.b1]["persona_key"] = "private-persona-changed"
        with self.assertRaises(ValueError):
            await self.b.receive(offer)
        with self.assertRaises(ValueError):
            await self.b.receive(binding)
        second = await self.completed(self.a, self.a1, self.peer, self.b, self.b1)
        self.assertNotEqual(old_key, self.a._find_peer(self.a1, self.peer)["secret"])
        self.assertNotEqual(first, second)
        with self.assertRaises(ValueError):
            await self.b.receive(offer)
        self.assertEqual(sum(body["payload"]["op"] == "pair_world" for _, body in self.messages), 1)

    async def test_disable_reenable_rotates_binding_and_old_receipts_stay_invalid(self):
        await self.completed(self.a, self.a1, self.peer, self.b, self.b1)
        offer = self.last_request("offer")
        binding = self.last_request("world_bind")
        old_key = self.a._find_peer(self.a1, self.peer)["secret"]
        await self.b.set_enabled_scopes([self.b2])
        with self.assertRaises(ValueError):
            await self.b.receive(binding)
        await self.b.set_enabled_scopes([self.b1, self.b2])
        with self.assertRaises(ValueError):
            await self.b.receive(offer)
        await self.completed(self.a, self.a1, self.peer, self.b, self.b1)
        self.assertNotEqual(old_key, self.a._find_peer(self.a1, self.peer)["secret"])
        with self.assertRaises(ValueError):
            await self.b.receive(offer)

    async def test_connection_removal_invalidates_cached_control_pair_and_terminal_messages(self):
        await self.completed(self.a, self.a1, self.peer, self.b, self.b1)
        requests = [self.last_request(op) for op in ("pair_world", "world_bind", "offer")]
        requests.append(self.last_request("end", sender="a") if any(
            name == "a" and body["payload"]["op"] == "end" for name, body in self.messages)
            else self.last_request("sync"))
        await self.b.remove_connection(self.a._state["instance_id"])
        for request in requests:
            with self.subTest(op=request["payload"]["op"]), self.assertRaises(ValueError):
                await self.b.receive(request)

    async def test_ownership_and_actor_changes_reject_old_bindings_and_cached_catalog(self):
        await self.a._prepare_peer(self.a1, self.peer)
        request = self.last_request("world_bind")
        await self.a.refresh_connection(self.connection["id"])
        catalog = self.last_request("world_catalog")
        self.b_actor.owners[self.b1] = self.b2
        with self.assertRaises(ValueError):
            await self.b.receive(request)
        with self.assertRaises(ValueError):
            await self.b.receive(catalog)
        self.b_actor.owners[self.b1] = self.b1
        self.b_actor.rows[self.b1]["actor_id"] = "b1-replaced"
        with self.assertRaises(ValueError):
            await self.b.receive(request)

    async def test_concurrent_double_ended_binding_derives_identical_keys(self):
        own = public_actor(await self.a_actor.describe(self.a1), self.a._state["instance_id"])["id"]
        await asyncio.gather(self.a._prepare_peer(self.a1, self.peer), self.b._prepare_peer(self.b1, own))
        self.assertEqual(self.a._find_peer(self.a1, self.peer)["secret"], self.b._find_peer(self.b1, own)["secret"])
        await self.completed(self.a, self.a1, self.peer, self.b, self.b1)

    async def test_disabled_binding_cannot_be_reenabled_by_actor_or_server_invitation(self):
        await self.a._prepare_peer(self.a1, self.peer)
        self.a._find_peer(self.a1, self.peer)["enabled"] = False
        other = public_actor(await self.b_actor.describe(self.b2), self.b._state["instance_id"])["id"]
        await self.a._prepare_peer(self.a1, other)
        self.a._find_peer(self.a1, other)["enabled"] = False
        self.assertEqual((await self.a.overview(self.a1))["peers"], [])
        self.assertEqual(self.a._automatic_peers(self.a1), [])
        for target in (self.peer, self.connection["id"]):
            with self.subTest(target=target), self.assertRaises(ValueError):
                await self.a.invite(self.a1, target)
        self.assertEqual(self.a_actor.calls, [])
        self.assertFalse(self.a._find_peer(self.a1, self.peer)["enabled"])

    async def test_active_trip_keeps_its_key_when_remote_identity_token_changes(self):
        visit = await self.a.invite(self.a1, self.peer)
        binding = self.last_request("world_bind")
        own = self.a._local_id(self.a._state["visits"][visit["id"]])
        peer = self.b._find_peer(self.b1, own)
        before = copy.deepcopy(peer)
        anchor = self.a._state["connections"][self.connection["id"]]["anchors"][0]
        payload = {**binding["payload"], "actor_token": "0" * 64}
        changed = envelope(anchor["secret"], binding["sender"], binding["target"], payload, self.now)
        with self.assertRaises(ValueError):
            await self.b.receive(changed)
        self.assertEqual(self.b._find_peer(self.b1, own), before)
        repeat = envelope(anchor["secret"], binding["sender"], binding["target"], binding["payload"], self.now)
        await self.b.receive(repeat)
        self.assertEqual(self.b._find_peer(self.b1, own)["secret"], before["secret"])
        await self.advance()
        self.assertEqual(self.a._state["visits"][visit["id"]]["status"], "completed")

    async def test_regrouped_life_rebinds_without_stale_scope_hiding_current_actor(self):
        old = await self.completed(self.a, self.a1, self.peer, self.b, self.b1)
        offer = self.last_request("offer")
        self.a_actor.rows[self.a3] = copy.deepcopy(self.a_actor.rows[self.a1])
        self.a_actor.owners[self.a1] = self.a3
        await self.a.set_enabled_scopes([self.a2, self.a3])
        new = await self.completed(self.a, self.a3, self.peer, self.b, self.b1)
        self.assertEqual(set(self.a_actor.memories[self.a1]), {old})
        self.assertEqual(set(self.a_actor.memories[self.a3]), {new})
        with self.assertRaises(ValueError):
            await self.b.receive(offer)

    async def test_catalog_does_not_publish_a_description_invalidated_while_awaiting(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.b_actor.describe
        delayed_once = False
        async def delayed(scope):
            nonlocal delayed_once
            description = await original(scope)
            if scope == self.b1 and not delayed_once:
                delayed_once = True
                entered.set()
                await release.wait()
            return description
        self.b_actor.describe = delayed
        pending = asyncio.create_task(self.a.refresh_connection(self.connection["id"]))
        await entered.wait()
        self.b_actor.rows[self.b1]["persona_key"] = "private-persona-new"
        release.set()
        with self.assertRaises(ValueError):
            await pending
        self.assertIn(self.connection["id"], self.a._state["connections"])
        self.assertIn("未通过确认", self.a._state["connections"][self.connection["id"]]["catalog_error"])

    async def test_catalog_network_failure_keeps_pairing_and_does_not_claim_an_old_server(self):
        from unittest.mock import AsyncMock, patch
        before = copy.deepcopy(self.a._state["connections"][self.connection["id"]])
        async def disconnected(*args):
            # The HTTP transport deliberately wraps connection failures in
            # ValueError; presentation must retain that underlying cause.
            raise ValueError("暂时联系不上对方，请检查串门服务") from TimeoutError()
        with patch.object(self.a, "_control_request", AsyncMock(side_effect=disconnected)):
            with self.assertRaises(ValueError):
                await self.a.refresh_connection(self.connection["id"])
        after = self.a._state["connections"][self.connection["id"]]
        self.assertEqual(after["anchors"], before["anchors"])
        self.assertEqual(after["actors"], before["actors"])
        self.assertIn("联系不上", after["catalog_error"])
        self.assertNotIn("旧版", after["catalog_error"])
        self.assertGreater(after["next_catalog_at"], self.now)


class LegacyGlobalMigrationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = GlobalPairingTests.asyncSetUp
    advance = GlobalPairingTests.advance
    completed = GlobalPairingTests.completed

    async def legacy_pairings(self):
        self.a._state["connections"] = {}
        self.b._state["connections"] = {}
        self.a._state["peers"] = {}
        self.b._state["peers"] = {}
        for own_scope, remote_scope in ((self.a1, self.b1), (self.a2, self.b2)):
            code = await self.b.create_pair_code(remote_scope)
            await self.a.pair(own_scope, self.b.config.public_url, code["code"])
        old_a = copy.deepcopy(self.a._state)
        old_b = copy.deepcopy(self.b._state)
        for state in (old_a, old_b):
            for key in ("world_pairing_version", "connections", "enabled_scopes", "life_generations"):
                state.pop(key, None)
            for peer in state["peers"].values():
                peer.pop("connection_id", None)
                peer.pop("life_generation", None)
        return old_a, old_b

    async def reload(self, old_a, old_b):
        await self.a.close()
        await self.b.close()
        await self.a_db.call("kv_set", KEY, old_a)
        await self.b_db.call("kv_set", KEY, old_b)
        for name, db, actor in (("a", self.a_db, self.a_actor), ("b", self.b_db, self.b_actor)):
            config = replace(getattr(self, name).config, max_peers=1)
            service = VisitService(db, config, actor, clock=lambda: self.now, rng=lambda: .9)
            await service.start()
            service.config = replace(config, public_url=f"http://127.0.0.1:{service.transport.bound_port}")
            self.addAsyncCleanup(service.close)
            setattr(self, name, service)
        for local, remote in ((self.a, self.b), (self.b, self.a)):
            for conn in local._state["connections"].values():
                conn["url"] = remote.config.public_url
            for peer in local._state["peers"].values():
                peer["url"] = remote.config.public_url

    async def test_migration_merges_server_anchors_keeps_credentials_and_opens_only_old_lives(self):
        old_a, old_b = await self.legacy_pairings()
        keys = {peer["id"]: peer["secret"] for peer in old_a["peers"].values()}
        await self.reload(old_a, old_b)
        self.assertEqual(self.a._state["enabled_scopes"], [self.a1, self.a2])
        self.assertEqual(self.b._state["enabled_scopes"], [self.b1, self.b2])
        self.assertFalse((await self.a.overview(self.a3))["scope_enabled"])
        self.assertEqual(len(self.a._state["connections"]), 1)
        conn = next(iter(self.a._state["connections"].values()))
        self.assertEqual(len(conn["anchors"]), 2)
        self.assertEqual({peer["id"]: peer["secret"] for peer in self.a._state["peers"].values()}, keys)
        # Original credentials still exchange directly, including with an old peer
        # which does not implement the global management operations.
        original = self.b.receive
        async def legacy_receive(body):
            if body["payload"].get("op") in {"world_catalog", "world_bind"}:
                raise ValueError("old protocol")
            return await original(body)
        self.b.transport.handler = legacy_receive
        await self.completed(self.a, self.a1, self.peer, self.b, self.b1)
        self.b.transport.handler = original
        await self.a.refresh_connection(conn["id"])
        await self.a.set_enabled_scopes([self.a1, self.a2, self.a3])
        await self.completed(self.a, self.a3, self.peer, self.b, self.b1)
        self.assertEqual(len(self.a._state["connections"]), 1)

    async def test_migration_retains_explicit_participation_and_old_worlds_above_new_limit(self):
        old_a, old_b = await self.legacy_pairings()
        old_a["enabled_scopes"] = [self.a1]
        second = copy.deepcopy(next(iter(old_a["peers"].values())))
        second["id"] = "another-server:another-actor"
        second["actor"].update(id=second["id"], instance_id="another-server", actor_id="another-actor")
        old_a["peers"][second["id"]] = second
        await self.reload(old_a, old_b)
        self.assertEqual(self.a._state["enabled_scopes"], [self.a1])
        self.assertEqual(len(self.a._state["connections"]), 2)
        remote = copy.deepcopy(second["actor"])
        remote.update(id="third-server:third-actor", instance_id="third-server", actor_id="third-actor")
        with self.assertRaises(ValueError):
            self.a._add_peer(self.a1, await self.a_actor.describe(self.a1), remote, "https://third.example", "key")
        self.assertEqual(len(self.a._state["connections"]), 2)

    async def test_migrated_pair_discovers_upgraded_persona_without_manual_refresh(self):
        old_a, old_b = await self.legacy_pairings()
        await self.reload(old_a, old_b)
        conn = next(iter(self.a._state["connections"].values()))
        anchor_keys = {anchor["secret"] for anchor in conn["anchors"]}
        old_key = self.a._find_peer(self.a1, self.peer)["secret"]
        self.b_actor.rows[self.b1]["persona_key"] = "private-upgraded-persona"
        await self.completed(self.a, self.a1, self.peer, self.b, self.b1)
        self.assertTrue(conn["catalog_ready"])
        self.assertNotEqual(old_key, self.a._find_peer(self.a1, self.peer)["secret"])
        self.assertEqual(anchor_keys, {anchor["secret"] for anchor in conn["anchors"]})

    async def test_migration_discovery_does_not_commit_after_close(self):
        old_a, old_b = await self.legacy_pairings()
        await self.reload(old_a, old_b)
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.a.transport.request
        async def delayed(url, body):
            reply = await original(url, body)
            if body["payload"].get("op") == "world_catalog":
                entered.set()
                await release.wait()
            return reply
        self.a.transport.request = delayed
        pending = asyncio.create_task(self.a.invite(self.a1, self.peer))
        await asyncio.wait_for(entered.wait(), 2)
        await self.a.close()
        before = copy.deepcopy(self.a._state)
        release.set()
        with self.assertRaises(ValueError):
            await asyncio.wait_for(pending, 2)
        self.assertEqual(before, self.a._state)
        self.assertEqual(self.a_actor.calls, [])
