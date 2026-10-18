"""自主作息的状态机回归，使用真实存储与模型替身。"""

import unittest
from datetime import datetime

from core.defaults import default_world
from core.engine import MessageContext, MyWorldEngine, TickOutcome
from core.models import parse_world
from tests import test_engine as fixture


class SleepRhythmTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    set_state = fixture.EngineTestCase.set_state
    get_state = fixture.EngineTestCase.get_state

    async def asyncSetUp(self):
        await fixture.EngineTestCase.asyncSetUp(self)
        raw = self.store.raw_world()
        raw["sleep"].update(mode="autonomous", goodnight=False, goodmorning=False, drowsy=False)
        raw.setdefault("events", {})["enabled"] = False
        self.store.save_world(raw)
        self.engine.reload_config()
        self.clock.set_struct(datetime(2026, 10, 19, 21, 30))
        self.llm.replies = []
        await self.set_state(state="idle", cold_start_done=True, node_id="study", world_time=10,
            energy=0.5, sleep_pressure=0.7, sleep_rhythm={"awake_since": self.clock.now() - 14 * 3600})

    async def assess(self):
        outcome = TickOutcome(session_id=fixture.SESSION)
        async with self.engine.session_state(fixture.SESSION) as state:
            await self.engine._tick_sleep_rhythm(state, outcome)
        return outcome

    async def test_model_can_sleep_before_fixed_night_and_choose_duration(self):
        self.llm.replies = ['{"decision":"sleep","hours":7.5,"reason":"今天走累了，早些休息"}']
        await self.assess()
        state = await self.get_state()
        self.assertEqual(state.current_action["type"], "walk_to")
        self.assertEqual(state.current_action["target_node"], "bedroom")
        self.assertEqual(state.current_plan["steps"][-1]["duration"], 27000)
        self.assertEqual(state.sleep_rhythm["source"], "llm")
        await self.engine.tick()
        await self.engine.tick()
        state = await self.get_state()
        self.assertEqual(state.node_id, "bedroom")
        self.assertEqual(state.current_action["type"], "sleep")
        self.assertEqual(state.current_action["duration_ticks"], 450)
        self.assertEqual(len(self.llm.calls), 1)

    async def test_continue_is_persisted_throttled_and_independent_of_chat_cooldown(self):
        self.llm.replies = ['{"decision":"continue","review_minutes":30,"reason":"看完这一章再休息"}']
        await self.set_state(cooldown_until=10000)
        await self.assess()
        self.assertEqual(len(self.llm.calls), 1)
        other = MyWorldEngine(store=self.store, db=self.db, clock=self.clock, llm=self.llm, tick_seconds=60)
        async with other.session_state(fixture.SESSION) as state:
            self.assertIn("这一章", state.sleep_rhythm["reason"])
            await other._tick_sleep_rhythm(state, TickOutcome(session_id=fixture.SESSION))
        self.assertEqual(len(self.llm.calls), 1)
        self.clock.advance(1801)
        self.llm.replies = ['{"decision":"sleep","hours":8,"reason":"现在休息"}']
        await self.assess()
        self.assertEqual(len(self.llm.calls), 2)

    async def test_failure_uses_fallback_and_overwork_preempts_a_long_action(self):
        await self.set_state(energy=0.2, sleep_rhythm={"awake_since": self.clock.now() - 18 * 3600})
        self.llm.default_reply = "不是合法 JSON"
        await self.assess()
        self.assertEqual((await self.get_state()).sleep_rhythm["source"], "fallback")
        await self.set_state(current_action={"type":"read", "desc":"看书", "interruptible":False},
            current_plan=None, sleep_delay_until=10000,
            sleep_rhythm={"awake_since":self.clock.now()-21*3600})
        await self.assess()
        state = await self.get_state()
        self.assertEqual(state.sleep_rhythm["source"], "safety")
        self.assertEqual(state.current_action["type"], "walk_to")
        self.assertFalse(state.sleep_delayed)

    async def test_sleep_decision_waits_for_activity_then_starts_without_another_llm_call(self):
        self.llm.replies = ['{"decision":"sleep","hours":8,"reason":"把手头事情收尾就去睡"}']
        await self.set_state(current_action={"type":"read", "desc":"看书"})
        await self.assess()
        self.assertEqual((await self.get_state()).current_action["type"], "read")
        await self.set_state(current_action=None)
        await self.assess()
        self.assertEqual((await self.get_state()).current_action["type"], "walk_to")
        self.assertEqual(len(self.llm.calls), 1)

    async def test_duration_is_bounded_and_wake_records_actual_sleep_once(self):
        await self.set_state(node_id="bedroom")
        self.llm.replies = ['{"decision":"sleep","hours":999,"reason":"休息"}']
        await self.assess()
        async with self.engine.session_state(fixture.SESSION) as state:
            self.assertEqual(state.current_action["duration_ticks"], 600)
            state.current_action["elapsed_ticks"] = 120
            self.engine._wake_up_state(state, MessageContext(session_id=fixture.SESSION, user_id="test"))
            self.assertEqual(state.sleep_history[-1]["hours"], 2)
            self.assertTrue(state.sleep_history[-1]["interrupted"])
            self.assertGreater(self.engine._rhythm_awake_hours(state), 8)
        snapshot = await self.engine.snapshot(fixture.SESSION)
        self.assertEqual(snapshot["sleep"]["mode"], "autonomous")
        self.assertEqual(len(snapshot["sleep"]["history"]), 1)

    async def test_full_sleep_resets_awake_time_but_a_nap_only_eases_it(self):
        async with self.engine.session_state(fixture.SESSION) as state:
            self.engine._start_sleep_rhythm(state, "nap", 30)
            action = {"type":"nap", "elapsed_ticks":30}
            self.engine._finish_sleep_rhythm(state, action, interrupted=False)
            self.engine._finish_sleep_rhythm(state, action, interrupted=False)
            self.assertAlmostEqual(self.engine._rhythm_awake_hours(state), 13)
            self.assertEqual(len(state.sleep_history), 1)
            self.engine._start_sleep_rhythm(state, "sleep", 480)
            self.engine._finish_sleep_rhythm(state, {"type":"sleep", "elapsed_ticks":480}, interrupted=False)
            self.assertEqual(self.engine._rhythm_awake_hours(state), 0)

    async def test_automatic_sleep_schedule_yields_but_manual_run_still_works(self):
        self.clock.set_struct(datetime(2026, 10, 19, 23, 30))
        await self.engine.run_schedules()
        self.assertIsNone((await self.get_state()).current_action)
        rows = await self.db.call("query_events", session_id=fixture.SESSION, limit=30)
        self.assertTrue(any("自主作息接管" in str(row) for row in rows))
        result = await self.engine.run_schedule_now(fixture.SESSION, "night_sleep")
        self.assertTrue(result["ok"], result)
        self.assertEqual((await self.get_state()).current_action["type"], "walk_to")

    async def test_pressure_keeps_building_after_sunrise_and_rest_does_not_force_another_night(self):
        async with self.engine.session_state(fixture.SESSION) as state:
            state.sleep_pressure = 0.8
            self.engine.dynamics._tick_sleep_pressure(state, minutes=60, world=self.engine.world,
                now=datetime(2026,10,19,9).timestamp())
            self.assertGreater(state.sleep_pressure, 0.8)
        await self.set_state(energy=0.1,sleep_pressure=0.1,sleep_rhythm={"awake_since":self.clock.now()-2*3600})
        await self.assess()
        self.assertEqual(len(self.llm.calls), 0)

    async def test_malformed_model_numbers_do_not_break_ticks(self):
        self.llm.replies = ['{"decision":"sleep","hours":"NaN","review_minutes":"oops"}']
        await self.assess()
        self.assertEqual((await self.get_state()).current_plan["steps"][-1]["duration"], 28800)

    async def test_sleeping_does_not_get_woken_by_a_morning_schedule(self):
        await self.set_state(node_id="bedroom")
        self.llm.replies = ['{"decision":"sleep","hours":8,"reason":"休息"}']
        await self.assess()
        schedule = self.engine.schedules.schedules[0]
        self.clock.set_struct(datetime(2026,10,19,7,0))
        schedule.time = "07:00"
        await self.engine.run_schedules()
        self.assertEqual((await self.get_state()).current_action["type"], "sleep")

    async def test_drowsy_overwork_cannot_be_extended_forever(self):
        await self.set_state(node_id="bedroom", state="drowsy", sleep_delay_until=10000,
            drowsy_sleep_step={"type":"sleep","duration":25200},
            sleep_rhythm={"awake_since":self.clock.now()-21*3600})
        await self.assess()
        state = await self.get_state()
        self.assertEqual(state.current_action["type"], "sleep")
        self.assertFalse(state.sleep_delayed)

    async def test_waking_during_drowsiness_cancels_pending_sleep_without_recording_sleep(self):
        await self.set_state(node_id="bedroom", state="drowsy",
            drowsy_sleep_step={"type":"sleep", "duration":25200},
            sleep_rhythm={"decision":"sleep", "duration_seconds":25200,
                "awake_since":self.clock.now()-14*3600})
        self.assertTrue(await self.engine.wake_up(fixture.SESSION))
        state = await self.get_state()
        self.assertEqual(state.sleep_rhythm["decision"], "continue")
        self.assertEqual(state.sleep_history, [])
        self.assertAlmostEqual(self.engine._rhythm_awake_hours(state), 14)
        await self.engine.maybe_decide(fixture.SESSION, force=True)
        await self.assess()
        state = await self.get_state()
        self.assertNotEqual((state.current_action or {}).get("type"), "sleep")
        self.assertEqual(len(self.llm.calls), 0)

    async def test_pending_sleep_respects_wake_and_delay_protection_before_walking(self):
        for protection in ("no_sleep_until", "sleep_delay_until"):
            with self.subTest(protection=protection):
                await self.set_state(current_action=None, current_plan=None,
                    sleep_rhythm={"decision":"sleep", "duration_seconds":28800,
                        "awake_since":self.clock.now()-14*3600},
                    **{"no_sleep_until":0, "sleep_delay_until":0, protection:100})
                await self.engine.maybe_decide(fixture.SESSION, force=True)
                state = await self.get_state()
                self.assertIsNone(state.current_action)
                self.assertIsNone(state.current_plan)
                await self.set_state(**{protection:0})
                await self.engine.maybe_decide(fixture.SESSION, force=True)
                self.assertEqual((await self.get_state()).current_action["type"], "walk_to")

    async def test_sleep_notes_do_not_repeat_an_old_bedtime_reason_after_waking(self):
        async with self.engine.session_state(fixture.SESSION) as state:
            state.sleep_rhythm.update(decision="awake", reason="准备去睡觉")
            self.assertNotIn("准备去睡觉", "".join(self.engine.sleep_notes(state)))

    async def test_overwork_with_disabled_sleep_backs_off_instead_of_logging_every_tick(self):
        self.engine.world.action_map()["sleep"].enabled = False
        await self.set_state(sleep_rhythm={"awake_since":self.clock.now()-21*3600})
        await self.assess()
        events = await self.db.call("query_events", session_id=fixture.SESSION, limit=30)
        await self.assess()
        await self.assess()
        self.assertEqual(await self.db.call("query_events", session_id=fixture.SESSION, limit=30), events)
        self.assertIsNone((await self.get_state()).current_action)

    async def test_disabling_sleep_records_actual_duration_and_clears_sleeping_status(self):
        await self.set_state(node_id="bedroom")
        self.llm.replies = ['{"decision":"sleep","hours":8,"reason":"休息"}']
        await self.assess()
        async with self.engine.session_state(fixture.SESSION) as state:
            state.current_action["elapsed_ticks"] = 90
        self.engine.world.action_map()["sleep"].enabled = False
        await self.engine._tick_session(fixture.SESSION)
        state = await self.get_state()
        self.assertEqual(state.sleep_history[-1]["hours"], 1.5)
        self.assertTrue(state.sleep_history[-1]["interrupted"])
        self.assertEqual(state.sleep_rhythm["decision"], "awake")

    async def test_interrupting_a_nap_when_gate_is_disabled_still_records_sleep(self):
        self.engine.world.sleep.applies_to_nap = False
        async with self.engine.session_state(fixture.SESSION) as state:
            self.engine._start_sleep_rhythm(state, "nap", 30)
            state.state = "napping"
            state.current_action = {"type":"nap", "elapsed_ticks":20, "interruptible":True}
        self.assertTrue(await self.engine.interrupt(fixture.SESSION))
        state = await self.get_state()
        self.assertAlmostEqual(state.sleep_history[-1]["hours"], 0.33)
        self.assertTrue(state.sleep_history[-1]["interrupted"])

    async def test_natural_sleep_completion_records_duration_and_does_not_repeat(self):
        await self.set_state(node_id="bedroom")
        self.llm.replies = ['{"decision":"sleep","hours":7,"reason":"明早想早点起"}']
        await self.assess()
        async with self.engine.session_state(fixture.SESSION) as state:
            state.current_action["elapsed_ticks"] = 419
        await self.engine._tick_session(fixture.SESSION)
        state = await self.get_state()
        self.assertIsNone(state.current_action)
        self.assertEqual(state.sleep_history[-1]["hours"],7)
        self.assertFalse(state.sleep_history[-1]["interrupted"])
        self.assertEqual(state.sleep_rhythm["decision"],"awake")
        await self.assess()
        self.assertEqual(len(self.llm.calls),1)

    async def test_fixed_mode_restores_existing_automatic_sleep_schedule(self):
        raw = self.store.raw_world()
        raw["sleep"]["mode"] = "fixed"
        self.store.save_world(raw)
        self.engine.reload_config()
        self.clock.set_struct(datetime(2026,10,19,23,30))
        await self.engine.run_schedules()
        self.assertEqual((await self.get_state()).current_action["type"],"walk_to")


class SleepConfigTests(unittest.TestCase):
    def test_default_and_old_configs_use_autonomy_with_fixed_opt_out(self):
        raw = default_world()
        raw["sleep"].pop("mode")
        world, warnings = parse_world(raw)
        self.assertFalse(warnings)
        self.assertEqual(world.sleep.mode, "autonomous")
        self.assertTrue(world.sleep_allowed_now(21))
        world.sleep.mode = "fixed"
        self.assertFalse(world.sleep_allowed_now(21))
