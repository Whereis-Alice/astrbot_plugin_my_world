"""Legacy SDK compatibility must retain full requests without replaying calls."""
import asyncio
import importlib
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

from tests.test_web_routes import load_plugin_module


class ProactiveRunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module = importlib.import_module(load_plugin_module().__name__ + ".adapters.proactive_runner")
        self.event = types.SimpleNamespace(unified_msg_origin="test:GroupMessage:room")
        self.parts = [types.SimpleNamespace(text="临时任务与世界书", _no_save=True),
                      types.SimpleNamespace(image_url="source-image", _no_save=True)]

    async def test_new_sdk_receives_parts_through_public_helper_once(self):
        result = object()
        calls = []

        async def helper(*, extra_user_content_parts=None, **arguments):
            calls.append((extra_user_content_parts, arguments))
            return result

        context = types.SimpleNamespace(tool_loop_agent=helper)
        response = await self.module.run_agent(context, event=self.event, chat_provider_id="model",
            extra_user_content_parts=self.parts, request_max_retries=1, max_steps=5)
        self.assertIs(response, result)
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0][0], self.parts)
        self.assertEqual(calls[0][1]["request_max_retries"], 1)
        self.assertEqual(calls[0][1]["max_steps"], 5)

    def sdk(self, runner):
        definitions = {
            "astrbot.api.provider": {"ProviderRequest": types.SimpleNamespace},
            "astrbot.core.agent.hooks": {"BaseAgentRunHooks": lambda: object()},
            "astrbot.core.agent.runners.tool_loop_agent_runner": {"ToolLoopAgentRunner": lambda: runner},
            "astrbot.core.astr_agent_context": {"AgentContextWrapper": types.SimpleNamespace,
                                               "AstrAgentContext": types.SimpleNamespace},
            "astrbot.core.astr_agent_tool_exec": {"FunctionToolExecutor": lambda: object()},
            "astrbot.core.utils.astrbot_path": {"get_astrbot_system_tmp_path": lambda: "host-overflow"},
        }
        modules = {name: types.ModuleType(name) for name in definitions}
        for name, values in definitions.items():
            modules[name].__dict__.update(values)
        return patch.dict(sys.modules, modules)

    async def test_legacy_request_retains_parts_tools_scoped_context_and_compression(self):
        result, scoped, read_tool, compressor = object(), object(), object(), object()
        steps = []

        async def step_until_done(limit):
            steps.append(limit)
            yield None

        runner = types.SimpleNamespace(reset=AsyncMock(), step_until_done=step_until_done,
                                       get_final_llm_resp=lambda: result)
        context = types.SimpleNamespace(tool_loop_agent=AsyncMock(),
            provider_manager=types.SimpleNamespace(get_provider_by_id=AsyncMock(return_value=object())))
        tools = types.SimpleNamespace(get_tool=lambda name: read_tool if name == "astrbot_file_read_tool" else None)
        history = [{"role": "assistant", "content": "之前的回应"}]
        with self.sdk(runner):
            response = await self.module.run_agent(context, event=self.event, chat_provider_id="model",
                prompt="现场", contexts=history, extra_user_content_parts=self.parts, tools=tools,
                agent_context=scoped, llm_compress_provider=compressor, request_max_retries=1,
                max_steps=5, tool_call_timeout=7, temperature=.4)
        self.assertIs(response, result)
        context.tool_loop_agent.assert_not_awaited()
        context.provider_manager.get_provider_by_id.assert_awaited_once_with("model")
        runner.reset.assert_awaited_once()
        options = runner.reset.await_args.kwargs
        request = options["request"]
        self.assertIs(request.extra_user_content_parts, self.parts)
        self.assertTrue(all(part._no_save for part in request.extra_user_content_parts))
        self.assertEqual(request.contexts, history)
        self.assertEqual(request.session_id, self.event.unified_msg_origin)
        self.assertIs(request.func_tool, tools)
        self.assertIs(options["run_context"].context, scoped)
        self.assertEqual(options["run_context"].tool_call_timeout, 7)
        self.assertIs(options["llm_compress_provider"], compressor)
        self.assertIs(options["read_tool"], read_tool)
        self.assertEqual(options["tool_result_overflow_dir"], "host-overflow")
        self.assertEqual((steps, options["temperature"], options["request_max_retries"]), ([5], .4, 1))

    async def test_legacy_failure_and_cancellation_propagate_without_helper_retry(self):
        for failure in (TimeoutError("模型超时"), asyncio.CancelledError()):
            with self.subTest(failure=type(failure).__name__):
                runner = types.SimpleNamespace(reset=AsyncMock(side_effect=failure))
                context = types.SimpleNamespace(tool_loop_agent=AsyncMock(),
                    provider_manager=types.SimpleNamespace(get_provider_by_id=AsyncMock(return_value=object())))
                with self.sdk(runner), self.assertRaises(type(failure)):
                    await self.module.run_agent(context, event=self.event, chat_provider_id="model",
                                                extra_user_content_parts=self.parts, request_max_retries=1)
                runner.reset.assert_awaited_once()
                context.tool_loop_agent.assert_not_awaited()
                context.provider_manager.get_provider_by_id.assert_awaited_once()
