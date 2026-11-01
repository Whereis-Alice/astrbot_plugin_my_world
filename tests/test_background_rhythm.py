"""Daily pacing survives reloads and keeps low allowances available later."""
import asyncio
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock

from core.background_budget import capture_background_admission
from core.background_rhythm import background_admission, background_window
from core.model_calls import model_context
from tests import test_engine as fixture
from tests import test_integration_integrity as integration


class BackgroundRhythmTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.EngineTestCase.asyncSetUp
    node_id = "study"

    def at(self, hour, minute=0, day=1):
        local = datetime(2026, 11, day, hour, minute, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.clock._now = local.timestamp()
        self.clock.set_struct(local)

    async def generate(self, purpose="plan"):
        with model_context(purpose, "pacing regression", fixture.SESSION):
            return await self.engine._budgeted_generate(self.llm, session_id=fixture.SESSION,
                system_prompt="world", prompt="life", max_tokens=100)

    async def test_early_burst_keeps_afternoon_room_and_reload_preserves_usage(self):
        self.at(0)
        raw = self.store.raw_world()
        raw["limits"]["background_daily_calls"] = 120
        self.store.save_world(raw)
        self.engine.reload_config()
        for _ in range(5):
            self.assertTrue((await self.generate()).ok)
        denied = await self.generate()
        self.assertFalse(denied.ok)
        self.assertIn("全天节奏", denied.error)
        self.engine.reload_config()
        self.assertFalse((await self.generate()).ok)
        self.assertEqual(len(self.llm.calls), 5)
        self.at(14)
        self.assertTrue((await self.generate()).ok)
        self.assertEqual((await self.engine._background_budget_status())["calls"], 6)

    async def test_concurrent_reservations_share_the_current_allowance(self):
        self.at(0)
        self.engine.world.limits.background_daily_calls = 48
        self.engine.world.limits.background_concurrency = 12
        answers = await asyncio.gather(*(self.generate() for _ in range(8)))
        self.assertEqual(sum(reply.ok for reply in answers), 2)
        self.assertEqual(len(self.llm.calls), 2)
        self.assertEqual(self.engine._background_active, 0)

    async def test_interactive_and_visit_calls_bypass_pacing_but_visits_keep_hard_limit(self):
        self.at(0)
        self.engine.world.limits.background_daily_calls = 24
        self.assertTrue((await self.generate()).ok)
        self.assertFalse((await self.generate()).ok)
        self.assertTrue((await self.generate("reflection")).ok)
        self.assertTrue((await self.generate("world_visit")).ok)
        day = self.engine._today_key(self.clock.now())
        for _ in range(22):
            await self.db.call("reserve_background", day, calls=24, tokens=0, reserve=1)
        self.assertTrue((await self.generate("world_visit")).ok)
        visit_budget = await self.engine._background_budget_status(purpose="world_visit", session_id=fixture.SESSION)
        for _ in range(22):
            await self.db.call("reserve_background", visit_budget["ledger"], calls=24, tokens=0, reserve=1)
        self.assertFalse((await self.generate("world_visit")).ok)
        self.assertTrue((await self.generate("chat")).ok)

    async def test_disabled_pacing_and_unlimited_budget_keep_existing_semantics(self):
        self.at(0)
        self.engine.world.limits.background_daily_calls = 24
        self.engine.world.limits.background_pacing = False
        for _ in range(3):
            self.assertTrue((await self.generate()).ok)
        self.engine.world.limits.background_pacing = True
        self.engine.world.limits.background_daily_calls = 0
        self.assertTrue((await self.generate()).ok)

    async def test_unknown_token_holds_follow_pacing_without_becoming_measured_usage(self):
        self.at(0)
        self.engine.world.limits.background_daily_tokens = 2400
        with capture_background_admission() as attempt:
            reply = await self.generate()
        self.assertFalse(reply.ok)  # Input + 100 output tokens exceed midnight's 100.
        self.assertTrue(attempt.deferred)
        self.assertFalse(attempt.started)
        self.assertGreater(attempt.next_at, self.clock.now())
        self.assertEqual((await self.engine._background_budget_status())["calls"], 0)
        self.at(1)
        self.assertTrue((await self.generate()).ok)
        status = await self.engine._background_budget_status()
        self.assertEqual(status["unknown"], 1)
        self.assertEqual(status["tokens"], 0)
        self.assertGreater(status["reserved"], 0)

    async def test_reservation_crossing_midnight_never_calls_model_or_spends_either_day(self):
        from unittest.mock import patch
        self.at(23, 59)
        previous_day = self.engine._today_key(self.clock.now())
        entered, release = asyncio.Event(), asyncio.Event()
        original_call = self.db.call

        async def held(method, *args, **kwargs):
            if method == "reserve_background":
                entered.set()
                await release.wait()
            return await original_call(method, *args, **kwargs)

        with patch.object(self.db, "call", side_effect=held):
            task = asyncio.create_task(self.generate())
            try:
                await asyncio.wait_for(entered.wait(), 2)
                self.at(0, 1, day=2)
                release.set()
                self.assertFalse((await task).ok)
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.llm.calls, [])
        self.assertEqual((await self.db.call("background_status", previous_day))["calls"], 0)
        self.assertEqual((await self.engine._background_budget_status())["calls"], 0)
        self.assertFalse(self.engine._background_failures)

    async def test_snapshot_distinguishes_pacing_from_daily_exhaustion(self):
        self.at(0)
        self.engine.world.limits.background_daily_calls = 24
        async with self.engine.session_state(fixture.SESSION) as state:
            state.life_updated_at = self.clock.now()
        await self.generate()
        snapshot = await self.engine.thoughts_payload(fixture.SESSION)
        self.assertEqual(snapshot["health"]["autonomous_generation"], "budget_waiting")
        self.assertIn("后续时段", snapshot["health"]["message"])


