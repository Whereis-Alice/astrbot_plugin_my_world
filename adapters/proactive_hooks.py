"""Native hook order with isolated replay state for known memory integrations."""
from __future__ import annotations

import asyncio
import copy
import inspect
from traceback import extract_tb


class ReadOnlyArchive:
    """Read through a memory manager without archiving a replayed user turn."""
    def __init__(self, archive):
        self.archive = archive

    def __getattr__(self, name):
        return getattr(self.archive, name)

    async def add_message_from_event(self, *args, **kwargs):
        return None

    async def align_webchat_history(self, *args, **kwargs):
        return None

    async def add_message_async(self, *args, **kwargs):
        return False

    async def record_bot_response_async(self, *args, **kwargs):
        return None

    async def enforce_message_limit(self, *args, **kwargs):
        return None


def memory_plugin(handler):
    path = str(getattr(handler, "__module__", ""))
    if "astrbot_plugin_anamnesis" in path:
        return "anamnesis"
    if "astrbot_plugin_context_scene_memory" in path:
        return "scene"
    return ""


def replay_handler(handler, hook):
    """Clone only invocation dependencies; never patch a running plugin globally."""
    owner = getattr(handler, "__self__", None)
    kind = memory_plugin(handler)
    if owner is None or not kind:
        return handler
    if kind == "anamnesis":
        # Reflection both archives and schedules model work. Run it once after
        # transport acceptance, never for a private JSON draft or preview.
        if hook == "OnLLMResponseEvent":
            return None
        if hook != "OnLLMRequestEvent" or not getattr(owner, "event_handler", None):
            return handler
        owner = copy.copy(owner)
        owner.event_handler = copy.copy(owner.event_handler)
        recall = copy.copy(owner.event_handler._memory_recall)
        recall.conversation_manager = ReadOnlyArchive(recall.conversation_manager)
        recall.message_utils = ReadOnlyArchive(recall.message_utils)
        owner.event_handler._memory_recall = recall
    elif hook in {"OnLLMRequestEvent", "OnLLMResponseEvent"}:
        owner = copy.copy(owner)
        if getattr(owner, "_sessions", None) is not None:
            owner._sessions = ReadOnlyArchive(owner._sessions)
        if getattr(owner, "_stats", None) is not None:
            owner._stats = copy.copy(owner._stats)

        async def keep_snapshot(umo, snapshot):
            return snapshot

        owner._maybe_compress_history = keep_snapshot
    return handler.__func__.__get__(owner, type(owner))


def card_agent_hooks(plugin, event):
    """Bind and release card maintenance without exposing private drafts to memory hooks."""
    from astrbot.core.agent.hooks import BaseAgentRunHooks

    class CardAgentHooks(BaseAgentRunHooks):
        run_context = None
        finished = False

        async def on_agent_begin(self, run_context):
            self.run_context = run_context
            if await fire_hook(plugin, event, "OnAgentBeginEvent", run_context):
                raise asyncio.CancelledError("主动工具开始被插件钩子终止")

        async def on_agent_done(self, run_context, response):
            if self.finished:
                return
            self.finished = True
            await fire_hook(plugin, event, "OnAgentDoneEvent", run_context, response)

        async def cleanup(self):
            await self.on_agent_done(self.run_context, None)

    return CardAgentHooks()


async def fire_hook(plugin, event, name, *args, accepted=False):
    """Use the native registry's activation, session filters and priority order."""
    from astrbot.core.star.star_handler import EventType, star_handlers_registry
    hook = getattr(EventType, name, None)
    if hook is None:
        return False
    handlers = star_handlers_registry.get_handlers_by_event_type(hook, plugins_name=event.plugins_name)
    for metadata in handlers:
        handler = metadata.handler
        if getattr(handler, "__self__", None) is plugin:
            continue  # Own speech/thought receipts are already managed by the engine.
        if accepted and not memory_plugin(handler):
            continue
        if not accepted:
            handler = replay_handler(handler, name)
        if handler is None:
            continue
        if name in {"OnAgentBeginEvent", "OnAgentDoneEvent"}:
            # These phases carry the private JSON task, not accepted speech.
            expected = ("bind_group_card_reminder_context" if name == "OnAgentBeginEvent"
                        else "release_group_card_reminder")
            if ("astrbot_plugin_dynamic_card_plus" not in str(getattr(handler, "__module__", ""))
                    or getattr(handler, "__name__", "") != expected):
                continue
        if (name == "OnLLMRequestEvent" and args
                and "astrbot_plugin_dynamic_card_plus" in str(getattr(handler, "__module__", ""))
                and getattr(handler, "__name__", "") == "inject_group_card_tool_hint"):
            tools = getattr(args[0], "func_tool", None)
            if tools is None or tools.get_tool("set_dynamic_group_card") is None:
                continue  # Do not consume a card reminder on a read-only thought.
        try:
            result = handler(event, *args)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            frames = extract_tb(exc.__traceback__, limit=-1)
            location = f"（{frames[0].name}:{frames[0].lineno}）" if frames else ""
            plugin.logger.warning(f"[my_world] 主动发言钩子 {name}/{metadata.handler_name} 失败：{type(exc).__name__}{location}")
        if event.is_stopped():
            return True
    return event.is_stopped()
