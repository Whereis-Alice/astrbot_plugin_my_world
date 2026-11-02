"""Local request preparation retains planning opportunities without hiding real failures."""
from __future__ import annotations

import asyncio
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from tests.fake_astrbot import install

install()

from core.background_budget import BackgroundReservation, capture_background_admission  # noqa: E402
from core.engine import TickOutcome  # noqa: E402
from core.engine.life_inference import LifeWorkDiscarded  # noqa: E402
from core.model_calls import model_context  # noqa: E402
from core.speech_runtime import SPEECH_REQUEST  # noqa: E402
from tests import test_world_generator as fixture  # noqa: E402

SCHEDULE = '{"schedules":[{"id":"outing","time":"18:00","action_chain":[{"type":"think","content":"出门看看"}]}]}'
PLAN = '{"plan":[{"action":"walk_to","target_node":"window"}],"reason":"看看窗外"}'


class ModelPreparationAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixture.EngineGeneratorTests.asyncSetUp(self)
        self.addAsyncCleanup(self.engine.close_background)
        self.engine.clock._now = datetime(2026, 11, 3, 8, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        data = self.store.raw_schedules()
        data["auto"] = {"enabled": True, "hour": 0, "per_day": 1}
        self.store.save_schedules(data)
        self.engine.reload_config()
        self.engine.world.limits.background_pacing = False
        self.creator.default_reply = SCHEDULE

    async def attempt(self):
        return await self.db.call("kv_get", "schedule.auto_day:global:attempt")

    async def status(self):
        day = self.engine._today_key(self.engine._now())
        return await self.db.call("background_status", day)

    async def assert_unspent(self):
        self.assertFalse(self.creator.calls)
        self.assertEqual(dict(calls=0, tokens=0, unknown=0, reserved=0), await self.status())

    async def assert_auto_wait(self, error):
        with self.assertRaisesRegex(ValueError, error):
            await self.engine.maybe_auto_generate_schedules()
        attempt = await self.attempt()
        self.assertEqual(0, attempt["count"])
        self.assertGreaterEqual(attempt["retry_at"] - self.engine._now(), 60)
        self.assertLessEqual(attempt["retry_at"] - self.engine._now(), 900)
        await self.assert_unspent()

    async def test_missing_creator_keeps_today_available_after_model_is_configured(self):
        self.engine.creator_llm = None
        for _ in range(3):
            await self.assert_auto_wait("没有可用的内容生成模型")
            self.engine.clock.advance(901)
        self.engine.creator_llm = self.creator
        self.assertIn("1 条", await self.engine.maybe_auto_generate_schedules())
        self.assertEqual(1, len(self.creator.calls))
        self.assertEqual(self.engine._today_key(self.engine._now()),
                         await self.db.call("kv_get", "schedule.auto_day:global"))

    async def test_provider_lookup_errors_keep_today_available_after_routing_recovers(self):
        self.creator.resolve_provider_id = AsyncMock(side_effect=RuntimeError("provider selection unavailable"))
        for _ in range(3):
            await self.assert_auto_wait("provider selection unavailable")
            self.engine.clock.advance(901)
        self.assertFalse(getattr(self.engine, "_background_failures", {}))
        del self.creator.resolve_provider_id
        self.assertIn("1 条", await self.engine.maybe_auto_generate_schedules())
        self.assertEqual(1, len(self.creator.calls))

    async def test_provider_lookup_cancel_preserves_cancellation_and_the_unissued_attempt(self):
        entered = asyncio.Event()

        async def unresolved(session):
            entered.set()
            await asyncio.Event().wait()

        self.creator.resolve_provider_id = unresolved
        task = asyncio.create_task(self.engine.maybe_auto_generate_schedules())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(0, (await self.attempt())["count"])
        self.assertGreater((await self.attempt())["retry_at"], self.engine._now())
        await self.assert_unspent()

    async def test_provider_lookup_timeout_spends_no_model_or_planning_attempt(self):
        self.engine.world.limits.background_llm_timeout_seconds = .01

        async def unresolved(session):
            await asyncio.Event().wait()

        self.creator.resolve_provider_id = unresolved
        await self.assert_auto_wait("TimeoutError")

    async def test_invalid_local_provider_result_keeps_the_original_error(self):
        self.creator.resolve_provider_id = AsyncMock(return_value=None)
        await self.assert_auto_wait("Provider 解析结果无效")

    async def test_direct_provider_lookup_failure_reports_unissued_and_keeps_the_exception(self):
        self.creator.resolve_provider_id = AsyncMock(side_effect=RuntimeError("provider selection unavailable"))
        with model_context("plan", "自主规划", fixture.SESSION), capture_background_admission() as admission:
            with self.assertRaisesRegex(RuntimeError, "provider selection unavailable"):
                await self.engine._budgeted_generate(self.creator, session_id=fixture.SESSION,
                    system_prompt="world", prompt="life")
        self.assertTrue(admission.deferred)
        self.assertFalse(admission.started)
        await self.assert_unspent()

    async def test_local_provider_failure_does_not_use_hourly_autonomous_planning(self):
        self.engine.llm.resolve_provider_id = AsyncMock(side_effect=RuntimeError("provider selection unavailable"))
        async with self.engine.session_state(fixture.SESSION) as state:
            outcome = TickOutcome(fixture.SESSION)
            result = await self.engine._ask_llm_for_plan(state, self.engine.node(state.node_id),
                                                       outcome, force=True, quiet=True)
            self.assertIsNone(result)
            self.assertEqual((0, 0), (outcome.llm_calls, state.llm_plan_count_hour))
            self.assertFalse(self.engine.llm.calls)
            del self.engine.llm.resolve_provider_id
            self.engine.llm.replies = [PLAN]
            result = await self.engine._ask_llm_for_plan(state, self.engine.node(state.node_id),
                                                       outcome, force=True, quiet=True)
            self.assertIsNotNone(result)
            self.assertEqual((1, 1), (outcome.llm_calls, state.llm_plan_count_hour))

    async def test_initial_life_validation_discard_is_unissued_and_keeps_the_attempt(self):
        with patch("core.engine.background.validate_life_preparation",
                   side_effect=LifeWorkDiscarded("life changed before preparation")):
            self.assertEqual("", await self.engine.maybe_auto_generate_schedules())
        self.assertEqual(0, (await self.attempt())["count"])
        await self.assert_unspent()

    async def test_unissued_validation_discard_never_refunds_a_newer_attempt_record(self):
        day = self.engine._today_key(self.engine._now())
        newer = {"day": day, "count": 2, "retry_at": self.engine._now() + 1200, "token": "new-worker-x"}

        async def changed(engine):
            await self.db.call("kv_set", "schedule.auto_day:global:attempt", newer)
            raise LifeWorkDiscarded("another worker owns the new attempt")

        with patch("core.engine.background.validate_life_preparation", side_effect=changed):
            self.assertEqual("", await self.engine.maybe_auto_generate_schedules())
        self.assertEqual(newer, await self.attempt())
        await self.assert_unspent()

    async def test_final_local_validation_refunds_an_unissued_budget_and_attempt(self):
        checked = 0

        async def changed(engine):
            nonlocal checked
            checked += 1
            if checked == 3:
                raise LifeWorkDiscarded("life changed just before dispatch")

        with patch("core.engine.background.validate_life_preparation", side_effect=changed):
            self.assertEqual("", await self.engine.maybe_auto_generate_schedules())
        self.assertEqual(3, checked)
        self.assertEqual(0, (await self.attempt())["count"])
        await self.assert_unspent()

    async def test_invalid_options_and_tool_preparation_remain_local_errors(self):
        with model_context("plan", "自主规划", fixture.SESSION):
            with capture_background_admission() as admission:
                with self.assertRaises(ValueError):
                    await self.engine._budgeted_generate(self.creator, session_id=fixture.SESSION,
                        system_prompt="world", prompt="life", max_tokens="invalid")
            self.assertTrue(admission.deferred)
            self.assertFalse(admission.started)
            token = SPEECH_REQUEST.set({"state": SimpleNamespace(node_id="study"), "allow_tools": True})
            try:
                with capture_background_admission() as admission:
                    with patch.object(self.engine, "allowed_tool_names", side_effect=RuntimeError("tool context changed")):
                        with self.assertRaisesRegex(RuntimeError, "tool context changed"):
                            await self.engine._budgeted_generate(self.creator, session_id=fixture.SESSION,
                                system_prompt="world", prompt="life")
                self.assertTrue(admission.deferred)
                self.assertFalse(admission.started)
            finally:
                SPEECH_REQUEST.reset(token)
        await self.assert_unspent()

    async def test_real_generated_failures_retain_the_three_attempts_and_the_budget(self):
        self.creator.generate = AsyncMock(side_effect=RuntimeError("real dispatched provider failure"))
        counts = []
        for index in range(3):
            with self.assertRaisesRegex(ValueError, "real dispatched provider failure"):
                await self.engine.maybe_auto_generate_schedules()
            counts.append((await self.attempt())["count"])
            self.engine.clock.advance(900 * 3 ** index + 1)
        self.assertEqual([1, 2, 3], counts)
        self.assertEqual(3, self.creator.generate.await_count)
        self.assertEqual(3, (await self.status())["calls"])
        self.assertEqual("", await self.engine.maybe_auto_generate_schedules())
        self.assertEqual(3, self.creator.generate.await_count)

    async def test_interactive_real_generation_error_keeps_its_original_behavior(self):
        self.creator.generate = AsyncMock(side_effect=RuntimeError("interactive provider failure"))
        with model_context("reply", "用户回复", fixture.SESSION), capture_background_admission() as admission:
            with self.assertRaisesRegex(RuntimeError, "interactive provider failure"):
                await self.engine._budgeted_generate(self.creator, session_id=fixture.SESSION,
                    system_prompt="world", prompt="reply")
        self.assertFalse(admission.deferred)
        self.assertEqual(1, self.creator.generate.await_count)
        self.assertEqual(dict(calls=0, tokens=0, unknown=0, reserved=0), await self.status())

    async def test_missing_main_model_returns_the_same_error_as_an_unissued_request(self):
        main = self.engine.llm
        self.engine.llm = None
        with capture_background_admission() as admission:
            self.assertIsNone(await self.engine._ask_llm(fixture.SESSION, "world", "life"))
        self.assertTrue(admission.deferred)
        self.assertFalse(admission.started)
        self.assertEqual("没有可用的模型", self.engine._last_llm[fixture.SESSION]["error"])
        self.assertFalse(main.calls)

    async def test_manual_visit_local_guard_failure_does_not_hide_its_error(self):
        def changed():
            raise RuntimeError("manual visit proof changed")

        with model_context("world_visit", "用户邀请串门", fixture.SESSION), capture_background_admission() as admission:
            with self.assertRaisesRegex(RuntimeError, "manual visit proof changed"):
                await self.engine._budgeted_generate(self.creator, session_id=fixture.SESSION,
                    system_prompt="world", prompt="visit", _visit_manual=True, _request_guard=changed)
        self.assertTrue(admission.deferred)
        self.assertFalse(admission.started)
        await self.assert_unspent()

    async def test_reservation_local_error_keeps_its_exception_and_never_dispatches(self):
        error = RuntimeError("local budget storage unavailable")
        local_db = SimpleNamespace(call=AsyncMock(side_effect=error))
        with capture_background_admission() as admission:
            with self.assertRaises(RuntimeError) as caught:
                async with BackgroundReservation(local_db, "local", calls=10, tokens=0, reserve=2):
                    self.fail("A failed reservation cannot enter the model body")
        self.assertIs(error, caught.exception)
        self.assertTrue(admission.deferred)
        self.assertFalse(admission.started)
        local_db.call.assert_awaited_once()
        await self.assert_unspent()

    async def test_reservation_failure_keeps_today_and_does_not_back_off_the_provider(self):
        original = self.db.call

        async def fail_reservation(method, *args, **kwargs):
            if method == "reserve_background":
                raise RuntimeError("local budget storage unavailable")
            return await original(method, *args, **kwargs)

        with patch.object(self.db, "call", side_effect=fail_reservation):
            for _ in range(3):
                await self.assert_auto_wait("local budget storage unavailable")
                self.assertEqual(0, self.engine._background_active)
                self.assertFalse(self.engine._background_failures)
                self.engine.clock.advance(901)
        self.assertIn("1 条", await self.engine.maybe_auto_generate_schedules())
        self.assertEqual(1, len(self.creator.calls))
        self.assertEqual(1, (await self.status())["calls"])
