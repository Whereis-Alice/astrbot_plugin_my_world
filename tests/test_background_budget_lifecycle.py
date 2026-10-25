"""Real SQLite cancellation boundaries distinguish unsent work from unknown usage."""

import asyncio
import threading
import unittest
from unittest.mock import AsyncMock, patch

from core.engine.life_inference import LifeWorkDiscarded
from core.llm_usage import record_llm_usage
from core.ports import LLMReply
from tests import test_engine as fixture


class BackgroundBudgetLifecycleTests(unittest.IsolatedAsyncioTestCase):
    node_id = 'study'
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    async def generate(self):
        return await self.engine._budgeted_generate(self.llm, session_id=fixture.SESSION,
            system_prompt='world', prompt='life', max_tokens=100)

    async def status(self):
        return await self.db.call('background_status', self.engine._today_key(self.clock.now()))

    async def thread_gate(self, method):
        """Pause a real worker before it writes; cancellation cannot stop this thread."""
        entered, release, finished = asyncio.Event(), threading.Event(), asyncio.Event()
        original = getattr(self.db.raw, method)
        loop = asyncio.get_running_loop()

        def held(*args, **kwargs):
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(3):
                raise AssertionError('test worker was not released')
            try:
                return original(*args, **kwargs)
            finally:
                loop.call_soon_threadsafe(finished.set)

        setattr(self.db.raw, method, held)
        self.addCleanup(setattr, self.db.raw, method, original)
        self.addCleanup(release.set)
        task = asyncio.create_task(self.generate())
        self.addAsyncCleanup(self.stop_task, task, release)
        await asyncio.wait_for(entered.wait(), 2)
        return task, release, finished

    async def stop_task(self, task, release):
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_cancel_during_reservation_refunds_without_calling_model(self):
        task, release, finished = await self.thread_gate('reserve_background')
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(finished.wait(), 2)
        self.assertEqual(await self.status(), dict(calls=0, tokens=0, unknown=0, reserved=0))
        self.assertFalse(self.llm.calls)
        self.assertEqual(self.engine._background_active, 0)

    async def test_repeated_cancellation_waits_for_reservation_cleanup(self):
        task, release, finished = await self.thread_gate('reserve_background')
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(finished.wait(), 2)
        self.assertEqual(await self.status(), dict(calls=0, tokens=0, unknown=0, reserved=0))
        self.assertFalse(self.llm.calls)

    async def test_discard_before_model_entry_refunds_the_reservation(self):
        class Unsent:
            async def __aenter__(self):
                raise LifeWorkDiscarded()

            async def __aexit__(self, *args):
                pass

        with patch('core.engine.background.released_life_lock', return_value=Unsent()):
            with self.assertRaises(LifeWorkDiscarded):
                await self.generate()
        self.assertFalse(self.llm.calls)
        self.assertEqual(await self.status(), dict(calls=0, tokens=0, unknown=0, reserved=0))

    async def test_real_life_decision_cancelled_before_releasing_lock_refunds_budget(self):
        self.engine.world.proactive_chat_paused = True
        self.engine.decider.should_ask_llm = lambda _state: True
        async with self.engine.session_state(fixture.SESSION) as state:
            state.state, state.energy = 'idle', .9
        entered, release = asyncio.Event(), asyncio.Event()
        original_save = self.engine.save_state

        async def save(state):
            if not entered.is_set() and (await self.status())['calls']:
                entered.set()
                await release.wait()
            await original_save(state)

        self.engine.save_state = save
        task = asyncio.create_task(self.engine.maybe_decide(fixture.SESSION, force=True))
        try:
            await asyncio.wait_for(entered.wait(), 2)
        finally:
            task.cancel()
            release.set()
            await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(task.cancelled())
        self.assertFalse(self.llm.calls)
        self.assertEqual(await self.status(), dict(calls=0, tokens=0, unknown=0, reserved=0))
        self.assertFalse(self.engine.lock(fixture.SESSION).locked())
        self.assertEqual(self.engine._background_active, 0)

    async def test_cancel_after_model_entry_keeps_unknown_usage(self):
        entered = asyncio.Event()

        async def pending(**kwargs):
            entered.set()
            await asyncio.Event().wait()

        self.llm.generate = AsyncMock(side_effect=pending)
        task = asyncio.create_task(self.generate())
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        status = await self.status()
        self.assertEqual((status['calls'], status['tokens'], status['unknown']), (1, 0, 1))
        self.assertGreater(status['reserved'], 0)
        self.assertEqual(self.engine._background_active, 0)

    async def test_cancellation_during_settlement_finishes_measured_write(self):
        async def measured(**kwargs):
            record_llm_usage(dict(prompt_tokens=7, completion_tokens=3))
            return LLMReply(text='ok')

        self.llm.generate = AsyncMock(side_effect=measured)
        task, release, finished = await self.thread_gate('settle_background')
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(finished.wait(), 2)
        self.assertEqual(await self.status(), dict(calls=1, tokens=10, unknown=0, reserved=0))
        self.llm.generate.assert_awaited_once()

    async def test_budget_denial_never_calls_the_model_or_refunds_other_work(self):
        day = self.engine._today_key(self.clock.now())
        self.engine.world.limits.background_daily_calls = 1
        await self.db.call('reserve_background', day, calls=1, tokens=0, reserve=50)
        before = await self.status()
        reply = await self.generate()
        self.assertFalse(reply.ok)
        self.assertFalse(self.llm.calls)
        self.assertEqual(await self.status(), before)

    async def test_refund_does_not_touch_another_inflight_reservation(self):
        day = self.engine._today_key(self.clock.now())
        await self.db.call('reserve_background', day, calls=0, tokens=0, reserve=50)
        task, release, finished = await self.thread_gate('reserve_background')
        task.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(finished.wait(), 2)
        self.assertEqual(await self.status(), dict(calls=1, tokens=0, unknown=0, reserved=50))

    async def test_model_failure_keeps_conservative_hold_and_backoff(self):
        self.llm.generate = AsyncMock(side_effect=RuntimeError('provider failed'))
        with self.assertRaisesRegex(RuntimeError, 'provider failed'):
            await self.generate()
        status = await self.status()
        self.assertEqual((status['calls'], status['unknown']), (1, 1))
        self.assertGreater(status['reserved'], 0)
        self.assertFalse((await self.generate()).ok)
        self.assertEqual(await self.status(), status)
        self.llm.generate.assert_awaited_once()
