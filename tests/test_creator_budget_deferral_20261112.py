"""Unissued life generation waits must not become failures or spent retries."""
from __future__ import annotations

import asyncio
import json
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from core.background_budget import capture_background_admission, record_background_admission
from core.engine.life_inference import LIFE_WORK
from core.model_calls import call_context
from core.ports import LLMReply
from tests import test_engine as fixture
from tests.stub_ports import StubLLM


class CreatorBudgetDeferralTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    async def exhaust(self):
        self.engine.world.limits.background_daily_calls = 1
        self.engine.world.limits.background_pacing = False
        day = self.engine._today_key(self.clock.now())
        await self.db.call("reserve_background", day, calls=1, tokens=0, reserve=0)

    async def test_paced_creator_wait_preserves_outer_admission_without_provider_warning(self):
        self.engine.world.limits.background_daily_calls = 24
        self.engine.world.limits.background_pacing = True
        pool = self.engine._background_budget_pool(session_id=fixture.SESSION)
        for _ in range(pool["window"]["call_allowance"]):
            await self.db.call("reserve_background", pool["ledger"], calls=24, tokens=0, reserve=0)
        token = LIFE_WORK.set(True)
        try:
            with patch.object(self.engine, "_log") as log, capture_background_admission() as outer:
                self.assertIsNone(await self.engine._ask_creator(fixture.SESSION, "system", "prompt"))
        finally:
            LIFE_WORK.reset(token)
        self.assertFalse(self.llm.calls)
        self.assertTrue(outer.deferred)
        self.assertFalse(outer.started)
        self.assertGreater(outer.next_at, self.clock.now())
        self.assertFalse(any(call.args[0] == "warning" for call in log.call_args_list))

    async def test_actual_creator_error_still_warns_and_has_concrete_message(self):
        self.engine.creator_llm.generate = AsyncMock(return_value=LLMReply(ok=False, error="provider offline"))
        with patch.object(self.engine, "_log") as log:
            with self.assertRaisesRegex(ValueError, "内容生成失败.*provider offline"):
                await self.engine._ask_creator(fixture.SESSION, "system", "prompt", raise_errors=True)
        self.assertTrue(any(call.args[0] == "warning" for call in log.call_args_list))

    async def test_nested_capture_retains_a_previous_dispatch(self):
        with capture_background_admission() as outer:
            record_background_admission(started=True)
            with capture_background_admission() as inner:
                record_background_admission(deferred=True, reason="later wait", next_at=42)
            self.assertFalse(inner.started)
            self.assertTrue(outer.started)
            self.assertTrue(outer.deferred)
            self.assertEqual((outer.reason, outer.next_at), ("later wait", 42))

    async def test_calendar_waits_do_not_exhaust_daily_attempts_and_later_quota_can_run(self):
        await self.exhaust()
        self.engine.world.calendar.auto_generate = True
        self.engine.world.calendar.world_type = "original"
        self.engine.world.calendar.use_public_calendar = False
        year = self.engine.local_now().year
        _, persona_key = await self.engine._journal_persona(fixture.SESSION)
        key = f"calendar_auto:{self.engine._journal_scope()}:{persona_key}:{year}"
        for _ in range(3):
            self.assertIsNone(await self.engine.ensure_calendar(fixture.SESSION, year, automatic=True))
            record = await self.db.call("kv_get", key)
            self.assertEqual(record["count"], 0)
            self.assertGreater(record["retry_at"], self.clock.now())
            self.clock.advance(301)
        self.assertFalse(self.llm.calls)
        self.engine.world.limits.background_daily_calls = 2000
        self.llm.replies = [json.dumps({"world_type": "original", "events": []})]
        result = await self.engine.ensure_calendar(fixture.SESSION, year, automatic=True)
        self.assertEqual(result["_generation"], "model")
        self.assertEqual(len(self.llm.calls), 1)

    async def test_diary_waits_preserve_retry_count_and_manual_generation_uses_creator(self):
        await self.exhaust()
        self.engine.world.diary.automatic = True
        day = (self.engine.local_now().date() - timedelta(days=1)).isoformat()
        sources = {"schedules": [], "events": [{"type": "action_done", "detail": "读完书"}],
                   "thoughts": [], "matters": [], "calendar": []}
        self.engine._diary_sources = AsyncMock(return_value=sources)
        creator = StubLLM([json.dumps({"title": "安静的一天", "content": "读完了一本书。"})])
        self.engine.creator_llm = creator
        _, persona_key = await self.engine._journal_persona(fixture.SESSION)
        key = f"diary_auto:{self.engine._journal_scope()}:{persona_key}:{day}"
        for _ in range(3):
            self.assertIsNone(await self.engine.ensure_diary(fixture.SESSION, day, automatic=True))
            self.assertEqual((await self.db.call("kv_get", key))["count"], 0)
            self.clock.advance(901)
        self.assertFalse(creator.calls)
        before = await self.db.call("background_status", self.engine._today_key(self.clock.now()))
        result = await self.engine.ensure_diary(fixture.SESSION, day, force=True)
        self.assertEqual(result["title"], "安静的一天")
        self.assertEqual(len(creator.calls), 1)
        self.assertFalse(self.llm.calls)
        self.assertEqual(before, await self.db.call("background_status", self.engine._today_key(self.clock.now())))

    async def test_manual_calendar_keeps_attribution_and_does_not_spend_background_quota(self):
        await self.exhaust()
        purposes = []

        async def generate(**kwargs):
            purposes.append(call_context()["purpose"])
            return LLMReply(text='{"world_type":"original","events":[]}')

        self.engine.creator_llm = type("Creator", (), {"generate": staticmethod(generate)})()
        self.engine.world.calendar.world_type = "original"
        self.engine.world.calendar.use_public_calendar = False
        before = await self.db.call("background_status", self.engine._today_key(self.clock.now()))
        result = await self.engine.ensure_calendar(fixture.SESSION, force=True)
        self.assertIsNotNone(result)
        self.assertEqual(purposes, ["calendar"])
        self.assertEqual(before, await self.db.call("background_status", self.engine._today_key(self.clock.now())))

    async def test_cancelled_journal_preparation_refunds_attempt_without_replacing_newer_record(self):
        now = self.clock.now()
        prior = {"day": self.engine._today_key(now), "count": 1}
        with self.assertRaises(asyncio.CancelledError):
            async with self.engine._journal_attempt("cancelled", prior, automatic=True, delay=300):
                raise asyncio.CancelledError()
        self.assertEqual((await self.db.call("kv_get", "cancelled"))["count"], 1)
        newer = {"day": prior["day"], "count": 3, "token": "newer", "retry_at": now + 500}
        async with self.engine._journal_attempt("newer", prior, automatic=True, delay=300):
            await self.db.call("kv_set", "newer", newer)
            record_background_admission(deferred=True, reason="wait")
        self.assertEqual(await self.db.call("kv_get", "newer"), newer)

    async def test_real_journal_dispatch_failure_consumes_attempt(self):
        self.engine.world.calendar.auto_generate = True
        self.engine.world.calendar.world_type = "original"
        self.engine.world.calendar.use_public_calendar = False
        self.engine.world.limits.background_pacing = False
        self.engine.creator_llm.generate = AsyncMock(return_value=LLMReply(ok=False, error="provider offline"))
        year = self.engine.local_now().year
        await self.engine.ensure_calendar(fixture.SESSION, year, automatic=True)
        _, persona_key = await self.engine._journal_persona(fixture.SESSION)
        key = f"calendar_auto:{self.engine._journal_scope()}:{persona_key}:{year}"
        self.assertEqual((await self.db.call("kv_get", key))["count"], 1)
