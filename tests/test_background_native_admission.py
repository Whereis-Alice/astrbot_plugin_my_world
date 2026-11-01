"""Native preparation is refundable until the model runner actually begins."""
import asyncio
import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests import test_integration_integrity as integration


class BackgroundNativeAdmissionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = integration.IntegrationIntegrityTests.asyncSetUp
    response = integration.IntegrationIntegrityTests.response

    async def status(self):
        engine = self.plugin.engine
        return await self.plugin.db.call("background_status", engine._today_key(engine._now()))

    async def generate(self, prepare, *, busy=False, allow_tools=False):
        engine = self.plugin.engine
        engine.world.limits.background_pacing = False
        engine.world.decider.proactive_tools = allow_tools
        channel = self.adapter_module.AstrBotLLM(self.plugin, provider_id="provider-a")
        runtime = importlib.import_module(self.module.__name__ + ".core.speech_runtime")
        calls = importlib.import_module(self.module.__name__ + ".core.model_calls")
        budget = importlib.import_module(self.module.__name__ + ".core.background_budget")
        native = importlib.import_module(self.module.__name__ + ".adapters.proactive_context")
        token = runtime.SPEECH_REQUEST.set({"target": self.session, "rows": [], "allow_tools": allow_tools,
                                          "state": await engine.load_state(self.session)})
        try:
            with patch.object(native.ProactiveContext, "prepare", prepare), \
                    patch.object(native.ProactiveContext, "close", AsyncMock()), \
                    patch.object(engine, "native_turn_busy", return_value=busy), \
                    calls.model_context("participation", "接话判断", self.session), \
                    budget.capture_background_admission() as admission:
                reply = await engine._budgeted_generate(channel, session_id=self.session,
                    system_prompt="persona", prompt="life", max_tokens=100)
                return reply, admission
        finally:
            runtime.SPEECH_REQUEST.reset(token)

    async def assert_unspent(self):
        self.assertEqual(await self.status(), dict(calls=0, tokens=0, unknown=0, reserved=0))
        self.assertFalse(self.plugin.engine._background_failures)
        self.assertEqual(self.plugin.engine._background_active, 0)
        self.plugin.context.tool_loop_agent.assert_not_awaited()
        self.plugin.context.llm_generate.assert_not_awaited()

    async def test_prepare_failure_refunds_and_remains_an_unsent_observation(self):
        self.plugin.context.tool_loop_agent = AsyncMock()
        self.plugin.context.llm_generate = AsyncMock()
        reply, admission = await self.generate(AsyncMock(side_effect=RuntimeError("source disappeared")))
        self.assertFalse(reply.ok)
        self.assertIn("准备失败", reply.error)
        self.assertTrue(admission.deferred)
        self.assertFalse(admission.started)
        await self.assert_unspent()

    async def test_provider_changed_by_builder_is_not_a_spent_provider_failure(self):
        self.plugin.context.tool_loop_agent = AsyncMock()
        self.plugin.context.llm_generate = AsyncMock()

        async def prepare(owner, options):
            return {**options, "chat_provider_id": "provider-b"}

        reply, admission = await self.generate(prepare)
        self.assertFalse(reply.ok)
        self.assertIn("路由已变化", reply.error)
        self.assertTrue(admission.deferred)
        self.assertFalse(admission.started)
        await self.assert_unspent()

    async def test_native_turn_started_before_dispatch_is_refundable(self):
        self.plugin.context.tool_loop_agent = AsyncMock()
        self.plugin.context.llm_generate = AsyncMock()

        async def prepare(owner, options):
            return options

        reply, admission = await self.generate(prepare, busy=True)
        self.assertFalse(reply.ok)
        self.assertIn("正常回复已开始", reply.error)
        self.assertFalse(admission.started)
        await self.assert_unspent()
        rows = await self.plugin.db.call("model_calls")
        self.assertEqual(rows["summary"]["calls"], 0)

    async def test_preparation_cancellation_refunds_the_real_reservation(self):
        self.plugin.context.tool_loop_agent = AsyncMock()
        self.plugin.context.llm_generate = AsyncMock()
        entered = asyncio.Event()

        async def prepare(owner, options):
            entered.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(self.generate(prepare))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.assert_unspent()

    async def test_actual_native_request_keeps_measured_usage(self):
        self.plugin.context.tool_loop_agent = AsyncMock(return_value=self.response("ok",
            usage={"prompt_tokens": 7, "completion_tokens": 3}))

        async def prepare(owner, options):
            return options

        reply, admission = await self.generate(prepare)
        self.assertTrue(reply.ok)
        self.assertTrue(admission.started)
        self.assertFalse(admission.deferred)
        self.assertEqual(await self.status(), dict(calls=1, tokens=10, unknown=0, reserved=0))

    async def test_actual_native_failure_still_keeps_unknown_usage_and_backoff(self):
        self.plugin.context.tool_loop_agent = AsyncMock(side_effect=RuntimeError("connection timed out"))

        async def prepare(owner, options):
            return options

        with self.assertRaisesRegex(RuntimeError, "connection timed out"):
            await self.generate(prepare)
        status = await self.status()
        self.assertEqual((status["calls"], status["unknown"]), (1, 1))
        self.assertGreater(status["reserved"], 0)
        self.assertTrue(self.plugin.engine._background_failures)

    async def test_native_runner_reset_failure_is_local_and_does_not_record_model_call(self):
        from tests.test_proactive_runner import ProactiveRunnerTests
        self.plugin.context.tool_loop_agent = AsyncMock()
        self.plugin.context.llm_generate = AsyncMock()
        self.plugin.context.provider_manager = SimpleNamespace(get_provider_by_id=AsyncMock(return_value=object()))
        runner = SimpleNamespace(reset=AsyncMock(side_effect=RuntimeError("runner preparation failed")))

        async def prepare(owner, options):
            owner.request = SimpleNamespace(func_tool=None, extra_user_content_parts=[], contexts=[])
            return options

        with ProactiveRunnerTests().sdk(runner):
            reply, admission = await self.generate(prepare)
        self.assertFalse(reply.ok)
        self.assertIn("runner preparation failed", reply.error)
        self.assertFalse(admission.started)
        await self.assert_unspent()
        self.assertEqual((await self.plugin.db.call("model_calls"))["summary"]["calls"], 0)

    async def test_midnight_during_native_preparation_cancels_before_dispatch(self):
        self.plugin.context.tool_loop_agent = AsyncMock()
        self.plugin.context.llm_generate = AsyncMock()
        engine = self.plugin.engine
        now = engine._now()
        previous_day = engine._today_key(now)

        async def prepare(owner, options):
            nonlocal now
            now += 24 * 3600
            return options

        with patch.object(engine, "_now", side_effect=lambda: now):
            reply, admission = await self.generate(prepare)
        self.assertFalse(reply.ok)
        self.assertIn("日期已变化", reply.error)
        self.assertFalse(admission.started)
        self.assertTrue(admission.deferred)
        self.assertEqual((await self.plugin.db.call("background_status", previous_day))["calls"], 0)
        self.plugin.context.tool_loop_agent.assert_not_awaited()
        self.assertFalse(engine._background_failures)

    async def test_tool_agent_dispatch_is_charged_and_recorded_with_conservative_usage(self):
        from tests.test_proactive_runner import ProactiveRunnerTests
        agent = importlib.import_module(self.module.__name__ + ".adapters.proactive_agent")
        self.plugin.context.tool_loop_agent = AsyncMock()
        self.plugin.context.provider_manager = SimpleNamespace(get_provider_by_id=AsyncMock(return_value=object()))
        result = self.response("ok", usage={"prompt_tokens": 7, "completion_tokens": 3})
        runner = SimpleNamespace(reset=AsyncMock(), get_final_llm_resp=lambda: result)

        async def steps(_):
            yield None

        runner.step_until_done = steps

        async def prepare(owner, options):
            self.assertIsInstance(owner.tool_agent, agent.ProactiveAgent)
            owner.tool_agent.event = SimpleNamespace(unified_msg_origin=self.session)
            owner.toolset = SimpleNamespace(get_tool=lambda _: None)
            owner.request = SimpleNamespace(func_tool=owner.toolset, extra_user_content_parts=[], contexts=[])
            return options

        with ProactiveRunnerTests().sdk(runner), patch.object(agent.ProactiveAgent, "valid", AsyncMock(return_value=True)):
            reply, admission = await self.generate(prepare, allow_tools=True)
        self.assertTrue(reply.ok, reply.error)
        self.assertTrue(admission.started)
        self.assertFalse(admission.deferred)
        status = await self.status()
        self.assertEqual((status["calls"], status["unknown"]), (1, 1))
        self.assertGreater(status["reserved"], 0)
        self.assertEqual((await self.plugin.db.call("model_calls"))["summary"]["calls"], 1)
        self.assertNotIn("before_request", runner.reset.await_args.kwargs)
