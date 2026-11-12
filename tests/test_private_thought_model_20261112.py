"""Unwritten private thoughts use life inference and stay out of speech delivery."""
from __future__ import annotations

import asyncio
import json
import unittest

from core.background_budget import capture_background_admission, record_background_admission
from core.engine import TickOutcome
from core.json_actions import PlannedAction
from core.memory import INNER
from core.model_calls import call_context
from core.speech_runtime import SPEECH_REQUEST
from tests import test_engine as fixture
from tests.stub_ports import StubLLM


class PrivateThoughtModelTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"

    async def asyncSetUp(self):
        await fixture.EngineTestCase.asyncSetUp(self)
        self.speech = StubLLM([fixture.SAY_REPLY] * 3)
        self.engine.speech_llm = self.speech
        raw = self.store.raw_world()
        definition = next(row for row in raw["actions"] if row["id"] == "think")
        definition.update(llm_level="single", category="instant", visible=True)
        self.store.save_world(raw)
        self.engine.reload_config()

    async def execute(self, **fields):
        outcome = TickOutcome(fixture.SESSION, speech_kind="life_share")
        action = PlannedAction(type="think", **fields)
        async with self.engine.session_state(fixture.SESSION) as state:
            await self.engine._execute_actions(state, self.engine.node(state.node_id), outcome,
                [action], depth=0, autonomous=True)
        await self.engine._deliver(outcome)
        return action, outcome

    def inner_memories(self):
        return self.engine.memory.db.query_memories(session_id=fixture.SESSION, memory_type=INNER, limit=20)

    async def test_unwritten_thought_calls_only_life_model_and_is_retained(self):
        thought = "那本书的结尾还没想明白，等会儿再翻一翻。"
        self.llm.replies = [json.dumps({"inner": thought}, ensure_ascii=False)]
        action, outcome = await self.execute(intent="回想刚读过的书")
        self.assertEqual(action.content, thought)
        self.assertEqual(len(self.llm.calls), 1)
        self.assertFalse(self.speech.calls)
        self.assertFalse(self.tools.calls)
        self.assertFalse(self.messenger.flat_messages)
        self.assertFalse(outcome.messages)
        thoughts = (await self.engine.thoughts_payload(fixture.SESSION))["thoughts"]
        self.assertEqual([row["content"] for row in thoughts], [thought])
        self.assertEqual([row["content"] for row in self.inner_memories()], [thought])
        events = await self.db.call("query_events", session_id=fixture.SESSION, limit=20)
        detail = next(row["detail"] for row in events if row["event_type"] == "action")
        self.assertEqual(detail["messages"], [])
        self.assertEqual(detail["content"], thought)
        prompt = self.llm.calls[0]["system_prompt"] + self.llm.calls[0]["prompt"]
        self.assertNotIn("发到群里的话", prompt)
        self.assertNotIn('"type":"say"', prompt)

    async def test_existing_content_avoids_a_second_call_and_is_retained_once(self):
        thought = "先记住这个念头就好。"
        await self.execute(content=thought, interject=True)
        self.assertFalse(self.llm.calls)
        self.assertFalse(self.speech.calls)
        self.assertFalse(self.messenger.flat_messages)
        self.assertEqual([row["content"] for row in self.inner_memories()], [thought])
        thoughts = (await self.engine.thoughts_payload(fixture.SESSION))["thoughts"]
        self.assertEqual([row["content"] for row in thoughts], [thought])

    async def test_private_template_becomes_a_thought_without_model_or_message(self):
        raw = self.store.raw_world()
        definition = next(row for row in raw["actions"] if row["id"] == "think")
        definition.update(llm_level="template", template="晚些时候再想想这件事。")
        self.store.save_world(raw)
        self.engine.reload_config()
        action, outcome = await self.execute()
        self.assertEqual(action.content, "晚些时候再想想这件事。")
        self.assertEqual([row["content"] for row in self.inner_memories()], [action.content])
        self.assertFalse(self.llm.calls)
        self.assertFalse(self.speech.calls)
        self.assertFalse(outcome.messages)
        self.assertFalse(self.messenger.flat_messages)

    async def test_missing_life_model_does_not_fall_back_to_speech_or_a_label(self):
        self.engine.llm = None
        action, outcome = await self.execute()
        self.assertEqual(action.content, "")
        self.assertFalse(outcome.messages)
        self.assertFalse(self.speech.calls)
        self.assertFalse(self.inner_memories())

    async def test_bad_or_empty_generation_does_not_invent_or_publish_a_thought(self):
        for reply in ('{"inner": {"text":"不是字符串"}}', fixture.SAY_REPLY, "坏掉的 JSON", '{"inner":""}'):
            with self.subTest(reply=reply):
                self.llm.replies = [reply]
                action, outcome = await self.execute()
                self.assertEqual(action.content, "")
                self.assertFalse(outcome.messages)
        self.assertFalse(self.speech.calls)
        self.assertFalse(self.messenger.flat_messages)
        self.assertFalse(self.inner_memories())

    async def test_an_unissued_wait_preserves_quota_and_propagates_admission(self):
        async def deferred(*args, **kwargs):
            record_background_admission(deferred=True, reason="等预算", next_at=self.clock.now() + 120)
            return None

        self.engine._ask_llm = deferred
        async with self.engine.session_state(fixture.SESSION) as state:
            with capture_background_admission() as admission:
                result = await self.engine._generate_private_thought(
                    state, self.engine.node(state.node_id), self.engine.world.action_map()["think"],
                    PlannedAction(type="think"))
            self.assertEqual(result, "")
            self.assertEqual(state.llm_text_count_hour, 0)
        self.assertTrue(admission.deferred)
        self.assertFalse(admission.started)
        self.assertFalse(self.inner_memories())

    async def test_thought_clears_native_speech_context_and_restores_it_after_cancellation(self):
        observed = []

        async def generate(channel, **kwargs):
            observed.append((channel, SPEECH_REQUEST.get(), call_context().get("purpose")))
            raise asyncio.CancelledError()

        self.engine._background_generate = generate
        native_context = {"allow_tools": True}
        token = SPEECH_REQUEST.set(native_context)
        try:
            async with self.engine.session_state(fixture.SESSION) as state:
                with self.assertRaises(asyncio.CancelledError):
                    await self.engine._generate_private_thought(
                        state, self.engine.node(state.node_id), self.engine.world.action_map()["think"],
                        PlannedAction(type="think"))
            self.assertIs(SPEECH_REQUEST.get(), native_context)
        finally:
            SPEECH_REQUEST.reset(token)
        self.assertEqual(observed, [(self.llm, None, "thought")])
        self.assertFalse(self.inner_memories())
        self.assertFalse(self.speech.calls)

    async def test_legacy_generation_override_still_accepts_three_arguments(self):
        async def legacy(session, system, prompt):
            return '{"inner":"还有点在意，晚些再看看。"}'

        self.engine._ask_llm = legacy
        action, _outcome = await self.execute()
        self.assertEqual(action.content, "还有点在意，晚些再看看。")
        self.assertFalse(self.speech.calls)

    async def test_outward_single_action_keeps_its_speech_model(self):
        async with self.engine.session_state(fixture.SESSION) as state:
            lines = await self.engine._generate_text_actions(state, self.engine.node(state.node_id), "说一句晚安")
        self.assertEqual(lines, ["有人在吗？"])
        self.assertEqual(len(self.speech.calls), 1)
        self.assertFalse(self.llm.calls)

    async def test_actual_background_budget_wait_does_not_dispatch_thought(self):
        self.engine.world.limits.background_daily_calls = 1
        self.engine.world.limits.background_pacing = False
        day = self.engine._today_key(self.clock.now())
        await self.db.call("reserve_background", day, calls=1, tokens=0, reserve=1)
        async with self.engine.session_state(fixture.SESSION) as state:
            with capture_background_admission() as admission:
                result = await self.engine._generate_private_thought(
                    state, self.engine.node(state.node_id), self.engine.world.action_map()["think"],
                    PlannedAction(type="think"))
            self.assertEqual(result, "")
            self.assertEqual(state.llm_text_count_hour, 0)
        self.assertTrue(admission.deferred)
        self.assertFalse(self.llm.calls)
        self.assertFalse(self.speech.calls)
