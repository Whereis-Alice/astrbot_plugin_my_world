"""An interrupted quiet activity retains its already invested online effort."""
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from core.engine import TickOutcome
from core.planner import create_plan
from tests import test_life_matters as fixture

S = fixture.SESSION


class QuietResumptionEffortTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.MattersAcceptanceTests.asyncSetUp

    async def prepare(self):
        definition = self.engine.world.action_map()["read"]
        definition.on_complete.trigger = "none"
        definition.on_complete.effects = {"boredom": "-0.1"}
        definition.on_complete.effects_per_minute = {"curiosity": "-0.005"}
        self.engine.world.state_dynamics.independent_life_enabled = False
        async with self.engine.session_state(S) as state:
            state.state, state.energy, state.curiosity, state.boredom = "idle", .9, .8, .8
            plan = create_plan(steps=[{"action": "read", "duration": 1200}],
                world_time=state.world_time, now=self.clock.now(), tick_seconds=self.engine.tick_seconds,
                valid_for=7200, reason="安静读完这一章")
            outcome = TickOutcome(S)
            await self.engine._apply_plan(state, self.engine.node(state.node_id), outcome, plan)
            await self.engine._tick_plan(state, self.engine.node(state.node_id), outcome, depth=0)
            self.assertEqual(state.current_action["duration_ticks"], 20)

    async def ticks(self, count):
        for _ in range(count):
            self.clock.advance(60)
            async with self.engine.session_state(S) as state:
                await self.engine._tick_continuous(state, self.engine.node(state.node_id), TickOutcome(S))

    async def interrupt_and_resume(self):
        async with self.engine.session_state(S) as state:
            self.engine.suspend_life(state, reason="先处理临时安排", source="schedule")
            self.assertIsNone(state.current_action)
        self.clock.advance(120)
        async with self.engine.session_state(S) as state:
            self.assertTrue(self.engine.resume_life(state))
            await self.engine._tick_plan(state, self.engine.node(state.node_id), TickOutcome(S), depth=0)

    async def test_completion_effects_include_the_first_online_segment(self):
        await self.prepare()
        await self.ticks(5)
        await self.interrupt_and_resume()
        await self.ticks(15)
        saved = await self.engine.load_state(S, cold_start=False)
        self.assertIsNone(saved.current_action)
        self.assertAlmostEqual(saved.curiosity, .7)
        self.assertAlmostEqual(saved.boredom, .7)  # Completion bonus occurs once.
        rows = await self.db.call("query_events", session_id=S, event_type="action_done")
        self.assertEqual(rows[0]["detail"]["elapsed_ticks"], 20)

    async def test_practice_keeps_one_activity_across_two_interruptions(self):
        await self.prepare()
        await self.ticks(5)
        await self.interrupt_and_resume()
        await self.ticks(4)
        await self.interrupt_and_resume()
        await self.ticks(11)
        saved = await self.engine.load_state(S, cold_start=False)
        practice = saved.life_interests["read"]
        self.assertEqual(practice["sessions"], 1)
        self.assertEqual(practice["minutes"], 20)
        self.assertAlmostEqual(saved.curiosity, .7)

    async def test_saved_resumption_survives_reload_without_counting_the_wait(self):
        await self.prepare()
        self.store.save_world(self.engine.world.model_dump(mode="json"))
        await self.ticks(5)
        async with self.engine.session_state(S) as state:
            self.engine.suspend_life(state, reason="暂时停一下", source="event")
            self.assertAlmostEqual(state.curiosity, .8)  # No premature completion.
        self.engine.reload_config()
        self.clock.advance(600)
        async with self.engine.session_state(S) as state:
            self.assertTrue(self.engine.resume_life(state))
            await self.engine._tick_plan(state, self.engine.node(state.node_id), TickOutcome(S), depth=0)
        await self.ticks(15)
        saved = await self.engine.load_state(S, cold_start=False)
        self.assertEqual(saved.life_interests["read"]["minutes"], 20)
        self.assertEqual(saved.life_interests["read"]["sessions"], 1)
        self.assertAlmostEqual(saved.curiosity, .7)

    async def test_midnight_keeps_real_dates_with_the_same_practice_identity(self):
        self.clock._now = datetime(2026, 10, 29, 23, 51, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        await self.prepare()
        await self.ticks(5)
        await self.interrupt_and_resume()
        await self.ticks(15)
        practice = (await self.engine.load_state(S, cold_start=False)).life_interests["read"]
        self.assertEqual(practice["sessions"], 1)
        self.assertEqual(practice["minutes"], 20)
        self.assertEqual(practice["days"], ["2026-10-29", "2026-10-30"])
        self.assertEqual(len({row["practice_id"] for row in practice["episodes"]}), 1)

    async def test_already_settled_time_effects_are_not_applied_again(self):
        await self.prepare()
        # Explicit restoration may settle non-energy effects while work runs.
        self.engine.world.action_map()["read"].recovery_kind = "restore"
        await self.ticks(5)
        await self.interrupt_and_resume()
        await self.ticks(15)
        saved = await self.engine.load_state(S, cold_start=False)
        self.assertAlmostEqual(saved.curiosity, .7)

    async def test_cancelled_activity_does_not_lend_its_time_to_a_new_start(self):
        await self.prepare()
        await self.ticks(5)
        async with self.engine.session_state(S) as state:
            self.engine.suspend_life(state, reason="今天不读这章了", source="event")
            await self.engine.apply_cancel(state, "queue", "放下这个安排", TickOutcome(S))
            self.assertFalse(state.life_resume)
        await self.prepare()
        await self.ticks(20)
        saved = await self.engine.load_state(S, cold_start=False)
        self.assertEqual(saved.life_interests["read"]["sessions"], 2)
        self.assertEqual(saved.life_interests["read"]["minutes"], 25)
        self.assertAlmostEqual(saved.curiosity, .7)


if __name__ == "__main__":
    unittest.main()
