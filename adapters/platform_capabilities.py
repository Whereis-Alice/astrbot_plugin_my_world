"""Target-local platform capabilities; never borrow another bot's event."""
from __future__ import annotations

from typing import Any


QQ_OFFICIAL = frozenset({"qq_official", "qq_official_webhook"})


def target_platform(plugin: Any, session: str) -> Any:
    getter = getattr(plugin.context, "get_platform_inst", None)
    try:
        return getter(session.split(":", 2)[0]) if callable(getter) else None
    except Exception:
        return None


def platform_family(plugin: Any, session: str) -> str:
    platform = target_platform(plugin, session)
    if platform is not None:
        try:
            return str(platform.meta().name or "")
        except Exception:
            return ""
    getter = getattr(plugin, "last_event", None)
    event = getter(session) if callable(getter) else None
    if event is None or str(getattr(event, "unified_msg_origin", "")) != session:
        return ""
    name = getattr(event, "get_platform_name", None)
    try:
        return str(name() or "") if callable(name) else ""
    except Exception:
        return ""


def official_delivery_hold(plugin: Any, session: str) -> str:
    """Reject the known SDK branch that silently skips an unresolved session.

    Context.send_message's boolean means a platform was found, not that a
    message was sent. Only inspect the cache contract when it actually exists;
    other host versions retain their own routing and permission checks.
    """
    if platform_family(plugin, session) not in QQ_OFFICIAL:
        return ""
    platform = target_platform(plugin, session)
    scenes = getattr(platform, "_session_scene", None)
    messages = getattr(platform, "_session_last_message_id", None)
    parts = session.split(":", 2)
    if len(parts) != 3 or parts[1] != "GroupMessage" or not isinstance(scenes, dict) or not isinstance(messages, dict):
        return ""
    # The official adapter strips a unique-session sender prefix before send.
    target = parts[2].rsplit("_", 1)[-1]
    proactive = scenes.get(target) == "group" and bool(getattr(platform, "_allow_group_proactive_send", False))
    if not messages.get(target) and not proactive:
        return "QQ 官方适配器尚未确认此会话的发送场景；请先在该会话与机器人交谈"
    return ""
