"""Short home intervals between visits accumulate toward the original rest."""
from __future__ import annotations

import asyncio
import unittest
from datetime import datetime

from tests.fake_astrbot import install

install()

from tests import test_engine as fixture  # noqa: E402
from tests import test_visits_return_review_k as returns  # noqa: E402
from core.engine.life_inference import LIFE_WORK, LifeWorkDiscarded, released_life_lock  # noqa: E402


class VisitHomeFractionalTimeTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = returns.VisitReturnClockTests.asyncSetUp
    arrived = returns.VisitReturnClockTests.arrived
    home = returns.VisitReturnClockTests.home
    finish = returns.VisitReturnClockTests.finish
    tick = returns.VisitReturnClockTests.tick
    record_dynamics = returns.VisitReturnClockTests.record_dynamics
    configure_restore = returns.VisitReturnClockTests.configure_restore

    async def fragmented_return(self, *, action="return_home_nap", duration=3):
        await self.home(action=action, duration=duration)
        async with self.engine.session_state(fixture.SESSION) as state:
            state.current_plan = None
        started = self.clock.now()
        records = self.record_dynamics()
        for _ in range(2):
            visit = self.arrived()
            self.clock.advance(10)
            await self.finish(visit)
            self.clock.advance(50)
            state = await self.tick()
        return state, started, records

    async def test_home_rest_finishes_after_its_accumulated_actual_minute(self):
        state, started, _ = await self.fragmented_return()
        self.assertNotEqual((state.current_action or {}).get("type"), "return_home_nap",
                            "Two actual 50-second home intervals must finish the remaining 60-second nap")
        self.assertEqual(state.sleep_history[-1]["ended_at"], started + 80)

    async def test_needs_stop_using_home_sleep_dynamics_at_the_actual_wake(self):
        _, _, records = await self.fragmented_return()
        home_sleep = sum(row["seconds"] for row in records if row["state"] == "napping")
        self.assertEqual(home_sleep, 60,
                         "The final 40 seconds at home are awake; they cannot recover as the original nap")
        away = sum(row["seconds"] for row in records if row["node"] is None)
        self.assertEqual(away, 20)

    async def test_persisted_remainder_finishes_during_the_next_uninterrupted_home_tick(self):
        await self.home(duration=3)
        started = self.clock.now()
        records = self.record_dynamics()
        visit = self.arrived()
        self.clock.advance(10)
        await self.finish(visit)
        self.clock.advance(50)
        state = await self.tick()
        self.assertEqual(state.current_action["home_elapsed_remainder"], 50)
        self.clock.advance(60)
        state = await self.tick()
        self.assertNotEqual((state.current_action or {}).get("type"), "return_home_nap")
        self.assertEqual(state.sleep_history[-1]["ended_at"], started + 70)
        self.assertEqual(sum(row["seconds"] for row in records if row["state"] == "napping"), 60)

    async def test_quiet_work_keeps_its_partial_progress_and_actual_earned_tick(self):
        state, started, _ = await self.fragmented_return(action="return_home_work", duration=4)
        self.assertEqual(state.current_action["elapsed_ticks"], 3)
        self.assertEqual(state.current_action["home_elapsed_remainder"], 40)
        self.assertEqual(state.current_action["progress_at"], started + 80)
        self.clock.advance(60)
        state = await self.tick()
        self.assertNotEqual((state.current_action or {}).get("type"), "return_home_work")
        events = await self.db.call("query_events", session_id=fixture.SESSION, event_type="action_done")
        self.assertEqual(events[0]["detail"]["elapsed_ticks"], 4)

    async def test_restore_effects_and_elapsed_needs_do_not_replay_the_saved_remainder(self):
        self.clock._now = datetime(2026, 11, 2, 12, 0).timestamp()
        await self.configure_restore(duration=3)
        async with self.engine.session_state(fixture.SESSION) as state:
            state.energy = .5
        for _ in range(2):
            visit = self.arrived()
            self.clock.advance(10)
            await self.finish(visit)
            self.clock.advance(50)
            state = await self.tick()
        self.assertNotEqual((state.current_action or {}).get("type"), "return_home_restore")
        # 120 newly elapsed seconds decay .02; one earned recovery minute adds .1.
        self.assertAlmostEqual(state.energy, .58)

    async def test_invalid_saved_partial_time_is_treated_as_zero(self):
        await self.home(duration=3)
        async with self.engine.session_state(fixture.SESSION) as state:
            for value in (True, "50", -1, 60, float("nan"), float("inf"), 10 ** 1000):
                state.current_action["home_elapsed_remainder"] = value
                self.assertEqual(self.engine._home_partial_seconds(state.current_action), 0)

    async def test_thinking_rejoins_the_latest_fractional_home_clock(self):
        await self.home(action="return_home_work", duration=100)
        async with self.engine.session_state(fixture.SESSION) as state:
            state.energy = .9
            state.current_plan = None
            state.awaiting_reply = True
            state.current_action["home_elapsed_remainder"] = 50
        token = LIFE_WORK.set(True)
        traces = []
        self.engine.decision_trace = lambda *args, **details: traces.append(details)
        try:
            async with self.engine.session_state(fixture.SESSION) as state:
                async with released_life_lock(self.engine):
                    self.clock.advance(50)
                    await asyncio.create_task(self.engine._tick_while_thinking(fixture.SESSION))
                self.assertEqual(state.current_action["elapsed_ticks"], 3)
                self.assertEqual(state.current_action["home_elapsed_remainder"], 40)
                self.assertTrue(state.awaiting_reply)
        except LifeWorkDiscarded:
            self.fail(f"Advancing the existing action clock must rejoin, changed fields: {traces}")
        finally:
            LIFE_WORK.reset(token)


if __name__ == "__main__":
    unittest.main()
