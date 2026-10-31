"""Verify visit tools with the installed AstrBot SDK and synthetic local transport only."""
from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
import sys
import types
from unittest.mock import AsyncMock

from astrbot.api import FunctionTool
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata

ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType("world_visit_sdk_check")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
tools = importlib.import_module(package.__name__ + ".adapters.visit_tools")


class Event(AstrMessageEvent):
    async def send(self, *args, **kwargs):
        raise AssertionError("SDK verification cannot send a real message")


async def check():
    message = AstrBotMessage()
    message.type, message.group_id, message.self_id = MessageType.GROUP_MESSAGE, "room", "bot"
    message.message_id, message.message = "synthetic", []
    message.sender = MessageMember(user_id="local-user", nickname="Local user")
    event = Event("去友邻世界找她玩一会儿", message,
                  PlatformMetadata(name="synthetic", description="test", id="test", support_proactive_message=False), "room")
    context = ContextWrapper(context=types.SimpleNamespace(event=event))
    service = types.SimpleNamespace(config=types.SimpleNamespace(enabled=True, actor="admin"),
        invite=AsyncMock(return_value={"status": "inviting"}),
        return_home=AsyncMock(return_value={"status": "interrupted"}),
        overview=AsyncMock(return_value={"peers": [{"id": "peer"}], "active": None}))
    plugin = types.SimpleNamespace(enabled=True, retired=False, visits=service,
        engine=types.SimpleNamespace(is_enabled=lambda origin: origin == event.unified_msg_origin,
                                     state_key=lambda origin: "one-life"),
        _can_admin=lambda source: True, in_self_initiated_call=lambda: False)
    tool = tools.WorldVisitTool(plugin=plugin)
    assert isinstance(tool, FunctionTool)
    assert tool.parameters["type"] == "object"
    result = await tool.call(context, peer_id="peer", direction="outgoing", evidence=event.get_message_str())
    assert "inviting" in result
    service.invite.assert_awaited_once_with("one-life", "peer", reason="", direction="outgoing")
    plugin.in_self_initiated_call = lambda: True
    denied = await tool.call(context, peer_id="peer", direction="outgoing", evidence=event.get_message_str())
    assert "后台" in denied and service.invite.await_count == 1
    assert isinstance(tools.WorldFriendsTool(plugin=plugin), FunctionTool)
    assert isinstance(tools.WorldReturnTool(plugin=plugin), FunctionTool)
    print("Native SDK tools: schema, wrapper, current local event and background denial passed")


if __name__ == "__main__":
    asyncio.run(check())
