"""Outward dialogue owns its model; silent life and unissued waits stay separate."""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock

from core.background_budget import capture_background_admission, record_background_admission
from core.engine import TickOutcome
from core.engine.life_inference import LIFE_WORK
from core.events import EventPackage
from core.ports import LLMReply
from core.speech_runtime import SPEECH_REQUEST, SpeechDraft
from tests import test_engine as fixture
from tests.stub_ports import StubLLM


class SpeechModelRoutingTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    ctx = fixture.EngineTestCase.ctx

    async def asyncSetUp(self):
        await fixture.EngineTestCase.asyncSetUp(self)
        self.speech = StubLLM([fixture.SAY_REPLY] * 6)
        self.engine.speech_llm = self.speech

    async def test_default_speech_channel_tracks_background_without_overriding_an_explicit_choice(self):
        self.engine.speech_llm = None
        self.assertIs(self.engine.speech_llm, self.llm)
        self.engine.llm = None
        self.assertIsNone(self.engine.speech_llm)
        self.engine.speech_llm = self.speech
        self.assertIs(self.engine.speech_llm, self.speech)
        self.assertIsNotNone(await self.engine._ask_speech_llm(fixture.SESSION, "world", "reply"))
        self.assertIsNone(await self.engine._ask_llm(fixture.SESSION, "world", "plan"))

    async def test_concurrent_speech_and_background_do_not_share_routing(self):
        entered, release = asyncio.Event(), asyncio.Event()
        observed = []

        async def generate(channel, **kwargs):
            observed.append((channel, kwargs["prompt"]))
            if channel is self.speech:
                entered.set()
                await release.wait()
            return LLMReply(text=kwargs["prompt"])

        self.engine._background_generate = generate
        task = asyncio.create_task(self.engine._ask_speech_llm(fixture.SESSION, "world", "reply"))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            self.assertEqual(await self.engine._ask_llm(fixture.OTHER_SESSION, "world", "plan"), "plan")
            release.set()
            self.assertEqual(await task, "reply")
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(observed, [(self.speech, "reply"), (self.llm, "plan")])

    async def test_speech_cancellation_restores_channel_and_never_falls_back(self):
        async def generate(channel, **kwargs):
            if channel is self.speech:
                raise asyncio.CancelledError()
            return LLMReply(text="quiet plan")

        self.engine._background_generate = generate
        with self.assertRaises(asyncio.CancelledError):
            await self.engine._ask_speech_llm(fixture.SESSION, "world", "reply")
        self.assertEqual(await self.engine._ask_llm(fixture.SESSION, "world", "plan"), "quiet plan")
        self.assertFalse(self.llm.calls)

    async def test_ask_speech_keeps_native_context_on_speech_channel_and_existing_monkeypatches(self):
        requests = []

        async def generate(channel, **kwargs):
            requests.append((channel, SPEECH_REQUEST.get(), kwargs))
            return LLMReply(text="hello")

        self.engine._background_generate = generate
        async with self.engine.session_state(fixture.SESSION) as state:
            self.assertEqual(await self.engine.ask_speech(state, fixture.SESSION, "world", "reply",
                rows=[], images=["prepared.png"]), "hello")
        channel, speech, options = requests[0]
        self.assertIs(channel, self.speech)
        self.assertTrue(speech["allow_tools"])
        self.assertEqual(options["image_urls"], ["prepared.png"])
        self.assertIsNone(SPEECH_REQUEST.get())

        async def legacy_override(session, system, prompt):
            return "custom generation"

        self.engine._ask_llm = legacy_override
        self.assertEqual(await self.engine._ask_speech_llm(fixture.SESSION, "world", "reply"), "custom generation")

    async def test_rule_text_and_reply_tool_followup_use_speech_model(self):
        async with self.engine.session_state(fixture.SESSION) as state:
            lines = await self.engine._generate_text_actions(state, self.engine.node(state.node_id), "说一句晚安")
            self.assertEqual(lines, ["有人在吗？"])
            outcome = TickOutcome(fixture.SESSION, speech_kind="reply")
            await self.engine._llm_followup(state, self.engine.node(state.node_id), outcome,
                "回答查到的天气", "天气晴朗")
        self.assertEqual(len(self.speech.calls), 2)
        self.assertFalse(self.llm.calls)

    async def test_legacy_reply_uses_speech_model_with_history(self):
        history = [{"role": "user", "content": "昨天见过的风车"}]
        outcome = await self.engine.handle_reply(self.ctx(text="在吗"), history=history)
        self.assertTrue(outcome.ok)
        self.assertEqual(len(self.speech.calls), 1)
        self.assertEqual(self.speech.calls[0]["contexts"], history)
        self.assertFalse(self.llm.calls)

    async def test_event_help_uses_speech_instead_of_helper(self):
        self.speech.replies = ['{"lines":["小树苗歪了，能帮我扶一下吗？"]}']
        async with self.engine.session_state(fixture.SESSION) as state:
            lines = await self.engine._compose_help_lines(state,
                EventPackage(title="种树", hook="小树苗歪了"), outcome=TickOutcome(fixture.SESSION))
        self.assertEqual(lines, ["小树苗歪了，能帮我扶一下吗？"])
        self.assertEqual(len(self.speech.calls), 1)
        self.assertFalse(self.llm.calls)

    async def test_event_reminder_uses_target_speech_context_and_preserves_native_receipt(self):
        native = object()
        self.speech.replies = [SpeechDraft('{"line":"那我自己试试看。"}', native_event=native)]
        outcome = TickOutcome(fixture.OTHER_SESSION)
        async with self.engine.session_state(fixture.SESSION) as state:
            line = await self.engine._compose_remind_line(state, title="小树苗", hook="苗有点歪",
                asked="能帮我扶一下吗", target=fixture.OTHER_SESSION, outcome=outcome)
        self.assertEqual(line, "那我自己试试看。")
        self.assertEqual(self.speech.calls[0]["session_id"], fixture.OTHER_SESSION)
        self.assertIs(outcome.native_events[fixture.OTHER_SESSION], native)
        self.assertFalse(self.llm.calls)

    async def test_event_budget_wait_preserves_failure_count_and_records_defer(self):
        self.engine.world.limits.background_daily_calls = 1
        self.engine.world.limits.background_pacing = False
        day = self.engine._today_key(self.clock.now())
        await self.db.call("reserve_background", day, calls=1, tokens=0, reserve=1)
        outcome = TickOutcome(fixture.SESSION)
        token = LIFE_WORK.set(True)
        try:
            async with self.engine.session_state(fixture.SESSION) as state:
                state.auto_event_failures = 2
                with capture_background_admission() as admission:
                    await self.engine._start_event(state, self.engine.node(state.node_id), outcome, tier="small")
                self.assertEqual(state.auto_event_failures, 2)
                self.assertGreaterEqual(state.auto_event_retry_at, self.clock.now() + 60)
                self.assertLessEqual(state.auto_event_retry_at, self.clock.now() + 900)
        finally:
            LIFE_WORK.reset(token)
        self.assertTrue(admission.deferred)
        self.assertFalse(admission.started)
        self.assertFalse(self.llm.calls)
        rows = await self.db.call("query_events", session_id=fixture.SESSION, limit=10)
        self.assertIn("event_deferred", [row["event_type"] for row in rows])
        self.assertNotIn("event_skip", [row["event_type"] for row in rows])

    async def test_event_with_a_real_dispatch_before_defer_still_counts_failure(self):
        async def incomplete(*args, **kwargs):
            record_background_admission(started=True)
            record_background_admission(deferred=True, reason="later step waiting", next_at=self.clock.now() + 120)
            return None

        self.engine._generate_event_package = incomplete
        async with self.engine.session_state(fixture.SESSION) as state:
            state.auto_event_failures = 2
            await self.engine._start_event(state, self.engine.node(state.node_id), TickOutcome(fixture.SESSION), tier="small")
            self.assertEqual(state.auto_event_failures, 3)
        rows = await self.db.call("query_events", session_id=fixture.SESSION, limit=10)
        self.assertIn("event_skip", [row["event_type"] for row in rows])

    async def test_malformed_event_package_still_counts_provider_failure(self):
        self.engine._generate_event_package = AsyncMock(return_value=None)
        async with self.engine.session_state(fixture.SESSION) as state:
            await self.engine._start_event(state, self.engine.node(state.node_id), TickOutcome(fixture.SESSION), tier="small")
            self.assertEqual(state.auto_event_failures, 1)
