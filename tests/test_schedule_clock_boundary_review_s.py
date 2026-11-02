"""Clock observation cannot cancel life work; state handoffs use a separate bounded wait."""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from tests import test_engine as fixture

S = fixture.SESSION


class ScheduleClockBoundaryReviewTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    async def held_clock(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def clock():
            async with self.engine.session_state(S):
                entered.set()
                await release.wait()

        task = asyncio.create_task(clock())
        self.engine._life_clock_jobs = {self.engine.state_key(S): task}
        await entered.wait()
        return task, release

    async def test_external_busy_without_clock_proceeds_to_bounded_lock_wait(self):
        async with self.engine.session_state(S):
            self.assertTrue(await asyncio.wait_for(self.engine._schedule_clock_ready(S), .5))

    async def test_completed_clock_proceeds_to_bounded_lock_wait(self):
        task = asyncio.create_task(asyncio.sleep(0))
        await task
        self.engine._life_clock_jobs = {self.engine.state_key(S): task}
        async with self.engine.session_state(S):
            self.assertTrue(await asyncio.wait_for(self.engine._schedule_clock_ready(S), .5))

    async def test_clock_timeout_observes_without_cancelling_it(self):
        task, release = await self.held_clock()
        try:
            pending = AsyncMock(return_value=(set(), {task}))
            with patch("asyncio.wait", pending):
                self.assertFalse(await self.engine._schedule_clock_ready(S))
            self.assertEqual(pending.await_args.kwargs["timeout"], 5.0)
            self.assertFalse(task.done())
            self.assertFalse(task.cancelled())
        finally:
            release.set()
            await task

    async def test_cancelled_schedule_wait_leaves_clock_running(self):
        task, release = await self.held_clock()
        try:
            waiter = asyncio.create_task(self.engine._schedule_clock_ready(S))
            await asyncio.sleep(0)
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            self.assertFalse(task.done())
            self.assertFalse(task.cancelled())
        finally:
            release.set()
            await task

    async def test_cancelled_clock_does_not_admit_a_schedule_scan(self):
        task, _release = await self.held_clock()
        waiter = asyncio.create_task(self.engine._schedule_clock_ready(S))
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertFalse(await waiter)

    async def test_failed_clock_preserves_schedule_for_a_later_scan(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def clock():
            async with self.engine.session_state(S):
                entered.set()
                await release.wait()
                raise ValueError("temporary clock failure")

        task = asyncio.create_task(clock())
        self.engine._life_clock_jobs = {self.engine.state_key(S): task}
        await entered.wait()
        waiter = asyncio.create_task(self.engine._schedule_clock_ready(S))
        await asyncio.sleep(0)
        release.set()
        self.assertFalse(await waiter)
        self.assertIsInstance(task.exception(), ValueError)
        self.assertFalse(self.engine.is_busy(S))

    async def test_lock_taken_after_clock_completion_proceeds_to_bounded_wait(self):
        task, release_clock = await self.held_clock()
        entered, release_external = asyncio.Event(), asyncio.Event()

        async def external():
            async with self.engine.session_state(S):
                entered.set()
                await release_external.wait()

        external_task = asyncio.create_task(external())
        await asyncio.sleep(0)
        waiter = asyncio.create_task(self.engine._schedule_clock_ready(S))
        await asyncio.sleep(0)
        try:
            release_clock.set()
            await entered.wait()
            self.assertTrue(await asyncio.wait_for(waiter, .5))
            self.assertTrue(self.engine.is_busy(S))
        finally:
            release_external.set()
            await asyncio.gather(task, external_task)

    async def test_reload_disabling_life_during_observation_cancels_old_scan(self):
        task, release = await self.held_clock()
        waiter = asyncio.create_task(self.engine._schedule_clock_ready(S))
        await asyncio.sleep(0)
        try:
            self.store.set_session_enabled(S, False)
            self.engine.reload_config()
            release.set()
            self.assertFalse(await waiter)
            self.assertFalse(self.engine.is_enabled(S))
        finally:
            release.set()
            await task

    async def test_reload_replacing_schedule_definitions_cancels_old_scan(self):
        task, release = await self.held_clock()
        waiter = asyncio.create_task(self.engine._schedule_clock_ready(S))
        await asyncio.sleep(0)
        try:
            self.engine.reload_config()
            release.set()
            self.assertFalse(await waiter)
        finally:
            release.set()
            await task
