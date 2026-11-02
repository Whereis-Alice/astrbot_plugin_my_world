"""Only an unsent scheduled line waits for its local hourly generation allowance."""
from datetime import datetime, timedelta
import unittest

from core.engine import MyWorldEngine
from tests import test_engine as fixture


class ScheduleTextWaitTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    def at(self, minutes):
        stamp = datetime(2026, 11, 3, 12) + timedelta(minutes=minutes)
        self.clock._now = stamp.timestamp()
        self.clock.set_struct(stamp)

    async def prepare(self, *, chain=None, blocked=False):
        self.at(-1)
        raw = self.store.raw_schedules()
        raw["schedules"] = [{"id": "appointment", "time": "12:00", "once": True,
            "date": "2026-11-03", "action_chain": chain or [{"type": "say", "intent": "问候一下"}]}]
        self.store.save_schedules(raw)
        raw_world = self.store.raw_world()
        raw_world["limits"]["max_llm_text_per_hour"] = 1
        if blocked:
            raw_world["life_share_session_whitelist"] = []
        self.store.save_world(raw_world)
        self.engine.reload_config()
        async with self.engine.session_state(fixture.SESSION) as state:
            state.state, state.energy, state.sleep_pressure = "idle", .9, 0
            state.current_action, state.current_plan = None, None
            state.world_time, state.cold_start_done = 720, True
        self.assertEqual(await self.engine.run_schedules(), [])
        self.at(0)

    async def exhaust(self):
        async with self.engine.session_state(fixture.SESSION) as state:
            state.llm_text_hour_marker = self.engine._hour_index(state)
            state.llm_text_count_hour = 1

    async def snapshot(self):
        return await self.engine.load_state(fixture.SESSION, cold_start=False)

    async def test_oneoff_waits_without_receipt_of_completion_then_runs_once(self):
        await self.prepare()
        await self.exhaust()
        self.assertEqual(await self.engine.run_schedules(), [])
        state = await self.snapshot()
        self.assertIn("appointment", state.schedule_delays)
        self.assertNotIn("appointment", state.schedule_last_fired)
        self.assertEqual([s.id for s in self.engine.schedules.schedules], ["appointment"])
        self.assertEqual(self.llm.calls, [])
        self.assertEqual(self.messenger.sent, [])
        events = await self.db.call("query_events", session_id=fixture.SESSION, event_type="schedule")
        self.assertEqual(events, [])
        self.assertTrue(await self.db.call("has_schedule_fired", session_id=fixture.SESSION,
            schedule_id="appointment", day_key="2026-11-03", slot="12:00"))
        self.at(30)
        self.assertEqual(await self.engine.run_schedules(), [])
        self.at(60)
        async with self.engine.session_state(fixture.SESSION) as state:
            state.world_time += 60
        rows = await self.engine.run_schedules()
        self.assertEqual([row.messages for row in rows], [["有人在吗？"]])
        state = await self.snapshot()
        self.assertNotIn("appointment", state.schedule_delays)
        self.assertIn("appointment", state.schedule_last_fired)
        self.assertEqual(self.engine.schedules.schedules, [])
        self.assertEqual(await self.engine.run_schedules(), [])
        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual(self.messenger.sent, [])

    async def test_same_library_rebuild_keeps_unsent_wait(self):
        await self.prepare()
        await self.exhaust()
        self.assertEqual(await self.engine.run_schedules(), [])
        await self.engine.close_background()
        self.engine = MyWorldEngine(store=self.store, db=self.db, llm=self.llm,
            messenger=self.messenger, tools=self.tools, persona=self.persona,
            clock=self.clock, tick_seconds=60, decider_interval=0)
        self.addAsyncCleanup(self.engine.close_background)
        self.at(60)
        async with self.engine.session_state(fixture.SESSION) as state:
            state.world_time += 60
        rows = await self.engine.run_schedules()
        self.assertEqual([row.messages for row in rows], [["有人在吗？"]])
        self.assertEqual(await self.engine.run_schedules(), [])
        self.assertEqual(len(self.llm.calls), 1)

    async def test_explicit_silence_after_a_real_model_request_is_not_retried(self):
        await self.prepare()
        self.llm.replies = [""]
        rows = await self.engine.run_schedules()
        self.assertEqual([row.messages for row in rows], [[]])
        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual((await self.snapshot()).schedule_delays, {})
        self.at(60)
        self.assertEqual(await self.engine.run_schedules(), [])
        self.assertEqual(len(self.llm.calls), 1)

    async def test_admin_denial_does_not_become_a_budget_wait(self):
        await self.prepare(blocked=True)
        await self.exhaust()
        rows = await self.engine.run_schedules()
        self.assertEqual([row.messages for row in rows], [[]])
        self.assertEqual((await self.snapshot()).schedule_delays, {})
        self.assertEqual(self.llm.calls, [])
        self.at(60)
        self.assertEqual(await self.engine.run_schedules(), [])

    async def test_fixed_line_does_not_need_the_model_allowance(self):
        await self.prepare(chain=[{"type": "say", "messages": ["约好了在这里见。"]}])
        await self.exhaust()
        rows = await self.engine.run_schedules()
        self.assertEqual([row.messages for row in rows], [["约好了在这里见。"]])
        self.assertEqual((await self.snapshot()).schedule_delays, {})
        self.assertEqual(self.llm.calls, [])

    async def test_single_share_uses_the_same_unsent_text_wait(self):
        await self.prepare(chain=[{"type": "share"}])
        await self.exhaust()
        self.assertEqual(await self.engine.run_schedules(), [])
        self.assertIn("appointment", (await self.snapshot()).schedule_delays)
        self.assertEqual(self.llm.calls, [])

    async def test_disabled_text_generation_does_not_retry_forever(self):
        await self.prepare()
        self.engine.world.limits.max_llm_text_per_hour = 0
        rows = await self.engine.run_schedules()
        self.assertEqual([row.messages for row in rows], [[]])
        self.assertEqual((await self.snapshot()).schedule_delays, {})
        self.assertEqual(self.llm.calls, [])
        self.at(30)
        self.assertEqual(await self.engine.run_schedules(), [])

    async def test_multi_step_life_starts_without_replaying_earlier_effects(self):
        await self.prepare(chain=[{"type": "read", "duration": 600}, {"type": "say", "intent": "聊聊读书"}])
        await self.exhaust()
        rows = await self.engine.run_schedules()
        self.assertEqual(len(rows), 1)
        state = await self.snapshot()
        self.assertEqual(state.current_action["type"], "read")
        self.assertEqual(state.current_plan["steps"][0]["action"], "say")
        self.assertEqual(state.schedule_delays, {})
        self.assertEqual(self.llm.calls, [])
        self.at(30)
        self.assertEqual(await self.engine.run_schedules(), [])

    async def test_wait_reuses_existing_bounded_delay_policy(self):
        await self.prepare()
        await self.exhaust()
        for minutes in (0, 30, 60):
            self.at(minutes)
            self.assertEqual(await self.engine.run_schedules(), [])
        state = await self.snapshot()
        self.assertTrue(state.schedule_delays["appointment"]["cancelled"])
        self.assertEqual(state.schedule_delays["appointment"]["until"], 0)
        self.assertNotIn("appointment", state.schedule_last_fired)
        self.at(90)
        async with self.engine.session_state(fixture.SESSION) as state:
            state.world_time += 60
        self.assertEqual(await self.engine.run_schedules(), [])
        self.assertEqual(self.llm.calls, [])

