"""A failed local state read waits without blocking another independent life."""
from __future__ import annotations

import asyncio
import copy
import unittest
from unittest.mock import AsyncMock, patch

from core.visits import VisitConfig, VisitService
from core.visits.protocol import public_actor
from tests.test_visits_idle_cost import RhythmHarness
from tests.test_visits_lifecycle_races import MemoryDb
from tests.test_visits_protocol import Actor


class VisitStateReadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = 1793505600.0
        self.actor = Actor("local")
        self.service = VisitService(MemoryDb(), VisitConfig(enabled=True, auto_enabled=False),
                                    self.actor, clock=lambda: self.now)
        await self.service._load()
        self.addAsyncCleanup(self.service.close)
        self.local = await self.actor.describe("bad")
        remote = await Actor("remote").describe("remote")
        self.visits = {}
        for scope in ("bad", "good"):
            peer = self.service._add_peer(scope, self.local,
                public_actor(remote, "remote-world"), "https://unused.example", "test-key")
            visit = self.service._new_visit(scope, peer, self.local, scope, "guest", "喝茶", True)
            visit["status"] = "offered"
            self.service._state["visits"][scope] = visit
            self.visits[scope] = visit
        self.advance = AsyncMock()
        self.automatic = AsyncMock()
        self.service._advance = self.advance
        self.service._automatic = self.automatic

    async def test_one_unavailable_life_does_not_block_other_visit_or_automatic(self):
        async def describe(scope):
            if scope == "bad":
                raise RuntimeError("private internal details")
            return self.local

        self.actor.describe = AsyncMock(side_effect=describe)
        before = copy.deepcopy(self.visits["bad"])
        await self.service.tick()
        self.advance.assert_awaited_once_with("good")
        self.automatic.assert_awaited_once()
        failed = self.visits["bad"]
        self.assertEqual(failed["status"], "offered")
        self.assertEqual(failed["calls"], before["calls"])
        self.assertEqual(failed["log"], before["log"])
        self.assertNotIn("arrived_at", failed)
        self.assertNotIn("network_failures", failed)
        self.assertEqual(failed["next_at"], self.now + 30)
        visible = self.service._visible(failed)
        self.assertIn("生活状态", visible["model_wait"]["reason"])
        self.assertNotIn("private internal details", str(visible))

    async def test_cooldown_survives_reload_and_success_clears_wait(self):
        self.actor.describe = AsyncMock(side_effect=TimeoutError)
        await self.service.tick()
        self.now += 29
        # A peer's schedule update cannot shorten our local read cooldown.
        for visit in self.visits.values():
            visit["next_at"] = self.now
        await self.service.tick()
        self.assertEqual(self.actor.describe.await_count, 2)
        reloaded = VisitService(self.service.db, self.service.config, self.actor,
                                clock=lambda: self.now)
        self.addAsyncCleanup(reloaded.close)
        await reloaded._load()
        self.assertEqual(reloaded._state["visits"]["bad"]["state_retry"]["attempts"], 1)
        self.now += 1
        self.actor.describe = AsyncMock(return_value=self.local)
        reloaded._advance = AsyncMock()
        reloaded._automatic = AsyncMock()
        await reloaded.tick()
        for visit in reloaded._state["visits"].values():
            self.assertNotIn("state_retry", visit)
        self.assertEqual(reloaded._advance.await_count, 2)

    async def test_third_failed_read_ends_only_failed_life_without_a_model(self):
        async def describe(scope):
            if scope == "bad":
                raise OSError("unavailable")
            return self.local

        self.actor.describe = AsyncMock(side_effect=describe)
        await self.service.tick()
        self.now += 30
        await self.service.tick()
        self.assertEqual(self.visits["bad"]["next_at"], self.now + 60)
        self.now += 60
        await self.service.tick()
        failed = self.visits["bad"]
        self.assertEqual(failed["status"], "interrupted")
        self.assertIn("生活状态", failed["end_reason"])
        self.assertNotIn("state_retry", failed)
        self.assertEqual(sum(failed["calls"].values()), 0)
        self.assertEqual(self.visits["good"]["status"], "offered")
        self.assertEqual(self.actor.calls, [])

    async def test_cancellation_propagates_without_fabricating_a_failure(self):
        self.actor.describe = AsyncMock(side_effect=asyncio.CancelledError)
        with self.assertRaises(asyncio.CancelledError):
            await self.service.tick()
        self.assertFalse(self.service._tick_lock.locked())
        self.assertNotIn("state_retry", self.visits["bad"])
        self.advance.assert_not_awaited()

    async def test_real_state_read_deadline_leaves_other_life_runnable(self):
        async def describe(scope):
            if scope == "bad":
                await asyncio.sleep(10)
            return self.local

        self.actor.describe = AsyncMock(side_effect=describe)
        with patch.object(self.service, "config", VisitConfig(enabled=True, timeout_seconds=.01)):
            await self.service.tick()
        self.assertIn("state_retry", self.visits["bad"])
        self.advance.assert_awaited_once_with("good")


class AutomaticStateReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_bad_scope_cools_down_and_other_scope_keeps_its_daily_opportunity(self):
        service = RhythmHarness()
        service._state["enabled_scopes"].append("other")
        service._state["peers"]["other-friend"] = {
            **service._state["peers"]["friend"], "scope": "other"}
        async def describe(scope):
            if scope == "life":
                raise OSError("state unavailable")
            return service.description()

        service.actor_port.describe.side_effect = describe
        before = copy.deepcopy(service._state["draws"]["life"])
        await service._automatic()
        service.invite.assert_awaited_once_with("other", "friend", automatic=True)
        failed = service._state["draws"]["life"]
        self.assertEqual(failed["next_auto_at"], service.now + 60)
        self.assertEqual({k: v for k, v in failed.items() if k != "next_auto_at"}, before)
        service._touched["other"] = service.now + 1000
        service.actor_port.describe.reset_mock()
        await service._automatic()
        service.actor_port.describe.assert_not_awaited()
        service.now += 60
        service.actor_port.describe.side_effect = lambda scope: service.description()
        await service._automatic()
        service.invite.assert_awaited_with("life", "friend", automatic=True)

    async def test_cancellation_does_not_change_daily_record(self):
        service = RhythmHarness()
        service.actor_port.describe.side_effect = asyncio.CancelledError
        before = copy.deepcopy(service._state)
        with self.assertRaises(asyncio.CancelledError):
            await service._automatic()
        self.assertEqual(service._state, before)

    async def test_failed_read_cannot_shorten_a_concurrently_saved_invitation_cooldown(self):
        service = RhythmHarness()
        async def describe(scope):
            service._state["draws"][scope]["next_auto_at"] = service.now + 3600
            raise OSError("state unavailable")

        service.actor_port.describe.side_effect = describe
        await service._automatic()
        self.assertEqual(service._state["draws"]["life"]["next_auto_at"], service.now + 3600)
        service.invite.assert_not_awaited()

