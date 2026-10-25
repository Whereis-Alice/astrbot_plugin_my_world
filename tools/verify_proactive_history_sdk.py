"""Verify accepted-history boundaries with the official SDK and disposable SQLite."""
import asyncio
import importlib
import json
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import AsyncMock, patch

from astrbot.api.provider import ProviderRequest
from astrbot.core.conversation_mgr import ConversationManager
from astrbot.core.db.po import Conversation
from astrbot.core.db.sqlite import SQLiteDatabase
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.star.context import Context

ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType("world_history_sdk_check")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
delivery = importlib.import_module(package.__name__ + ".adapters.proactive_delivery")
models = importlib.import_module(package.__name__ + ".core.engine.models")


class Event(AstrMessageEvent):
    async def send(self, *_args, **_kwargs):
        raise AssertionError("No real transport may be invoked")


async def check_case(manager, kind):
    message = AstrBotMessage()
    message.type, message.group_id, message.self_id = MessageType.GROUP_MESSAGE, kind, "bot"
    message.message_id, message.message = "source", []
    message.sender = MessageMember(user_id="person", nickname="Person")
    event = Event("观察到的消息", message,
        PlatformMetadata(name="synthetic", description="test", id="synthetic"), kind)
    target = event.unified_msg_origin
    if kind in {"existing", "switch before acceptance"}:
        await manager.new_conversation(target, content=[{"role": "user", "content": "原来的话"}])
    expected = await manager.get_curr_conversation_id(target)
    conversation = (await manager.get_conversation(target, expected) if expected else
                    Conversation(platform_id="synthetic", user_id=target, cid="", history="[]"))
    request = ProviderRequest(conversation=conversation)
    event.set_extra("provider_request", request)
    context = Context.__new__(Context)
    context.conversation_manager = manager
    context.get_config = lambda *_args, **_kwargs: {"plugin_set": ["*"]}
    plugin = types.SimpleNamespace(context=context, engine=types.SimpleNamespace(), take_llm_response=lambda _: None)

    def view():
        result = models.TickOutcome(target, speech_kind="life_share", native_events={target: event})
        result.add_speech("第一段")
        result.add_speech("第二段")
        return result

    new_branch = None
    first_branch = None
    with patch.object(delivery, "fire_hook", AsyncMock(return_value=False)):
        async with delivery.delivery(plugin, target, view()):
            if kind == "no acceptance":
                pass
            elif kind == "second segment only":
                await delivery.accepted(plugin, target, "第二段", 1)
                first_branch = await manager.get_curr_conversation_id(target)
            else:
                if kind == "switch before acceptance":
                    new_branch = await manager.new_conversation(target)
                await delivery.accepted(plugin, target, "第一段", 0)
                await delivery.accepted(plugin, target, "第一段", 0)
                first_branch = await manager.get_curr_conversation_id(target)
                if kind == "user reply between segments":
                    current = await manager.get_conversation(target, first_branch)
                    history = json.loads(current.history)
                    history.append({"role": "user", "content": "然后呢？"})
                    await manager.update_conversation(target, first_branch, history=history)
                elif kind == "switch after first":
                    new_branch = await manager.new_conversation(target)
                elif kind == "reset after first":
                    await manager.delete_conversation(target)
                elif kind == "independent stale batch":
                    async with delivery.delivery(plugin, target, view()):
                        await delivery.accepted(plugin, target, "第二段", 1)
                    first = await manager.get_conversation(target, first_branch)
                    assert json.loads(first.history) == [{"role": "assistant", "content": "第一段"}]
                await delivery.accepted(plugin, target, "第二段", 1)
    assert request.conversation.cid == (expected or ""), "Do not mutate inference provenance"
    cid = await manager.get_curr_conversation_id(target)
    if kind in {"no acceptance", "reset after first"}:
        assert cid is None, (kind, cid)
    elif kind.startswith("switch"):
        assert cid == new_branch
        current = await manager.get_conversation(target, cid)
        assert json.loads(current.history) == [], (kind, current.history)
        if kind == "switch after first":
            original = await manager.get_conversation(target, first_branch)
            assert json.loads(original.history) == [{"role": "assistant", "content": "第一段"}]
        else:
            original = await manager.get_conversation(target, expected)
            assert json.loads(original.history) == [{"role": "user", "content": "原来的话"}]
    else:
        current = await manager.get_conversation(target, cid)
        wanted = (["原来的话"] if kind == "existing" else []) + ["第一段", "第二段"]
        if kind == "user reply between segments":
            wanted = ["第一段", "然后呢？", "第二段"]
        elif kind == "second segment only":
            wanted = ["第二段"]
        assert [row["content"] for row in json.loads(current.history)] == wanted, current.history
    return {"case": kind, "passed": True}


async def main():
    with tempfile.TemporaryDirectory(prefix="my-world-history-sdk-") as folder:
        database = SQLiteDatabase(str(Path(folder) / "history.db"))
        await database.initialize()
        manager = ConversationManager(database)
        # Selection preferences are isolated; all conversation CRUD uses real SQLite.
        preferences = types.SimpleNamespace(session_get=AsyncMock(return_value=None),
            session_put=AsyncMock(), session_remove=AsyncMock())
        try:
            with patch("astrbot.core.conversation_mgr.sp", preferences):
                results = [await check_case(manager, kind) for kind in (
                    "new branch", "existing", "user reply between segments", "switch before acceptance",
                    "switch after first", "reset after first", "independent stale batch",
                    "second segment only", "no acceptance")]
            print(json.dumps(results, ensure_ascii=False))
        finally:
            await database.engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
