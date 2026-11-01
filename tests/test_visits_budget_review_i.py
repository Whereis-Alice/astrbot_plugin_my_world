"""A started visit request stays billed to the life and day that admitted it."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.llm_usage import record_llm_usage
from core.ports import LLMReply
from tests import test_visit_budget_isolation as isolation


class VisitBudgetCrossDayReviewTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = isolation.VisitBudgetIsolationTests.asyncSetUp
    generate = isolation.VisitBudgetIsolationTests.generate
    status = isolation.VisitBudgetIsolationTests.status
    ordinary = isolation.VisitBudgetIsolationTests.ordinary
    usage = isolation.VisitBudgetIsolationTests.usage

    async def test_started_response_after_midnight_settles_only_original_day(self):
        self.engine.visits.config.daily_calls = 1
        original = await self.status()

        async def response(**kwargs):
            self.clock.advance(86400)
            record_llm_usage({"prompt_tokens": 7, "completion_tokens": 3})
            return LLMReply(text="a confirmed choice")

        channel = SimpleNamespace(generate=AsyncMock(side_effect=response))
        self.assertTrue((await self.generate(channel=channel)).ok)
        old = await self.db.call("background_status", original["ledger"])
        self.assertEqual(old, dict(calls=1, tokens=10, unknown=0, reserved=0))
        current = await self.status()
        self.assertNotEqual(current["ledger"], original["ledger"])
        self.assertEqual(self.usage(current), isolation.EMPTY)
        self.assertTrue(current["allowed"])
        self.assertEqual(await self.ordinary(), isolation.EMPTY)
        channel.generate.assert_awaited_once()

    async def test_started_failure_after_midnight_retains_unknown_only_original_day(self):
        self.engine.visits.config.daily_calls = 1
        original = await self.status()

        async def failure(**kwargs):
            self.clock.advance(86400)
            raise TimeoutError("a started provider request did not return usage")

        channel = SimpleNamespace(generate=AsyncMock(side_effect=failure))
        with self.assertRaises(TimeoutError):
            await self.generate(channel=channel)
        old = await self.db.call("background_status", original["ledger"])
        self.assertEqual((old["calls"], old["tokens"], old["unknown"]), (1, 0, 1))
        self.assertGreater(old["reserved"], 0)
        current = await self.status()
        self.assertNotEqual(current["ledger"], original["ledger"])
        self.assertEqual(self.usage(current), isolation.EMPTY)
        self.assertTrue(current["allowed"])
        self.assertEqual(await self.ordinary(), isolation.EMPTY)
        channel.generate.assert_awaited_once()
