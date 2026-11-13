"""Unsent background requests preserve quiet thought and sleep opportunities."""
from __future__ import annotations

import asyncio
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from core.background_budget import record_background_admission
from core.engine import TickOutcome
from core.planner import create_plan
from tests import test_engine as fixture


class QuietLifeAdmissionTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp
    get_state = fixture.EngineTestCase.get_state
    set_state = fixture.EngineTestCase.set_state

    async def quiet(self, *, rule=None):
        outcome = TickOutcome(session_id=fixture.SESSION)
        with patch.object(self.engine.decider, "rule_plan", return_value=rule), \
                patch.object(self.engine.decider, "should_ask_llm", return_value=True):
            async with self.engine.session_state(fixture.SESSION) as state:
                state.state, state.energy, state.sleep_pressure = "idle", .9, 0
                await self.engine._quiet_rule_plan(state, self.engine.node(state.node_id), outcome)
        return outcome

    async def defer(self, *_args, **_kwargs):
        record_background_admission(deferred=True, reason="后台通道忙", next_at=self.clock.now() + 120)
        return None

    async def test_wait_preserves_thought_and_retries_when_ready(self):
        self.engine._ask_llm = AsyncMock(side_effect=self.defer)
        first = await self.quiet()
        saved = await self.get_state()
        self.assertEqual(first.llm_calls, 0)
        self.assertEqual(saved.last_quiet_plan_at + 3600, self.clock.now() + 120)
        self.assertIsNone(saved.current_action)
        await self.quiet()
        self.assertEqual(self.engine._ask_llm.await_count, 1)
        self.clock.advance(120)

        async def choose(*_args, **_kwargs):
            record_background_admission(started=True)
            return '{"plan":[{"action":"read","duration":600}],"reason":"接着读一会儿"}'

        self.engine._ask_llm.side_effect = choose
        second = await self.quiet()
        self.assertEqual(self.engine._ask_llm.await_count, 2)
        self.assertEqual(second.llm_calls, 1)
        self.assertEqual((await self.get_state()).current_action["type"], "read")
        self.assertEqual(self.messenger.sent, [])

    async def test_local_preparation_failure_retains_opportunity(self):
        with patch.object(self.engine, "prepare_context_memory", AsyncMock(side_effect=RuntimeError("local lookup failed"))):
            outcome = await self.quiet()
        self.assertEqual(outcome.llm_calls, 0)
        self.assertEqual(self.llm.calls, [])
        saved = await self.get_state()
        self.assertEqual(saved.last_quiet_plan_at + 3600, self.clock.now() + self.engine.tick_seconds)
        self.assertIsNone(saved.current_plan)

    async def test_cancelled_local_preparation_refunds_only_the_unsent_thought(self):
        with patch.object(self.engine, "prepare_context_memory", AsyncMock(side_effect=asyncio.CancelledError())):
            with self.assertRaises(asyncio.CancelledError):
                await self.quiet()
        saved = await self.get_state()
        self.assertEqual(saved.last_quiet_plan_at + 3600, self.clock.now() + self.engine.tick_seconds)
        self.assertEqual(self.llm.calls, [])

    async def test_cancelled_dispatched_thought_keeps_its_hourly_limit(self):
        async def cancel(*_args, **_kwargs):
            record_background_admission(started=True)
            raise asyncio.CancelledError()

        self.engine._ask_llm = AsyncMock(side_effect=cancel)
        with self.assertRaises(asyncio.CancelledError):
            await self.quiet()
        self.assertEqual((await self.get_state()).last_quiet_plan_at, self.clock.now())

    async def test_dispatched_failure_keeps_hourly_thought_limit(self):
        async def failed(*_args, **_kwargs):
            record_background_admission(started=True)
            return None

        self.engine._ask_llm = AsyncMock(side_effect=failed)
        first = await self.quiet()
        self.assertEqual(first.llm_calls, 1)
        self.assertEqual((await self.get_state()).last_quiet_plan_at, self.clock.now())
        self.clock.advance(120)
        await self.quiet()
        self.assertEqual(self.engine._ask_llm.await_count, 1)

    async def test_rules_keep_life_moving_while_model_waits(self):
        self.engine._ask_llm = AsyncMock(side_effect=self.defer)
        rule = create_plan(steps=[{"action": "read", "duration": 600}],
                           now=self.clock.now(), world_time=0, tick_seconds=60,
                           reason="先安静读书", source="rule")
        outcome = await self.quiet(rule=rule)
        self.assertEqual(outcome.llm_calls, 0)
        self.assertEqual((await self.get_state()).current_action["type"], "read")
        self.assertEqual(self.messenger.sent, [])


class SleepAdmissionTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    get_state = fixture.EngineTestCase.get_state
    set_state = fixture.EngineTestCase.set_state

    async def asyncSetUp(self):
        await fixture.EngineTestCase.asyncSetUp(self)
        raw = self.store.raw_world()
        raw["sleep"].update(mode="autonomous", goodnight=False, goodmorning=False, drowsy=False)
        self.store.save_world(raw)
        self.engine.reload_config()
        self.clock.set_struct(datetime(2026, 11, 13, 21, 30))
        await self.set_state(state="idle", cold_start_done=True, node_id="study", world_time=10,
                             energy=.5, sleep_pressure=.7,
                             sleep_rhythm={"awake_since": self.clock.now() - 14 * 3600})

    async def assess(self):
        outcome = TickOutcome(session_id=fixture.SESSION)
        async with self.engine.session_state(fixture.SESSION) as state:
            await self.engine._tick_sleep_rhythm(state, outcome)
        return outcome

    async def defer(self, *_args, **_kwargs):
        record_background_admission(deferred=True, reason="后台额度暂未释放", next_at=self.clock.now() + 120)
        return None

    async def test_unsent_sleep_assessment_is_retried_without_fake_model_call(self):
        self.engine._ask_llm = AsyncMock(side_effect=self.defer)
        outcome = await self.assess()
        state = await self.get_state()
        self.assertEqual(outcome.llm_calls, 0)
        self.assertEqual(state.sleep_rhythm["next_evaluation_at"], self.clock.now() + 120)
        self.assertNotEqual(state.sleep_rhythm.get("source"), "llm")
        self.assertIsNone(state.current_action)
        await self.assess()
        self.assertEqual(self.engine._ask_llm.await_count, 1)
        self.clock.advance(120)

        async def sleep(*_args, **_kwargs):
            record_background_admission(started=True)
            return '{"decision":"sleep","hours":7,"reason":"现在想睡了"}'

        self.engine._ask_llm.side_effect = sleep
        outcome = await self.assess()
        self.assertEqual(outcome.llm_calls, 1)
        state = await self.get_state()
        self.assertEqual(state.sleep_rhythm["source"], "llm")
        self.assertEqual(state.current_action["target_node"], "bedroom")

    async def test_local_sleep_background_failure_keeps_a_short_retry(self):
        with patch.object(self.engine, "task_background", AsyncMock(side_effect=RuntimeError("local lookup failed"))):
            outcome = await self.assess()
        state = await self.get_state()
        self.assertEqual(outcome.llm_calls, 0)
        self.assertEqual(state.sleep_rhythm["next_evaluation_at"], self.clock.now() + self.engine.tick_seconds)
        self.assertEqual(self.llm.calls, [])
        self.assertIsNone(state.current_action)

    async def test_cancelled_local_sleep_preparation_keeps_a_short_retry(self):
        with patch.object(self.engine, "task_background", AsyncMock(side_effect=asyncio.CancelledError())):
            with self.assertRaises(asyncio.CancelledError):
                await self.assess()
        state = await self.get_state()
        self.assertEqual(state.sleep_rhythm["next_evaluation_at"], self.clock.now() + self.engine.tick_seconds)
        self.assertEqual(self.llm.calls, [])

    async def test_cancelled_dispatched_sleep_preserves_its_evaluation_interval(self):
        async def cancel(*_args, **_kwargs):
            record_background_admission(started=True)
            raise asyncio.CancelledError()

        self.engine._ask_llm = AsyncMock(side_effect=cancel)
        with self.assertRaises(asyncio.CancelledError):
            await self.assess()
        state = await self.get_state()
        self.assertEqual(state.sleep_rhythm["next_evaluation_at"],
                         self.clock.now() + self.engine.world.sleep.evaluation_minutes * 60)

    async def test_necessary_rule_sleep_does_not_wait_for_model_admission(self):
        await self.set_state(sleep_rhythm={"awake_since": self.clock.now() - 18 * 3600})
        self.engine._ask_llm = AsyncMock(side_effect=self.defer)
        outcome = await self.assess()
        state = await self.get_state()
        self.assertEqual(outcome.llm_calls, 0)
        self.assertEqual(state.sleep_rhythm["source"], "fallback")
        self.assertEqual(state.current_action["target_node"], "bedroom")
        self.assertEqual(self.messenger.sent, [])

    async def test_dispatched_sleep_failure_keeps_normal_evaluation_interval(self):
        async def failed(*_args, **_kwargs):
            record_background_admission(started=True)
            return None

        self.engine._ask_llm = AsyncMock(side_effect=failed)
        outcome = await self.assess()
        self.assertEqual(outcome.llm_calls, 1)
        state = await self.get_state()
        self.assertEqual(state.sleep_rhythm["source"], "fallback")
        self.assertEqual(state.sleep_rhythm["next_evaluation_at"],
                         self.clock.now() + self.engine.world.sleep.evaluation_minutes * 60)
