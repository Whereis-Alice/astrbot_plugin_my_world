"""Successful journal awaits cannot carry old visits into a real actor lifecycle."""
from __future__ import annotations

import asyncio
import copy
import threading
import unittest
from unittest.mock import AsyncMock, patch

from tests import fake_astrbot
fake_astrbot.install()

from core.visits import VisitConfig, VisitService  # noqa: E402
from core.visits.protocol import clean_decision, public_actor  # noqa: E402
from tests import test_engine as engine_fixture  # noqa: E402
from tests import test_visits_actor as actor_fixture  # noqa: E402
from tests.test_visits_protocol import Actor  # noqa: E402


class VisitFlowLifecycleTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"

    async def asyncSetUp(self):
        await actor_fixture.VisitActorTests.asyncSetUp(self)
        config = VisitConfig(enabled=True, auto_enabled=False, reception_start=0, reception_end=0)
        self.service = VisitService(self.db, config, self.port, clock=self.clock.now, rng=lambda: .9)
        self.engine.visits = self.service
        self.plugin.visits = self.service
        await self.service._load()
        self.addAsyncCleanup(self.service.close)
        local = await self.port.describe(engine_fixture.SESSION)
        remote = public_actor(await Actor("friend").describe("life-peer"), "b" * 32)
        peer = self.service._add_peer(engine_fixture.SESSION, local, remote,
                                     "http://not-contacted.example", "test-key")
        self.visit = self.service._new_visit(engine_fixture.SESSION, peer, local, "a" * 32,
                                             "host", "接着聊天", False)
        self.service._state["visits"][self.visit["id"]] = self.visit
        self.service._activate(self.visit)
        self.service._record_turn(self.visit, self.visit["guest_id"], clean_decision({"say": "刚才的话还没说完。"}))
        await self.service._save()
        self.llm.calls.clear()
        self.llm.default_reply = '{"say":"那你接着说，我在听。","thought":"继续听她说完。"}'

    def hold_journal_write(self, predicate=None):
        original = self.db.raw.kv_set
        loop = asyncio.get_running_loop()
        entered, release = asyncio.Event(), threading.Event()
        held = False

        def write(key, value):
            nonlocal held
            waiting = predicate(self.visit) if predicate else self.visit.get("model_pending")
            if key == "world_visits.v1" and waiting and not held:
                held = True
                original(key, value)
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(5):
                    raise RuntimeError("test journal was not released")
                return None
            return original(key, value)

        self.db.raw.kv_set = write
        self.addCleanup(setattr, self.db.raw, "kv_set", original)
        self.addCleanup(release.set)
        return entered, release

    async def test_real_return_home_queued_during_admission_does_not_start_old_model(self):
        entered, release = self.hold_journal_write()
        log = copy.deepcopy(self.visit["log"])
        observed = []
        generate = self.llm.generate

        async def actual_model(**kwargs):
            observed.append(self.visit["status"])
            return await generate(**kwargs)

        self.llm.generate = actual_model
        advancing = asyncio.create_task(self.service._advance(self.visit["id"]))
        returning = None
        try:
            await asyncio.wait_for(entered.wait(), 3)
            with patch.object(self.service, "_notify_end", new=AsyncMock()), \
                 patch.object(self.service, "_observe_memory", new=AsyncMock()):
                returning = asyncio.create_task(self.service.return_home(engine_fixture.SESSION))
                await asyncio.sleep(0)
                self.assertFalse(returning.done())
                release.set()
                await asyncio.wait_for(asyncio.gather(advancing, returning), 4)
            self.assertEqual(self.visit["status"], "interrupted")
            self.assertEqual(self.visit["end_reason"], "真人要求告别归家")
            self.assertEqual(self.visit["log"], log)
            self.assertFalse(observed, f"old model started in {observed}")
            self.assertFalse(self.llm.calls)
            self.assertEqual(self.visit["calls"][self.service._local_id(self.visit)], 0)
        finally:
            release.set()
            for task in (advancing, returning):
                if task and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in (advancing, returning) if task), return_exceptions=True)

    async def test_real_closed_service_during_successful_admission_does_not_start_model(self):
        entered, release = self.hold_journal_write()
        log = copy.deepcopy(self.visit["log"])
        advancing = asyncio.create_task(self.service._advance(self.visit["id"]))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            await self.service.close()
            saved = copy.deepcopy(self.service._state)
            release.set()
            await asyncio.wait_for(advancing, 4)
            self.assertEqual(self.service._state, saved)
            self.assertEqual(self.visit["log"], log)
            self.assertFalse(self.llm.calls)
        finally:
            release.set()
            if not advancing.done():
                advancing.cancel()
            await asyncio.gather(advancing, return_exceptions=True)

    async def test_plugin_retirement_during_successful_admission_does_not_start_model(self):
        entered, release = self.hold_journal_write()
        advancing = asyncio.create_task(self.service._advance(self.visit["id"]))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            self.plugin.retired = True
            await self.engine.close_background()
            await self.service.close()
            release.set()
            await asyncio.wait_for(advancing, 4)
            self.assertFalse(self.llm.calls)
        finally:
            release.set()
            if not advancing.done():
                advancing.cancel()
            await asyncio.gather(advancing, return_exceptions=True)

    async def test_real_life_disable_during_paid_decision_save_does_not_apply_old_words(self):
        entered, release = self.hold_journal_write(
            lambda visit: "decision" in visit and "model_pending" not in visit)
        log = copy.deepcopy(self.visit["log"])
        pool = self.engine._background_budget_pool(purpose="world_visit", session_id=engine_fixture.SESSION)
        advancing = asyncio.create_task(self.service._advance(self.visit["id"]))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            self.assertEqual(len(self.llm.calls), 1)
            self.store.set_session_enabled(engine_fixture.SESSION, False)
            self.engine.reload_config()
            release.set()
            await asyncio.wait_for(advancing, 4)
            self.assertEqual(self.visit["log"], log)
            self.assertEqual(len(self.llm.calls), 1)
            actual = await self.db.call("background_status", pool["ledger"])
            self.assertEqual(actual["calls"], 1)
            self.assertEqual(self.visit["calls"][self.service._local_id(self.visit)], 1)
            self.assertEqual(self.visit["status"], "interrupted")
        finally:
            release.set()
            if not advancing.done():
                advancing.cancel()
            await asyncio.gather(advancing, return_exceptions=True)

    async def test_real_life_disable_during_saved_decision_retry_clear_does_not_apply_old_words(self):
        from core.visits.model_retry import defer_decision

        decision = await self.service._decide(self.visit["id"], "turn")
        cursor = self.service._decision_cursor(self.visit, "turn")
        defer_decision(self.visit, cursor, category="state", now=self.clock.now(), candidate=decision)
        self.visit["model_retry"]["next_at"] = self.clock.now()
        await self.service._save()
        log = copy.deepcopy(self.visit["log"])
        entered, release = self.hold_journal_write(lambda visit: "model_retry" not in visit)
        advancing = asyncio.create_task(self.service._advance(self.visit["id"]))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            self.store.set_session_enabled(engine_fixture.SESSION, False)
            self.engine.reload_config()
            release.set()
            await asyncio.wait_for(advancing, 4)
            self.assertEqual(self.visit["log"], log)
            self.assertEqual(len(self.llm.calls), 1)
            self.assertEqual(self.visit["status"], "interrupted")
        finally:
            release.set()
            if not advancing.done():
                advancing.cancel()
            await asyncio.gather(advancing, return_exceptions=True)

    async def test_real_return_home_during_adapter_memory_read_does_not_start_old_model(self):
        original = self.db.raw.query_memories
        loop = asyncio.get_running_loop()
        entered, release = asyncio.Event(), threading.Event()
        held = False

        def query(**kwargs):
            nonlocal held
            if kwargs.get("text_terms") == [self.visit["remote"]["id"]] and not held:
                held = True
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(5):
                    raise RuntimeError("test actor memory read was not released")
            return original(**kwargs)

        self.db.raw.query_memories = query
        log = copy.deepcopy(self.visit["log"])
        observed = []
        generate = self.llm.generate

        async def actual_model(**kwargs):
            observed.append(self.visit["status"])
            return await generate(**kwargs)

        self.llm.generate = actual_model
        advancing = asyncio.create_task(self.service._advance(self.visit["id"]))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            with patch.object(self.service, "_notify_end", new=AsyncMock()), \
                 patch.object(self.service, "_observe_memory", new=AsyncMock()):
                await self.service.return_home(engine_fixture.SESSION)
            self.assertEqual(self.visit["status"], "interrupted")
            release.set()
            await asyncio.wait_for(advancing, 4)
            self.assertEqual(self.visit["log"], log)
            self.assertFalse(observed, f"old model started after memory read in {observed}")
            self.assertFalse(self.llm.calls)
            self.assertEqual(self.visit["calls"][self.service._local_id(self.visit)], 0)
        finally:
            release.set()
            if not advancing.done():
                advancing.cancel()
            await asyncio.gather(advancing, return_exceptions=True)
            self.db.raw.query_memories = original

    async def return_home_during_dispatch_wait(self, entered, release, advancing, pool, log):
        try:
            await asyncio.wait_for(entered.wait(), 3)
            with patch.object(self.service, "_notify_end", new=AsyncMock()), \
                 patch.object(self.service, "_observe_memory", new=AsyncMock()):
                await self.service.return_home(engine_fixture.SESSION)
            release.set()
            await asyncio.wait_for(advancing, 4)
            self.assertEqual(self.visit["status"], "interrupted")
            self.assertEqual(self.visit["end_reason"], "真人要求告别归家")
            self.assertEqual(self.visit["log"], log)
            self.assertFalse(self.llm.calls)
            self.assertEqual(self.visit["calls"][self.service._local_id(self.visit)], 0)
            actual = await self.db.call("background_status", pool["ledger"])
            self.assertEqual(actual["calls"], 0)
            self.assertEqual(actual["reserved"], 0)
            self.assertFalse(self.engine._background_failures)
        finally:
            release.set()
            if not advancing.done():
                advancing.cancel()
            await asyncio.gather(advancing, return_exceptions=True)

    async def test_real_sqlite_budget_wait_cannot_dispatch_after_return_home(self):
        original = self.db.raw.reserve_background
        loop = asyncio.get_running_loop()
        entered, release = asyncio.Event(), threading.Event()
        pool = self.engine._background_budget_pool(purpose="world_visit", session_id=engine_fixture.SESSION)
        log = copy.deepcopy(self.visit["log"])

        def reserve(day, **kwargs):
            result = original(day, **kwargs)
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(5):
                raise RuntimeError("test budget was not released")
            return result

        self.db.raw.reserve_background = reserve
        advancing = asyncio.create_task(self.service._advance(self.visit["id"]))
        try:
            await self.return_home_during_dispatch_wait(entered, release, advancing, pool, log)
        finally:
            self.db.raw.reserve_background = original

    async def test_provider_resolution_wait_cannot_dispatch_after_return_home(self):
        entered, release = asyncio.Event(), asyncio.Event()
        pool = self.engine._background_budget_pool(purpose="world_visit", session_id=engine_fixture.SESSION)
        log = copy.deepcopy(self.visit["log"])

        async def resolve(scope):
            entered.set()
            await release.wait()
            return "test-provider"

        self.llm.resolve_provider_id = resolve
        advancing = asyncio.create_task(self.service._advance(self.visit["id"]))
        await self.return_home_during_dispatch_wait(entered, release, advancing, pool, log)

    async def test_admitted_provider_wait_cannot_dispatch_after_return_home(self):
        entered, release = asyncio.Event(), asyncio.Event()
        pool = self.engine._background_budget_pool(purpose="world_visit", session_id=engine_fixture.SESSION)
        log = copy.deepcopy(self.visit["log"])
        generate = self.llm.generate

        async def resolve(scope):
            return "test-provider"

        async def admitted(*, resolved_provider_id, before_request, **kwargs):
            self.assertNotIn("_request_guard", kwargs)
            entered.set()
            await release.wait()
            await before_request()
            return await generate(**kwargs)

        self.llm.resolve_provider_id = resolve
        self.llm.generate_admitted = admitted
        advancing = asyncio.create_task(self.service._advance(self.visit["id"]))
        await self.return_home_during_dispatch_wait(entered, release, advancing, pool, log)

    async def test_actual_started_request_keeps_its_paid_call_after_return_home(self):
        entered, release = asyncio.Event(), asyncio.Event()
        generate = self.llm.generate
        pool = self.engine._background_budget_pool(purpose="world_visit", session_id=engine_fixture.SESSION)
        log = copy.deepcopy(self.visit["log"])

        async def started(**kwargs):
            self.assertNotIn("_request_guard", kwargs)
            reply = await generate(**kwargs)
            entered.set()
            await release.wait()
            return reply

        self.llm.generate = started
        advancing = asyncio.create_task(self.service._advance(self.visit["id"]))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            with patch.object(self.service, "_notify_end", new=AsyncMock()), \
                 patch.object(self.service, "_observe_memory", new=AsyncMock()):
                await self.service.return_home(engine_fixture.SESSION)
            release.set()
            await asyncio.wait_for(advancing, 4)
            self.assertEqual(self.visit["status"], "interrupted")
            self.assertEqual(self.visit["log"], log)
            self.assertEqual(len(self.llm.calls), 1)
            self.assertEqual(self.visit["calls"][self.service._local_id(self.visit)], 1)
            actual = await self.db.call("background_status", pool["ledger"])
            self.assertEqual(actual["calls"], 1)
        finally:
            release.set()
            if not advancing.done():
                advancing.cancel()
            await asyncio.gather(advancing, return_exceptions=True)

    async def test_trusted_unstarted_status_after_return_home_refunds_exact_admission(self):
        self.visit["status"] = "offered"
        self.visit.pop("arrived_at", None)
        self.plugin.enabled = False
        describe = self.port.describe
        entered, release = asyncio.Event(), asyncio.Event()
        log = copy.deepcopy(self.visit["log"])

        async def actual_readiness(scope):
            result = await describe(scope)
            entered.set()
            await release.wait()
            return result

        self.port.describe = actual_readiness
        advancing = asyncio.create_task(self.service._advance(self.visit["id"]))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            with patch.object(self.service, "_notify_end", new=AsyncMock()), \
                 patch.object(self.service, "_observe_memory", new=AsyncMock()):
                await self.service.return_home(engine_fixture.SESSION)
            release.set()
            await asyncio.wait_for(advancing, 4)
            self.assertEqual(self.visit["status"], "interrupted")
            self.assertEqual(self.visit["end_reason"], "真人要求告别归家")
            self.assertEqual(self.visit["log"], log)
            self.assertEqual(self.visit["calls"][self.service._local_id(self.visit)], 0)
            self.assertFalse(self.llm.calls)
        finally:
            release.set()
            if not advancing.done():
                advancing.cancel()
            await asyncio.gather(advancing, return_exceptions=True)
            self.port.describe = describe

    async def test_real_inbox_cursor_change_clears_only_paid_verification_token(self):
        from core.visits.model_retry import defer_decision

        self.service._record_turn(self.visit, self.visit["host_id"], clean_decision({"say": "我们再喝一杯茶吧。"}))
        self.visit["scene_stage"] = "event_offer"
        decision = await self.service._decide(self.visit["id"], "scene")
        cursor = self.service._decision_cursor(self.visit, "scene")
        defer_decision(self.visit, cursor, category="state", now=self.clock.now(), candidate=decision)
        self.visit["model_retry"]["next_at"] = self.clock.now()
        self.visit.pop("decision_cursor", None)
        await self.service._save()
        calls = copy.deepcopy(self.visit["calls"])
        candidate = copy.deepcopy(self.visit["model_retry"]["candidate"])
        peer = self.service._find_peer(engine_fixture.SESSION, self.visit["remote"]["id"])
        payload = {"op": "step", "id": self.visit["id"], "seq": len(self.visit["log"]),
                   "decision": {"say": "好呀，茶还是热的。"}}
        entered, release = self.hold_journal_write()
        deciding = asyncio.create_task(self.service._decide(self.visit["id"], "scene"))
        incoming = None

        async def actual_inbox():
            async with self.service._lock:
                result = await self.service._inbox(peer, self.visit["local"], payload)
                await self.service._save()
                return result

        try:
            await asyncio.wait_for(entered.wait(), 3)
            incoming = asyncio.create_task(actual_inbox())
            await asyncio.sleep(0)
            release.set()
            result, _ = await asyncio.wait_for(asyncio.gather(deciding, incoming), 4)
            self.assertIsNone(result)
            self.assertEqual(self.visit["status"], "active")
            self.assertNotIn("model_pending", self.visit)
            self.assertEqual(self.visit["calls"], calls)
            self.assertEqual(self.visit["model_retry"]["candidate"], candidate)
            self.assertEqual(len(self.llm.calls), 1)
            self.assertEqual(sum(row["text"] == "好呀，茶还是热的。" for row in self.visit["log"]), 1)
        finally:
            release.set()
            for task in (deciding, incoming):
                if task and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in (deciding, incoming) if task), return_exceptions=True)