class BackgroundWindowTests(unittest.TestCase):
    def test_unused_allowance_accumulates_and_local_midnight_resets_the_day(self):
        local = datetime(2026, 11, 2, 13, tzinfo=ZoneInfo("Asia/Shanghai"))
        window = background_window(local, calls=120, tokens=0, enabled=True)
        self.assertEqual(window["call_allowance"], 70)
        self.assertTrue(background_admission({"calls": 69}, calls=120, tokens=0, window=window)["allowed"])
        midnight = background_window(local.replace(hour=0), calls=120, tokens=0, enabled=True)
        self.assertEqual(midnight["call_allowance"], 5)

    def test_dst_day_uses_actual_midnight_interval(self):
        local = datetime(2026, 11, 1, 0, tzinfo=ZoneInfo("America/New_York"))
        window = background_window(local, calls=100, tokens=0, enabled=True)
        self.assertEqual(window["end"] - window["start"], 25 * 3600)
        self.assertEqual(window["call_allowance"], 4)

    def test_midnight_dst_fold_keeps_one_daily_window_and_monotonic_allowance(self):
        zone = ZoneInfo("America/Havana")
        before = datetime(2026, 11, 1, 0, 59, 59, tzinfo=zone)
        after = datetime.fromtimestamp(before.timestamp() + 1, zone)
        first = background_window(before, calls=100, tokens=0, enabled=True)
        second = background_window(after, calls=100, tokens=0, enabled=True)
        self.assertEqual(first["start"], second["start"])
        self.assertEqual(first["end"], second["end"])
        self.assertGreaterEqual(second["call_allowance"], first["call_allowance"])

    def test_fractional_release_time_actually_admits_the_next_call(self):
        zone = ZoneInfo("Asia/Shanghai")
        local = datetime(2026, 11, 1, 0, tzinfo=zone)
        for limit in (11, 13, 17, 19, 23):
            with self.subTest(limit=limit):
                window = background_window(local, calls=limit, tokens=0, enabled=True)
                denied = background_admission({"calls": 1}, calls=limit, tokens=0, window=window)
                released = datetime.fromtimestamp(denied["next_at"], zone)
                later = background_window(released, calls=limit, tokens=0, enabled=True)
                self.assertTrue(background_admission({"calls": 1}, calls=limit, tokens=0, window=later)["allowed"])


class ResolvedProviderTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = integration.IntegrationIntegrityTests.asyncSetUp
    response = integration.IntegrationIntegrityTests.response

    async def test_resolved_generation_does_not_resolve_again_or_leak_internal_option(self):
        channel = self.adapter_module.AstrBotLLM(self.plugin, role="helper")
        channel.resolve_provider_id = AsyncMock(side_effect=AssertionError("already admitted"))
        self.plugin.context.llm_generate = AsyncMock(return_value=self.response())
        reply = await channel.generate_resolved(resolved_provider_id="admitted-provider",
            session_id=self.session, system_prompt="persona", prompt="life", max_tokens=100)
        self.assertTrue(reply.ok)
        sent = self.plugin.context.llm_generate.call_args.kwargs
        self.assertEqual(sent["chat_provider_id"], "admitted-provider")
        self.assertNotIn("_resolved_provider_id", sent)
