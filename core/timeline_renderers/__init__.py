"""按职责分派日志正文；新增事件只进入对应的渲染模块。"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..models import WorldConfig
from .conversation import render as render_conversation
from .actions import render as render_actions
from .integrations import render as render_integrations
from .rhythm import render as render_rhythm
from .memory import render as render_memory
from .events import render as render_events
from .common import render_raw


_RENDERERS: dict[str, Callable[..., str]] = {
    "bot_message": render_conversation,
    "bot_spoke": render_conversation,
    "engagement": render_conversation,
    "incoming": render_conversation,
    "reply": render_conversation,
    "send_failed": render_conversation,
    "speech_decision": render_conversation,
    "speech_decision_failed": render_conversation,
    "speech_result": render_conversation,
    "takeover_failed": render_conversation,
    "user_message": render_conversation,
    "action": render_actions,
    "action_done": render_actions,
    "action_start": render_actions,
    "cancel": render_actions,
    "chain": render_actions,
    "extreme": render_actions,
    "interrupt": render_actions,
    "move": render_actions,
    "plan": render_actions,
    "schedule": render_actions,
    "schedule_edit": render_actions,
    "skip": render_actions,
    "command": render_integrations,
    "command_call": render_integrations,
    "command_result": render_integrations,
    "ext_fields": render_integrations,
    "nickname": render_integrations,
    "poke": render_integrations,
    "search": render_integrations,
    "search_digest": render_integrations,
    "search_sources": render_integrations,
    "tool": render_integrations,
    "tool_call": render_integrations,
    "tool_result": render_integrations,
    "vision": render_integrations,
    "cold_start": render_rhythm,
    "day_mood": render_rhythm,
    "desire_push": render_rhythm,
    "drowsy": render_rhythm,
    "goodmorning": render_rhythm,
    "goodnight": render_rhythm,
    "manual": render_rhythm,
    "mood_reset": render_rhythm,
    "sleep_reply": render_rhythm,
    "sleep_rhythm": render_rhythm,
    "sleep_skip": render_rhythm,
    "soothed": render_rhythm,
    "storm": render_rhythm,
    "touch": render_rhythm,
    "wake_up": render_rhythm,
    "context": render_memory,
    "memory": render_memory,
    "recall_done": render_memory,
    "recall_start": render_memory,
    "remember": render_memory,
    "event": render_events,
    "event_check": render_events,
    "event_choice": render_events,
    "event_idle": render_events,
    "event_result": render_events,
    "help": render_events,
}


def render_builtin(kind: str, detail: dict[str, Any], world: WorldConfig | None, *, compact: bool) -> str:
    renderer = _RENDERERS.get(kind)
    if renderer is None:
        return render_raw(kind, detail)
    return renderer(kind, detail, world, compact=compact)
