"""Separate actual speech from life inference without replacing native replies."""
import asyncio
import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests import test_scope_api as scope
from tests.test_dialogue_inject import Event
from tests import test_visits_actor as visits
from core.ports import LLMReply


class SpeechProviderRoutingTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = scope.ScopeApiTests.asyncSetUp

    async def test_native_selection_is_event_local_and_empty_preserves_other_router(self):
        self.plugin.speech_provider_id = "speech-best"
        self.plugin.llm_provider_id = "life-small"
        first = Event("在看什么？", session=self.session)
        first.set_extra("selected_provider", "other-router")
        second = Event("在看什么？", session="other:GroupMessage:200")
        second.set_extra("selected_provider", "other-router")
        await asyncio.gather(self.plugin.on_waiting_llm_request(first),
                             self.plugin.on_waiting_llm_request(second))
        self.assertEqual(first.get_extra("selected_provider"), "speech-best")
        self.assertEqual(second.get_extra("selected_provider"), "other-router")
        self.plugin.speech_provider_id = ""
        await self.plugin.on_waiting_llm_request(second)
        self.assertEqual(second.get_extra("selected_provider"), "other-router")
        self.assertEqual(self.plugin.llm_provider_id, "life-small")

    async def test_disabled_stopped_command_and_internal_events_are_untouched(self):
        self.plugin.speech_provider_id = "speech-best"
        for kind in ("disabled", "stopped", "command", "internal", "proactive", "self"):
            with self.subTest(kind=kind):
                self.plugin.enabled = kind != "disabled"
                event = Event("/mw status" if kind == "command" else "在吗？",
                              session=self.session, sender_id="1" if kind == "self" else "42")
                event.set_extra("selected_provider", "original")
                if kind == "stopped":
                    event.stop_event()
                if kind == "proactive":
                    event.set_extra("my_world_proactive", True)
                token = self.plugin.set_self_initiated() if kind == "internal" else None
                try:
                    await self.plugin.on_waiting_llm_request(event)
                finally:
                    if token is not None:
                        self.plugin.reset_self_initiated(token)
                self.assertEqual(event.get_extra("selected_provider"), "original")

    async def test_speech_and_background_use_different_real_adapters(self):
        engine = self.plugin.engine
        self.plugin.speech_provider_id = "speech-best"
        self.plugin.llm_provider_id = "life-small"
        self.plugin.context.llm_generate = AsyncMock(return_value=SimpleNamespace(
            completion_text="好的", role="assistant"))
        self.assertEqual(await engine._ask_speech_llm(self.session, "角色", "回答"), "好的")
        self.assertEqual(await engine._ask_llm(self.session, "角色", "私下思考"), "好的")
        ids = [call.kwargs["chat_provider_id"] for call in self.plugin.context.llm_generate.call_args_list]
        self.assertEqual(ids, ["speech-best", "life-small"])
        self.assertEqual(engine.speech_llm.role, "main")

    async def test_background_cannot_borrow_an_inflight_reply_override(self):
        self.plugin.speech_provider_id = ""
        self.plugin.llm_provider_id = ""
        self.plugin.context.get_current_chat_provider_id = AsyncMock(return_value="session-default")
        self.plugin._turn_provider_overrides[self.session] = "reply-only"
        self.assertEqual(await self.plugin.engine.llm.resolve_provider_id(self.session), "session-default")
        self.assertEqual(await self.plugin.engine.speech_llm.resolve_provider_id(self.session), "reply-only")
        self.plugin.llm_provider_id = "life-small"
        self.assertEqual(await self.plugin.engine.speech_llm.resolve_provider_id(self.session), "life-small")

    async def test_invalid_explicit_speech_does_not_retry_on_background(self):
        self.plugin.speech_provider_id = "missing-speech"
        self.plugin.llm_provider_id = "life-small"
        self.plugin.context.llm_generate = AsyncMock(side_effect=ValueError("Provider not found"))
        result = await self.plugin.engine._ask_speech_llm(self.session, "角色", "回答")
        self.assertIsNone(result)
        self.plugin.context.llm_generate.assert_awaited_once()
        self.assertEqual(self.plugin.context.llm_generate.call_args.kwargs["chat_provider_id"], "missing-speech")
        event = Event("在吗？", session=self.session)
        await self.plugin.on_waiting_llm_request(event)
        # Native provider validation receives the explicit ID too.
        self.assertEqual(event.get_extra("selected_provider"), "missing-speech")

    async def test_provider_slot_reports_new_speech_and_background_independently(self):
        self.plugin.speech_provider_id = "speech-best"
        self.plugin.llm_provider_id = "life-small"
        slots = self.plugin._provider_slots()
        self.assertEqual((slots["speech"], slots["llm"]), ("speech-best", "life-small"))
        adapter = importlib.import_module(self.module.__name__ + ".adapters.llm").AstrBotLLM
        self.assertEqual(await adapter(self.plugin, role="creator").resolve_provider_id(self.session), "life-small")

    async def test_background_task_inside_speech_cannot_reenter_native_speech_tools(self):
        self.plugin.llm_provider_id = "life-small"
        self.plugin.context.tool_loop_agent = AsyncMock()
        self.plugin.context.llm_generate = AsyncMock(return_value=SimpleNamespace(
            completion_text="安静计划", role="assistant"))
        runtime = importlib.import_module(self.module.__name__ + ".core.speech_runtime")
        native = importlib.import_module(self.module.__name__ + ".adapters.proactive_context")
        token = runtime.SPEECH_REQUEST.set({"target": self.session, "rows": [], "allow_tools": True})
        try:
            with patch.object(native.ProactiveContext, "prepare", AsyncMock()) as prepare:
                result = await self.plugin.engine.llm.generate(session_id=self.session,
                    system_prompt="角色", prompt="安静计划")
                self.assertTrue(result.ok)
                prepare.assert_not_awaited()
        finally:
            runtime.SPEECH_REQUEST.reset(token)

    async def test_nested_quiet_generation_does_not_inherit_outward_model(self):
        engine = self.plugin.engine
        seen = []

        async def generate(channel, **kwargs):
            seen.append((channel, kwargs["prompt"]))
            if kwargs["prompt"] == "台词":
                await engine._ask_llm(self.session, "角色", "工具中的判断")
                await asyncio.create_task(engine._ask_llm(self.session, "角色", "子任务判断"))
            return LLMReply(text="好的")

        engine._background_generate = generate
        await engine._ask_speech_llm(self.session, "角色", "台词")
        self.assertEqual(seen, [(engine.speech_llm, "台词"),
                                (engine.llm, "工具中的判断"), (engine.llm, "子任务判断")])

    async def test_quiet_and_visit_json_cannot_replace_chat_reply_hook_receipt(self):
        original, response = object(), SimpleNamespace(completion_text="新结果", role="assistant")
        self.plugin.remember_llm_response(self.session, original)
        self.plugin.context.get_current_chat_provider_id = AsyncMock(return_value="session-default")
        self.plugin.context.llm_generate = AsyncMock(return_value=response)
        await self.plugin.engine.llm.generate(session_id=self.session, system_prompt="角色", prompt="生活计划")
        self.assertIs(self.plugin._llm_responses[self.session], original)
        calls = importlib.import_module(self.module.__name__ + ".core.model_calls")
        with calls.model_context("world_visit", "邀请", self.session):
            await self.plugin.engine.speech_llm.generate(session_id=self.session, system_prompt="角色", prompt="串门JSON")
        self.assertIs(self.plugin._llm_responses[self.session], original)
        await self.plugin.engine.speech_llm.generate(session_id=self.session, system_prompt="角色", prompt="台词")
        self.assertIs(self.plugin.take_llm_response(self.session), response)


class VisitSpeechRoutingTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = visits.VisitActorTests.asyncSetUp

    async def test_dialogue_and_private_visit_phases_use_separate_channels(self):
        speech = SimpleNamespace(generate=AsyncMock(return_value=LLMReply(text='{"accept":true,"say":"来了"}')))
        self.engine.speech_llm = speech
        await self.port.decide(visits.fixture.SESSION, "propose", self.visit)
        speech.generate.assert_awaited_once()
        self.assertFalse(self.llm.calls)
        self.llm.default_reply = '{"reflection":"一起下棋很开心"}'
        await self.port.decide(visits.fixture.SESSION, "finish", self.visit)
        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual(speech.generate.await_count, 1)
