"""Request hooks must see the same tool permission that the native runner uses."""
import importlib
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

from tests import test_integration_integrity as integration
from tests.test_dialogue_inject import Event


class Tools:
    def __init__(self, names):
        self.tools = [types.SimpleNamespace(name=name) if isinstance(name, str) else name for name in names]

    def names(self):
        return [tool.name for tool in self.tools]

    def get_tool(self, name):
        return next((tool for tool in self.tools if tool.name == name), None)


class Part:
    def __init__(self, text):
        self.text = text

    def mark_as_temp(self):
        self._no_save = True
        return self


class Request:
    def __init__(self, **values):
        self.system_prompt, self.func_tool = "", None
        self.audio_urls = []
        self.__dict__.update(values)


class ProactiveToolContractTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = integration.IntegrationIntegrityTests.asyncSetUp

    def fixture(self, hook):
        module = importlib.import_module(self.module.__name__ + ".adapters.proactive_context")
        source = Event("最近的原话", session=self.session)
        source.clear_result = lambda: None
        self.plugin._last_events[self.session] = source
        self.plugin._host_persona_prompt = AsyncMock(return_value="原生人设")
        provider = types.SimpleNamespace(meta=lambda: types.SimpleNamespace(id="model"))
        self.plugin.context.provider_manager = types.SimpleNamespace(get_provider_by_id=AsyncMock(return_value=provider))
        self.plugin.context.get_config = lambda *_: {"plugin_set": ["*"]}
        self.plugin.context.conversation_manager = types.SimpleNamespace(get_curr_conversation_id=AsyncMock(return_value="cid"),
            get_conversation=AsyncMock(return_value=types.SimpleNamespace(history="[]")))
        host = types.ModuleType("astrbot.core.astr_main_agent")

        async def build(**kwargs):
            request = kwargs["req"]
            request.func_tool = Tools(["set_dynamic_group_card", "lookup_weather"])
            request.system_prompt = "原生人设与上下文"
            return types.SimpleNamespace(provider_request=request, provider=provider, reset_coro=None)

        host.build_main_agent = build
        host._get_compress_provider = AsyncMock(return_value=None)
        api = types.ModuleType("astrbot.api.provider")
        api.ProviderRequest = Request
        parts = types.ModuleType("astrbot.core.agent.message")
        parts.TextPart, parts.ImageURLPart = Part, object
        hooks = types.ModuleType("astrbot.core.agent.hooks")
        hooks.BaseAgentRunHooks = type("BaseAgentRunHooks", (), {})
        for patcher in (
            patch.dict(sys.modules, {host.__name__: host, api.__name__: api, parts.__name__: parts, hooks.__name__: hooks}),
            patch.object(module, "build_config", lambda *_: types.SimpleNamespace(
                llm_compress_instruction="", llm_compress_keep_recent_ratio=.15,
                dequeue_context_length=1, max_context_length=-1, tool_schema_mode="full")),
            patch.object(module, "fire_hook", hook),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        return module.ProactiveContext(self.plugin, {"target": self.session, "rows": [], "allow_tools": False})

    def options(self):
        return {"chat_provider_id": "model", "prompt": "只规划接下来做什么，输出 JSON", "system_prompt": "小世界人设"}

    async def test_no_tool_plan_does_not_receive_a_required_card_tool_hint(self):
        observed = []

        async def hook(plugin, event, name, request):
            observed.append(request.func_tool)
            if request.func_tool and request.func_tool.get_tool("set_dynamic_group_card"):
                request.extra_user_content_parts.append(Part("必须先调用 set_dynamic_group_card").mark_as_temp())
            request.extra_user_content_parts.append(Part("世界书与记忆照常注入").mark_as_temp())
            return False

        native = self.fixture(hook)
        prepared = await native.prepare(self.options())
        self.assertEqual(observed, [None])
        text = "\n".join(part.text for part in prepared["extra_user_content_parts"])
        self.assertNotIn("必须先调用", text)
        self.assertIn("世界书与记忆照常注入", text)
        self.assertTrue(all(part._no_save for part in prepared["extra_user_content_parts"]))

    async def test_allowed_tools_are_guarded_before_hooks_and_the_narrowed_request_is_retained(self):
        allowed = Tools(["set_dynamic_group_card", "lookup_weather"])
        gate = Tools(["set_dynamic_group_card"])
        gate.tools = [allowed.get_tool("set_dynamic_group_card")]
        seen = []

        async def hook(plugin, event, name, request):
            seen.append((request, request.func_tool))
            request.func_tool = gate
            return False

        native = self.fixture(hook)
        native.tool_agent = types.SimpleNamespace(prepare=AsyncMock(return_value=allowed))
        await native.prepare(self.options())
        self.assertIs(seen[0][1], allowed)
        self.assertIs(native.request, seen[0][0])
        self.assertIs(native.toolset, gate)
        self.assertIs(native.event.get_extra("provider_request"), native.request)
        native.tool_agent.prepare.assert_awaited_once_with(event=native.event)

    async def test_no_tool_hook_cannot_enable_execution_by_adding_a_tool(self):
        async def hook(plugin, event, name, request):
            request.func_tool = Tools(["set_dynamic_group_card"])
            return False

        native = self.fixture(hook)
        await native.prepare(self.options())
        self.assertIsNone(native.event.get_extra("provider_request").func_tool)

    async def test_hook_cannot_add_an_unapproved_or_unguarded_tool(self):
        allowed = Tools(["lookup_weather"])

        async def hook(plugin, event, name, request):
            request.func_tool = Tools(["lookup_weather", "unapproved_tool"])
            return False

        native = self.fixture(hook)
        native.tool_agent = types.SimpleNamespace(prepare=AsyncMock(return_value=allowed))
        tool_module = types.ModuleType("astrbot.core.agent.tool")
        tool_module.ToolSet = Tools
        with patch.dict(sys.modules, {tool_module.__name__: tool_module}):
            await native.prepare(self.options())
        self.assertEqual(native.toolset.names(), ["lookup_weather"])
        self.assertIs(native.toolset.get_tool("lookup_weather"), allowed.get_tool("lookup_weather"))

    async def test_card_hook_does_not_consume_a_reminder_without_its_tool(self):
        hooks = importlib.import_module(self.module.__name__ + ".adapters.proactive_hooks")
        event = Event("源消息", session=self.session)
        event.plugins_name, event.is_stopped = None, lambda: False
        calls = []

        class Card:
            async def inject_group_card_tool_hint(self, event, request):
                calls.append("card")

        Card.inject_group_card_tool_hint.__module__ = "data.plugins.astrbot_plugin_dynamic_card_plus.main"
        owner = Card()

        async def book(event, request):
            calls.append("book")

        metadata = [types.SimpleNamespace(handler=owner.inject_group_card_tool_hint, handler_name="card"),
                    types.SimpleNamespace(handler=book, handler_name="worldbook")]
        registry = types.ModuleType("astrbot.core.star.star_handler")
        registry.EventType = types.SimpleNamespace(OnLLMRequestEvent="request")
        registry.star_handlers_registry = types.SimpleNamespace(get_handlers_by_event_type=lambda *_, **__: metadata)
        with patch.dict(sys.modules, {registry.__name__: registry}):
            for tools in (None, Tools(["lookup_weather"]), Tools(["set_dynamic_group_card"])):
                await hooks.fire_hook(self.plugin, event, "OnLLMRequestEvent", types.SimpleNamespace(func_tool=tools))
        self.assertEqual(calls, ["book", "book", "card", "book"])

    async def test_card_lifecycle_is_once_and_does_not_expose_private_task_to_other_plugins(self):
        hooks = importlib.import_module(self.module.__name__ + ".adapters.proactive_hooks")
        event = Event("源消息", session=self.session)
        event.plugins_name, event.is_stopped = None, lambda: False
        calls = []

        class Card:
            async def bind_group_card_reminder_context(self, event, context):
                calls.append(("begin", context))

            async def release_group_card_reminder(self, event, context, response):
                calls.append(("done", context, response))

        for method in (Card.bind_group_card_reminder_context, Card.release_group_card_reminder):
            method.__module__ = "data.plugins.astrbot_plugin_dynamic_card_plus.main"
        owner = Card()

        async def other(event, *args):
            self.fail("Private Agent JSON must not enter unrelated lifecycle hooks")

        registry = types.ModuleType("astrbot.core.star.star_handler")
        registry.EventType = types.SimpleNamespace(OnAgentBeginEvent="begin", OnAgentDoneEvent="done")
        registry.star_handlers_registry = types.SimpleNamespace(get_handlers_by_event_type=lambda phase, **kwargs: [
            types.SimpleNamespace(handler=owner.bind_group_card_reminder_context if phase == "begin"
                else owner.release_group_card_reminder, handler_name="card"),
            types.SimpleNamespace(handler=other, handler_name="other")])
        base = types.ModuleType("astrbot.core.agent.hooks")
        base.BaseAgentRunHooks = type("BaseAgentRunHooks", (), {})
        with patch.dict(sys.modules, {registry.__name__: registry, base.__name__: base}):
            for completed in (True, False):
                with self.subTest(completed=completed):
                    calls.clear()
                    adapter, context, response = hooks.card_agent_hooks(self.plugin, event), object(), object()
                    await adapter.on_agent_begin(context)
                    if completed:
                        await adapter.on_agent_done(context, response)
                    await adapter.cleanup()
                    await adapter.cleanup()
                    self.assertEqual(calls, [("begin", context), ("done", context, response if completed else None)])

    async def test_failed_request_preparation_still_releases_card_lifecycle(self):
        async def hook(plugin, event, name, request):
            raise RuntimeError("request failed after reminder binding")

        native = self.fixture(hook)
        native.tool_agent = types.SimpleNamespace(prepare=AsyncMock(return_value=Tools(["set_dynamic_group_card"])))
        cleanup = AsyncMock()
        context = importlib.import_module(self.module.__name__ + ".adapters.proactive_context")
        with patch.object(context, "card_agent_hooks", return_value=types.SimpleNamespace(cleanup=cleanup)):
            with self.assertRaisesRegex(RuntimeError, "request failed"):
                await native.prepare(self.options())
            await native.close()
        cleanup.assert_awaited_once()
