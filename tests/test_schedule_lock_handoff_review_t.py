"""Short state handoffs cannot starve a due appointment or revive stale preparation."""

import asyncio
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from core.engine.life_inference import LifeWorkDiscarded
from core.engine.state_store import StateLockBusy
from tests import test_engine as fixture

S, OTHER = fixture.SESSION, fixture.OTHER_SESSION


class ObservedLock(asyncio.Lock):
    """Expose arrival at the real asyncio lock queue without timing assumptions."""

    def __init__(self):
        super().__init__()
        self.queued = asyncio.Event()

    async def acquire(self):
        if self.locked():
            self.queued.set()
        return await super().acquire()


class ScheduleLockHandoffTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    async def prepare(self, *, other=False):
        if other:
            self.store.add_session(OTHER, session_type="group", platform="aiocqhttp",
                                   cold_start_node="study")
        raw = self.store.raw_schedules()
        raw["schedules"] = [{"id": "noon_read", "enabled": True, "time": "12:00",
                             "action_chain": [{"type": "read", "duration": 600}]}]
        self.store.save_schedules(raw)
        self.engine.reload_config()
        self.engine.world.action_map()["read"].enabled = True
        self.engine.world.action_map()["read"].on_complete.trigger = "none"
        self.clock._now = datetime(2026, 11, 3, 11, 59,
                                  tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        self.clock._struct = None
        for sid in self.engine.tick_session_ids():
            async with self.engine.session_state(sid) as state:
                state.state, state.energy, state.boredom = "idle", .9, .01
                state.current_action = state.current_plan = None
                state.cold_start_done = True
        self.assertEqual(await self.engine.run_schedules(), [])
        self.clock.advance(60)
        lock = ObservedLock()
        self.engine._locks[self.engine.state_key(S)] = lock
        return lock

    async def handoff(self, *, completed_clock):
        lock = await self.prepare()
        if completed_clock:
            clock = asyncio.create_task(asyncio.sleep(0))
            await clock
            self.engine._life_clock_jobs = {self.engine.state_key(S): clock}
        else:
            self.engine._life_clock_jobs = {}
        seen_by_later_writer = []

        async def later_writer():
            async with self.engine.session_state(S) as state:
                seen_by_later_writer.append((state.schedule_cursor,
                                             (state.current_action or {}).get("type")))

        async with self.engine.session_state(S):
            scan = asyncio.create_task(self.engine.run_schedules())
            await asyncio.wait_for(lock.queued.wait(), 1)
            writer = asyncio.create_task(later_writer())
            await asyncio.sleep(0)
        rows = await asyncio.wait_for(scan, 2)
        await asyncio.wait_for(writer, 2)
        self.assertEqual([row.session_id for row in rows], [S])
        self.assertEqual(seen_by_later_writer, [(self.clock.now(), "read")])
        self.assertEqual(await self.engine.run_schedules(), [])
        events = await self.db.call("query_events", session_id=S, event_type="schedule")
        self.assertEqual(len(events), 1)
        self.assertEqual(self.llm.calls, [])
        self.assertFalse(lock.locked())

    async def test_completed_clock_short_handoff_runs_before_later_writers(self):
        await self.handoff(completed_clock=True)

    async def test_retired_clock_short_handoff_runs_before_later_writers(self):
        await self.handoff(completed_clock=False)

    async def test_long_busy_life_preserves_cursor_while_other_life_completes(self):
        await self.prepare(other=True)
        before = (await self.engine.load_state(S)).schedule_cursor
        other_ran = asyncio.Event()
        original = self.engine._run_chain

        async def run_chain(state, *args, **kwargs):
            if state.session_id == OTHER:
                other_ran.set()
            return await original(state, *args, **kwargs)

        with patch.object(self.engine, "SCHEDULE_LOCK_WAIT_SECONDS", .08), patch.object(
                self.engine, "_run_chain", side_effect=run_chain):
            async with self.engine.session_state(S):
                scan = asyncio.create_task(self.engine.run_schedules())
                await asyncio.wait_for(other_ran.wait(), .5)
                rows = await asyncio.wait_for(scan, .5)
                self.assertTrue(self.engine.is_busy(S))
        self.assertEqual([row.session_id for row in rows], [OTHER])
        self.assertEqual((await self.engine.load_state(S)).schedule_cursor, before)
        self.assertFalse(self.engine.is_busy(S))
        retry = await self.engine.run_schedules()
        self.assertEqual([row.session_id for row in retry], [S])
        self.assertEqual(self.llm.calls, [])

    async def test_lock_timeout_does_not_cancel_an_admitted_scan(self):
        await self.prepare()
        original = self.engine._run_chain

        async def slow_chain(*args, **kwargs):
            await asyncio.sleep(.08)
            return await original(*args, **kwargs)

        with patch.object(self.engine, "SCHEDULE_LOCK_WAIT_SECONDS", .01), patch.object(
                self.engine, "_run_chain", side_effect=slow_chain):
            rows = await asyncio.wait_for(self.engine.run_schedules(), 1)
        self.assertEqual([row.session_id for row in rows], [S])
        self.assertEqual((await self.engine.load_state(S)).current_action["type"], "read")

    async def test_scan_business_timeout_keeps_its_error_and_retry_window(self):
        await self.prepare()
        before = (await self.engine.load_state(S)).schedule_cursor
        error = TimeoutError("schedule routing provider timed out")
        with patch.object(self.engine, "resolve_schedule_target", side_effect=error):
            with self.assertRaises(TimeoutError) as caught:
                await self.engine.run_schedules()
        self.assertIs(caught.exception, error)
        self.assertNotIsInstance(caught.exception, StateLockBusy)
        self.assertEqual((await self.engine.load_state(S)).schedule_cursor, before)
        self.assertFalse(await self.db.call("has_schedule_fired", session_id=S,
                         schedule_id="noon_read", day_key="2026-11-03", slot="12:00"))
        self.assertEqual([row.session_id for row in await self.engine.run_schedules()], [S])

    async def stale_queued_scan(self, change):
        lock = await self.prepare()
        await lock.acquire()
        try:
            scan = asyncio.create_task(self.engine._run_schedule_scope(S, set()))
            await asyncio.wait_for(lock.queued.wait(), 1)
            change()
            with patch.object(self.engine, "load_state", new=AsyncMock()) as load:
                self.assertFalse(scan.done())
                lock.release()
                self.assertEqual(await asyncio.wait_for(scan, 1), [])
        finally:
            if lock.locked():
                lock.release()
        self.assertEqual(load.await_count, 0)
        self.assertFalse(lock.locked())

    async def test_reload_during_lock_wait_does_not_read_or_run_old_schedule(self):
        await self.stale_queued_scan(self.engine.reload_config)

    async def test_disabling_life_during_lock_wait_does_not_read_or_run(self):
        def disable():
            self.store.set_session_enabled(S, False)
            self.engine.reload_config()
        await self.stale_queued_scan(disable)

    async def test_regroup_during_lock_wait_never_reads_new_life_under_old_lock(self):
        def regroup():
            self.store.add_session(OTHER, session_type="group", platform="aiocqhttp")
            raw = self.store.raw_sessions()
            raw["groups"] = [{"id": "shared", "sessions": [S, OTHER], "main_session": OTHER}]
            self.store.save_sessions(raw)
            self.engine.reload_config()
        await self.stale_queued_scan(regroup)
        self.assertEqual(self.engine.state_key(S), OTHER)
        self.assertFalse(self.engine.lock(OTHER).locked())

    async def test_reload_while_preparing_locked_state_does_not_save_stale_changes(self):
        await self.prepare()
        before = (await self.engine.load_state(S)).to_payload()

        async def stale_preparation(state):
            state.node_id = "bedroom"
            self.engine.reload_config()

        with patch.object(self.engine, "_validate_running_life", side_effect=stale_preparation), patch.object(
                self.engine, "save_state", new=AsyncMock()) as save:
            self.assertEqual(await self.engine._run_schedule_scope(S, set()), [])
        self.assertEqual(save.await_count, 0)
        self.assertEqual((await self.engine.load_state(S)).to_payload(), before)
        self.assertFalse(self.engine.is_busy(S))

    async def test_cancelled_lock_wait_leaves_owner_and_future_acquisition_healthy(self):
        lock = await self.prepare()
        async with self.engine.session_state(S):
            scan = asyncio.create_task(self.engine.run_schedules())
            await asyncio.wait_for(lock.queued.wait(), 1)
            scan.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await scan
            self.assertTrue(lock.locked())
        self.assertEqual([row.session_id for row in await self.engine.run_schedules()], [S])
        self.assertFalse(lock.locked())

    async def test_session_state_reports_only_acquisition_timeout_as_busy(self):
        await self.prepare()
        async with self.engine.session_state(S):
            with self.assertRaises(StateLockBusy):
                async with self.engine.session_state(S, lock_timeout=.01):
                    self.fail("A held state lock cannot admit another writer")
            self.assertTrue(self.engine.is_busy(S))
        with self.assertRaisesRegex(TimeoutError, "business") as caught:
            async with self.engine.session_state(S, lock_timeout=.01):
                raise TimeoutError("business")
        self.assertNotIsInstance(caught.exception, StateLockBusy)
        self.assertFalse(self.engine.is_busy(S))

    async def test_lock_check_failure_before_state_read_releases_original_lock(self):
        await self.prepare()

        def discard():
            raise LifeWorkDiscarded()

        with patch.object(self.engine, "load_state", new=AsyncMock()) as load:
            with self.assertRaises(LifeWorkDiscarded):
                async with self.engine.session_state(S, lock_timeout=.1, lock_check=discard):
                    self.fail("Discarded lock guard cannot enter state preparation")
        self.assertEqual(load.await_count, 0)
        self.assertFalse(self.engine.is_busy(S))
