"""Model admission failures explain the ending without exposing provider bodies."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import unittest

from core.ports import LLMReply
from core.visits.decisions import VisitDecisionError
from tests import test_engine as engine_fixture
from tests import test_visits_actor as actor_fixture
from tests import test_visits_protocol as protocol_fixture


class VisitDecisionAdapterTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = actor_fixture.VisitActorTests.asyncSetUp

    async def test_exhausted_budget_has_a_distinct_reason_and_no_model_call(self):
        self.engine.visits.config.daily_calls = 1
        await self.port.decide(engine_fixture.SESSION, "propose", self.visit)
        with self.assertRaises(VisitDecisionError) as error:
            await self.port.decide(engine_fixture.SESSION, "propose", self.visit)
        self.assertEqual(error.exception.category, "budget")
        self.assertIn("预算已用完", str(error.exception))
        self.assertEqual(len(self.llm.calls), 1)

    async def test_busy_and_backoff_reasons_do_not_call_the_model(self):
        for message, category in (
            ("后台通道忙，留待下一次", "busy"),
            ("后台模型暂时退避，规则继续运行；上次失败：private provider body", "backoff"),
        ):
            with self.subTest(category=category):
                async def unavailable(*args, **kwargs):
                    return LLMReply(ok=False, error=message)
                self.engine._budgeted_generate = unavailable
                with self.assertRaises(VisitDecisionError) as error:
                    await self.port.decide(engine_fixture.SESSION, "propose", self.visit)
                self.assertEqual(error.exception.category, category)
                self.assertNotIn("private", str(error.exception))
        self.assertFalse(self.llm.calls)

    async def test_timeout_and_provider_exception_are_safe_distinct_failures(self):
        for exception, category in ((TimeoutError("private host"), "timeout"),
                                    (RuntimeError("private provider body"), "provider")):
            with self.subTest(category=category):
                async def unavailable(*args, **kwargs):
                    raise exception
                self.engine._budgeted_generate = unavailable
                with self.assertRaises(VisitDecisionError) as error:
                    await self.port.decide(engine_fixture.SESSION, "propose", self.visit)
                self.assertEqual(error.exception.category, category)
                self.assertNotIn("private", str(error.exception))

    async def test_cancellation_still_propagates(self):
        async def cancelled(*args, **kwargs):
            raise asyncio.CancelledError()
        self.engine._budgeted_generate = cancelled
        with self.assertRaises(asyncio.CancelledError):
            await self.port.decide(engine_fixture.SESSION, "propose", self.visit)

    async def test_invalid_output_has_a_format_reason_without_response_text(self):
        self.llm.default_reply = "private invalid output"
        with self.assertRaises(VisitDecisionError) as error:
            await self.port.decide(engine_fixture.SESSION, "propose", self.visit)
        self.assertEqual(error.exception.category, "format")
        self.assertNotIn("private", str(error.exception))


class VisitDecisionEndingTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = protocol_fixture.VisitProtocolTests.asyncSetUp

    async def finish_waiting(self, pending):
        self.assertEqual(pending["status"], "inviting")
        for _ in range(3):
            current = self.a._state["visits"][pending["id"]]
            if current["status"] == "interrupted":
                return (await self.a.overview("life-a"))["visits"][0]
            self.assertIn("model_retry", current)
            self.now = max(self.now + 1, current["model_retry"]["next_at"] + 1)
            await self.a.tick()
        self.fail("persistent inference failure must stop after bounded retries")

    async def test_safe_model_failure_ends_without_inviting_or_counting_success(self):
        async def exhausted(*args, **kwargs):
            raise VisitDecisionError("budget")
        self.a_actor.decide = exhausted
        pending = await self.a.invite("life-a", self.peer)
        self.assertEqual(pending["status"], "interrupted")
        self.assertIn("预算已用完", pending["end_reason"])
        self.assertFalse(pending.get("arrived_at"))
        self.assertFalse(pending["log"])
        self.assertFalse(self.b._state["visits"])
        self.assertFalse(self.a._state["draws"])

    async def test_untyped_failure_keeps_a_safe_generic_reason(self):
        async def failed(*args, **kwargs):
            raise RuntimeError("private provider body")
        self.a_actor.decide = failed
        pending = await self.a.invite("life-a", self.peer)
        pending = await self.finish_waiting(pending)
        self.assertEqual(pending["status"], "interrupted")
        self.assertNotIn("private", pending["end_reason"])
        self.assertIn("思考未完成", pending["end_reason"])

    async def test_automatic_model_failure_does_not_spend_a_success(self):
        async def exhausted(*args, **kwargs):
            raise VisitDecisionError("budget")
        self.a_actor.decide = exhausted
        self.a.config = replace(self.a.config, auto_enabled=True)
        pending = await self.a.invite("life-a", self.peer, automatic=True)
        self.assertEqual(pending["status"], "interrupted")
        self.assertIn("预算已用完", pending["end_reason"])
        days = self.a._state["draws"]["life-a"]["peer_days"]
        entry = next(iter(days.values()))["days"]["2026-11-01"]
        self.assertEqual(entry["successes"], 0)
        self.assertFalse(self.b._state["visits"])

    async def test_receiver_model_failure_reaches_the_caller_as_an_ending_reason(self):
        async def exhausted(*args, **kwargs):
            raise VisitDecisionError("budget")
        self.b_actor.decide = exhausted
        await self.a.invite("life-a", self.peer)
        self.now += 65
        await self.b.tick()
        self.now += 65
        await self.a.tick()
        ended = (await self.a.overview("life-a"))["visits"][0]
        self.assertEqual(ended["status"], "interrupted")
        self.assertIn("预算已用完", ended["end_reason"])
        self.assertFalse(ended.get("arrived_at"))

    async def test_service_inference_timeout_keeps_its_distinct_reason(self):
        async def stalled(*args, **kwargs):
            await asyncio.Future()
        self.a_actor.decide = stalled
        self.a.config = replace(self.a.config, timeout_seconds=.02)
        ended = await self.a.invite("life-a", self.peer)
        ended = await self.finish_waiting(ended)
        self.assertEqual(ended["status"], "interrupted")
        self.assertIn("回应超时", ended["end_reason"])
        self.assertFalse(self.b._state["visits"])
