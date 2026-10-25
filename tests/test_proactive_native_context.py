"""Native-context dispatch, replay isolation and public recipient regressions."""
import asyncio
import importlib
import types
import unittest
from core.engine import TickOutcome
from tests import test_participation as participation
from unittest.mock import AsyncMock, patch

from tests import test_integration_integrity as integration
from tests import test_expression_gate as gates


class ActivityProvenanceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = gates.ExpressionGateTests.asyncSetUp
    chat = gates.ExpressionGateTests.chat
    finish_personal_activity = gates.ExpressionGateTests.finish_personal_activity

    async def test_other_room_plan_cannot_send_wording_generated_for_this_room(self):
        from core.planner import create_plan
        async with self.engine.session_state(gates.GROUP) as state:
            plan = create_plan(steps=[{"action": "say", "messages": ["旧群的台词"],
                "session": participation.OTHER}], world_time=state.world_time, now=self.clock.now(),
                tick_seconds=self.engine.tick_seconds, source="llm")
            plan["native_context_target"] = gates.GROUP
            plan["send_to"] = participation.OTHER
            outcome = TickOutcome(participation.OTHER)
            self.engine._freeze_plan_target(state, outcome, plan, fallback=participation.OTHER)
            await self.engine._apply_plan(state, self.engine.node(state.node_id), outcome, plan)
            self.assertEqual(plan["steps"][0]["messages"], [])
            self.assertEqual(plan["steps"][0]["session"], participation.OTHER)
    async def test_saved_personal_outreach_cannot_publish_to_life_disabled_group(self):
        self.engine.world.life_share_session_whitelist = []
        outcome = await self.finish_personal_activity("outreach")
        self.engine._llm_followup.assert_not_awaited()
        self.assertTrue(any("生活分享" in note for note in outcome.notes))

    async def test_old_outreach_personal_activity_can_still_consider_real_pending_chat(self):
        self.engine.world.life_share_session_whitelist = []
        await self.chat("这本书你觉得怎么样", message_id="fresh-question")
        await self.finish_personal_activity("outreach")
        self.engine._llm_followup.assert_awaited_once()
        call = self.engine._llm_followup.call_args
        self.assertNotIn("outreach", self.engine.expression_contract(call.args[0], gates.GROUP, "life_share"))


class NativeContextTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = integration.IntegrationIntegrityTests.asyncSetUp
    response = integration.IntegrationIntegrityTests.response

    async def test_failed_native_preparation_does_not_fall_back_to_incomplete_context(self):
        runtime = importlib.import_module(self.module.__name__ + ".core.speech_runtime")
        native = importlib.import_module(self.module.__name__ + ".adapters.proactive_context")
        self.plugin.context.tool_loop_agent = AsyncMock()
        self.plugin.context.llm_generate = AsyncMock()
        adapter = self.adapter_module.AstrBotLLM(self.plugin, provider_id="provider-a", role="main")
        token = runtime.SPEECH_REQUEST.set({"target": self.session, "rows": [], "allow_tools": False})
        try:
            with patch.object(native.ProactiveContext, "prepare", AsyncMock(side_effect=RuntimeError("history unavailable"))):
                reply = await adapter.generate(session_id=self.session, system_prompt="人设", prompt="主动任务")
        finally:
            runtime.SPEECH_REQUEST.reset(token)
        self.assertFalse(reply.ok)
        self.plugin.context.llm_generate.assert_not_awaited()
        self.plugin.context.tool_loop_agent.assert_not_awaited()
        self.assertFalse(self.plugin.in_self_initiated_call())
    async def test_group_outreach_marks_recipient_on_first_speech_not_echo_or_tail(self):
        world = self.plugin.store.raw_world()
        world["proactive_session_whitelist"] = [self.session]
        self.plugin.store.save_world(world)
        self.plugin.engine.reload_config()
        self.plugin.typing_delay_for = lambda _: 0
        self.plugin.context.send_message = AsyncMock(return_value=True)
        policy = importlib.import_module(self.module.__name__ + ".core.speech_policy")
        with policy.contact_send(self.session, {"42"}, 1):
            await self.plugin.messenger.send_text(self.session, ["调试回显", "最近还在看那本书吗", "我想听你讲讲"])
        chains = [call.args[1].chain for call in self.plugin.context.send_message.call_args_list]
        self.assertEqual([part["qq"] for part in chains[1] if part.get("type") == "at"], ["42"])
        self.assertFalse(any(part.get("type") == "at" for part in chains[0] + chains[2]))

    async def test_native_request_preparation_runs_even_with_proactive_tools_off(self):
        adapter = self.adapter_module.AstrBotLLM(self.plugin, provider_id="provider-a", role="main")
        runtime = importlib.import_module(self.module.__name__ + ".core.speech_runtime")
        native = importlib.import_module(self.module.__name__ + ".adapters.proactive_context")
        observed = []

        async def prepare(owner, options):
            observed.append(self.plugin.in_self_initiated_call())
            return {**options, "contexts": [{"role": "assistant", "content": "之前的主动分享"}]}

        async def invoke(owner, **options):
            observed.append(options["contexts"][0]["content"])
            return self.response("完整上下文生成的台词")

        self.plugin.context.tool_loop_agent = AsyncMock()
        token = runtime.SPEECH_REQUEST.set({"target": self.session, "rows": [], "allow_tools": False})
        try:
            with patch.object(native.ProactiveContext, "prepare", prepare), patch.object(native.ProactiveContext, "invoke", invoke):
                reply = await adapter.generate(session_id=self.session, system_prompt="人设", prompt="主动判断")
        finally:
            runtime.SPEECH_REQUEST.reset(token)
        self.assertTrue(reply.ok, reply.error)
        self.assertEqual(observed, [True, "之前的主动分享"])
        self.assertFalse(self.plugin.in_self_initiated_call())

    async def test_replayed_memory_request_reads_but_does_not_archive_user_again(self):
        hooks = importlib.import_module(self.module.__name__ + ".adapters.proactive_hooks")
        manager = types.SimpleNamespace(add_message_from_event=AsyncMock(), get_messages=AsyncMock(return_value=["已有记忆"]))
        utils = types.SimpleNamespace(enforce_message_limit=AsyncMock())
        recall = types.SimpleNamespace(conversation_manager=manager, message_utils=utils)

        class MemoryPlugin:
            async def request(self, event, req):
                service = self.event_handler._memory_recall
                req.extend(await service.conversation_manager.get_messages())
                await service.conversation_manager.add_message_from_event(event, "user", "旧消息")
                await service.message_utils.enforce_message_limit("room")

        MemoryPlugin.request.__module__ = "data.plugins.astrbot_plugin_anamnesis.main"
        owner = MemoryPlugin()
        owner.event_handler = types.SimpleNamespace(_memory_recall=recall)
        request = []
        await hooks.replay_handler(owner.request, "OnLLMRequestEvent")(None, request)
        self.assertEqual(request, ["已有记忆"])
        manager.add_message_from_event.assert_not_awaited()
        utils.enforce_message_limit.assert_not_awaited()
        self.assertIs(owner.event_handler._memory_recall.conversation_manager, manager)
        await owner.request(None, [])
        manager.add_message_from_event.assert_awaited_once()

    async def test_hook_cancellation_propagates_and_does_not_reach_later_plugins(self):
        hooks = importlib.import_module(self.module.__name__ + ".adapters.proactive_hooks")
        event = types.SimpleNamespace(plugins_name=["enabled"], is_stopped=lambda: False)
        later = AsyncMock()

        async def cancel(*args):
            raise asyncio.CancelledError()

        registry = types.SimpleNamespace(get_handlers_by_event_type=lambda *args, **kwargs: [
            types.SimpleNamespace(handler=cancel, handler_name="cancel"),
            types.SimpleNamespace(handler=later, handler_name="later")])
        module_name = "astrbot.core.star.star_handler"
        fake = types.ModuleType(module_name)
        fake.EventType = types.SimpleNamespace(OnLLMRequestEvent="request")
        fake.star_handlers_registry = registry
        with patch.dict("sys.modules", {module_name: fake}):
            with self.assertRaises(asyncio.CancelledError):
                await hooks.fire_hook(self.plugin, event, "OnLLMRequestEvent", None)
        later.assert_not_awaited()
