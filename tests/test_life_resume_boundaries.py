"""Saved quiet work resumes its remainder without replaying speech or later steps."""
import unittest
from datetime import datetime

from core.models import ChainStep
from core.planner import create_plan
from tests import test_action_record_boundaries as records
from tests import test_engine as fixture


class LifeResumeBoundaryTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp
    configure_action = records.ActionRecordBoundaryTests.configure_action
    execute = records.ActionRecordBoundaryTests.execute

    def quiet_action(self, action_id="shelf_work", **fields):
        settings = {"name": "整理书架", "category": "continuous", "llm_level": "template",
                    "template": "", "visible": False, "duration_mode": "fixed", "duration": 600,
                    "on_complete": {"trigger": "none"}}
        settings.update(fields)
        return self.configure_action(action_id=action_id, **settings)

    async def work_minutes(self, minutes):
        for _ in range(minutes):
            self.clock.advance(60)
            async with self.engine.session_state(fixture.SESSION) as state:
                await self.engine._tick_continuous(state, self.engine.node(state.node_id),
                                                   fixture.TickOutcome(fixture.SESSION))

    async def interrupt_and_resume(self):
        outcome = fixture.TickOutcome(fixture.SESSION, speech_kind="life_share")
        async with self.engine.session_state(fixture.SESSION) as state:
            self.engine.suspend_life(state, reason="临时处理眼前的事", source="event")
            resumed = self.engine.resume_life(state)
            if resumed:
                await self.engine._tick_plan(state, self.engine.node(state.node_id), outcome, depth=0)
        await self.engine._deliver(outcome)
        return state

    async def test_fixed_activity_resumes_only_unfinished_minutes(self):
        action_id = self.quiet_action()
        await self.execute(action_id)
        await self.work_minutes(3)
        state = await self.interrupt_and_resume()
        self.assertIsNotNone(state.current_action)
        self.assertEqual(7, state.current_action["duration_ticks"])

    async def test_llm_duration_remainder_can_be_shorter_than_new_activity_minimum(self):
        action_id = self.quiet_action(duration_mode="llm", duration_min=600, duration_max=1200)
        await self.execute(action_id, duration=600)
        await self.work_minutes(9)
        state = await self.interrupt_and_resume()
        self.assertIsNotNone(state.current_action)
        self.assertEqual(1, state.current_action["duration_ticks"])

    async def test_interruption_does_not_automatically_replay_template_speech(self):
        action_id = self.quiet_action(template="我要先整理这层书架。")
        await self.execute(action_id)
        await self.work_minutes(3)
        state = await self.interrupt_and_resume()
        self.assertEqual(["我要先整理这层书架。"], self.messenger.flat_messages)
        self.assertIsNone(state.current_action)

    async def test_schedule_keeps_running_activity_ahead_of_its_queued_tail(self):
        first = self.quiet_action()
        second = self.quiet_action("labels_work", name="给书贴标签", duration=1800)
        outcome = fixture.TickOutcome(fixture.SESSION, speech_kind="schedule")
        async with self.engine.session_state(fixture.SESSION) as state:
            await self.engine._run_chain(state, [ChainStep(type=first), ChainStep(type=second)], outcome, depth=0)
        await self.work_minutes(3)
        async with self.engine.session_state(fixture.SESSION) as state:
            self.engine.suspend_life(state, reason="临时停一下", source="event")
            saved = state.life_resume["plan"]["steps"]
            self.assertEqual([first, second], [step["action"] for step in saved])
            self.assertEqual(420, saved[0]["duration"])
            self.assertEqual(0, saved[1]["duration"], "queued duration retains its original configured fallback")

    async def test_same_activity_can_finish_after_its_first_start_used_daily_quota(self):
        action_id = self.quiet_action(quota={"day": 1})
        await self.execute(action_id)
        await self.work_minutes(3)
        state = await self.interrupt_and_resume()
        self.assertIsNotNone(state.current_action)
        self.assertEqual(7, state.current_action["duration_ticks"])
        table = state.action_usage[action_id]
        self.assertEqual(1, table[self.engine._usage_keys()["day"]])

    async def test_new_fixed_activity_still_uses_configured_duration(self):
        action_id = self.quiet_action()
        await self.execute(action_id, duration=60)
        state = await self.engine.load_state(fixture.SESSION, cold_start=False)
        self.assertEqual(10, state.current_action["duration_ticks"])

    async def test_second_interruption_keeps_remaining_time_and_single_period_charge(self):
        action_id = self.quiet_action(quota={"day": 1, "week": 1, "month": 1})
        await self.execute(action_id)
        await self.work_minutes(3)
        await self.interrupt_and_resume()
        await self.work_minutes(2)
        state = await self.interrupt_and_resume()
        self.assertEqual(5, state.current_action["duration_ticks"])
        self.assertTrue(all(state.action_usage[action_id][key] == 1 for key in self.engine._usage_keys().values()))
        await self.work_minutes(5)
        state = await self.engine.load_state(fixture.SESSION, cold_start=False)
        self.assertIsNone(state.current_action)
        events = await self.db.call("query_events", session_id=fixture.SESSION, limit=30)
        self.assertEqual(1, sum(row["event_type"] == "action_done" for row in events))
        await self.execute(action_id)
        state = await self.engine.load_state(fixture.SESSION, cold_start=False)
        self.assertIsNone(state.current_action, "a genuinely new activity still obeys its quota")

    async def test_midnight_only_charges_the_new_day_not_the_same_week_and_month(self):
        self.clock.set_struct(datetime(2026, 10, 24, 23, 57))
        action_id = self.quiet_action(quota={"day": 1, "week": 1, "month": 1})
        old_keys = self.engine._usage_keys()
        await self.execute(action_id)
        await self.work_minutes(3)
        self.clock.set_struct(datetime(2026, 10, 25, 0, 0))
        state = await self.interrupt_and_resume()
        new_keys = self.engine._usage_keys()
        self.assertNotEqual(old_keys["day"], new_keys["day"])
        self.assertEqual(old_keys["week"], new_keys["week"])
        self.assertEqual(7, state.current_action["duration_ticks"])
        self.assertTrue(all(state.action_usage[action_id][key] == 1
                            for key in {*old_keys.values(), *new_keys.values()}))

    async def test_generated_plan_cannot_supply_a_remaining_time_receipt(self):
        action_id = self.quiet_action(quota={"day": 1})
        async with self.engine.session_state(fixture.SESSION) as state:
            state.current_plan = create_plan(steps=[{"action": action_id, "duration": 60,
                "_life_resume": {"ticks": 1, "usage_keys": self.engine._usage_keys()}}],
                world_time=state.world_time, now=self.clock.now())
            self.assertNotIn("_life_resume", state.current_plan["steps"][0])
            await self.engine._tick_plan(state, self.engine.node(state.node_id),
                fixture.TickOutcome(fixture.SESSION), depth=0)
        self.assertEqual(10, state.current_action["duration_ticks"])
        self.assertEqual(1, state.action_usage[action_id][self.engine._usage_keys()["day"]])

    async def test_expired_bookmark_cannot_start_old_work(self):
        action_id = self.quiet_action()
        await self.execute(action_id)
        await self.work_minutes(3)
        async with self.engine.session_state(fixture.SESSION) as state:
            self.engine.suspend_life(state, reason="先处理别的事", source="event")
        self.clock.advance(1801)
        async with self.engine.session_state(fixture.SESSION) as state:
            self.assertFalse(self.engine.resume_life(state))
            self.assertEqual({}, state.life_resume)
            self.assertIsNone(state.current_action)

    async def test_schedule_executes_remainder_then_original_tail_duration(self):
        first = self.quiet_action()
        second = self.quiet_action("labels_work", name="给书贴标签", duration=1800)
        async with self.engine.session_state(fixture.SESSION) as state:
            await self.engine._run_chain(state, [ChainStep(type=first), ChainStep(type=second)],
                fixture.TickOutcome(fixture.SESSION, speech_kind="schedule"), depth=0)
        await self.work_minutes(3)
        state = await self.interrupt_and_resume()
        self.assertEqual(first, state.current_action["type"])
        self.assertEqual(7, state.current_action["duration_ticks"])
        await self.work_minutes(7)
        async with self.engine.session_state(fixture.SESSION) as state:
            await self.engine._tick_plan(state, self.engine.node(state.node_id),
                fixture.TickOutcome(fixture.SESSION), depth=0)
        self.assertEqual(second, state.current_action["type"])
        self.assertEqual(30, state.current_action["duration_ticks"])
