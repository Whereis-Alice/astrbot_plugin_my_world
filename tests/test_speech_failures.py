"""A failed generation is not a character's silence or an invalid JSON choice."""
import asyncio
import unittest
from unittest.mock import AsyncMock

from core.ports import LLMReply
from tests import test_participation as fixture
from tests import test_chat_browse as browsing

GROUP, OTHER = fixture.GROUP, fixture.OTHER


class SpeechFailureTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.ParticipationTests.asyncSetUp
    chat = fixture.ParticipationTests.chat

    async def test_preparation_failure_keeps_real_reason_without_format_retry(self):
        error = "主动上下文准备失败：RuntimeError: 目标会话没有原生事件"
        self.llm.generate = AsyncMock(return_value=LLMReply(ok=False, error=error))
        await self.chat()
        await self.engine.maybe_participate(GROUP)
        state = await self.engine.load_state(GROUP)
        self.assertIn(error, state.participation[GROUP]["reason"])
        self.assertNotIn("没有返回完整", state.participation[GROUP]["reason"])
        self.assertEqual(state.participation[GROUP]["decision"], "failed")
        self.assertEqual(self.llm.generate.await_count, 1)
        self.assertFalse(self.messenger.sent)
        self.assertFalse(state.thoughts)

    async def test_failed_repair_reports_call_error_without_using_the_invalid_draft(self):
        self.llm.generate = AsyncMock(side_effect=[
            LLMReply(text='{"say":"这句缺少想法"}', ok=True),
            LLMReply(ok=False, error="Provider 请求超时"),
        ])
        await self.chat()
        await self.engine.maybe_participate(GROUP)
        state = await self.engine.load_state(GROUP)
        self.assertIn("Provider 请求超时", state.participation[GROUP]["reason"])
        self.assertEqual(self.llm.generate.await_count, 2)
        self.assertFalse(self.messenger.sent)
        self.assertFalse(state.thoughts)

    async def test_backoff_reports_the_gate_without_another_provider_call(self):
        self.engine._background_failures = {(id(self.llm), "participation"): {
            "until": self.clock.now() + 60, "error": "Provider 限流", "count": 1,
        }}
        await self.chat()
        await self.engine.maybe_participate(GROUP)
        state = await self.engine.load_state(GROUP)
        self.assertIn("退避", state.participation[GROUP]["reason"])
        self.assertIn("Provider 限流", state.participation[GROUP]["reason"])
        self.assertEqual(self.llm.calls, [])
        self.assertFalse(self.messenger.sent)

    async def test_empty_success_does_not_borrow_the_previous_failure(self):
        self.engine._last_llm[GROUP] = {"at": self.clock.now(), "ok": False,
                                        "error": "旧错误不属于本轮"}
        self.engine._ask_llm = AsyncMock(return_value="")
        await self.chat()
        await self.engine.maybe_participate(GROUP)
        state = await self.engine.load_state(GROUP)
        self.assertIn("空内容", state.participation[GROUP]["reason"])
        self.assertNotIn("旧错误", state.participation[GROUP]["reason"])
        self.engine._ask_llm.assert_awaited_once()

    async def test_failure_is_redacted_before_snapshot_and_persistent_diagnostics(self):
        secret = "abk_privateCredential12345"
        error = f"请求失败 api_key={secret} https://private.example/path?token=hidden"
        self.llm.generate = AsyncMock(return_value=LLMReply(ok=False, error=error))
        await self.chat()
        await self.engine.maybe_participate(GROUP)
        state = await self.engine.load_state(GROUP)
        for value in (state.participation[GROUP]["reason"], self.engine._last_llm[GROUP]["error"]):
            self.assertIn("请求失败", value)
            self.assertNotIn(secret, value)
            self.assertNotIn("private.example", value)

    async def test_whitespace_only_output_is_empty_and_does_not_request_format_repair(self):
        self.llm.generate = AsyncMock(return_value=LLMReply(text=" \n\t", ok=True))
        await self.chat()
        await self.engine.maybe_participate(GROUP)
        state = await self.engine.load_state(GROUP)
        self.assertIn("空内容", state.participation[GROUP]["reason"])
        self.llm.generate.assert_awaited_once()
        self.assertFalse(self.messenger.sent)

    async def test_helper_speech_reports_its_own_failure(self):
        self.engine.helper_llm = self.llm
        self.llm.generate = AsyncMock(return_value=LLMReply(ok=False, error="打杂模型不可用"))
        async with self.engine.session_state(GROUP) as state:
            raw, decision = await self.engine.ask_speech_decision(state, GROUP, "system", "prompt",
                rows=[], channel="helper", targets={}, allowed=("listen",))
        self.assertIsNone(raw)
        self.assertIn("打杂模型不可用", decision.error)
        self.llm.generate.assert_awaited_once()

    async def test_successful_outer_generation_clears_a_nested_helpers_failure(self):
        self.engine.helper_llm = object()

        async def generate(channel, **kwargs):
            if channel is self.engine.helper_llm:
                return LLMReply(ok=False, error="可选工具参数补全失败")
            await self.engine._ask_helper(GROUP, "tool", "params")
            return LLMReply(ok=True, text='{"inner":"先听听。","intent":"listen","say":""}')

        self.engine._background_generate = generate
        async with self.engine.session_state(GROUP) as state:
            _, decision = await self.engine.ask_speech_decision(state, GROUP, "system", "prompt",
                rows=[], targets={}, allowed=("listen",))
        self.assertEqual(decision.error, "")
        self.assertEqual(decision.intent, "listen")
        self.assertTrue(self.engine._last_llm[GROUP]["ok"])

    async def test_concurrent_generations_keep_each_attempts_own_error(self):
        from core.llm_attempt import capture_attempt
        entered, release = asyncio.Event(), asyncio.Event()

        async def generate(channel, **kwargs):
            if kwargs["session_id"] == GROUP:
                entered.set()
                await release.wait()
                return LLMReply(ok=False, error="第一会话失败")
            release.set()
            return LLMReply(ok=False, error="第二会话失败")

        self.engine._background_generate = generate

        async def ask(target):
            with capture_attempt(target) as attempt:
                await self.engine._ask_llm(target, "system", "prompt")
                return attempt.error

        first = asyncio.create_task(ask(GROUP))
        await entered.wait()
        second = asyncio.create_task(ask(OTHER))
        self.assertEqual(await asyncio.gather(first, second), ["第一会话失败", "第二会话失败"])


class BrowseFailureTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = browsing.ChatBrowseTests.asyncSetUp
    configure = browsing.ChatBrowseTests.configure
    note = browsing.ChatBrowseTests.note
    prepare = browsing.ChatBrowseTests.prepare
    complete = browsing.ChatBrowseTests.complete
    add_group = browsing.ChatBrowseTests.add_group

    async def test_reading_thought_survives_a_failed_speech_generation_with_real_reason(self):
        action = await self.prepare(reply_enabled=True, message_id="read-message")
        original = self.llm.generate
        attempts = []

        async def generate(**kwargs):
            attempts.append(kwargs)
            if len(attempts) > 1:
                return LLMReply(ok=False, error="后台每日调用预算已用完")
            return await original(**kwargs)

        self.llm.generate = generate
        state = await self.complete(action)
        thought = next(row for row in state.thoughts if row.get("browse_id") == action["id"])
        self.assertEqual(thought["content"], "也想在自己的窗边留一小盆花。")
        self.assertIn("预算已用完", thought["detail"]["browse_reply"]["reason"])
        self.assertEqual(thought["detail"]["browse_reply"]["status"], "failed")
        self.assertFalse(thought.get("browse_replied"))
        self.assertEqual(len(attempts), 2)
        self.assertFalse(self.messenger.sent)
