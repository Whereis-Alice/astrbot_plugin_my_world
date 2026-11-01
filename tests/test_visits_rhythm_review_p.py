"""Real exchanges preserve state failure isolation and admission-day invitation quotas."""
from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
from datetime import datetime, timezone
import unittest
from unittest.mock import AsyncMock, patch

from tests import test_visits_global_pairing as fixture

from core.visits import VisitService


class VisitRhythmReviewTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.GlobalPairingTests.asyncSetUp
    advance = fixture.GlobalPairingTests.advance

    async def automatic_midnight_world(self):
        self.a.config = replace(self.a.config, auto_enabled=True, auto_probability=1, auto_interval_minutes=0)
        self.a._random = lambda: 0
        self.a_actor.local_date = lambda scope: self.a_actor.rows[scope]["local_date"]
        await self.a.set_enabled_scopes([self.a1])
        self.now = datetime(2026, 11, 1, 23, 59, 59, tzinfo=timezone.utc).timestamp()

    def next_local_day(self):
        self.now += 2
        for actor in (self.a_actor, self.b_actor):
            for row in actor.rows.values():
                row["local_date"] = "2026-11-02"

    def days(self):
        return self.a._state["draws"][self.a1]["peer_days"][self.b._state["instance_id"]]["days"]

    async def assert_final_read_isolated(self, failure):
        self.a.config = replace(self.a.config, auto_enabled=True, auto_probability=1, timeout_seconds=.5)
        self.a._random = lambda: 0
        self.a_actor.local_date = lambda scope: self.a_actor.rows[scope]["local_date"]
        original_prepare, original_describe = self.a._prepare_peer, self.a_actor.describe
        prepared, reads, final_reads = set(), [], []

        async def prepare(scope, peer):
            result = await original_prepare(scope, peer)
            prepared.add(scope)
            return result

        async def describe(scope):
            reads.append(scope)
            if scope in prepared:
                prepared.remove(scope)
                final_reads.append(scope)
                if scope == self.a1:
                    if failure == "timeout":
                        await asyncio.Future()
                    raise OSError("isolated private final state failure")
            return await original_describe(scope)

        with patch.object(self.a, "_prepare_peer", side_effect=prepare), patch.object(
                self.a_actor, "describe", side_effect=describe):
            await asyncio.wait_for(self.a.tick(), 3)
            self.assertEqual(final_reads, [self.a1, self.a2])
            self.assertEqual([scope for scope, phase, _ in self.a_actor.calls if phase == "propose"], [self.a2])
            self.assertEqual(len(self.a._state["visits"]), 1)
            visit = next(iter(self.a._state["visits"].values()))
            self.assertEqual(visit["scope"], self.a2)
            self.assertEqual(visit["calls"][self.a._local_id(visit)], 1)
            world = self.b._state["instance_id"]
            first, second = (self.a._state["draws"][scope] for scope in (self.a1, self.a2))
            self.assertEqual(first["next_auto_at"], self.now + 3600)
            self.assertEqual(first["peer_days"][world]["days"], {})
            self.assertEqual(first["peer_days"][world]["last_prepare_at"], self.now)
            self.assertEqual(second["peer_days"][world]["days"]["2026-11-01"],
                             {"picked": True, "attempts": 1, "successes": 0})
            before = list(reads)
            await self.a.tick()
            self.assertEqual(reads, before)
            self.assertFalse(self.a._tick_lock.locked())
        self.assertEqual(len(self.a._state["connections"]), 1)
        self.assertEqual(len(self.b._state["connections"]), 1)
        self.assertEqual(sum(body["payload"]["op"] == "pair_world" for _, body in self.messages), 1)

    async def test_final_state_timeout_cools_only_its_life_and_keeps_other_life_running(self):
        await self.assert_final_read_isolated("timeout")

    async def test_final_state_error_cools_only_its_life_and_keeps_other_life_running(self):
        await self.assert_final_read_isolated("error")

    async def test_midnight_lock_wait_anchors_the_new_invitation_and_survives_reload(self):
        await self.automatic_midnight_world()
        original_prepare, original_describe = self.a._prepare_peer, self.a_actor.describe
        prepared = set()
        entered, release, returned = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def prepare(scope, peer):
            result = await original_prepare(scope, peer)
            prepared.add(scope)
            return result

        async def describe(scope):
            local = await original_describe(scope)
            if scope in prepared:
                prepared.remove(scope)
                entered.set()
                await release.wait()
                returned.set()
            return local

        with patch.object(self.a, "_prepare_peer", side_effect=prepare), patch.object(
                self.a_actor, "describe", side_effect=describe):
            task = asyncio.create_task(self.a.invite(self.a1, self.peer, automatic=True))
            acquired = False
            try:
                await asyncio.wait_for(entered.wait(), 2)
                await self.a._lock.acquire()
                acquired = True
                release.set()
                await asyncio.wait_for(returned.wait(), 2)
                self.next_local_day()
            finally:
                release.set()
                if acquired:
                    self.a._lock.release()
            result = await asyncio.wait_for(task, 3)
        visit = self.a._state["visits"][result["id"]]
        self.assertEqual(visit["local"]["local_date"], "2026-11-02")
        self.assertEqual(datetime.fromtimestamp(visit["created_at"], timezone.utc).date().isoformat(), "2026-11-02")
        await self.advance()
        self.assertEqual(visit["status"], "completed")
        self.assertEqual(self.days(), {"2026-11-02": {"picked": True, "attempts": 1, "successes": 1}})
        self.assertEqual(sum(phase == "propose" for _, phase, _ in self.a_actor.calls), 1)
        self.assertEqual(len(self.a._state["visits"]), 1)
        anchors = copy.deepcopy(self.a._state["connections"][self.b._state["instance_id"]]["anchors"])
        await self.a.close()
        resumed = VisitService(self.a_db, self.a.config, self.a_actor, clock=lambda: self.now, rng=lambda: 0)
        self.addAsyncCleanup(resumed.close)
        await resumed._load()
        self.assertEqual(resumed._state["draws"][self.a1], self.a._state["draws"][self.a1])
        self.assertEqual(resumed._state["connections"][self.b._state["instance_id"]]["anchors"], anchors)
        budget = AsyncMock(side_effect=AssertionError("Consumed day must not read model budget"))
        with patch.object(self.a_actor, "describe", side_effect=AssertionError("Consumed day must not load state")) as description, patch.object(
                self.a_actor, "automatic_budget", new=budget, create=True):
            for _ in range(10):
                await resumed.tick()
            description.assert_not_awaited()
            budget.assert_not_awaited()
        self.assertEqual(sum(phase == "propose" for _, phase, _ in self.a_actor.calls), 1)

    async def test_yesterday_admission_keeps_its_day_when_arrival_is_after_midnight(self):
        await self.automatic_midnight_world()
        result = await self.a.invite(self.a1, self.peer, automatic=True)
        self.next_local_day()
        self.a.config = replace(self.a.config, auto_enabled=False)
        await self.advance()
        visit = self.a._state["visits"][result["id"]]
        self.assertEqual(visit["status"], "completed")
        self.assertEqual(visit["local"]["local_date"], "2026-11-01")
        self.assertEqual(datetime.fromtimestamp(visit["arrived_at"], timezone.utc).date().isoformat(), "2026-11-02")
        self.assertEqual(self.days(), {"2026-11-01": {"picked": True, "attempts": 1, "successes": 1}})
        self.a.config = replace(self.a.config, auto_enabled=True)
        await self.a.tick()
        self.assertEqual(sum(phase == "propose" for _, phase, _ in self.a_actor.calls), 2)
        self.assertEqual(self.days()["2026-11-02"], {"picked": True, "attempts": 1, "successes": 0})

    async def test_a_delayed_yesterday_snapshot_cannot_bypass_todays_completed_invitation(self):
        await self.automatic_midnight_world()
        original_prepare, original_describe = self.a._prepare_peer, self.a_actor.describe
        prepared, gated = set(), False
        entered, release = asyncio.Event(), asyncio.Event()

        async def prepare(scope, peer):
            result = await original_prepare(scope, peer)
            prepared.add(scope)
            return result

        async def describe(scope):
            nonlocal gated
            local = await original_describe(scope)
            if scope in prepared:
                prepared.remove(scope)
                if not gated:
                    gated = True
                    entered.set()
                    await release.wait()
            return local

        with patch.object(self.a, "_prepare_peer", side_effect=prepare), patch.object(
                self.a_actor, "describe", side_effect=describe):
            old_request = asyncio.create_task(self.a.invite(self.a1, self.peer, automatic=True))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                self.next_local_day()
                current = await self.a.invite(self.a1, self.peer, automatic=True)
                await self.advance()
                self.assertEqual(self.a._state["visits"][current["id"]]["status"], "completed")
                self.assertEqual(self.days()["2026-11-02"]["successes"], 1)
            finally:
                release.set()
            with self.assertRaises(ValueError):
                await asyncio.wait_for(old_request, 3)
        self.assertEqual(len(self.a._state["visits"]), 1)
        self.assertEqual(self.days(), {"2026-11-02": {"picked": True, "attempts": 1, "successes": 1}})
        self.assertEqual(sum(phase == "propose" for _, phase, _ in self.a_actor.calls), 1)
