"""SQLite work must finish before a cancelled state lease admits its successor."""

import asyncio
import threading
import unittest

from tests import test_engine as fixture


class DatabaseCancellationTests(unittest.IsolatedAsyncioTestCase):
    node_id = 'study'
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    async def assert_cancelled_save_keeps_lease(self, *, repeat=False):
        async with self.engine.session_state(fixture.SESSION) as state:
            state.energy = .5

        entered, successor_entered = asyncio.Event(), asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        original = self.db.raw.save_state
        first_write = True

        def delayed_save(*args, **kwargs):
            nonlocal first_write
            if first_write:
                first_write = False
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(3):
                    raise AssertionError('SQLite worker was not released')
            return original(*args, **kwargs)

        self.db.raw.save_state = delayed_save

        async def update_old():
            async with self.engine.session_state(fixture.SESSION) as state:
                state.energy = .6

        async def update_new():
            async with self.engine.session_state(fixture.SESSION) as state:
                successor_entered.set()
                state.energy = .8

        old = asyncio.create_task(update_old())
        new = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            old.cancel()
            # Wait until cancellation has actually been handled, rather than
            # depending on thread scheduling or sleep duration.
            for _ in range(10):
                await asyncio.sleep(0)
            if repeat:
                old.cancel()
                for _ in range(10):
                    await asyncio.sleep(0)
            new = asyncio.create_task(update_new())
            for _ in range(10):
                await asyncio.sleep(0)
            self.assertFalse(old.done(), 'cancellation abandoned an in-flight save')
            self.assertFalse(successor_entered.is_set(), 'next state lease entered before the save finished')
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await old
            await asyncio.wait_for(new, 2)
            state = await self.engine.load_state(fixture.SESSION)
            self.assertAlmostEqual(state.energy, .8)
            self.assertFalse(self.engine.lock(fixture.SESSION).locked())
        finally:
            release.set()
            await asyncio.gather(old, *([new] if new else []), return_exceptions=True)
            self.db.raw.save_state = original

    async def test_cancelled_state_save_finishes_before_releasing_session_lock(self):
        await self.assert_cancelled_save_keeps_lease()

    async def test_repeated_cancellation_cannot_abandon_state_save(self):
        await self.assert_cancelled_save_keeps_lease(repeat=True)

    async def test_cancelled_run_finishes_its_atomic_write(self):
        entered, finished = asyncio.Event(), asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()

        def transaction():
            with self.db.raw.atomic():
                self.db.raw.kv_set('before', 1)
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(3):
                    raise AssertionError('SQLite transaction was not released')
                self.db.raw.kv_set('after', 2)
            loop.call_soon_threadsafe(finished.set)

        task = asyncio.create_task(self.db.run(transaction))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            for _ in range(10):
                await asyncio.sleep(0)
            self.assertFalse(task.done(), 'run returned while its transaction was still open')
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(finished.is_set())
            self.assertEqual(await self.db.call('kv_get', 'after'), 2)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

