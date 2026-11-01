"""A failed scan cannot lose accepted work or advance past an unclaimed appointment."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import unittest
from unittest.mock import patch

from core.engine import MyWorldEngine
from core.engine.life_inference import LifeWorkDiscarded
from tests import test_engine as fixture


class ScheduleClaimReviewTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    def install(self, *schedules):
        raw = self.store.raw_schedules()
        raw["schedules"] = list(schedules)
        self.store.save_schedules(raw)
        self.engine.reload_config()

    async def ready(self, *, two_lives=False, once=False, chain=None):
        if two_lives:
            self.store.add_session(fixture.OTHER_SESSION, session_type="group", platform="aiocqhttp")
            self.engine.reload_config()
        self.install({"id": "appointment", "enabled": True, "time": "12:00",
                      "once": once, "date": "2026-11-02" if once else "",
                      "action_chain": chain or [{"type": "say", "messages": ["appointment"]}]})
        self.clock.set_struct(datetime(2026, 11, 2, 11, 59))
        for session_id in self.engine.tick_session_ids():
            async with self.engine.session_state(session_id) as state:
                state.state, state.current_action, state.current_plan = "idle", None, None
                state.energy, state.sleep_pressure, state.cold_start_done = .9, 0, True
        self.assertEqual(await self.engine.run_schedules(), [])
        self.assertFalse(self.engine._schedule_reset_pending)

    async def fired(self, session_id, schedule_id="appointment", slot="12:00"):
        return await self.db.call("has_schedule_fired", session_id=session_id,
                                  schedule_id=schedule_id, day_key="2026-11-02", slot=slot)

    async def deliver(self, rows):
        for row in rows:
            await self.engine._deliver(row)

    async def test_other_lifes_failure_keeps_success_and_shared_oneoff_pending(self):
        await self.ready(two_lives=True, once=True)
        self.clock.set_struct(datetime(2026, 11, 2, 12, 0))
        healthy_finished = asyncio.Event()
        original_scope, original_route = self.engine._run_schedule_scope, self.engine.resolve_schedule_target

        async def scope(session_id, once_done):
            result = await original_scope(session_id, once_done)
            if session_id == fixture.OTHER_SESSION:
                healthy_finished.set()
            return result

        async def route(state, schedule, session_id):
            if session_id == fixture.SESSION:
                await healthy_finished.wait()
                raise RuntimeError("temporary local routing read")
            return await original_route(state, schedule, session_id)

        with patch.object(self.engine, "_run_schedule_scope", side_effect=scope), patch.object(
                self.engine, "resolve_schedule_target", side_effect=route):
            first = await self.engine.run_schedules()
        self.assertEqual([(row.session_id, row.messages) for row in first],
                         [(fixture.OTHER_SESSION, ["appointment"])])
        self.assertTrue(await self.fired(fixture.OTHER_SESSION))
        self.assertFalse(await self.fired(fixture.SESSION))
        self.assertEqual([row.id for row in self.engine.schedules.schedules], ["appointment"])
        await self.deliver(first)
        self.clock.set_struct(datetime(2026, 11, 2, 12, 1))
        second = await self.engine.run_schedules()
        self.assertEqual([(row.session_id, row.messages) for row in second],
                         [(fixture.SESSION, ["appointment"])])
        await self.deliver(second)
        self.assertEqual(await self.engine.run_schedules(), [])
        self.assertFalse(self.engine.schedules.schedules)
        self.assertEqual(self.messenger.flat_messages, ["appointment", "appointment"])
        self.assertEqual(len(self.llm.calls), 0)

    async def test_later_failure_keeps_earlier_outcome_in_the_same_life(self):
        await self.ready()
        self.install(*[{"id": name, "enabled": True, "time": "12:00", "priority": priority,
                        "once": True, "date": "2026-11-02",
                        "action_chain": [{"type": "say", "messages": [name]}]}
                       for name, priority in [("first", 20), ("second", 10)]])
        self.clock.set_struct(datetime(2026, 11, 2, 11, 59))
        await self.engine.run_schedules()
        self.clock.set_struct(datetime(2026, 11, 2, 12, 0))
        original = self.engine.resolve_schedule_target

        async def route(state, schedule, session_id):
            if schedule.id == "second":
                raise RuntimeError("temporary local routing read")
            return await original(state, schedule, session_id)

        with patch.object(self.engine, "resolve_schedule_target", side_effect=route):
            first = await self.engine.run_schedules()
        self.assertEqual([row.messages for row in first], [["first"]])
        self.assertTrue(await self.fired(fixture.SESSION, "first"))
        self.assertFalse(await self.fired(fixture.SESSION, "second"))
        self.assertEqual([row.id for row in self.engine.schedules.schedules], ["second"])
        await self.deliver(first)
        self.clock.advance(90)
        self.clock.set_struct(datetime(2026, 11, 2, 12, 1, 30))
        await self.deliver(await self.engine.run_schedules())
        self.assertEqual(self.messenger.flat_messages, ["first", "second"])
        self.assertFalse(self.engine.schedules.schedules)

    async def test_postclaim_execution_failure_keeps_started_work_without_replay(self):
        await self.ready(two_lives=True, chain=[{"type": "read", "duration": 300}])
        self.clock.set_struct(datetime(2026, 11, 2, 12, 0))
        original = self.engine._run_chain
        attempts, started = [], {}

        async def chain(state, steps, outcome, **kwargs):
            attempts.append(state.session_id)
            result = await original(state, steps, outcome, **kwargs)
            if state.session_id == fixture.SESSION:
                started.update(state.current_action or {})
                raise RuntimeError("local post-execution failure")
            return result

        with patch.object(self.engine, "_run_chain", side_effect=chain):
            rows = await self.engine.run_schedules()
            self.assertEqual([row.session_id for row in rows], [fixture.OTHER_SESSION])
            self.assertTrue(await self.fired(fixture.SESSION))
            state = await self.engine.load_state(fixture.SESSION, cold_start=False)
            self.assertEqual(state.current_action, started)
            self.assertNotIn("appointment", state.schedule_delays)
            self.assertNotIn("appointment", state.schedule_last_fired)
            self.clock.set_struct(datetime(2026, 11, 2, 12, 1))
            self.assertEqual(await self.engine.run_schedules(), [])
        self.assertCountEqual(attempts, [fixture.SESSION, fixture.OTHER_SESSION])
        self.assertEqual((await self.engine.load_state(fixture.SESSION)).current_action, started)

    async def test_first_failed_scan_keeps_its_actual_floor_across_rebuild(self):
        await self.ready()
        async with self.engine.session_state(fixture.SESSION) as state:
            state.schedule_cursor = 0
        at = datetime(2026, 11, 2, 12, 0, 10)
        self.clock.set_struct(at)
        with patch.object(self.engine, "resolve_schedule_target", side_effect=RuntimeError("local read")):
            with self.assertRaises(RuntimeError):
                await self.engine.run_schedules()
        floor = (at - timedelta(seconds=90)).timestamp()
        self.assertEqual((await self.engine.load_state(fixture.SESSION)).schedule_cursor, floor)
        self.assertFalse(await self.fired(fixture.SESSION))
        await self.engine.close_background()
        self.engine = MyWorldEngine(store=self.store, db=self.db, llm=self.llm,
                                    messenger=self.messenger, tools=self.tools,
                                    persona=self.persona, clock=self.clock,
                                    tick_seconds=60, decider_interval=0)
        self.addAsyncCleanup(self.engine.close_background)
        self.clock.set_struct(at + timedelta(seconds=60))
        rows = await self.engine.run_schedules()
        self.assertEqual([row.messages for row in rows], [["appointment"]])
        await self.deliver(rows)
        self.assertEqual(await self.engine.run_schedules(), [])
        self.assertEqual(self.messenger.flat_messages, ["appointment"])

    async def test_cleanup_failure_keeps_already_completed_outcome_and_receipt(self):
        await self.ready()
        self.clock.set_struct(datetime(2026, 11, 2, 12, 0))
        with patch.object(self.engine, "_schedule_prune_protections", side_effect=RuntimeError("local cleanup read")):
            rows = await self.engine.run_schedules()
        self.assertEqual([row.messages for row in rows], [["appointment"]])
        self.assertTrue(await self.fired(fixture.SESSION))
        await self.deliver(rows)
        self.assertEqual(await self.engine.run_schedules(), [])
        self.assertEqual(self.messenger.flat_messages, ["appointment"])

    async def test_repeated_failure_does_not_slide_the_floor_or_revive_expired_work(self):
        await self.ready()
        async with self.engine.session_state(fixture.SESSION) as state:
            state.schedule_cursor = 0
        at = datetime(2026, 11, 2, 12, 0, 10)
        floor = (at - timedelta(seconds=90)).timestamp()
        for delay in (0, 60):
            self.clock.set_struct(at + timedelta(seconds=delay))
            with patch.object(self.engine, "resolve_schedule_target", side_effect=RuntimeError("local read")):
                with self.assertRaises(RuntimeError):
                    await self.engine.run_schedules()
            self.assertEqual((await self.engine.load_state(fixture.SESSION)).schedule_cursor, floor)
        self.clock.set_struct(at + timedelta(seconds=511))  # Original floor is now 601 seconds old.
        self.assertEqual(await self.engine.run_schedules(), [])
        self.assertFalse(await self.fired(fixture.SESSION))
        self.assertEqual(self.messenger.sent, [])

    async def test_true_cancellation_keeps_claims_and_never_returns_old_outcomes(self):
        await self.ready(two_lives=True)
        self.clock.set_struct(datetime(2026, 11, 2, 12, 0))
        healthy_finished, release = asyncio.Event(), asyncio.Event()
        original_scope, original_route = self.engine._run_schedule_scope, self.engine.resolve_schedule_target

        async def scope(session_id, once_done):
            result = await original_scope(session_id, once_done)
            if session_id == fixture.OTHER_SESSION:
                healthy_finished.set()
            return result

        async def route(state, schedule, session_id):
            if session_id == fixture.SESSION:
                await release.wait()
            return await original_route(state, schedule, session_id)

        with patch.object(self.engine, "_run_schedule_scope", side_effect=scope), patch.object(
                self.engine, "resolve_schedule_target", side_effect=route):
            task = asyncio.create_task(self.engine.run_schedules())
            await asyncio.wait_for(healthy_finished.wait(), 3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(await self.fired(fixture.OTHER_SESSION))
        self.assertEqual(self.messenger.sent, [])

    async def test_discarded_life_decision_does_not_revive_its_generated_content(self):
        await self.ready(two_lives=True)
        self.clock.set_struct(datetime(2026, 11, 2, 12, 0))
        original = self.engine.resolve_schedule_target

        async def route(state, schedule, session_id):
            if session_id == fixture.SESSION:
                raise LifeWorkDiscarded()
            return await original(state, schedule, session_id)

        with patch.object(self.engine, "resolve_schedule_target", side_effect=route):
            rows = await self.engine.run_schedules()
        self.assertEqual([row.session_id for row in rows], [fixture.OTHER_SESSION])
        await self.deliver(rows)
        self.assertEqual(self.messenger.sent, [(fixture.OTHER_SESSION, ["appointment"])])
        self.assertFalse(await self.fired(fixture.SESSION))
