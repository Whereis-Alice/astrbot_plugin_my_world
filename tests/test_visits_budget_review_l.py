"""Local visit-budget reads cannot stall other lives or manufacture quota usage."""
from __future__ import annotations

import asyncio
import copy
import unittest
from unittest.mock import AsyncMock

from tests import test_visits_protocol as protocol
from core.visits.decisions import VisitDecisionError
from core.visits.ports import VisitStateError

VisitConfig, VisitService = protocol.VisitConfig, protocol.VisitService


class MemoryKV:
    def __init__(self, value):
        self.value = copy.deepcopy(value)

    async def call(self, operation, key, value=None):
        if operation == "kv_get":
            return copy.deepcopy(self.value)
        if operation == "kv_set":
            self.value = copy.deepcopy(value)
            return None
        raise AssertionError(operation)


class VisitAutomaticBudgetReadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = 1793505600.0
        self.invited, self.described, self.reads = [], [], []
        self.world = "b" * 32
        remote = {"id": self.world + ":guest", "instance_id": self.world,
                  "actor_id": "guest", "name": "friend", "world": "friend's world"}
        state = {"instance_id": "a" * 32, "world_pairing_version": 1,
                 "enabled_scopes": ["life-a", "life-b"], "visits": {}, "peers": {},
                 "connections": {self.world: {"id": self.world, "url": "http://unused.invalid",
                     "enabled": True, "actors": [remote], "default_actor": remote["id"]}},
                 "draws": {"life-a": {"rhythm_version": 3, "peer_days": {}, "budget_hold": ["old"]},
                           "life-b": {"rhythm_version": 3, "peer_days": {}}}}
        self.db = MemoryKV(state)
        self.port = type("Port", (), {})()
        self.port.canonical_scope = lambda scope: scope if scope in {"life-a", "life-b"} else ""
        self.port.local_date = lambda scope: "2026-11-01"
        self.port.decide = AsyncMock(side_effect=AssertionError("No model is called in these preparations"))
        async def describe(scope):
            self.described.append(scope)
            return {"actor_id": scope, "persona_key": "persona-" + scope,
                    "name": scope, "world": "home", "ready": True,
                    "chat_busy": False, "reception_open": True, "local_date": "2026-11-01"}
        self.port.describe = describe
        config = VisitConfig(enabled=True, auto_enabled=True, timeout_seconds=.02)
        self.service = VisitService(self.db, config, self.port, clock=lambda: self.now, rng=lambda: .9)
        self.addAsyncCleanup(self.service.close)
        async def invite(scope, *args, **kwargs):
            self.invited.append(scope)
            return {"id": "prepared-by-another-life"}
        self.service.invite = invite

    def budget_reader(self, failure, *, second=False):
        async def budget(scope):
            self.reads.append(scope)
            if scope == "life-a" and (not second or self.reads.count(scope) == 2):
                if failure == "hang":
                    await asyncio.Future()
                elif failure == "cancel":
                    raise asyncio.CancelledError()
                else:
                    raise OSError("synthetic private budget read failure")
            return {"allowed": True, "snapshot": ["current"]}
        self.port.automatic_budget = budget

    async def assert_first_read_isolated(self, failure):
        self.budget_reader(failure)
        await asyncio.wait_for(self.service.tick(), .2)
        self.assertEqual(self.invited, ["life-b"])
        self.assertEqual(self.described, ["life-b"])
        record = self.service._state["draws"]["life-a"]
        self.assertEqual(record["next_auto_at"], self.now + 60)
        self.assertEqual(record["budget_hold"], ["old"])
        self.assertFalse(record["peer_days"])
        self.assertFalse(self.service._state["visits"])
        self.assertFalse(self.service._tick_lock.locked())
        self.port.decide.assert_not_awaited()
        before = list(self.reads)
        await self.service.tick()
        self.assertEqual(self.reads.count("life-a"), before.count("life-a"))

    async def test_initial_budget_failure_cools_only_that_life(self):
        await self.assert_first_read_isolated("failure")

    async def test_initial_budget_hang_obeys_timeout_and_releases_tick(self):
        await self.assert_first_read_isolated("hang")

    async def test_budget_cancellation_propagates_without_failure_record(self):
        self.budget_reader("cancel")
        with self.assertRaises(asyncio.CancelledError):
            await self.service.tick()
        self.assertFalse(self.service._tick_lock.locked())
        self.assertFalse(self.invited)
        self.assertNotIn("next_auto_at", self.service._state["draws"]["life-a"])
        self.assertEqual(self.service._state["draws"]["life-a"]["budget_hold"], ["old"])
        self.port.decide.assert_not_awaited()

    async def assert_second_read_isolated(self, failure):
        self.budget_reader(failure, second=True)
        selected = self.service.invite
        async def invite(scope, *args, **kwargs):
            if scope == "life-a":
                return await VisitService.invite(self.service, scope, *args, **kwargs)
            return await selected(scope, *args, **kwargs)
        self.service.invite = invite
        await asyncio.wait_for(self.service.tick(), .2)
        self.assertEqual(self.invited, ["life-b"])
        self.assertFalse(self.service._state["visits"])
        record = self.service._state["draws"]["life-a"]
        self.assertEqual(record["budget_hold"], ["old"])
        self.assertEqual(record["next_auto_at"], self.now + 3600)
        self.assertEqual(record["peer_days"][self.world]["days"], {})
        self.assertEqual(record["peer_days"][self.world]["last_prepare_at"], self.now)
        self.assertFalse(self.service._tick_lock.locked())
        self.port.decide.assert_not_awaited()

    async def test_final_invite_budget_failure_keeps_the_opportunity_and_other_life(self):
        await self.assert_second_read_isolated("failure")

    async def test_final_invite_budget_hang_keeps_the_opportunity_and_other_life(self):
        await self.assert_second_read_isolated("hang")

    async def test_budget_failure_snapshot_itself_is_bounded_and_safe(self):
        self.budget_reader("hang")
        with self.assertRaises(VisitStateError) as error:
            await asyncio.wait_for(self.service._automatic_budget_snapshot("life-a"), .2)
        self.assertNotIn("synthetic private", str(error.exception))
        self.port.decide.assert_not_awaited()

    async def test_failed_budget_snapshot_still_settles_the_original_model_rejection(self):
        await self.service._load()
        local = await self.port.describe("life-a")
        peer = self.service._automatic_peers("life-a")[0]
        visit = self.service._new_visit("life-a", peer, local, "c" * 32, "guest", "tea", True)
        visit["proposal_stage"] = "pending"
        self.service._state["visits"][visit["id"]] = visit
        self.port.decide = AsyncMock(side_effect=VisitDecisionError("budget", request_started=False))
        self.budget_reader("hang")
        self.assertIsNone(await asyncio.wait_for(self.service._decide(visit["id"], "propose"), .2))
        self.assertEqual(visit["status"], "interrupted")
        self.assertEqual(visit["calls"][self.service._local_id(visit)], 0)
        self.assertNotIn("model_pending", visit)
        self.assertNotIn("model_retry", visit)
        self.assertEqual(self.service._state["draws"]["life-a"]["budget_hold"], ["old"])
        self.assertIsNone(await self.service._decide(visit["id"], "propose"))
        self.port.decide.assert_awaited_once()

    async def test_exhausted_budget_remains_distinct_from_failed_read(self):
        async def budget(scope):
            return {"allowed": scope != "life-a", "snapshot": ["exhausted"]}
        self.port.automatic_budget = budget
        await self.service.tick()
        self.assertEqual(self.invited, ["life-b"])
        self.assertNotIn("next_auto_at", self.service._state["draws"]["life-a"])
        self.assertFalse(self.service._state["draws"]["life-a"]["peer_days"])
        self.port.decide.assert_not_awaited()

    async def test_disabled_life_is_not_recreated_when_its_failed_read_finishes(self):
        async def budget(scope):
            if scope == "life-a":
                self.port.canonical_scope = lambda value: value if value == "life-b" else ""
                raise OSError("life disabled during read")
            return {"allowed": True, "snapshot": ["current"]}
        self.port.automatic_budget = budget
        await self.service.tick()
        self.assertEqual(self.invited, ["life-b"])
        self.assertNotIn("next_auto_at", self.service._state["draws"]["life-a"])
        self.assertFalse(self.service._state["visits"])
        self.port.decide.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
