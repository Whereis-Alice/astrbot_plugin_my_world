"""Provider compatibility, hook isolation and concurrent native turn integrity."""

import asyncio
import importlib
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tests import test_scope_api as scope_api
from tests.test_dialogue_inject import Event


class IntegrationIntegrityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await scope_api.ScopeApiTests.asyncSetUp(self)
        self.adapter_module = importlib.import_module(self.module.__name__ + ".adapters.llm")
        self.plugin.profile_weaver_service = None

    def llm(self, **kwargs):
        return self.adapter_module.AstrBotLLM(self.plugin, provider_id="provider-a", role="helper", **kwargs)

    def response(self, text="好了", **kwargs):
        return SimpleNamespace(completion_text=text, role="assistant", **kwargs)

    async def generate(self, llm=None, **kwargs):
        return await (llm or self.llm()).generate(session_id=self.session, prompt="问候",
                                                 system_prompt="角色人设", **kwargs)

    async def test_output_limit_reaches_provider_and_unsupported_limit_is_cached_per_provider(self):
        self.plugin.context.llm_generate = AsyncMock(side_effect=[TypeError("unexpected keyword argument 'max_tokens'"), self.response()])
        llm = self.llm()
        self.assertTrue((await self.generate(llm, max_tokens=512)).ok)
        first, second = self.plugin.context.llm_generate.call_args_list
        self.assertEqual(first.kwargs["max_tokens"], 512)
        self.assertNotIn("max_tokens", second.kwargs)
        self.plugin.context.llm_generate.reset_mock(side_effect=True)
        self.plugin.context.llm_generate.return_value = self.response()
        await self.generate(llm, max_tokens=512)
        self.assertNotIn("max_tokens", self.plugin.context.llm_generate.call_args.kwargs)
        llm.provider_id = "provider-b"
        await self.generate(llm, max_tokens=512)
        self.assertEqual(self.plugin.context.llm_generate.call_args.kwargs["max_tokens"], 512)

    async def test_provider_error_response_is_failure(self):
        error = self.response("上游拒绝了请求")
        error.role = "err"
        self.plugin.context.llm_generate = AsyncMock(return_value=error)
        reply = await self.generate()
        self.assertFalse(reply.ok)
        self.assertEqual(reply.error, error.completion_text)
        self.assertFalse(self.plugin.in_self_initiated_call())

    async def test_transport_and_content_failures_do_not_strip_context_or_retry(self):
        llm = self.llm(sampling={"temperature": .8, "top_p": .9})
        history = [{"role": "user", "content": "我们的约定"}]
        for error in ("HTTP 429 quota exceeded", "connection timed out", "PROHIBITED_CONTENT image", "HTTP 503 service unavailable"):
            self.plugin.context.llm_generate = AsyncMock(side_effect=RuntimeError(error))
            result = await self.generate(llm, contexts=history, image_urls=["https://image.invalid/x"])
            self.assertFalse(result.ok, error)
            self.assertEqual(self.plugin.context.llm_generate.await_count, 1, error)
            self.assertEqual(self.plugin.context.llm_generate.call_args.kwargs["contexts"], history)
            self.assertEqual(llm._sampling_rejected_providers, set())

    async def test_sampling_then_image_compatibility_retry_preserves_other_arguments(self):
        self.plugin.context.llm_generate = AsyncMock(side_effect=[TypeError("sampling unsupported"),
            ValueError("unsupported image input"), self.response()])
        llm = self.llm(sampling={"temperature": .8, "top_p": .9})
        history = [{"role": "user", "content": "先前说过的话"}]
        result = await self.generate(llm, contexts=history, image_urls=["https://image.invalid/x"], max_tokens=500)
        self.assertTrue(result.ok)
        calls = self.plugin.context.llm_generate.call_args_list
        self.assertEqual(len(calls), 3)
        self.assertNotIn("top_p", calls[1].kwargs)
        self.assertEqual(calls[2].kwargs["contexts"], history)
        self.assertEqual(calls[2].kwargs["max_tokens"], 500)
        self.assertIsNone(calls[2].kwargs["image_urls"])
        llm.provider_id = "provider-b"
        self.plugin.context.llm_generate.side_effect = None
        self.plugin.context.llm_generate.return_value = self.response()
        await self.generate(llm)
        self.assertEqual(self.plugin.context.llm_generate.call_args.kwargs["top_p"], .9)

    async def test_nested_and_many_concurrent_self_calls_keep_their_marker(self):
        outer = self.plugin.set_self_initiated()
        inner = self.plugin.set_self_initiated()
        self.plugin.reset_self_initiated(inner)
        self.assertTrue(self.plugin.in_self_initiated_call())
        self.plugin.reset_self_initiated(outer)
        gate = asyncio.Event()
        marked = []

        async def branch():
            token = self.plugin.set_self_initiated()
            marked.append(True)
            try:
                await gate.wait()
                return self.plugin.in_self_initiated_call()
            finally:
                self.plugin.reset_self_initiated(token)

        tasks = [asyncio.create_task(branch()) for _ in range(12)]
        while len(marked) != len(tasks):
            await asyncio.sleep(0)
        self.assertFalse(self.plugin.in_self_initiated_call(), "真人任务不能跟着后台调用被屏蔽")
        gate.set()
        self.assertTrue(all(await asyncio.gather(*tasks)))
        self.assertFalse(self.plugin._self_initiated_tasks)

    async def test_reply_lock_pruning_does_not_overwrite_the_requested_scope(self):
        for index in range(130):
            self.plugin._reply_locks[f"stale-{index}"] = asyncio.Lock()
        lock = self.plugin._reply_lock(self.session)
        self.assertIs(self.plugin._reply_lock(self.session), lock)
        self.assertIn(self.plugin.engine.merge_scope(self.session), self.plugin._reply_locks)

    async def test_native_partial_or_auxiliary_response_cannot_replace_the_final_reply(self):
        event = Event("你好", session=self.session)
        turn = {"session_id": self.session, "reply": "主回复", "started_at": time.time()}
        event.set_extra(self.module.MW_TURN_EXTRA, turn)
        await self.plugin.on_llm_response(event, self.response("流片段", is_chunk=True))
        self.assertEqual(turn["reply"], "主回复")
        token = self.plugin.set_self_initiated()
        try:
            await self.plugin.on_llm_response(event, self.response("复盘 JSON"))
        finally:
            self.plugin.reset_self_initiated(token)
        self.assertEqual(turn["reply"], "主回复")

    async def test_native_error_cannot_advance_messages_or_enter_character_memory(self):
        event = Event("你好", session=self.session)
        turn = {"session_id": self.session, "started_at": time.time()}
        event.set_extra(self.module.MW_TURN_EXTRA, turn)
        self.plugin.engine.mark_reply_batch_replied = AsyncMock()
        self.plugin.engine.record_self_reply = AsyncMock()
        self.plugin._spawn_reflection = lambda _turn: self.fail("错误不能触发角色复盘")
        error = self.response("API error")
        error.role = "err"
        await self.plugin.on_llm_response(event, error)
        await self.plugin.after_message_sent(event)
        self.plugin.engine.mark_reply_batch_replied.assert_not_awaited()
        self.plugin.engine.record_self_reply.assert_not_awaited()

    async def test_intermediate_tool_send_does_not_consume_the_native_turn(self):
        event = Event("帮我看看天气", session=self.session)
        turn = {"session_id": self.session, "started_at": time.time(), "tools": []}
        event.set_extra(self.module.MW_TURN_EXTRA, turn)
        self.plugin.engine.mark_reply_batch_replied = AsyncMock()
        self.plugin._spawn_reflection = lambda _turn: self.fail("Agent 未完成不能复盘")
        await self.plugin.after_message_sent(event)
        self.assertIs(event.get_extra(self.module.MW_TURN_EXTRA), turn)
        self.plugin.engine.mark_reply_batch_replied.assert_not_awaited()

    async def test_retired_api_cannot_write_and_unload_only_removes_its_own_routes(self):
        request = SimpleNamespace(method="POST", query={}, json=AsyncMock(return_value={"scope": "all"}))
        api_module = self.module.web.api
        from unittest.mock import patch
        self.plugin._retired = True
        with patch.object(api_module, "request", request):
            result = await self.routes["restore-default"]()
        self.assertEqual(result["status_code"], 503)
        foreign = ("/other-plugin/status", lambda: None, ["GET"], "foreign")
        self.plugin.context.registered_web_apis.append(foreign)
        await self.plugin.terminate()
        self.assertEqual(self.plugin.context.registered_web_apis, [foreign])

    async def test_unload_cancels_outstanding_plugin_model_requests(self):
        entered = asyncio.Event()

        async def provider(**kwargs):
            entered.set()
            await asyncio.Event().wait()

        self.plugin.context.llm_generate = provider
        task = asyncio.create_task(self.generate())
        await asyncio.wait_for(entered.wait(), 2)
        await self.plugin.terminate()
        self.assertTrue(task.cancelled())
        self.assertFalse(self.plugin._self_initiated_tasks)

    async def test_partial_segment_failure_preserves_receipt_and_failure_cooldown(self):
        self.plugin.typing_delay_for = lambda _text: 0
        self.plugin.context.send_message = AsyncMock(side_effect=[True, RuntimeError("platform disconnected")])
        messenger = self.plugin.messenger
        self.assertTrue(await messenger.send_text(self.session, ["先送达", "随后失败", "不能再发"]))
        self.assertEqual(self.plugin.context.send_message.await_count, 2)
        self.assertEqual(messenger.take_sent_texts(self.session), ["先送达"])
        self.assertTrue(messenger.blocked(self.session))
        self.assertIn("platform disconnected", messenger.take_fail_note(self.session))

    async def test_native_turn_commits_its_own_batch_when_the_next_turn_is_pending(self):
        engine = self.plugin.engine
        async with engine.session_state(self.session) as state:
            first = state.note_chat(user_id="42", name="人类", text="第一条", now=engine._now(), origin=self.session)
            first["event_key"] = "native-first"
            second = state.note_chat(user_id="42", name="人类", text="第二条", now=engine._now() + 1, origin=self.session)
            second["event_key"] = "native-second"
            first_seq, second_seq = first["seq"], second["seq"]
        engine._replied_batch_keys[self.session] = {"native-second"}
        await engine.mark_reply_batch_replied(self.session, batch=[first])
        async with engine.session_state(self.session) as state:
            self.assertEqual(engine._watermark(state, self.session)[1], first_seq)
        self.assertEqual(engine._replied_batch_keys[self.session], {"native-second"})
        await engine.mark_reply_batch_replied(self.session, batch=[second])
        async with engine.session_state(self.session) as state:
            self.assertEqual(engine._watermark(state, self.session)[1], second_seq)
        self.assertNotIn(self.session, engine._replied_batch_keys)


if __name__ == "__main__":
    unittest.main()
