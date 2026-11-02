"""Real online quiet progress survives irregular beats and inference rejoin."""
import asyncio
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from core.engine.life_inference import LIFE_WORK, LifeWorkDiscarded, released_life_lock
from core.models import ActionDef
from tests import test_engine as fixture

S = fixture.SESSION


class IdleClockJitterTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    async def start_idle(self):
        self.clock._now = datetime(2026, 11, 2, 23, 45, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        for action in self.engine.world.actions:
            if action.idle:
                action.enabled = False
        self.engine.world.actions.append(ActionDef(
            id="jitter_idle", name="练习素描", category="continuous", idle=True,
            life_role="interest", duration_mode="fixed", duration=600,
        ))
        self.engine.world.idle_life.duration_min_minutes = 10
        self.engine.world.idle_life.duration_max_minutes = 10
        self.engine.world.sleep.mode = "activity"
        self.engine.world.state_dynamics.energy_decay_per_min = 0
        self.engine.world.state_dynamics.daily_mood_enabled = False
        self.engine.decider.should_ask_llm = lambda *_args, **_kwargs: False
        self.engine.decider.rule_plan = lambda *_args, **_kwargs: None
        async with self.engine.session_state(S) as state:
            state.node_id, state.state, state.energy = "study", "idle", .9
            state.current_action = state.current_plan = None
            state.life_updated_at = self.clock.now()
            self.engine._tick_idle_life(state)
        return self.clock.now()

    async def test_irregular_online_beats_do_not_backdate_already_recorded_idle(self):
        started = await self.start_idle()
        last_at = started
        delays = (60, 90, 120, 60, 90, 120, 60, 90, 120, 90)
        for delay in delays:
            self.clock.advance(delay)
            await self.engine._tick_session(S)
            state = await self.engine.load_state(S, cold_start=False)
            record = state.life_interests["jitter_idle"]
            self.assertGreaterEqual(record["last_at"], last_at)
            self.assertEqual(self.clock.now(), record["last_at"])
            last_at = record["last_at"]
        self.assertEqual(started + sum(delays), last_at)
        self.assertEqual(10, record["minutes"])
        self.assertEqual(1, record["sessions"])
        self.assertEqual([], self.llm.calls)
        self.assertEqual([], self.messenger.sent)

    async def test_idle_time_receipt_rejoins_without_discard_or_duplicate_work(self):
        await self.start_idle()
        token = LIFE_WORK.set(True)
        try:
            async with self.engine.session_state(S) as state:
                try:
                    async with released_life_lock(self.engine):
                        self.clock.advance(90)
                        await asyncio.create_task(self.engine._tick_while_thinking(S))
                except LifeWorkDiscarded:
                    self.fail("an online idle clock receipt must not discard a prepared decision")
                self.assertEqual({}, state.life_interests)
                self.assertEqual("jitter_idle", state.idle_activity["id"])
                self.assertEqual(1, state.idle_activity["progress_ticks"])
                self.assertEqual(self.clock.now(), state.idle_activity["progress_at"])
                self.engine._settle_idle_practice(state)
                record = state.life_interests["jitter_idle"]
                self.assertEqual(1, record["minutes"])
                self.assertEqual(self.clock.now(), record["last_at"])
                self.engine._settle_idle_practice(state)
                self.assertEqual(1, record["minutes"])
        finally:
            LIFE_WORK.reset(token)
        self.assertEqual([], self.llm.calls)
        self.assertEqual([], self.messenger.sent)
