"""Arrival, deferred home completion and real night intervals share one clock."""
from __future__ import annotations

from datetime import datetime
import types
import unittest
from zoneinfo import ZoneInfo

from tests import test_engine as fixture
from tests import test_visits_return_review_k as previous


class VisitReturnBoundaryTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = previous.VisitReturnClockTests.asyncSetUp
    arrived = previous.VisitReturnClockTests.arrived
    home = previous.VisitReturnClockTests.home
    finish = previous.VisitReturnClockTests.finish
    tick = previous.VisitReturnClockTests.tick
    configure_restore = previous.VisitReturnClockTests.configure_restore
    record_dynamics = previous.VisitReturnClockTests.record_dynamics

    async def advance(self, *, thinking=False):
        if thinking:
            await self.engine._tick_while_thinking(fixture.SESSION)
            return await self.engine.load_state(fixture.SESSION, cold_start=False)
        return await self.tick()

    async def first_arrival(self, *, thinking=False):
        self.clock._now = datetime(2026, 11, 2, 12, 0).timestamp()
        await self.configure_restore(duration=100)
        async with self.engine.session_state(fixture.SESSION) as state:
            state.energy = .5
        self.clock.advance(60)
        self.arrived()
        self.clock.advance(60)
        records = self.record_dynamics()
        state = await self.advance(thinking=thinking)
        self.assertEqual(state.current_action["elapsed_ticks"], 3)
        self.assertAlmostEqual(state.energy, .58)
        self.assertEqual([(row["node"], row["seconds"]) for row in records], [("study", 60), (None, 60)])
        self.assertFalse(hasattr(state, "_visit_rest_progress"))
        self.assertFalse(hasattr(state, "_visit_need_segments"))
        self.assertEqual(self.llm.calls, [])
        self.assertEqual(self.tools.calls, [])

    async def test_first_active_tick_keeps_the_last_real_home_minute(self):
        await self.first_arrival()

    async def test_first_active_thinking_tick_keeps_the_last_real_home_minute(self):
        await self.first_arrival(thinking=True)

    async def test_unfinished_restore_resumes_the_first_home_tick_after_an_active_tick(self):
        for thinking in (False, True):
            with self.subTest(thinking=thinking):
                self.service._state["visits"].clear()
                self.clock._now = datetime(2026, 11, 2, 12, 0).timestamp()
                await self.configure_restore(duration=100)
                async with self.engine.session_state(fixture.SESSION) as state:
                    state.energy = .5
                self.clock.advance(60)
                visit = self.arrived()
                self.clock.advance(60)
                state = await self.advance(thinking=thinking)
                self.assertEqual(state.current_action["elapsed_ticks"], 3)
                self.assertNotIn("offline_tick_settled", state.current_action)
                await self.finish(visit)
                self.clock.advance(60)
                state = await self.advance(thinking=thinking)
                self.assertEqual(state.current_action["elapsed_ticks"], 4)
                self.assertAlmostEqual(state.energy, .67)
                self.assertEqual(self.llm.calls, [])

    async def test_arrival_keeps_the_last_quiet_work_tick_without_completing_abroad(self):
        self.clock._now = datetime(2026, 11, 2, 12, 0).timestamp()
        self.engine.world.state_dynamics.energy_decay_per_min = .01
        self.engine.world.state_dynamics.atmosphere_multiplier = 0
        self.engine.world.state_dynamics.independent_life_enabled = False
        self.engine.world.state_dynamics.daily_mood_enabled = False
        await self.home(action="return_home_work", duration=3)
        async with self.engine.session_state(fixture.SESSION) as state:
            state.current_plan = None
        self.clock.advance(60)
        visit = self.arrived()
        self.clock.advance(60)
        state = await self.tick()
        self.assertEqual(state.current_action["elapsed_ticks"], 3)
        self.assertTrue(state.current_action["offline_home_complete"])
        self.assertAlmostEqual(state.energy, .48)
        self.clock.advance(60)
        state = await self.tick()
        self.assertEqual(state.current_action["elapsed_ticks"], 3)
        self.assertAlmostEqual(state.energy, .47)
        await self.finish(visit)
        self.clock.advance(60)
        state = await self.tick()
        self.assertNotEqual((state.current_action or {}).get("type"), "return_home_work")
        self.assertAlmostEqual(state.energy, .46)
        self.assertEqual(self.llm.calls, [])
        self.assertEqual(self.tools.calls, [])

    async def completed_restore(self, *, thinking=False):
        self.clock._now = datetime(2026, 11, 2, 12, 0).timestamp()
        await self.configure_restore(duration=3)
        async with self.engine.session_state(fixture.SESSION) as state:
            state.energy = .5
        self.clock.advance(60)
        visit = self.arrived()
        self.clock.advance(60)
        state = await self.advance(thinking=thinking)
        self.assertTrue(state.current_action["offline_recovery_complete"])
        self.assertEqual(state.current_action["elapsed_ticks"], 3)
        self.assertAlmostEqual(state.energy, .58)
        self.clock.advance(60)
        state = await self.advance(thinking=thinking)
        self.assertEqual(state.current_action["elapsed_ticks"], 3)
        self.assertAlmostEqual(state.energy, .57)
        await self.finish(visit)
        self.clock.advance(60)
        state = await self.advance(thinking=thinking)
        self.assertNotEqual((state.current_action or {}).get("type"), "return_home_restore")
        self.assertAlmostEqual(state.energy, .56)
        self.assertEqual(self.llm.calls, [])

    async def test_completed_home_restore_waits_for_return_without_recovering_again(self):
        await self.completed_restore()

    async def test_completed_home_restore_keeps_thinking_clock_semantics(self):
        await self.completed_restore(thinking=True)

    async def test_completed_home_nap_keeps_its_real_wake_time_across_active_ticks(self):
        self.clock._now = datetime(2026, 11, 2, 12, 0).timestamp()
        start = self.clock.now()
        self.engine.world.state_dynamics.energy_decay_per_min = .01
        self.engine.world.state_dynamics.atmosphere_multiplier = 0
        self.engine.world.state_dynamics.independent_life_enabled = False
        self.engine.world.state_dynamics.daily_mood_enabled = False
        self.engine.world.sleep.quality.enabled = False
        self.engine.world.sleep.goodmorning = False
        await self.home(duration=3)
        async with self.engine.session_state(fixture.SESSION) as state:
            state.current_plan = None
        self.clock.advance(60)
        visit = self.arrived()
        self.clock.advance(60)
        state = await self.tick()
        self.assertEqual(state.current_action["offline_wake_at"], start + 60)
        self.clock.advance(60)
        state = await self.tick()
        self.assertEqual(state.current_action["offline_wake_at"], start + 60)
        await self.finish(visit)
        self.clock.advance(60)
        state = await self.tick()
        self.assertEqual(state.sleep_history[-1]["ended_at"], start + 60)
        self.assertNotEqual((state.current_action or {}).get("type"), "return_home_nap")
        self.assertEqual(self.llm.calls, [])

    async def night_comparison(self, hour, *, thinking=False):
        async def scenario(online):
            self.service._state["visits"].clear()
            self.clock._now = datetime(2026, 11, 2, hour, 55).timestamp()
            self.engine.world.night.start_hour = 22
            self.engine.world.night.end_hour = 7
            await self.configure_restore(duration=100)
            self.engine.world.action_map()["return_home_restore"].on_complete.effects_per_minute = {"energy": "+0.02"}
            async with self.engine.session_state(fixture.SESSION) as state:
                state.energy = .5
                state.sleep_pressure = .2
            for _ in range(10):
                self.clock.advance(60)
                if online:
                    await self.advance(thinking=thinking)
            visit = self.arrived()
            self.clock.advance(60)
            await self.finish(visit)
            return await self.advance(thinking=thinking)
        online = await scenario(True)
        expected = (online.energy, online.sleep_pressure)
        batched = await scenario(False)
        self.assertAlmostEqual(batched.energy, expected[0])
        self.assertAlmostEqual(batched.sleep_pressure, expected[1])
        self.assertEqual(batched.affect_synced_at, self.clock.now())
        # Both intervals contain exactly five real home minutes at night.
        self.assertAlmostEqual(batched.energy, .565 if not thinking else .59)
        self.assertEqual(self.llm.calls, [])
        self.assertFalse(hasattr(batched, "_visit_need_segments"))

    async def test_night_start_uses_real_overlap_and_matches_online_minutes(self):
        await self.night_comparison(21)

    async def test_night_end_uses_real_overlap_and_matches_online_minutes(self):
        await self.night_comparison(6)

    async def test_night_boundary_keeps_thinking_free_of_extra_stay_up_costs(self):
        await self.night_comparison(21, thinking=True)

    async def test_night_windows_follow_real_dst_seconds_in_the_world_timezone(self):
        self.engine.world.timezone = "America/New_York"
        self.engine.world.night.start_hour = 1
        self.engine.world.night.end_hour = 3
        self.engine.clock = types.SimpleNamespace(now=self.clock.now)
        zone = ZoneInfo("America/New_York")
        for day, seconds in ((datetime(2026, 11, 1), 3 * 3600), (datetime(2026, 3, 8), 3600)):
            with self.subTest(day=day.date()):
                first = day.replace(hour=0, minute=30, tzinfo=zone).timestamp()
                last = day.replace(hour=3, minute=30, tzinfo=zone).timestamp()
                periods = self.engine._night_segments(first, last)
                self.assertEqual(sum(end - start for start, end in periods), last - first)
                self.assertEqual(sum(end - start for start, end in periods
                                     if self.engine.world.is_night_time(self.engine.local_at(start).hour)), seconds)
                self.assertEqual(len(periods), 3)

    async def test_active_visit_and_completed_ones_share_the_awake_need_cap(self):
        await self.home(duration=6)
        for _ in range(10):
            visit = self.arrived()
            self.clock.advance(1800)
            await self.finish(visit)
        self.clock.advance(240)
        for _ in range(10):
            visit = self.arrived()
            self.clock.advance(1800)
            await self.finish(visit)
        self.arrived()
        self.clock.advance(60)
        records = self.record_dynamics()
        state = await self.tick()
        self.assertEqual(sum(row["seconds"] for row in records if row["state"] == "idle"), 6 * 3600)
        self.assertTrue(state.current_action["offline_wake"])
        self.assertLessEqual(state.world_time, 24 * 3600 / self.engine.tick_seconds)
        self.assertEqual(self.llm.calls, [])
