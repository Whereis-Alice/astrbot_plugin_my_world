"""Unsent automatic schedule requests retain their provider retry allowance."""
from __future__ import annotations

import unittest
import asyncio
from datetime import datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from tests import fake_astrbot

fake_astrbot.install()

from tests import test_world_generator as fixture  # noqa: E402
from core.background_budget import record_background_admission  # noqa: E402


class AutoScheduleAdmissionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.EngineGeneratorTests.asyncSetUp

    async def prepare(self):
        self.engine.clock._now = datetime(2026, 11, 3, 8, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        raw = self.store.raw_schedules()
        raw["auto"] = {"enabled": True, "hour": 0, "per_day": 1}
        self.store.save_schedules(raw)
        self.engine.reload_config()
        self.engine.world.limits.background_pacing = False
        self.creator.default_reply = '{"schedules":[{"id":"outing","time":"18:00","action_chain":[{"type":"think","content":"出门看看"}]}]}'
        return self.engine._today_key(self.engine._now())

    async def attempt(self):
        return await self.db.call("kv_get", "schedule.auto_day:global:attempt")

    async def test_budget_waits_survive_reload_without_using_the_daily_retry_count(self):
        day = await self.prepare()
        self.engine.world.limits.background_daily_calls = 1
        await self.db.call("reserve_background", day, calls=1, tokens=0, reserve=0)
        observed = []
        for index in range(3):
            with self.assertRaises(ValueError):
                await self.engine.maybe_auto_generate_schedules()
            observed.append((await self.attempt())["count"])
            self.engine.clock.advance(900 * 3 ** index + 1)
            self.engine.reload_config()
            self.engine.world.limits.background_daily_calls = 1
            self.engine.world.limits.background_pacing = False
        self.assertFalse(self.creator.calls)
        self.assertEqual([0, 0, 0], observed)
        self.engine.world.limits.background_daily_calls = 100
        self.assertIn("1 条", await self.engine.maybe_auto_generate_schedules())
        self.assertEqual(1, len(self.creator.calls))
        self.assertEqual(day, await self.db.call("kv_get", "schedule.auto_day:global"))

    async def test_busy_channel_does_not_use_a_provider_retry(self):
        await self.prepare()
        self.engine.world.limits.background_concurrency = 1
        self.engine._background_active = 1
        with self.assertRaises(ValueError):
            await self.engine.maybe_auto_generate_schedules()
        self.assertFalse(self.creator.calls)
        self.assertEqual(0, (await self.attempt())["count"])
        self.assertGreater((await self.attempt())["retry_at"], self.engine._now())
        self.engine._background_active = 0
        self.engine.clock.advance(901)
        self.assertIn("1 条", await self.engine.maybe_auto_generate_schedules())

    async def test_backoff_keeps_the_original_real_failure_count(self):
        await self.prepare()
        self.creator.generate = AsyncMock(side_effect=RuntimeError("provider offline"))
        with self.assertRaises(ValueError):
            await self.engine.maybe_auto_generate_schedules()
        self.assertEqual(1, (await self.attempt())["count"])
        self.engine.clock.advance(901)
        for failure in self.engine._background_failures.values():
            failure["until"] = self.engine._now() + 3600
        with self.assertRaises(ValueError):
            await self.engine.maybe_auto_generate_schedules()
        self.creator.generate.assert_awaited_once()
        self.assertEqual(1, (await self.attempt())["count"])

    async def test_late_deferral_does_not_overwrite_a_newer_jobs_attempt(self):
        day = await self.prepare()
        newer = {"day": day, "count": 2, "retry_at": self.engine._now() + 1200, "token": "new-worker"}

        async def deferred(*args, **kwargs):
            record_background_admission(deferred=True, next_at=self.engine._now() + 60)
            await self.db.call("kv_set", "schedule.auto_day:global:attempt", newer)
            raise ValueError("waiting")

        self.engine.generate_schedules = deferred
        with self.assertRaises(ValueError):
            await self.engine.maybe_auto_generate_schedules()
        self.assertEqual(newer, await self.attempt())
        self.assertFalse(self.creator.calls)

    async def test_cancelled_confirmed_deferral_preserves_the_unspent_attempt(self):
        await self.prepare()
        entered = asyncio.Event()

        async def deferred(*args, **kwargs):
            record_background_admission(deferred=True, next_at=self.engine._now() + 60)
            entered.set()
            await asyncio.Event().wait()

        self.engine.generate_schedules = deferred
        task = asyncio.create_task(self.engine.maybe_auto_generate_schedules())
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        self.assertEqual(0, (await self.attempt())["count"])
        self.assertGreater((await self.attempt())["retry_at"], self.engine._now())
        self.assertFalse(self.creator.calls)

    async def test_cancellation_during_real_reservation_refunds_the_attempt_and_budget(self):
        day = await self.prepare()
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.db.call

        async def delayed_call(method, *args, **kwargs):
            if method == "reserve_background":
                entered.set()
                await release.wait()
            return await original(method, *args, **kwargs)

        self.db.call = delayed_call
        task = asyncio.create_task(self.engine.maybe_auto_generate_schedules())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self.db.call = original
        self.assertEqual(0, (await self.attempt())["count"])
        status = await self.db.call("background_status", day)
        self.assertEqual(0, status["calls"])
        self.assertEqual(0, status["reserved"])
        self.assertFalse(self.creator.calls)
