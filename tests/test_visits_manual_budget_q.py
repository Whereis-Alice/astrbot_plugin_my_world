"""Human invitations are unmetered; automatic peers exchange real life allowances."""
from __future__ import annotations

from dataclasses import replace
import copy
import json
from types import SimpleNamespace
import unittest

from tests import fake_astrbot
fake_astrbot.install()

from core.model_calls import model_context  # noqa: E402
from core.visits import VisitConfig, VisitService  # noqa: E402
from tests import test_engine as engine_fixture  # noqa: E402
from tests import test_visits_actor as actor_fixture  # noqa: E402
from tests import test_visit_tools as tool_fixture  # noqa: E402


class _WorldHarness:
    node_id = "study"

    def __init__(self, owner):
        self.owner = owner

    def __getattr__(self, name):
        return getattr(self.owner, name)


class ManualVisitBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cfg = VisitConfig(enabled=True, listen_host="127.0.0.1", listen_port=0,
            auto_enabled=True, outings_enabled=True, daily_calls=1, daily_tokens=1,
            max_calls=2, turns_per_actor=1, reception_start=10, reception_end=11)
        self.a = await self.make_world()
        self.b = await self.make_world()
        self.a.service.config = replace(self.cfg,
            public_url=f"http://127.0.0.1:{self.a.service.transport.bound_port}")
        self.b.service.config = replace(self.cfg,
            public_url=f"http://127.0.0.1:{self.b.service.transport.bound_port}")
        code = await self.b.service.create_pair_code()
        connection = await self.a.service.pair_world(self.b.service.config.public_url, code["code"])
        self.peer_id = connection["actors"][0]["id"]
        self.scope = engine_fixture.SESSION
        await self.a.service.set_enabled_scopes([self.scope])
        await self.b.service.set_enabled_scopes([self.scope])
        self.operations = []
        original = self.a.service.transport.request

        async def request(url, body):
            self.operations.append(body["payload"]["op"])
            return await original(url, body)

        self.a.service.transport.request = request

    async def make_world(self):
        world = _WorldHarness(self)
        await actor_fixture.VisitActorTests.asyncSetUp(world)
        platform = SimpleNamespace(meta=lambda: SimpleNamespace(name="aiocqhttp"))
        world.plugin.context = SimpleNamespace(
            get_platform_inst=lambda platform_id: platform if platform_id == "aiocqhttp" else None)
        world.service = VisitService(world.db, self.cfg, world.port, clock=world.clock.now, rng=lambda: 0)
        world.engine.visits = world.service
        world.plugin.visits = world.service
        self.addAsyncCleanup(world.service.close)
        await world.service.start()
        await world.service.set_enabled_scopes([engine_fixture.SESSION])
        world.llm.replies = []
        world.llm.default_reply = '{"accept":true,"say":"一起去湖边走走吧。","outing":{"place":"湖边","activity":"散步看落日","minutes":45}}'
        return world

    async def exhaust(self, world):
        pool = world.engine._background_budget_pool(purpose="world_visit", session_id=engine_fixture.SESSION)
        status = await world.db.call("background_status", pool["ledger"])
        for _ in range(pool["call_limit"] - status["calls"]):
            result = await world.db.call("reserve_background", pool["ledger"],
                calls=pool["call_limit"], tokens=0, reserve=0)
            self.assertTrue(result["allowed"])
        return pool["ledger"], copy.deepcopy(await world.db.call("background_status", pool["ledger"]))

    async def test_human_visit_ignores_both_daily_ledgers_reception_and_trip_quotas(self):
        first_ledger, first_before = await self.exhaust(self.a)
        second_ledger, second_before = await self.exhaust(self.b)
        self.a.plugin.in_self_initiated_call = lambda: False
        self.a.plugin._can_admin = lambda event: False
        self.a.engine._background_active = 100
        self.a.engine._background_failures = {(id(self.a.llm), "world_visit", self.scope, ""): {
            "until": self.a.clock.now() + 86400, "count": 3, "error": "自动通道暂时退避"}}
        event = SimpleNamespace(unified_msg_origin=self.scope,
            get_sender_id=lambda: "current-human", get_self_id=lambda: "bot",
            get_message_str=lambda: "去她家玩一会儿")
        context = SimpleNamespace(context=SimpleNamespace(event=event))
        result = await tool_fixture.tools.WorldVisitTool(plugin=self.a.plugin).call(context,
            peer_id=self.peer_id, direction="outgoing", reason="去湖边玩", evidence="去她家玩一会儿")
        self.assertIn("邀请处理结果", result)
        proposal = json.loads(result[result.index("{"):])
        self.assertEqual(proposal["status"], "inviting")
        self.assertEqual(len(self.a.llm.calls), 1)
        received = next(iter(self.b.service._state["visits"].values()))
        self.assertTrue(received["manual"])
        self.assertFalse((await self.b.port.describe(self.scope))["reception_open"])
        await self.b.service._advance(received["id"])
        self.assertTrue(received.get("arrived_at"))
        self.assertEqual(len(self.b.llm.calls), 1)
        self.assertEqual(await self.a.db.call("background_status", first_ledger), first_before)
        self.assertEqual(await self.b.db.call("background_status", second_ledger), second_before)
        for _ in range(4):
            with model_context("world_visit", "真人游玩", self.scope):
                reply = await self.a.engine._budgeted_generate(self.a.llm,
                    session_id=self.scope, system_prompt="各自决定自己的行动", prompt="一起玩", _visit_manual=True)
            self.assertTrue(reply.ok)
        self.assertEqual(len(self.a.llm.calls), 5)
        self.assertEqual(await self.a.db.call("background_status", first_ledger), first_before)
        self.assertEqual(self.a.service._state["draws"], {})
        self.assertEqual(self.a.engine._background_active, 100)
        self.assertFalse(any("_visit_manual" in call for call in self.a.llm.calls))

    async def test_automatic_peer_exhaustion_is_checked_before_any_proposal_and_retained(self):
        await self.exhaust(self.b)
        # Keep the started allowance settings unchanged: a real configuration
        # change deliberately invalidates cached remote observations on reload.
        self.a.service.config = replace(self.a.service.config, reception_start=0, reception_end=0)
        self.b.service.config = replace(self.b.service.config, reception_start=0, reception_end=0)
        binding = await self.a.service._prepare_peer(self.scope, self.peer_id)
        self.operations.clear()
        with self.assertRaises(ValueError):
            await self.a.service.invite(self.scope, binding, automatic=True)
        self.assertEqual(len(self.a.llm.calls), 0)
        self.assertEqual(len(self.b.llm.calls), 0)
        self.assertEqual(self.a.service._state["visits"], {})
        self.assertEqual(self.operations, ["world_bind", "world_availability"])
        connection = self.a.service._state["connections"][self.b.service._state["instance_id"]]
        hold = connection["availability"][self.peer_id]
        self.assertFalse(hold["allowed"])
        self.assertGreater(hold["reset_at"], self.a.clock.now())
        for _ in range(3):
            with self.assertRaises(ValueError):
                await self.a.service.invite(self.scope, binding, automatic=True)
        self.assertEqual(self.operations, ["world_bind", "world_availability"])
        await self.a.service._save()
        resumed = VisitService(self.a.db, self.a.service.config, self.a.port, clock=self.a.clock.now)
        self.addAsyncCleanup(resumed.close)
        await resumed._load()
        original = resumed.transport.request

        async def request(url, body):
            self.operations.append(body["payload"]["op"])
            return await original(url, body)

        resumed.transport.request = request
        self.assertFalse(await resumed._automatic_peer_budget_available(self.scope, binding))
        self.assertFalse(self.a.llm.calls)
        self.a.clock.advance(86401)
        self.b.clock.advance(86401)
        self.assertTrue(await resumed._automatic_peer_budget_available(self.scope, binding))
        self.assertEqual(self.operations, ["world_bind", "world_availability", "world_availability"])
        self.assertFalse(self.a.llm.calls)

    async def test_failed_contacts_are_only_diagnostics_and_successful_visits_are_history(self):
        self.a.service.config = replace(self.a.service.config, daily_tokens=0)
        self.a.llm.default_reply = '{"accept":false,"say":"今天想先整理花园，下次一起吧。"}'
        failed = await self.a.service.invite(self.scope, self.peer_id, manual=True)
        overview = await self.a.service.global_overview(self.scope)
        self.assertFalse(overview["history"])
        self.assertEqual([row["id"] for row in overview["diagnostics"]], [failed["id"]])
        stored = self.a.service._state["visits"][failed["id"]]
        stored.update(status="completed", arrived_at=self.a.clock.now(), ended_at=self.a.clock.now())
        overview = await self.a.service.global_overview(self.scope)
        self.assertEqual([row["id"] for row in overview["history"]], [failed["id"]])
        self.assertFalse(overview["diagnostics"])
        self.assertNotIn("snapshot", json.dumps(overview.get("automatic_budget")))

    async def test_local_automatic_exhaustion_is_saved_without_network_or_model_and_clears_next_day(self):
        await self.exhaust(self.a)
        self.a.service.config = replace(self.a.service.config, reception_start=0, reception_end=0, daily_tokens=0)
        with self.assertRaises(ValueError):
            await self.a.service.invite(self.scope, self.peer_id, automatic=True)
        self.assertFalse(self.operations)
        self.assertFalse(self.a.llm.calls)
        record = self.a.service._state["draws"][self.scope]
        self.assertFalse(record["automatic_budget"]["allowed"])
        self.assertIsNotNone(record["budget_hold"])
        resumed = VisitService(self.a.db, self.a.service.config, self.a.port, clock=self.a.clock.now)
        self.addAsyncCleanup(resumed.close)
        await resumed._load()
        self.assertFalse(await resumed._automatic_budget_available(self.scope))
        self.a.clock.advance(86401)
        self.assertTrue(await resumed._automatic_budget_available(self.scope))
        self.assertFalse(self.operations)
        self.assertFalse(self.a.llm.calls)

    async def test_remote_budget_race_is_recorded_and_manual_invitation_ignores_the_hold(self):
        self.a.service.config = replace(self.a.service.config,
            reception_start=0, reception_end=0, daily_calls=2, daily_tokens=0)
        self.b.service.config = replace(self.b.service.config,
            reception_start=0, reception_end=0, daily_tokens=0)
        binding = await self.a.service._prepare_peer(self.scope, self.peer_id)
        self.assertTrue(await self.a.service._automatic_peer_budget_available(self.scope, binding))
        before_ledger, before = await self.exhaust(self.b)
        failed = await self.a.service.invite(self.scope, binding, automatic=True)
        self.assertEqual(failed["status"], "declined")
        stored = self.a.service._state["visits"][failed["id"]]
        self.assertEqual(stored["budget_side"], "remote")
        self.assertFalse(stored["automatic_budget"]["allowed"])
        self.assertEqual(len(self.a.llm.calls), 1)
        self.assertFalse(self.b.llm.calls)
        self.assertTrue(self.a.service._peer_automatic_hold(self.a.service._find_peer(self.scope, binding)))
        self.operations.clear()
        with self.assertRaises(ValueError):
            await self.a.service.invite(self.scope, binding, automatic=True)
        self.assertFalse(self.operations)
        proposal = await self.a.service.invite(self.scope, binding, manual=True, reason="真人叫她们再约")
        self.assertEqual(proposal["status"], "inviting")
        received = self.b.service._state["visits"][proposal["id"]]
        await self.b.service._advance(received["id"])
        self.assertTrue(received.get("arrived_at"))
        self.assertTrue(received["manual"])
        self.assertEqual(await self.b.db.call("background_status", before_ledger), before)

    async def test_directory_capabilities_follow_the_remote_configuration_and_survive_reload(self):
        self.b.service.config = replace(self.b.service.config, outings_enabled=False)
        connection_id = self.b.service._state["instance_id"]
        result = await self.a.service.refresh_connection(connection_id)
        self.assertEqual(result["capabilities"], {"manual_visits": 1, "outings": 0, "scope_routing": 1})
        resumed = VisitService(self.a.db, self.a.service.config, self.a.port, clock=self.a.clock.now)
        self.addAsyncCleanup(resumed.close)
        await resumed._load()
        self.assertEqual(resumed._state["connections"][connection_id]["capabilities"], result["capabilities"])

    async def test_active_automatic_trip_continues_as_manual_on_both_sides_without_spending_daily_ledgers(self):
        self.a.service.config = replace(self.a.service.config,
            reception_start=0, reception_end=0, daily_calls=4, daily_tokens=0)
        self.b.service.config = replace(self.b.service.config,
            reception_start=0, reception_end=0, daily_calls=4, daily_tokens=0)
        invitation = await self.a.service.invite(self.scope, self.peer_id, automatic=True)
        visit_id = invitation["id"]
        await self.b.service._advance(visit_id)
        await self.a.service._send(visit_id, {"op": "status"})
        self.a.service._synchronize(self.a.service._state["visits"][visit_id],
            self.b.service._wire(self.b.service._state["visits"][visit_id]), self.peer_id)
        self.assertTrue(self.a.service._state["visits"][visit_id].get("arrived_at"))
        self.assertFalse(self.a.service._state["visits"][visit_id]["manual"])
        a_ledger, a_before = await self.exhaust(self.a)
        b_ledger, b_before = await self.exhaust(self.b)
        continued = await self.a.service.invite(self.scope, self.peer_id, manual=True, reason="再一起去花园玩")
        self.assertEqual(continued["id"], visit_id)
        self.assertTrue(self.a.service._state["visits"][visit_id]["manual"])
        self.assertTrue(self.b.service._state["visits"][visit_id]["manual"])
        await self.a.port.decide(self.scope, "turn", self.a.service._state["visits"][visit_id])
        await self.b.port.decide(self.scope, "turn", self.b.service._state["visits"][visit_id])
        self.assertEqual(await self.a.db.call("background_status", a_ledger), a_before)
        self.assertEqual(await self.b.db.call("background_status", b_ledger), b_before)

    async def test_host_outing_prompt_has_public_places_and_independent_guest_choices(self):
        for node in self.a.engine.world.nodes:
            node.prompt = "private-world-node-instruction"
        visit = {"local": self.a.local, "role": "host", "outing_version": 1, "manual": True,
                 "remote": {"id": self.peer_id, "name": "朋友", "world": "邻家"},
                 "outing": {"activity": "散步", "place": "花园",
                            "route": [{"place": f"路段{index}", "activity": "散步"} for index in range(64)]},
                 "outing_offer": {"place": "花园", "activity": "看花", "minutes": 45}}
        await self.a.port.decide(self.scope, "turn", visit)
        materials = json.loads(self.a.llm.calls[-1]["prompt"])
        self.assertTrue(materials["host_places"])
        self.assertLessEqual(len(materials["host_places"]), 12)
        self.assertEqual(len(materials["outing"]["route"]), 4)
        self.assertEqual(materials["outing"]["earlier_stops"], 60)
        self.assertNotIn("private-world-node-instruction", json.dumps(materials))
        decision = self.a.port._parse_decision("turn",
            '{"outing_accept":false,"return_after":true,"farewell":"晚上再联系你。",'
            '"say":"我们可以先去图书馆吗？","action":"把书放回自己的包里"}')
        self.assertFalse(decision["outing_accept"])
        self.assertTrue(decision["return_after"])
        self.assertEqual(decision["farewell"], "晚上再联系你。")
        self.assertEqual(decision["say"], "我们可以先去图书馆吗？")
        pacing = self.a.port._parse_decision("turn",
            '{"continue_activity":true,"next_contact_minutes":2.5,"say":"这一手先别告诉我答案。"}')
        self.assertTrue(pacing["continue_activity"])
        self.assertEqual(pacing["pause_minutes"], 2.5)

    async def test_signed_remote_budget_push_extends_shared_lease_and_holds_global_peer_after_reload(self):
        for world in (self.a, self.b):
            world.service.config = replace(world.service.config,
                reception_start=0, reception_end=0, daily_calls=4, daily_tokens=0)
        invitation = await self.a.service.invite(self.scope, self.peer_id, automatic=True)
        visit_id = invitation["id"]
        await self.b.service._advance(visit_id)
        self.a.service._synchronize(self.a.service._state["visits"][visit_id],
            self.b.service._wire(self.b.service._state["visits"][visit_id]), self.peer_id)
        before_expiry = self.a.service._state["visits"][visit_id]["expires_at"]
        await self.exhaust(self.b)
        budget = await self.b.port.automatic_budget(self.scope)
        async with self.b.service._lock:
            await self.b.service._hold_outing_budget(self.b.service._state["visits"][visit_id], budget)
        await self.b.service._publish_outing_hold(visit_id)
        visit = self.a.service._state["visits"][visit_id]
        self.assertGreater(visit["expires_at"], before_expiry)
        self.assertIn(self.peer_id, visit["budget_holds"])
        connection = self.a.service._state["connections"][self.b.service._state["instance_id"]]
        self.assertFalse(connection["availability"][self.peer_id]["allowed"])
        self.assertEqual(connection["availability"][self.peer_id]["reset_at"], budget["reset_at"])
        self.assertEqual(connection["availability"][self.peer_id]["used"], budget["used"])
        self.assertEqual(connection["availability"][self.peer_id]["limit"], budget["limit"])
        resumed = VisitService(self.a.db, self.a.service.config, self.a.port, clock=self.a.clock.now)
        self.addAsyncCleanup(resumed.close)
        await resumed._load()
        self.assertFalse(await resumed._automatic_peer_budget_available(self.scope, self.peer_id))
        self.assertEqual(len(self.a.llm.calls), 1)
        self.assertEqual(len(self.b.llm.calls), 1)

    async def test_public_record_pages_all_confirmed_facts_and_rejects_other_lives(self):
        self.a.llm.default_reply = '{"accept":false,"say":"这会儿想先休息一下。"}'
        row = await self.a.service.invite(self.scope, self.peer_id, manual=True)
        stored = self.a.service._state["visits"][row["id"]]
        own = self.a.service._local_id(stored)
        stored.update(status="completed", arrived_at=self.a.clock.now(), ended_at=self.a.clock.now(),
                      private={own: {"thought": "private-record-marker"}})
        stored["log"] = [{"seq": seq, "actor_id": own, "kind": "say", "text": f"相处事实{seq}"}
                         for seq in range(1, 601)]
        latest = await self.a.service.public_record(self.scope, row["id"])
        self.assertEqual([item["seq"] for item in latest["log"]], list(range(505, 601)))
        self.assertEqual((latest["log_offset"], latest["log_count"], latest["next_before"]), (504, 600, 504))
        self.assertNotIn("private-record-marker", json.dumps(latest))
        self.assertNotIn("private", latest)
        self.assertNotIn("log_prefix", latest)
        collected, before = list(latest["log"]), latest["next_before"]
        while before:
            previous = await self.a.service.public_record(self.scope, row["id"], before=before, limit=192)
            collected = previous["log"] + collected
            before = previous["next_before"]
        self.assertEqual([item["seq"] for item in collected], list(range(1, 601)))
        self.assertFalse(previous["has_more"])
        with self.assertRaises(ValueError):
            await self.a.service.public_record(engine_fixture.OTHER_SESSION, row["id"])
        for kwargs in ({"before": -1}, {"before": True}, {"limit": 0}, {"limit": 193}):
            with self.assertRaises(ValueError):
                await self.a.service.public_record(self.scope, row["id"], **kwargs)

    async def test_known_token_reservation_failure_is_published_as_a_hold_until_budget_changes(self):
        budget = await self.a.port.automatic_budget(self.scope)
        self.assertTrue(budget["allowed"])
        self.a.service._state["draws"][self.scope] = {"budget_hold": budget["snapshot"]}
        status = await self.a.service._local_automatic_availability(self.scope)
        self.assertFalse(status["allowed"])
        self.assertFalse(await self.a.service._automatic_budget_available(self.scope))
        self.a.service.config = replace(self.a.service.config, daily_tokens=100000)
        self.assertTrue((await self.a.service._local_automatic_availability(self.scope))["allowed"])
        self.assertTrue(await self.a.service._automatic_budget_available(self.scope))
        self.assertFalse(self.a.llm.calls)
