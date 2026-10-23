"""把事件日志渲染成人能看懂的「她在干什么、为什么」。

引擎只负责写入结构化的事件（``event_type`` + ``detail``），这里负责把它翻译成中文时间线，
供 Web 编辑器的「日志」页和调试命令使用。纯函数，方便单测。
"""

from __future__ import annotations

from typing import Any

from .models import WorldConfig
from .timeline_renderers import render_builtin
from .timeline_renderers.common import (
    EVENT_SOURCE_LABELS as EVENT_SOURCE_LABELS,
    _clip as _clip,
    _join as _join,
    _reasoning_text as _reasoning_text,
    action_name as action_name,
    node_name as node_name,
    render_raw,
)


def render_event(
    event: dict[str, Any],
    world: WorldConfig | None = None,
    *,
    compact: bool = False,
    ext_render: Any = None,
) -> str:
    """把一个事件渲染成一行中文说明。

    ``compact`` 是调试输出的精简模式：只留"发生了什么"，去掉参数、返回值、
    模型原话这些需要展开看的细节。

    ``ext_render``：扩展注册的渲染器 ``(类型, detail) -> str``。扩展自己的事件
    （``ext_*``）优先交给它，写不出中文才走下面的通用兜底。
    """

    text = ""
    if callable(ext_render):
        try:
            text = str(ext_render(event.get("event_type"), event.get("detail") or {}) or "").strip()
        except Exception:
            text = ""
    if not text:
        try:
            text = _render_body(event, world, compact=compact)
        except (TypeError, ValueError, OverflowError, AttributeError):
            # Persisted JSON may use an old or invalid field shape. Keep the
            # record visible without changing it or interrupting the whole page.
            kind, detail = _body_fields(event)
            text = render_raw(kind, detail) + "（记录字段异常，显示原始摘要）"
    where = _place_text(event)
    return f"{text}　〔在 {where}〕" if where else text


def _place_text(event: dict[str, Any]) -> str:
    """这件事发生在哪个会话（会话组里的几个群 / 私聊共用一册日志，得标出来）。"""

    detail = event.get("detail") or {}
    if not isinstance(detail, dict):
        return ""
    return " ".join(str(detail.get("session") or "").split())


def _body_fields(event: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    kind = str(event.get("event_type") or "")
    detail = event.get("detail") or {}
    return kind, detail if isinstance(detail, dict) else {"value": detail}


def _render_body(
    event: dict[str, Any],
    world: WorldConfig | None = None,
    *,
    compact: bool = False,
) -> str:
    kind, detail = _body_fields(event)
    return render_builtin(kind, detail, world, compact=compact)


def build_timeline(
    events: list[dict[str, Any]],
    world: WorldConfig | None = None,
    *,
    ext_render: Any = None,
) -> list[dict[str, Any]]:
    """给一组事件补上渲染文本，返回给前端。"""

    result: list[dict[str, Any]] = []
    for event in events:
        item = dict(event)
        item["text"] = render_event(event, world, ext_render=ext_render)
        result.append(item)
    return result


