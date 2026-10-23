"""共享的文本与名称格式化；不依赖引擎或宿主。"""
from __future__ import annotations

from typing import Any

from ..models import WorldConfig


def _join(items: Any, sep: str = " / ", limit: int = 3) -> str:
    if isinstance(items, str):
        items = [items]
    if not isinstance(items, (list, tuple)) or not items:
        return ""
    texts = [str(item).strip() for item in items if str(item).strip()]
    if not texts:
        return ""
    shown = texts[:limit]
    suffix = f"…（共 {len(texts)} 条）" if len(texts) > limit else ""
    return sep.join(shown) + suffix


def _clip(text: Any, limit: int = 120) -> str:
    value = str(text or "").strip().replace("\n", " ")
    return value if len(value) <= limit else value[:limit] + "…"


# 事件是哪来的：日志与调试输出里标一行，一眼能看出这条是不是固定的
EVENT_SOURCE_LABELS: dict[str, str] = {
    "llm": "临场生成",
    "user": "用户投递",
    "follow": "线索续演",
}


def node_name(node_id: str, world: WorldConfig | None) -> str:
    if world is not None:
        node = world.node_map().get(node_id)
        if node is not None:
            return node.name or node.id
    return node_id or "未知地点"


def action_name(action_id: str, world: WorldConfig | None) -> str:
    if world is not None:
        action = world.action_map().get(action_id)
        if action is not None:
            return action.name or action.id
    return action_id


def _reasoning_text(reasoning: Any) -> str:
    if not isinstance(reasoning, dict) or not reasoning:
        return ""
    labels = {
        "env": "在哪",
        "state": "状态",
        "mood": "心情",
        "who": "在和谁说话",
        "intent": "打算",
    }
    parts = [
        f"{labels.get(key, key)}：{_clip(value, 40)}"
        for key, value in reasoning.items()
        if str(value).strip()
    ]
    return "；".join(parts)


def render_raw(kind: str, detail: dict[str, Any]) -> str:
    """保留旧记录与未知类型的原始摘要，不推断字段的意义。"""
    # 未知类型：把 detail 原样压成一行，至少不丢信息
    if detail:
        # session 由外层统一补在行尾，这里不再重复一份
        pairs = "，".join(
            f"{key}={_clip(value, 40)}"
            for key, value in detail.items()
            if key != "session"
        )
        if pairs:
            return f"{kind}：{pairs}"
    return kind or "（未知事件）"
