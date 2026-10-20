"""Actual elapsed life must survive action boundaries, rest and daily budgets."""

import unittest
from datetime import datetime
from unittest.mock import AsyncMock, Mock

from core.engine import TickOutcome
from core.models import ChainStep, ScheduleDef
from core.state import WorldState
from tests import test_engine as fixture
from tests import test_life_matters as matters_fixture

SESSION = fixture.SESSION


class LifeClockBoundaryTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = matters_fixture.MattersAcceptanceTests.asyncSetUp
    add_group = fixture.EngineTestCase.add_group

    async def test_sleep_last_tick_uses_sleep_recovery_before_waking(self):
        sleep = self.engine.world.action_map()["sleep"]
        sleep.on_complete.effects = {}
        sleep.on_complete.effects_per_minute = {}
        self.engine.world.sleep.quality.enabled = False
        self.engine.world.sleep.goodmorning = False
        self.engine.world.state_dynamics.daily_mood_enabled = False
        self.engine._maybe_run_event = AsyncMock()
        self.engine._sync_nickname = AsyncMock()
        async with self.engine.session_state(SESSION) as state:
            state.state, state.energy, state.sleep_pressure = "sleeping", .2, .8
            state.current_action = {"type": "sleep", "duration_ticks": 480, "elapsed_ticks": 479}
            state.life_updated_at = self.clock.now()
        self.clock.advance(60)
        await self.engine._tick_session(SESSION)
        state = await self.engine.load_state(SESSION, cold_start=False)
        self.assertIsNone(state.current_action)
        self.assertAlmostEqual(state.energy, .2 + self.engine.dynamics.config.sleep_energy_recovery_per_min)
        self.assertLess(state.sleep_pressure, .8)

    async def test_new_sleep_does_not_retroactively_recover_the_previous_awake_tick(self):
        self.engine.world.sleep.quality.enabled = False
        self.engine.world.sleep.goodmorning = False
        self.engine.world.state_dynamics.daily_mood_enabled = False
        self.engine._maybe_run_event = AsyncMock()
        self.engine._sync_nickname = AsyncMock()
        observed = []
        original = self.engine.dynamics.tick

        def dynamics(state, **kwargs):
            observed.append(state.state)
            return original(state, **kwargs)

        self.engine.dynamics.tick = Mock(side_effect=dynamics)
        async with self.engine.session_state(SESSION) as state:
            state.state, state.energy = "idle", .2
            state.current_action = None
            state.current_plan = {"steps": [{"action": "sleep", "duration": 28800}],
                                  "current_step": 0, "source": "manual"}
            state.life_updated_at = self.clock.now()
        self.clock.advance(60)
        await self.engine._tick_session(SESSION)
        self.assertEqual(observed, ["idle"])

    async def test_multiday_offline_wake_is_anchored_to_the_last_online_time(self):
        state = WorldState(SESSION, node_id="study", state="sleeping", energy=.1)
        previous = self.clock.now()
        state.life_updated_at = previous
        state.sleep_rhythm = {"sleep_started_at": previous, "awake_before_sleep": 16}
        state.current_action = {"type": "sleep", "elapsed_ticks": 0, "duration_ticks": 480}
        self.clock.advance(3 * 86400)
        elapsed = self.engine.reconcile_life_time(state)
        await self.engine._tick_continuous(state, self.engine.node("study"), TickOutcome(SESSION))
        self.assertEqual(state.sleep_history[-1]["ended_at"], previous + 8 * 3600)
        self.assertAlmostEqual(self.engine._rhythm_awake_hours(state), 64)
        self.assertEqual(elapsed, 6 * 3600, "bound need settlement without moving the actual wake time")

    async def test_offline_wake_settles_awake_needs_after_sleep_completion(self):
        self.engine.world.sleep.goodmorning = False
        self.engine.world.sleep.quality.enabled = False
        self.engine._maybe_run_event = AsyncMock()
        self.engine._sync_nickname = AsyncMock()
        async with self.engine.session_state(SESSION) as state:
            state.state, state.energy, state.sleep_pressure = "sleeping", .1, .8
            state.current_action = {"type": "sleep", "elapsed_ticks": 0, "duration_ticks": 60}
            state.life_updated_at = self.clock.now()
        observed = []
        original = self.engine.dynamics.tick

        def dynamics(state, **kwargs):
            observed.append((state.state, kwargs["elapsed_seconds"]))
            return original(state, **kwargs)

        self.engine.dynamics.tick = Mock(side_effect=dynamics)
        self.clock.advance(3 * 3600)
        await self.engine._tick_session(SESSION)
        self.assertEqual(observed, [("sleeping", 3600), ("idle", 7200)])

    async def test_action_minimum_cannot_overrun_remaining_daily_matter_budget(self):
        self.engine.world.limits.life_matter_daily_minutes = 5
        self.engine.world.action_map()["read"].duration_min = 300
        self.engine.world.action_map()["read"].on_complete.trigger = "none"
        self.clock.set_struct(datetime(2026, 10, 19, 12))
        async with self.engine.session_state(SESSION) as state:
            state.state, state.energy = "idle", .9
            item = self.engine.add_life_matter(state, "慢慢读完风车说明")
            item.update(work_day=self.engine._today_key(self.clock.now()), worked_minutes=4)
            item["steps"] = [{"id": "read_step", "text": "读下一节", "action": "read",
                              "node": "study", "minutes": 20, "status": "pending"}]
            await self.engine._start_matter_step(state, item)
            await self.engine._tick_continuous(state, self.engine.node("study"), TickOutcome(SESSION))
            self.assertEqual(item["worked_minutes"], 5)
            self.assertIsNone(state.current_action, "put away this project when today's effort is used")
            self.assertEqual(item["steps"][0]["worked_minutes"], 1)
            self.assertEqual(item["steps"][0]["status"], "pending")
            self.assertNotIn(item["id"], state.life_artifacts)
            self.clock.advance(86400)
            item.update(work_day=self.engine._today_key(self.clock.now()), worked_minutes=0)
            await self.engine._start_matter_step(state, item)
            self.assertEqual(state.current_action["matter_id"], item["id"])
            self.assertEqual(item["steps"][0]["worked_minutes"], 1)

    async def test_coarse_ticks_cannot_count_more_matter_work_than_daily_budget(self):
        self.engine.tick_seconds = 600
        self.engine.world.limits.life_matter_daily_minutes = 5
        state = WorldState(SESSION, node_id="study", energy=.9)
        item = self.engine.add_life_matter(state, "慢慢读书")
        item.update(work_day=self.engine._today_key(self.clock.now()), worked_minutes=4)
        item["steps"] = [{"id": "read_step", "text": "读下一节", "action": "read",
                          "node": "study", "minutes": 20, "status": "pending"}]
        action = {"type": "read", "elapsed_ticks": 1, "duration_ticks": 2,
                  "matter_id": item["id"], "matter_step_id": "read_step"}
        self.assertEqual(self.engine.settle_matter_work(state, action), 1)
        self.assertEqual(item["worked_minutes"], 5)
        self.assertEqual(item["steps"][0]["worked_minutes"], 1)

    async def test_schedule_check_spanning_midnight_keeps_the_previous_weekday(self):
        self.engine.schedules.schedules = [ScheduleDef(id="late_read", time="23:59", days=["mon"],
            action_chain=[ChainStep(type="read", duration=300)], sessions=[SESSION])]
        self.engine.world.action_map()["read"].on_complete.trigger = "none"
        self.clock._struct = None
        self.clock._now = datetime(2026, 10, 19, 23, 58, 30).timestamp()
        previous = self.clock.now()
        self.clock.advance(110)
        async with self.engine.session_state(SESSION) as state:
            state.state, state.energy, state.schedule_cursor = "idle", .9, previous
        self.engine._schedule_reset_pending = False
        rows = await self.engine.run_schedules()
        self.assertEqual(len(rows), 1)
        state = await self.engine.load_state(SESSION, cold_start=False)
        self.assertEqual(state.current_action["type"], "read")
        runs = await self.db.call("query_events", session_id=SESSION, event_type="schedule", limit=10)
        self.assertTrue(runs)
        await self.engine.run_schedules()
        self.assertEqual(len(await self.db.call("query_events", session_id=SESSION, event_type="schedule", limit=10)), 1)

    async def test_midnight_pruning_keeps_a_recent_unchecked_once_appointment(self):
        schedules = self.store.raw_schedules()
        schedules["schedules"] = [ScheduleDef(id="late_once", time="23:59", date="2026-10-19", once=True,
            action_chain=[ChainStep(type="read", duration=300)], sessions=[SESSION]).model_dump()]
        self.store.save_schedules(schedules)
        self.engine.reload_config()
        self.clock._struct = None
        self.clock._now = datetime(2026, 10, 19, 23, 58, 30).timestamp()
        previous = self.clock.now()
        self.clock.advance(110)
        async with self.engine.session_state(SESSION) as state:
            state.state, state.energy, state.schedule_cursor = "idle", .9, previous
        self.engine._schedule_reset_pending = False
        rows = await self.engine.run_schedules()
        self.assertEqual(len(rows), 1)
        self.assertEqual((await self.engine.load_state(SESSION, cold_start=False)).current_action["type"], "read")
        self.assertFalse(any(row.id == "late_once" for row in self.engine.schedules.schedules))

    async def delayed_once(self):
        schedules = self.store.raw_schedules()
        schedules["schedules"] = [ScheduleDef(id="late_once", time="23:59", date="2026-10-19", once=True,
            action_chain=[ChainStep(type="read", duration=300)], sessions=[SESSION]).model_dump()]
        self.store.save_schedules(schedules)
        self.engine.reload_config()
        self.clock._struct = None
        self.clock._now = datetime(2026, 10, 19, 23, 58, 30).timestamp()
        previous = self.clock.now()
        self.clock.advance(110)
        async with self.engine.session_state(SESSION) as state:
            state.state, state.energy, state.schedule_cursor = "reading", .9, previous
            state.current_action = {"type": "read", "duration_ticks": 999, "elapsed_ticks": 0}
            state.current_plan = {"steps": [{"action": "read"}], "current_step": 0, "source": "schedule"}
        self.engine._schedule_reset_pending = False
        await self.engine.run_schedules()
        state = await self.engine.load_state(SESSION, cold_start=False)
        self.assertEqual(state.schedule_delays["late_once"]["day"], "2026-10-19")
        self.assertGreater(state.schedule_delays["late_once"]["until"], self.clock.now())

    async def test_once_delay_crosses_midnight_and_reload_then_executes_only_once(self):
        await self.delayed_once()
        self.clock.advance(31 * 60)
        async with self.engine.session_state(SESSION) as state:
            state.state, state.current_action, state.current_plan = "idle", None, None
        self.engine.reload_config()  # A fresh short lookback must not lose the stored retry.
        rows = await self.engine.run_schedules()
        self.assertEqual(len(rows), 1)
        state = await self.engine.load_state(SESSION, cold_start=False)
        self.assertEqual(state.current_action["type"], "read")
        self.assertIn("late_once", state.schedule_last_fired)
        self.assertNotIn("late_once", state.schedule_delays)
        self.assertFalse(any(row.id == "late_once" for row in self.engine.schedules.schedules))
        self.assertEqual(await self.engine.run_schedules(), [])

    async def test_cancelled_cross_midnight_delay_is_pruned_without_execution(self):
        await self.delayed_once()
        async with self.engine.session_state(SESSION) as state:
            self.engine._cancel_schedule(state, self.engine.schedules.schedules[0], "2026-10-19")
            state.state, state.current_action, state.current_plan = "idle", None, None
        self.clock.advance(31 * 60)
        self.engine.reload_config()
        self.assertEqual(await self.engine.run_schedules(), [])
        self.assertFalse(any(row.id == "late_once" for row in self.engine.schedules.schedules))
        self.assertNotIn("late_once", (await self.engine.load_state(SESSION, cold_start=False)).schedule_last_fired)

    async def test_expired_cross_midnight_delay_is_not_revived_after_a_long_stop(self):
        await self.delayed_once()
        self.clock.advance(12 * 3600)
        async with self.engine.session_state(SESSION) as state:
            state.state, state.current_action, state.current_plan = "idle", None, None
        self.engine.reload_config()
        self.assertEqual(await self.engine.run_schedules(), [])
        self.assertFalse(any(row.id == "late_once" for row in self.engine.schedules.schedules))
        self.assertNotIn("late_once", (await self.engine.load_state(SESSION, cold_start=False)).schedule_last_fired)

    async def test_pruning_only_keeps_valid_delays_and_rechecks_expiry_the_same_day(self):
        await self.delayed_once()
        schedules = self.store.raw_schedules()
        schedules["schedules"].append(ScheduleDef(id="old_unclaimed", time="10:00", date="2026-10-19", once=True,
            action_chain=[ChainStep(type="read", duration=300)], sessions=[SESSION]).model_dump())
        self.store.save_schedules(schedules)
        self.engine.reload_config()
        self.clock.advance(60)
        self.assertEqual(await self.engine.run_schedules(), [])
        self.assertEqual([row.id for row in self.engine.schedules.schedules], ["late_once"])
        self.clock.advance(12 * 3600)
        self.assertEqual(await self.engine.run_schedules(), [])
        self.assertFalse(self.engine.schedules.schedules)

    async def test_shared_library_keeps_another_characters_pending_cross_midnight_retry(self):
        await self.delayed_once()
        self.store.add_session(fixture.OTHER_SESSION, platform="aiocqhttp", session_type="group", cold_start_node="study")
        self.engine.reload_config()
        self.clock.advance(60)
        async with self.engine.session_state(fixture.OTHER_SESSION) as state:
            state.state, state.energy = "idle", .9
        await self.engine.run_schedules()
        self.assertTrue(any(row.id == "late_once" for row in self.engine.schedules.schedules))
        self.clock.advance(30 * 60)
        async with self.engine.session_state(SESSION) as state:
            state.state, state.current_action, state.current_plan = "idle", None, None
        rows = await self.engine.run_schedules()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].session_id, SESSION)
        self.assertFalse(any(row.id == "late_once" for row in self.engine.schedules.schedules))

    async def test_today_gate_does_not_use_yesterdays_exhausted_delay_without_a_retry_date(self):
        self.clock.set_struct(datetime(2026, 10, 20, 23, 59))
        schedule = ScheduleDef(id="late_daily", time="23:59", action_chain=[ChainStep(type="walk_to", target_node="bedroom")])
        state = WorldState(SESSION, node_id="study")
        state.schedule_delays[schedule.id] = {"day": "2026-10-19", "count": 2, "minutes": 60, "until": 0}
        self.engine._active_thread = Mock(return_value={"id": "ordinary"})
        self.engine._thread_critical = Mock(return_value=False)
        self.engine._ask_schedule_choice = AsyncMock(return_value=("delay", "先处理眼前事情"))
        choice, _ = await self.engine._schedule_gate(state, schedule, label="late_daily")
        self.assertEqual(choice, "delay")
        self.assertEqual(self.engine._ask_schedule_choice.await_count, 1)
        choice, _ = await self.engine._schedule_gate(state, schedule, label="late_daily", day_key="2026-10-19")
        self.assertEqual(choice, "do")

    async def test_reload_during_retry_collection_keeps_the_replacement_appointment(self):
        schedules = self.store.raw_schedules()
        schedules["schedules"] = [ScheduleDef(id="reused_once", time="23:59", date="2026-10-19", once=True,
            action_chain=[ChainStep(type="read", duration=300)], sessions=[SESSION]).model_dump()]
        self.store.save_schedules(schedules)
        self.engine.reload_config()

        async def executed_scope(session_id, once_done):
            once_done.add(("", "reused_once"))
            return []

        async def reload_during_collection():
            replacement = self.store.raw_schedules()
            replacement["schedules"][0]["date"] = "2026-10-21"
            self.store.save_schedules(replacement)
            self.engine.reload_config()
            return {"": ("2026-10-20", set())}

        self.engine._run_schedule_scope = AsyncMock(side_effect=executed_scope)
        self.engine._schedule_prune_protections = AsyncMock(side_effect=reload_during_collection)
        await self.engine.run_schedules()
        self.assertEqual([row.date for row in self.engine.schedules.schedules], ["2026-10-21"])
        self.assertTrue(self.engine._schedule_reset_pending)
