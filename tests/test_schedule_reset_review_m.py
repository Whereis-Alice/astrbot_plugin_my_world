"""A changed appointment is checked once by each actual life, including a busy one."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, patch

from core.engine import MyWorldEngine
from tests import test_engine as fixture


class EpochClock:
    """Use the production calendar conversion for different world timezones."""
    def __init__(self, at):
        self.at = at

    def now(self):
        return self.at


class ScheduleResetReviewTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp
    add_schedule = fixture.EngineTestCase.add_schedule
    add_group = fixture.EngineTestCase.add_group
    get_state = fixture.EngineTestCase.get_state

    async def empty_scan(self, at=datetime(2026, 9, 10, 12, 0)):
        raw = self.store.raw_schedules()
        raw["schedules"] = []
        self.store.save_schedules(raw)
        self.engine.reload_config()
        self.clock.set_struct(at)
        for session_id in self.engine.tick_session_ids():
            async with self.engine.session_state(session_id) as state:
                state.state, state.current_action, state.current_plan = "idle", None, None
                state.energy, state.sleep_pressure = .9, 0
                state.cold_start_done = True
        self.assertEqual(await self.engine.run_schedules(), [])

    def change_due(self, at=datetime(2026, 9, 10, 12, 0, 10), **fields):
        self.clock.set_struct(at)
        self.add_schedule(id="changed", time="12:00",
                          action_chain=[{"type": "say", "messages": ["appointment"]}], **fields)

    async def busy_scan(self, session_id=fixture.SESSION):
        async with self.engine.lock(self.engine.state_key(session_id)):
            return await self.engine.run_schedules()

    async def rebuild_engine(self):
        await self.engine.close_background()
        self.engine = MyWorldEngine(store=self.store, db=self.db, llm=self.llm,
                                    messenger=self.messenger, tools=self.tools,
                                    persona=self.persona, clock=self.clock,
                                    tick_seconds=60, decider_interval=0)
        self.addAsyncCleanup(self.engine.close_background)

    async def test_ordinary_busy_scan_keeps_its_previous_cursor(self):
        await self.empty_scan(datetime(2026, 9, 10, 11, 58))
        self.change_due(datetime(2026, 9, 10, 11, 59))
        await self.engine.run_schedules()
        self.clock.set_struct(datetime(2026, 9, 10, 12, 0))
        self.assertEqual(await self.busy_scan(), [])
        self.clock.set_struct(datetime(2026, 9, 10, 12, 1))
        rows = await self.engine.run_schedules()
        self.assertEqual([message for row in rows for message in row.messages], ["appointment"])
        self.assertEqual(len(self.llm.calls), 0)

    async def test_busy_changed_slot_runs_next_minute_without_another_tick(self):
        await self.empty_scan()
        self.change_due()
        self.assertEqual(await self.busy_scan(), [])
        self.clock.set_struct(datetime(2026, 9, 10, 12, 1, 10))
        rows = await self.engine.run_schedules()
        self.assertEqual([message for row in rows for message in row.messages], ["appointment"])
        self.assertEqual(await self.engine.run_schedules(), [])
        self.assertEqual(len(self.llm.calls), 0)

    async def test_busy_recheck_keeps_the_original_short_window_through_unchanged_reload(self):
        await self.empty_scan()
        self.change_due()
        self.assertEqual(await self.busy_scan(), [])
        self.clock.set_struct(datetime(2026, 9, 10, 12, 2, 10))
        self.engine.reload_config()
        self.assertEqual(await self.busy_scan(), [])
        self.clock.set_struct(datetime(2026, 9, 10, 12, 3, 10))
        rows = await self.engine.run_schedules()
        self.assertEqual([message for row in rows for message in row.messages], ["appointment"])

    async def test_successful_life_does_not_repeat_routing_while_its_neighbour_is_busy(self):
        self.store.add_session(fixture.OTHER_SESSION, session_type="group", platform="aiocqhttp")
        self.engine.reload_config()
        await self.empty_scan()
        self.change_due()
        route = AsyncMock(wraps=self.engine.resolve_schedule_target)
        with patch.object(self.engine, "resolve_schedule_target", route):
            first = await self.busy_scan()
            self.assertEqual([row.session_id for row in first], [fixture.OTHER_SESSION])
            self.clock.set_struct(datetime(2026, 9, 10, 12, 1, 10))
            second = await self.engine.run_schedules()
            self.assertEqual([row.session_id for row in second], [fixture.SESSION])
            self.clock.set_struct(datetime(2026, 9, 10, 12, 2, 10))
            self.assertEqual(await self.engine.run_schedules(), [])
        self.assertEqual(route.await_count, 2)
        for row in [*first, *second]:
            await self.engine._deliver(row)
        self.assertEqual(self.messenger.flat_messages.count("appointment"), 2)
        self.assertEqual(len(self.llm.calls), 0)

    async def test_shared_group_has_one_recheck_and_one_delivery(self):
        self.add_group()
        await self.empty_scan()
        self.change_due()
        self.assertEqual(await self.busy_scan(fixture.OTHER_SESSION), [])
        self.clock.set_struct(datetime(2026, 9, 10, 12, 1, 10))
        rows = await self.engine.run_schedules()
        self.assertEqual(len(rows), 1)
        await self.engine._deliver(rows[0])
        self.assertEqual(self.messenger.flat_messages.count("appointment"), 1)
        self.assertEqual(len(self.llm.calls), 0)

    async def test_midnight_oneoff_is_preserved_until_busy_life_checks_it(self):
        await self.empty_scan(datetime(2026, 9, 11, 0, 0))
        self.clock.set_struct(datetime(2026, 9, 11, 0, 0, 10))
        self.add_schedule(id="changed", time="23:59", once=True, date="2026-09-10",
                          action_chain=[{"type": "say", "messages": ["midnight appointment"]}])
        self.assertEqual(await self.busy_scan(), [])
        self.assertEqual([row["id"] for row in self.store.raw_schedules()["schedules"]], ["changed"])
        self.clock.set_struct(datetime(2026, 9, 11, 0, 1, 10))
        rows = await self.engine.run_schedules()
        self.assertEqual([message for row in rows for message in row.messages], ["midnight appointment"])
        self.assertEqual(self.store.raw_schedules()["schedules"], [])

    async def test_reset_still_expires_at_the_existing_max_gap(self):
        await self.empty_scan()
        self.change_due()
        self.assertEqual(await self.busy_scan(), [])
        self.clock.set_struct(datetime(2026, 9, 10, 12, 9))
        route = AsyncMock(wraps=self.engine.resolve_schedule_target)
        with patch.object(self.engine, "resolve_schedule_target", route):
            self.assertEqual(await self.engine.run_schedules(), [])
        self.assertEqual(route.await_count, 0)
        self.assertFalse(self.engine._schedule_reset_pending)

    async def test_reset_uses_the_life_calendar_in_an_aware_timezone(self):
        zone = timezone(timedelta(hours=-5))
        await self.empty_scan(datetime(2026, 9, 10, 12, 0, tzinfo=zone))
        self.change_due(datetime(2026, 9, 10, 12, 0, 10, tzinfo=zone))
        self.assertEqual(await self.busy_scan(), [])
        self.clock.set_struct(datetime(2026, 9, 10, 12, 1, 10, tzinfo=zone))
        rows = await self.engine.run_schedules()
        self.assertEqual([message for row in rows for message in row.messages], ["appointment"])

    async def test_two_world_timezones_recheck_their_own_simultaneous_appointments(self):
        self.store.add_session(fixture.OTHER_SESSION, session_type="group", platform="aiocqhttp")
        local_world = self.store.raw_world()
        local_world["timezone"] = "UTC"
        self.store.save_scope(fixture.OTHER_SESSION, local_world, {"schedules": []}, ["settings", "schedules"])
        self.engine.reload_config()
        await self.empty_scan()
        self.engine.clock = self.clock = EpochClock(datetime(2026, 9, 10, 4, tzinfo=timezone.utc).timestamp())
        await self.engine.run_schedules()
        self.clock.at += 10
        self.add_schedule(id="changed", time="12:00",
                          action_chain=[{"type": "say", "messages": ["Shanghai appointment"]}])
        self.store.save_scope(fixture.OTHER_SESSION, local_world,
                              {"schedules": [{"id": "utc", "enabled": True, "time": "04:00",
                                              "action_chain": [{"type": "say", "messages": ["UTC appointment"]}]}]},
                              ["settings", "schedules"])
        self.engine.reload_config()
        first = await self.busy_scan()
        self.assertEqual([message for row in first for message in row.messages], ["UTC appointment"])
        self.clock.at += 60
        second = await self.engine.run_schedules()
        self.assertEqual([message for row in second for message in row.messages], ["Shanghai appointment"])
        self.assertEqual(await self.engine.run_schedules(), [])
        self.assertEqual(len(self.llm.calls), 0)

    async def test_failed_preclaim_scan_preserves_the_changed_appointment(self):
        await self.empty_scan()
        self.change_due()
        with patch.object(self.engine, "resolve_schedule_target", side_effect=RuntimeError("temporary local read")):
            with self.assertRaises(RuntimeError):
                await self.engine.run_schedules()
        self.clock.set_struct(datetime(2026, 9, 10, 12, 1, 10))
        rows = await self.engine.run_schedules()
        self.assertEqual([message for row in rows for message in row.messages], ["appointment"])

    async def test_cancelled_preclaim_scan_preserves_the_changed_appointment(self):
        await self.empty_scan()
        self.change_due()
        entered = asyncio.Event()
        release = asyncio.Event()
        original = self.engine.resolve_schedule_target

        async def delayed(*args):
            entered.set()
            await release.wait()
            return await original(*args)

        with patch.object(self.engine, "resolve_schedule_target", side_effect=delayed):
            task = asyncio.create_task(self.engine.run_schedules())
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.clock.set_struct(datetime(2026, 9, 10, 12, 1, 10))
        rows = await self.engine.run_schedules()
        self.assertEqual([message for row in rows for message in row.messages], ["appointment"])

    async def test_old_cancelled_scan_cannot_consume_a_new_configuration_recheck(self):
        await self.empty_scan()
        self.change_due()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def delayed(*args):
            entered.set()
            await release.wait()
            return fixture.SESSION

        with patch.object(self.engine, "resolve_schedule_target", side_effect=delayed):
            task = asyncio.create_task(self.engine.run_schedules())
            await asyncio.wait_for(entered.wait(), 2)
            self.clock.set_struct(datetime(2026, 9, 10, 12, 1, 10))
            self.add_schedule(id="changed", time="12:01",
                              action_chain=[{"type": "say", "messages": ["new appointment"]}])
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        rows = await self.engine.run_schedules()
        self.assertEqual([message for row in rows for message in row.messages], ["new appointment"])

    async def test_disabling_old_life_drops_its_key_without_rechecking_successful_life(self):
        self.store.add_session(fixture.OTHER_SESSION, session_type="group", platform="aiocqhttp")
        self.engine.reload_config()
        await self.empty_scan()
        self.change_due()
        rows = await self.busy_scan()
        self.assertEqual([row.session_id for row in rows], [fixture.OTHER_SESSION])
        raw = self.store.raw_sessions()
        next(row for row in raw["sessions"] if row["session_id"] == fixture.SESSION)["enabled"] = False
        self.store.save_sessions(raw)
        self.engine.reload_config()
        self.assertFalse(self.engine._schedule_reset_pending)
        self.assertEqual(await self.engine.run_schedules(), [])

    async def test_shared_owner_survives_disabled_representative_without_old_key(self):
        self.add_group()
        await self.empty_scan()
        self.change_due()
        self.assertEqual(await self.busy_scan(), [])
        raw = self.store.raw_sessions()
        next(row for row in raw["sessions"] if row["session_id"] == fixture.SESSION)["enabled"] = False
        self.store.save_sessions(raw)
        self.engine.reload_config()
        self.assertNotIn(fixture.SESSION, self.engine._schedule_reset_cursors)
        self.assertEqual(self.engine.tick_session_ids(), [fixture.OTHER_SESSION])
        self.clock.set_struct(datetime(2026, 9, 10, 12, 1, 10))
        rows = await self.engine.run_schedules()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].messages, ["appointment"])

    async def test_new_state_owner_does_not_inherit_an_old_lifes_wait_window(self):
        self.add_group()
        await self.empty_scan()
        self.change_due()
        self.assertEqual(await self.busy_scan(), [])
        self.clock.set_struct(datetime(2026, 9, 10, 12, 3, 10))
        raw = self.store.raw_sessions()
        next(row for row in raw["sessions"] if row["session_id"] == fixture.SESSION)["enabled"] = False
        self.store.save_sessions(raw)
        self.engine.reload_config()
        rows = await self.engine.run_schedules()
        self.assertEqual(rows, [])
        self.assertFalse(self.engine._schedule_reset_pending)

    async def test_existing_save_uses_its_original_cursor_without_new_persistent_fields(self):
        await self.empty_scan(datetime(2026, 9, 10, 11, 59))
        self.change_due(datetime(2026, 9, 10, 11, 59))
        await self.engine.run_schedules()
        before = (await self.get_state()).to_payload()
        self.assertNotIn("_schedule_reset_cursors", before)
        self.clock.set_struct(datetime(2026, 9, 10, 12, 1))
        rows = await self.engine.run_schedules()
        after = (await self.get_state()).to_payload()
        self.assertEqual([message for row in rows for message in row.messages], ["appointment"])
        self.assertEqual(before.keys(), after.keys())
        self.assertNotIn("_schedule_reset_cursors", after)

    async def test_busy_recheck_survives_a_complete_engine_rebuild(self):
        await self.empty_scan()
        self.change_due()
        self.assertEqual(await self.busy_scan(), [])
        self.clock.set_struct(datetime(2026, 9, 10, 12, 1, 10))
        await self.rebuild_engine()
        rows = await self.engine.run_schedules()
        self.assertEqual([message for row in rows for message in row.messages], ["appointment"])
        self.assertFalse(self.engine._schedule_reset_pending)

    async def test_completed_recheck_does_not_route_again_after_engine_rebuild(self):
        await self.empty_scan()
        self.change_due()
        rows = await self.engine.run_schedules()
        self.assertEqual(len(rows), 1)
        await self.engine._deliver(rows[0])
        await self.rebuild_engine()
        route = AsyncMock(wraps=self.engine.resolve_schedule_target)
        with patch.object(self.engine, "resolve_schedule_target", route):
            self.assertEqual(await self.engine.run_schedules(), [])
        self.assertEqual(route.await_count, 0)
        self.assertEqual(self.messenger.flat_messages.count("appointment"), 1)

    async def test_engine_rebuild_recovers_only_the_busy_life(self):
        self.store.add_session(fixture.OTHER_SESSION, session_type="group", platform="aiocqhttp")
        self.engine.reload_config()
        await self.empty_scan()
        self.change_due()
        first = await self.busy_scan()
        self.assertEqual([row.session_id for row in first], [fixture.OTHER_SESSION])
        self.clock.set_struct(datetime(2026, 9, 10, 12, 1, 10))
        await self.rebuild_engine()
        route = AsyncMock(wraps=self.engine.resolve_schedule_target)
        with patch.object(self.engine, "resolve_schedule_target", route):
            second = await self.engine.run_schedules()
        self.assertEqual([row.session_id for row in second], [fixture.SESSION])
        self.assertEqual(route.await_count, 1)
        self.assertEqual(len(self.llm.calls), 0)

    async def test_shared_midnight_oneoff_is_kept_for_busy_life_across_rebuild(self):
        self.store.add_session(fixture.OTHER_SESSION, session_type="group", platform="aiocqhttp")
        self.engine.reload_config()
        await self.empty_scan(datetime(2026, 9, 11, 0, 0))
        self.clock.set_struct(datetime(2026, 9, 11, 0, 0, 10))
        self.add_schedule(id="changed", time="23:59", once=True, date="2026-09-10",
                          action_chain=[{"type": "say", "messages": ["midnight appointment"]}])
        first = await self.busy_scan()
        self.assertEqual([row.session_id for row in first], [fixture.OTHER_SESSION])
        self.assertEqual([row["id"] for row in self.store.raw_schedules()["schedules"]], ["changed"])
        self.clock.set_struct(datetime(2026, 9, 11, 0, 1, 10))
        await self.rebuild_engine()
        second = await self.engine.run_schedules()
        self.assertEqual([row.session_id for row in second], [fixture.SESSION])
        self.assertEqual(self.store.raw_schedules()["schedules"], [])

    async def test_cleanup_failure_preserves_sent_work_and_never_pays_to_route_again(self):
        await self.empty_scan()
        self.change_due()
        with patch.object(self.engine, "_consume_schedule_recheck", side_effect=RuntimeError("temporary KV failure")):
            rows = await self.engine.run_schedules()
        self.assertEqual(len(rows), 1)
        await self.engine._deliver(rows[0])
        await self.rebuild_engine()
        route = AsyncMock(wraps=self.engine.resolve_schedule_target)
        with patch.object(self.engine, "resolve_schedule_target", route):
            self.assertEqual(await self.engine.run_schedules(), [])
        self.assertEqual(route.await_count, 0)
        self.assertFalse(self.engine._schedule_reset_pending)
        self.assertEqual(self.messenger.flat_messages.count("appointment"), 1)

    async def test_old_cleanup_cannot_erase_a_new_revision_after_cursor_commit(self):
        await self.empty_scan()
        self.change_due()
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.db.run

        async def delayed(func, *args, **kwargs):
            if getattr(func, "__name__", "") == "_consume_schedule_recheck":
                entered.set()
                await release.wait()
            return await original(func, *args, **kwargs)

        with patch.object(self.db, "run", side_effect=delayed):
            first = asyncio.create_task(self.engine.run_schedules())
            await asyncio.wait_for(entered.wait(), 2)
            self.clock.set_struct(datetime(2026, 9, 10, 12, 1, 10))
            self.add_schedule(id="changed", time="12:01",
                              action_chain=[{"type": "say", "messages": ["new appointment"]}])
            release.set()
            completed = await first
        self.assertEqual([message for row in completed for message in row.messages], ["appointment"])
        await self.rebuild_engine()
        rows = await self.engine.run_schedules()
        self.assertEqual([message for row in rows for message in row.messages], ["new appointment"])

    async def test_expired_busy_recheck_is_not_replayed_after_engine_rebuild(self):
        await self.empty_scan()
        self.change_due()
        self.assertEqual(await self.busy_scan(), [])
        self.clock.set_struct(datetime(2026, 9, 10, 12, 20))
        await self.rebuild_engine()
        self.assertFalse(self.engine._schedule_reset_pending)
        self.assertEqual(await self.engine.run_schedules(), [])

    async def test_legacy_save_without_recheck_record_keeps_the_original_completed_cursor(self):
        await self.empty_scan()
        self.change_due()
        self.assertEqual(len(await self.engine.run_schedules()), 1)
        await self.db.call("kv_delete", self.engine.SCHEDULE_RECHECK_KEY)
        before = (await self.get_state()).to_payload()
        await self.rebuild_engine()
        self.assertFalse(self.engine._schedule_reset_pending)
        route = AsyncMock(wraps=self.engine.resolve_schedule_target)
        with patch.object(self.engine, "resolve_schedule_target", route):
            self.assertEqual(await self.engine.run_schedules(), [])
        self.assertEqual(route.await_count, 0)
        after = (await self.get_state()).to_payload()
        self.assertEqual(before.keys(), after.keys())
        self.assertEqual(before["schedule_cursor"], after["schedule_cursor"])
