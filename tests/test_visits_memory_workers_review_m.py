"""Slow settlement never owns the whole visit clock or abandons a SQLite write."""
from __future__ import annotations

import asyncio
import copy
import json
import threading
import unittest
from unittest.mock import AsyncMock

from core.visits import VisitConfig, VisitService
from core.visits.persistence import KEY, MAX_MEMORY_WORKERS
from core.visits.protocol import public_actor
from tests import test_visits_actor as actor_fixture
from tests import test_visits_lifecycle_races as protocol_fixture


class VisitMemoryWorkerTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = protocol_fixture.VisitLifecycleRaceTests.asyncSetUp
    pair = protocol_fixture.VisitLifecycleRaceTests.pair
    exchange = protocol_fixture.VisitLifecycleRaceTests.exchange

    async def finished(self):
        visit, _ = self.pair(arrived=True)
        await self.a._finish(visit, "completed", "已经确认的告别", notify=False)
        return visit

    async def settle_task(self, visit_id):
        task = self.a._memory_tasks.get(visit_id)
        if task:
            await asyncio.wait_for(asyncio.shield(task), 1)

    async def test_slow_memory_releases_global_clock_and_repeated_ticks_keep_one_write(self):
        old = await self.finished()
        newer = self.a._new_visit("life-a", self.peer, self.local, "b" * 32,
                                  "guest", "下一次联系", False)
        newer["status"] = "offered"
        self.a._state["visits"][newer["id"]] = newer
        before = copy.deepcopy(self.a._state["draws"])
        entered, release = asyncio.Event(), asyncio.Event()
        self.addCleanup(release.set)
        original, attempts = self.a_actor.remember, []

        async def blocked(scope, snapshot):
            attempts.append(snapshot["id"])
            entered.set()
            await release.wait()
            await original(scope, snapshot)

        self.a_actor.remember = blocked
        advanced = []

        async def advance(visit_id):
            advanced.append(visit_id)

        self.a._advance, self.a._automatic = advance, AsyncMock()
        await asyncio.wait_for(self.a.tick(), 1)
        await asyncio.wait_for(entered.wait(), 1)
        task = self.a._memory_tasks[old["id"]]
        self.assertFalse(task.done())
        self.assertFalse(self.a._tick_lock.locked())
        self.assertEqual(advanced, [newer["id"]])
        for _ in range(3):
            await asyncio.wait_for(self.a.tick(), 1)
        self.assertIs(self.a._memory_tasks[old["id"]], task)
        self.assertEqual(attempts, [old["id"]])
        self.assertEqual(self.a._state["draws"], before)
        self.assertNotIn("remembered", old)
        self.assertNotIn("remember_retry_at", old)
        self.assertEqual(self.a_actor.calls, [])
        release.set()
        await self.settle_task(old["id"])
        self.assertTrue(old["remembered"])
        self.assertIn(old["id"], self.a_actor.memories)

    async def test_worker_limit_leaves_extra_memories_pending_until_original_slot_finishes(self):
        template = await self.finished()
        visits = []
        for index in range(MAX_MEMORY_WORKERS + 1):
            visit = copy.deepcopy(template)
            visit["id"] = f"memory-worker-{index}"
            self.a._state["visits"][visit["id"]] = visit
            visits.append(visit)
        self.a._state["visits"].pop(template["id"])
        releases = {visit["id"]: asyncio.Event() for visit in visits}
        for release in releases.values():
            self.addCleanup(release.set)
        original, attempts = self.a_actor.remember, []

        async def blocked(scope, snapshot):
            attempts.append(snapshot["id"])
            await releases[snapshot["id"]].wait()
            await original(scope, snapshot)

        self.a_actor.remember = blocked
        for visit in visits:
            await self.a._observe_memory(visit["id"])
        self.assertEqual(len(self.a._memory_tasks), MAX_MEMORY_WORKERS)
        self.assertEqual(attempts, [visit["id"] for visit in visits[:-1]])
        self.assertNotIn(visits[-1]["id"], self.a._memory_tasks)
        self.assertTrue(all(not visit.get("remembered") for visit in visits))
        releases[visits[0]["id"]].set()
        await self.settle_task(visits[0]["id"])
        await self.a._observe_memory(visits[-1]["id"])
        self.assertEqual(len(self.a._memory_tasks), MAX_MEMORY_WORKERS)
        self.assertEqual(attempts.count(visits[-1]["id"]), 1)
        for release in releases.values():
            release.set()
        await self.a._await_memory_tasks(tuple(self.a._memory_tasks.values()))
        self.assertTrue(all(visit.get("remembered") for visit in visits))

    async def test_old_worker_cannot_ack_late_confirmed_facts_and_next_write_extends_same_memory(self):
        visit = await self.finished()
        entered, release = asyncio.Event(), asyncio.Event()
        self.addCleanup(release.set)
        original, snapshots = self.a_actor.remember, []

        async def blocked(scope, snapshot):
            snapshots.append(copy.deepcopy(snapshot))
            if len(snapshots) == 1:
                entered.set()
                await release.wait()
            await original(scope, snapshot)

        self.a_actor.remember = blocked
        await self.a._observe_memory(visit["id"])
        await asyncio.wait_for(entered.wait(), 1)
        self.a._append(visit, visit["host_id"], "say", "最后确认的告别原话")
        await self.a._save()
        await self.a._observe_memory(visit["id"])
        self.assertEqual(len(snapshots), 1)
        release.set()
        await self.settle_task(visit["id"])
        self.assertNotIn("remembered", visit)
        await self.a._remember(visit["id"])
        self.assertTrue(visit["remembered"])
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(snapshots[1]["log"][:len(snapshots[0]["log"])], snapshots[0]["log"])
        self.assertEqual(len(self.a_actor.memories), 1)
        self.assertIn("最后确认的告别原话", str(self.a_actor.memories[visit["id"]]["log"]))

    async def test_failed_worker_keeps_durable_backoff_and_reload_does_not_spin(self):
        visit = await self.finished()
        original, attempts = self.a_actor.remember, []

        async def failing(scope, snapshot):
            attempts.append(snapshot["id"])
            raise RuntimeError("isolated settlement failure")

        self.a_actor.remember = failing
        await self.a._remember(visit["id"])
        self.assertEqual(visit["remember_failures"], 1)
        retry_at = visit["remember_retry_at"]
        await self.a.close()
        resumed = VisitService(self.a.db, self.a.config, self.a_actor, clock=lambda: self.now)
        self.addAsyncCleanup(resumed.close)
        await resumed._load()
        for _ in range(3):
            await resumed._observe_memory(visit["id"])
        self.assertEqual(attempts, [visit["id"]])
        self.assertFalse(resumed._memory_tasks)
        self.assertEqual(resumed._state["visits"][visit["id"]]["remember_retry_at"], retry_at)
        self.a_actor.remember = original
        self.now = retry_at
        await resumed._remember(visit["id"])
        self.assertTrue(resumed._state["visits"][visit["id"]]["remembered"])
        self.assertEqual(self.a_actor.calls, [])

    async def test_tick_cancellation_does_not_cancel_its_owned_memory_write(self):
        visit = await self.finished()
        entered, release = asyncio.Event(), asyncio.Event()
        self.addCleanup(release.set)
        original, attempts = self.a_actor.remember, []

        async def blocked(scope, snapshot):
            attempts.append(snapshot["id"])
            entered.set()
            await release.wait()
            await original(scope, snapshot)

        self.a_actor.remember = blocked
        tick = asyncio.create_task(self.a.tick())
        await asyncio.wait_for(entered.wait(), 1)
        memory = self.a._memory_tasks[visit["id"]]
        tick.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await tick
        self.assertFalse(memory.done())
        self.assertFalse(self.a._tick_lock.locked())
        await self.a._observe_memory(visit["id"])
        self.assertEqual(attempts, [visit["id"]])
        release.set()
        await self.settle_task(visit["id"])
        self.assertTrue(visit["remembered"])

    async def test_return_home_reports_confirmed_end_while_memory_remains_single_flight(self):
        visit, _ = self.pair(arrived=True)
        before = copy.deepcopy(self.a._state["draws"])
        release = asyncio.Event()
        self.addCleanup(release.set)
        original, attempts = self.a_actor.remember, []

        async def blocked(scope, snapshot):
            attempts.append(snapshot["id"])
            await release.wait()
            await original(scope, snapshot)

        self.a_actor.remember = blocked
        self.a._notify_end = AsyncMock()
        result = await asyncio.wait_for(self.a.return_home("life-a"), 1)
        self.assertEqual(result["status"], "interrupted")
        self.assertIsNone(self.a.active("life-a"))
        self.assertIn(visit["id"], self.a._memory_tasks)
        self.assertNotIn("remembered", visit)
        self.assertEqual(self.a._state["draws"], before)
        await self.a.return_home("life-a")
        self.assertEqual(attempts, [visit["id"]])
        release.set()
        await self.settle_task(visit["id"])
        self.assertTrue(visit["remembered"])

    async def test_close_cancels_owned_port_tasks_and_reload_keeps_unwritten_memory_pending(self):
        visit = await self.finished()
        release = asyncio.Event()
        self.addCleanup(release.set)
        original, attempts = self.a_actor.remember, []

        async def blocked(scope, snapshot):
            attempts.append(snapshot["id"])
            await release.wait()
            await original(scope, snapshot)

        self.a_actor.remember = blocked
        await self.a._observe_memory(visit["id"])
        await self.a.close()
        self.assertFalse(self.a._memory_tasks)
        self.assertFalse(self.a._remembering)
        self.assertNotIn("remembered", visit)
        self.assertNotIn("remember_retry_at", visit)
        await self.a._observe_memory(visit["id"])
        self.assertEqual(attempts, [visit["id"]])
        self.a_actor.remember = original
        resumed = VisitService(self.a.db, self.a.config, self.a_actor, clock=lambda: self.now)
        self.addAsyncCleanup(resumed.close)
        await resumed._load()
        await resumed._remember(visit["id"])
        self.assertTrue(resumed._state["visits"][visit["id"]]["remembered"])
        self.assertEqual(len(self.a_actor.memories), 1)


class VisitMemorySQLiteWorkerTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = actor_fixture.VisitActorTests.asyncSetUp

    async def test_close_waits_for_actual_transaction_then_reload_reuses_one_receipt(self):
        service = VisitService(self.db, VisitConfig(enabled=True, auto_enabled=False), self.port,
                               clock=self.clock.now)
        self.addAsyncCleanup(service.close)
        await service._load()
        remote = {"actor_id": "memory-guest", "persona_key": "other-persona", "name": "友邻",
                  "world": "友邻世界", "ready": True, "home_node": "home"}
        peer = service._add_peer(self.visit["scope"], self.local, public_actor(remote, "other-instance"),
                                 "http://not-contacted.example", "test-key")
        visit = service._new_visit(self.visit["scope"], peer, self.local, "sqlite-memory-worker",
                                   "guest", "喝茶", False)
        visit.update(status="completed", arrived_at=self.clock.now(), ended_at=self.clock.now(),
                     end_reason="已经告别")
        service._append(visit, peer["id"], "say", "第一次真实确认的回忆")
        service._state["visits"][visit["id"]] = visit
        await service._save()
        original_run = self.db.run
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        writes = []

        async def hold_transaction(func, *args, **kwargs):
            if func != self.engine.memory.remember_visit or writes:
                return await original_run(func, *args, **kwargs)

            def held(*values, **options):
                with self.db.raw.atomic():
                    writes.append(visit["id"])
                    result = func(*values, **options)
                    entered.set()
                    if not release.wait(3):
                        raise TimeoutError("test transaction was not released")
                    return result

            return await original_run(held, *args, **kwargs)

        self.db.run = hold_transaction
        await service._observe_memory(visit["id"])
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(.005)
        self.assertTrue(entered.is_set())
        memory = service._memory_tasks[visit["id"]]
        await service._observe_memory(visit["id"])
        self.assertIs(service._memory_tasks[visit["id"]], memory)
        self.assertEqual(writes, [visit["id"]])
        # The real transaction still owns SQLite. Only the visit coordinator's
        # wait is bounded; another request to the same DB cannot bypass it.
        reading = asyncio.create_task(self.db.call("query_events", session_id=visit["scope"]))
        closing = asyncio.create_task(service.close())
        try:
            await asyncio.sleep(.02)
            self.assertFalse(reading.done())
            self.assertFalse(closing.done())
            closing.cancel()
            await asyncio.sleep(.01)
            closing.cancel()
            await asyncio.sleep(.01)
            self.assertFalse(closing.done())
            self.assertFalse(memory.done())
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(closing, 1)
        await asyncio.wait_for(reading, 1)
        self.db.run = original_run
        self.assertFalse(service._memory_tasks)
        self.assertFalse(service._remembering)
        self.assertNotIn("remembered", visit)
        before = await self.db.call("query_memories", session_id=visit["scope"])
        recorded = [row for row in before if json.loads(row.get("context") or "{}").get("visit_id") == visit["id"]]
        self.assertEqual(len(recorded), 1)
        saved = await self.db.call("kv_get", KEY)
        self.assertNotIn("remembered", saved["visits"][visit["id"]])
        resumed = VisitService(self.db, service.config, self.port, clock=self.clock.now)
        self.addAsyncCleanup(resumed.close)
        await resumed._load()
        await resumed._remember(visit["id"])
        after = await self.db.call("query_memories", session_id=visit["scope"])
        self.assertEqual(after, before)
        self.assertEqual(len(self.db.raw._query("SELECT * FROM memory_receipt")), 1)
        self.assertTrue(resumed._state["visits"][visit["id"]]["remembered"])
        self.assertEqual(self.llm.calls, [])
