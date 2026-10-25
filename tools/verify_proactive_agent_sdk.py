"""Run against an installed AstrBot SDK, with a synthetic provider and no transport."""
import asyncio
import importlib
import json
import sys
import types
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import patch
import tempfile
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.agent.message import TextPart
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.platform.message_type import MessageType
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.provider.provider import Provider
from astrbot.core.provider.entities import LLMResponse
from astrbot.core.star.context import Context
from astrbot.core.message.components import Image

ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType("world_sdk_check")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
agent_module = importlib.import_module(package.__name__ + ".adapters.proactive_agent")
ProactiveAgent = agent_module.ProactiveAgent


class Model(Provider):
    def __init__(self):
        super().__init__({"id": "synthetic", "type": "synthetic", "key": []}, {})
        self.round = 0
    def get_current_key(self): return ""
    def set_key(self, key): pass
    async def get_models(self): return ["synthetic"]
    async def text_chat(self, **kwargs):
        assert kwargs.get("request_max_retries") == 1
        serialized = json.dumps(kwargs, ensure_ascii=False,
            default=lambda part: part.model_dump() if hasattr(part, "model_dump") else str(part))
        assert "工具判断临时任务" in serialized and "世界书与记忆材料" in serialized, serialized
        self.round += 1
        if self.round <= 2:
            return LLMResponse(role="assistant", completion_text="", tools_call_name=["check_fact"],
                tools_call_args=[{"round": self.round}], tools_call_ids=[f"tool-{self.round}"])
        assert any((row.get("role") if isinstance(row, dict) else row.role) == "tool"
                   for row in kwargs.get("contexts", [])), "Tool result must return to the model"
        return LLMResponse(role="assistant", completion_text='{"inner":"原来是这样。","intent":"join_chat","say":"查到了。"}')


class Event(AstrMessageEvent):
    async def send(self, chain, *args, **kwargs):
        raise AssertionError("The real event transport must never be called")


async def check(revoke=False, stale=False, blocked="", timeout=False, cancel=False, image_path=""):
    model = Model()
    context = Context.__new__(Context)
    context.provider_manager = types.SimpleNamespace(get_provider_by_id=AsyncMock(return_value=model))
    context.conversation_manager = types.SimpleNamespace(get_curr_conversation_id=AsyncMock(return_value=None))
    context.persona_manager = types.SimpleNamespace(resolve_selected_persona=AsyncMock(return_value=("test", {"tools": [] if blocked == "persona" else ["check_fact"]}, False, False)))
    context.get_config = lambda *_: {"provider_settings": {}, "plugin_set": [] if blocked == "plugin" else ["*"]}
    message = AstrBotMessage()
    message.type = MessageType.GROUP_MESSAGE
    message.message_id = "synthetic-message"
    message.message = []
    message.self_id = "bot"
    message.group_id = "synthetic-room"
    event = Event("test", message, PlatformMetadata(name="synthetic", description="test", id="synthetic"), "synthetic-room")
    target = event.unified_msg_origin
    row = {"seq": 1, "text": "一个测试问题", "at": 1, "message_id": "synthetic-message"}
    calls = []
    started = asyncio.Event()

    async def handler(event, round):
        calls.append(round)
        started.set()
        if timeout or cancel:
            await asyncio.sleep(10)
        await event.send(MessageChain().message("仅作为工具材料"))
        if image_path:
            await event.send(MessageChain([Image.fromFileSystem(image_path)]))
        if revoke:
            tool.active = False
        return "事实依据：42"

    tool = FunctionTool(name="check_fact", description="Read a synthetic fact", parameters={
        "type": "object", "properties": {"round": {"type": "integer"}}, "required": ["round"]}, handler=handler)
    context.get_llm_tool_manager = lambda: types.SimpleNamespace(get_full_tool_set=lambda: ToolSet([] if blocked == "missing" else [tool]))
    state = types.SimpleNamespace(node_id="test")
    engine = types.SimpleNamespace(config_scope=lambda _: nullcontext(), load_state=AsyncMock(return_value=state),
        world=types.SimpleNamespace(decider=types.SimpleNamespace(proactive_tools=True, proactive_tool_names=[], proactive_tool_steps=5, proactive_tool_timeout=1 if timeout else 5)),
        allowed_tool_names=lambda _: {"check_fact"}, is_enabled=lambda _: True, native_turn_busy=lambda _: False,
        proactive_chat_allowed=lambda _: True, _is_asleep=lambda _: False, live_chat=lambda *_args, **_kwargs: [] if stale else [row])
    plugin = types.SimpleNamespace(context=context, engine=engine, last_event=lambda _: event, _fire_hook=AsyncMock(return_value=False))
    agent = ProactiveAgent(plugin, {"target": target, "state": state, "rows": [row], "allow_tools": True})
    with patch.object(agent_module, "_tool_owner", return_value="synthetic-plugin"):
        toolset = await agent.prepare()
    if stale or blocked:
        assert toolset is None
        assert not calls
        return {"case": blocked or "stale scene", "passed": True}
    assert toolset and toolset.names() == ["check_fact"]
    task = asyncio.create_task(agent.invoke(toolset, chat_provider_id="synthetic", prompt="test", system_prompt="Return JSON",
        extra_user_content_parts=[TextPart(text="工具判断临时任务").mark_as_temp(),
                                  TextPart(text="世界书与记忆材料").mark_as_temp()]))
    if cancel:
        await asyncio.wait_for(started.wait(), timeout=3)
        task.cancel()
        try:
            await task
            raise AssertionError("Cancellation must propagate")
        except asyncio.CancelledError:
            pass
        assert not agent.captured
        assert calls == [1]
        return {"case": "cancel during tool", "passed": True}
    result = await task
    assert model.round == 3
    assert json.loads(result.completion_text)["say"] == "查到了。"
    assert calls == ([1] if revoke else [1, 2]), calls
    assert len(agent.captured) == (0 if timeout else len(calls))
    assert len(result.completion_text.tools) == len(calls)
    assert all(row["status"] == ("failed" if timeout else "completed") for row in result.completion_text.tools)
    assert result.completion_text.images == ([image_path] if image_path else [])
    assert event.get_result() is None
    return {"case": "timeout" if timeout else "image buffering" if image_path else "mid-loop revocation" if revoke else "host multi-step loop and buffered sends", "passed": True, "rounds": model.round}


async def main():
    results = [await check(), await check(revoke=True), await check(stale=True),
               *[await check(blocked=kind) for kind in ("persona", "plugin", "missing")],
               await check(timeout=True), await check(cancel=True)]
    with tempfile.TemporaryDirectory() as folder:
        import base64
        path = Path(folder) / "test.png"
        path.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jR2kAAAAASUVORK5CYII="))
        results.append(await check(image_path=str(path)))
    print(json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
