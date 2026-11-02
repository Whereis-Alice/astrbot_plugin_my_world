"""Schedule acceptance using only the production tick and its own background jobs."""
import asyncio
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from core.models import ChainStep, ScheduleDef
from tests import test_engine as fixture

S = fixture.SESSION


class ScheduleClockFairnessTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    async def prepare(self):
        self.clock._now = datetime(2026, 11, 3, 11, 59, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        self.clock._struct = None
        self.engine.queue_background = lambda *args, **kwargs: None
        self.engine._maybe_run_event = AsyncMock()
        self.engine._sync_nickname = AsyncMock()
        self.engine.schedules.schedules = [ScheduleDef(id="noon_read", time="12:00", days=[],
            action_chain=[ChainStep(type="read", duration=600)], sessions=[S])]
        self.engine.world.action_map()["read"].on_complete.trigger = "none"
        self.engine.world.action_map()["read"].enabled = True
        async with self.engine.session_state(S) as state:
            state.state, state.energy, state.boredom = "idle", .9, .01
            state.current_action = state.current_plan = None
            state.life_updated_at = self.clock.now()
            state.schedule_cursor = self.clock.now() - 30

    async def settle_tick(self):
        result = await self.engine.tick()
        await asyncio.wait_for(asyncio.gather(*tuple(getattr(self.engine, "_life_delivery_jobs", ()))), 5)
        await asyncio.wait_for(self.engine._schedule_delivery_job, 5)
        return result

    async def test_slow_clock_does_not_starve_daily_schedule_or_replay_it(self):
        await self.prepare()
        original = self.engine._tick_session

        async def delayed(session):
            async with self.engine.session_state(session):
                await asyncio.sleep(.5)
            return await original(session)

        with patch.object(self.engine, "_tick_session", side_effect=delayed):
            for minute in range(4):
                if minute:
                    self.clock.advance(60)
                await self.settle_tick()
                state = await self.engine.load_state(S, cold_start=False)
                self.assertEqual(state.schedule_cursor, self.clock.now())
                if minute:
                    self.assertEqual(state.current_action["type"], "read")
        rows = await self.db.call("query_events", session_id=S, event_type="schedule")
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.llm.calls, [])
        self.assertEqual(self.messenger.sent, [])

    async def test_external_busy_life_does_not_hold_other_schedule_scans(self):
        await self.prepare()
        other = fixture.OTHER_SESSION
        self.store.add_session(other, session_type="group", platform="aiocqhttp", cold_start_node="study")
        self.engine.reload_config()
        self.engine.schedules.schedules = [ScheduleDef(id="noon_read", time="12:00", days=[],
            action_chain=[ChainStep(type="read", duration=600)], sessions=[])]
        self.engine.world.action_map()["read"].on_complete.trigger = "none"
        async with self.engine.session_state(other) as state:
            state.state, state.energy = "idle", .9
            state.current_action = state.current_plan = None
        self.clock.advance(60)
        async with self.engine.session_state(S):
            rows = await asyncio.wait_for(self.engine.run_schedules(), 2)
        self.assertEqual([row.session_id for row in rows], [other])
        state = await self.engine.load_state(S, cold_start=False)
        self.assertLess(state.schedule_cursor, self.clock.now())
        self.assertFalse(self.engine.lock(self.engine.state_key(S)).locked())
