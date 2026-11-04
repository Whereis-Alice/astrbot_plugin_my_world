"""An agreed return keeps its original clock through cancellation and reload."""
from __future__ import annotations

import asyncio
import copy
import unittest

from core.visits import VisitService
from core.visits.outings import begin_activity
from core.visits.persistence import KEY
from tests import test_visits_lifecycle_races as fixture


class VisitReturnClockAtomicityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.VisitLifecycleRaceTests.asyncSetUp
    pair = fixture.VisitLifecycleRaceTests.pair
    exchange = fixture.VisitLifecycleRaceTests.exchange

    def agreed_return(self):
        visit, _ = self.pair(arrived=True)
        visit["outing_version"] = 1
        visit["outing_host_farewell"] = "下次再一起散步。"
        begin_activity(visit, {"place": "湖边", "activity": "散步", "minutes": 10,
                              "return_after": True}, self.now,
                       {"return_after": True, "farewell": "好，回家见。"})
        self.now = visit["outing"]["until"] + 90
        return visit

    async def test_cancel_after_commit_keeps_agreed_clock_and_complete_farewells(self):
        visit = self.agreed_return()
        until = visit["outing"]["until"]
        initial_log = copy.deepcopy(visit["log"])
        original = self.a.db.call

        async def committed_then_cancelled(operation, key, value):
            result = await original(operation, key, value)
            if operation == "kv_set":
                raise asyncio.CancelledError()
            return result

        self.a.db.call = committed_then_cancelled
        with self.assertRaises(asyncio.CancelledError):
            await self.a._complete_outing_if_due(visit)
        self.a.db.call = original
        saved = self.a.db.state[KEY]["visits"][visit["id"]]
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["ended_at"], until)
        self.assertEqual(saved["log"][:-2], initial_log)
        self.assertEqual([row["at"] for row in saved["log"][-2:]], [until, until])
        # Delivery retries use the real current clock, independent of return time.
        self.assertEqual(saved["end_pending"]["next_at"], self.now)

        resumed = VisitService(self.a.db, self.a.config, self.a_actor, clock=lambda: self.now)
        self.addAsyncCleanup(resumed.close)
        await resumed._load()
        restored = resumed._state["visits"][visit["id"]]
        self.assertFalse(await resumed._complete_outing_if_due(restored))
        self.assertEqual(restored["ended_at"], until)
        self.assertEqual(restored["log"], saved["log"])
        self.assertEqual(resumed.pause_intervals("life-a", visit["arrived_at"], self.now,
                         persona_key=visit["local"]["persona_key"]), [(visit["arrived_at"], until)])

    async def test_agreed_return_is_persisted_once_and_manual_end_keeps_actual_clock(self):
        visit = self.agreed_return()
        original, writes = self.a.db.call, []

        async def saving(operation, key, value):
            if operation == "kv_set":
                writes.append(copy.deepcopy(value))
            return await original(operation, key, value)

        self.a.db.call = saving
        self.assertTrue(await self.a._complete_outing_if_due(visit))
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]["visits"][visit["id"]]["ended_at"], visit["outing"]["until"])

        self.now += 300
        await self.a._finish(visit, "interrupted", "真人要求归家", notify=False)
        self.assertEqual(visit["ended_at"], self.now)
        self.assertNotIn("end_pending", visit)
