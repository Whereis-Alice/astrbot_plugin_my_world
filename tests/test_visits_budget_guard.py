"""Automatic contact waits for affordable local inference before network binding."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
import unittest

from core.db import AsyncDatabase
from core.visits import VisitConfig, VisitService
from core.visits.decisions import VisitDecisionError
from tests import test_engine as engine_fixture
from tests import test_visits_actor as actor_fixture
from tests import test_visits_protocol as protocol_fixture


S = engine_fixture.SESSION


class VisitBudgetGuardTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"

    async def asyncSetUp(self):
        await actor_fixture.VisitActorTests.asyncSetUp(self)
        platform = SimpleNamespace(meta=lambda: SimpleNamespace(name="aiocqhttp"))
        self.plugin.context = SimpleNamespace(
            get_platform_inst=lambda platform_id: platform if platform_id == "aiocqhttp" else None)
        config = VisitConfig(enabled=True, listen_host="127.0.0.1", listen_port=0,
                             auto_enabled=False, reception_start=0, reception_end=0)
        self.a = VisitService(self.db, config, self.port, clock=self.clock.now)
        self.engine.visits = self.a
        remote_db = AsyncDatabase(self.data_dir / "remote.db")
        self.addAsyncCleanup(remote_db.close)
        self.b = VisitService(remote_db, config, protocol_fixture.Actor("b"), clock=self.clock.now)
        self.b.actor_port.routing_contexts = lambda scope: ["aiocqhttp:GroupMessage:1001"] if scope == "life-b" else []
        await self.a.start()
        await self.b.start()
        self.addAsyncCleanup(self.a.close)
        self.addAsyncCleanup(self.b.close)
        self.a.config = replace(config, public_url=f"http://127.0.0.1:{self.a.transport.bound_port}")
        self.b.config = replace(config, public_url=f"http://127.0.0.1:{self.b.transport.bound_port}")
        code = await self.b.create_pair_code("life-b")
        self.peer = (await self.a.pair(S, self.b.config.public_url, code["code"]))["id"]
        await self.a.refresh_connection(self.b._state["instance_id"])
        self.a.config = replace(self.a.config, auto_enabled=True)
        self.requests = []
        original = self.a.transport.request

        async def request(url, body):
            self.requests.append(body["payload"]["op"])
            return await original(url, body)

        self.a.transport.request = request
        self.llm.default_reply = '{"accept":false,"say":"今天先留在家里。"}'

    async def reserve(self, *, calls=0, tokens=0, reserve=50):
        pool = self.engine._background_budget_pool(purpose="world_visit", session_id=S)
        return await self.db.call("reserve_background", pool["ledger"], calls=calls, tokens=tokens, reserve=reserve)

    def limits(self, **changes):
        self.engine.visits.config = replace(self.engine.visits.config, **changes)

    async def retry_ticks(self, count=3):
        for _ in range(count):
            self.clock.advance(3601)
            await self.a.tick()

    async def test_exhausted_calls_skip_network_journal_and_daily_chances(self):
        self.limits(daily_calls=1)
        await self.reserve(calls=1)
        await self.retry_ticks()
        self.assertEqual(self.requests, [])
        self.assertEqual(self.llm.calls, [])
        self.assertEqual(self.a._state["visits"], {})
        self.assertEqual(self.a._state["draws"], {})
        with self.assertRaises(VisitDecisionError) as error:
            await self.a.invite(S, self.peer, automatic=True)
        self.assertEqual(error.exception.category, "budget")
        self.assertEqual(self.requests, [])

    async def test_exhausted_tokens_skip_preparation_without_an_attempt(self):
        self.limits(daily_tokens=50)
        await self.reserve(tokens=50)
        await self.retry_ticks()
        self.assertFalse(self.requests)
        self.assertFalse(self.a._state["visits"])
        self.assertFalse(self.llm.calls)

    async def test_manual_contact_still_explains_why_its_model_could_not_start(self):
        self.limits(daily_calls=1)
        await self.reserve(calls=1)
        ended = await self.a.invite(S, self.peer)
        self.assertEqual(ended["status"], "interrupted")
        self.assertIn("预算已用完", ended["end_reason"])
        self.assertFalse(self.a._state["draws"])
        self.assertFalse(self.llm.calls)

    async def test_limit_change_and_next_local_day_restore_automatic_contact(self):
        self.limits(daily_calls=1)
        await self.reserve(calls=1)
        await self.a.tick()
        self.assertFalse(self.requests)
        self.limits(daily_calls=2)
        await self.a.tick()
        self.assertEqual(len(self.llm.calls), 1)
        self.limits(daily_calls=1)
        self.clock.advance(86400)
        await self.a.tick()
        self.assertEqual(len(self.llm.calls), 2)
        self.assertEqual(len(self.a._state["visits"]), 2)
        self.assertTrue(all(not v.get("arrived_at") for v in self.a._state["visits"].values()))

    async def test_known_token_shortage_waits_across_ticks_and_reload(self):
        self.limits(daily_tokens=10)
        await self.a.tick()
        self.assertEqual(len(self.a._state["visits"]), 1)
        first_requests = list(self.requests)
        self.assertIn("world_bind", first_requests)
        self.assertFalse(self.llm.calls)
        await self.retry_ticks()
        self.assertEqual(self.requests, first_requests)
        self.assertEqual(len(self.a._state["visits"]), 1)

        await self.a.close()
        resumed = VisitService(self.db, self.a.config, self.port, clock=self.clock.now)
        self.addAsyncCleanup(resumed.close)
        self.engine.visits = resumed
        await resumed.start()
        await resumed.tick()
        self.assertEqual(len(resumed._state["visits"]), 1)
        self.assertFalse(self.llm.calls)
        self.limits(daily_tokens=50000)
        await resumed.tick()
        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual(len(resumed._state["visits"]), 2)

    async def test_release_of_other_work_reopens_a_known_token_shortage(self):
        self.limits(daily_tokens=10000)
        await self.reserve(tokens=10000, reserve=9900)
        await self.a.tick()
        self.assertEqual(len(self.a._state["visits"]), 1)
        self.assertFalse(self.llm.calls)
        self.clock.advance(3601)
        await self.a.tick()
        self.assertEqual(len(self.a._state["visits"]), 1)
        pool = self.engine._background_budget_pool(purpose="world_visit", session_id=S)
        await self.db.call("release_background", pool["ledger"], reserve=9900)
        await self.a.tick()
        self.assertEqual(len(self.a._state["visits"]), 2)
        self.assertEqual(len(self.llm.calls), 1)

    async def test_budget_preview_never_reserves_a_call(self):
        first = await self.port.automatic_budget(S)
        second = await self.port.automatic_budget(S)
        self.assertEqual(first, second)
        self.assertTrue(first["allowed"])
        self.assertEqual(first["snapshot"][3:], [0, 0, 0, 0])
        self.assertIsNone(await self.db.call("kv_get", "background_budget:" + first["snapshot"][0]))
