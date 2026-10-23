"""生活节奏与状态变化的日志正文。"""
from __future__ import annotations

from typing import Any

from ..models import WorldConfig
from .common import _clip, node_name


def render(kind: str, detail: dict[str, Any], world: WorldConfig | None, *, compact: bool) -> str:
    if kind == "sleep_rhythm":
        source = {"llm": "模型决定", "fallback": "规则兜底", "safety": "过劳保护"}.get(str(detail.get("source")), "自主作息")
        duration = f"，计划睡 {detail.get('hours')} 小时" if detail.get("decision") == "sleep" else ""
        return f"自主作息（{source}）：{detail.get('note') or ''}{duration}"

    if kind == "cold_start":
        return f"冷启动（{detail.get('mode', 'awakening')}），从「{node_name(detail.get('node', ''), world)}」开始"

    if kind == "wake_up":
        return f"被 {detail.get('by') or '有人'} 叫醒"

    if kind == "mood_reset":
        return (
            f"心情低落太久了，她自己缓了缓（效价回到 {float(detail.get('valence') or 0):.2f}）"
        )

    if kind == "day_mood":
        label = _clip(detail.get("label") or detail.get("day_mood"), 12)
        hint = _clip(detail.get("hint"), 60)
        return f"今天的调子：{label}——{hint}" if hint else f"今天的调子：{label}"

    if kind == "desire_push":
        who = "、".join(
            str(item) for item in (detail.get("people") or []) if str(item)
        )
        reason = _clip(detail.get("reason"), 60)
        head = "想被人碰一碰"
        if who:
            head += f"（能找的人：{_clip(who, 40)}）"
        return f"{head}：{reason}" if reason else head

    if kind == "touch":
        who = _clip(detail.get("by"), 20)
        parts = "、".join(
            _clip(item, 12) for item in (detail.get("parts") or []) if str(item)
        )
        head = f"{who} 碰到了她：{parts}" if parts else f"{who} 碰了她"
        return head

    if kind == "storm":
        if detail.get("on"):
            return "被惹到了：情绪上来了，还在气头上"
        return "气消了"

    if kind == "sleep_reply":
        return (
            "她在睡觉，只回了一句固定文案："
            f"「{_clip(detail.get('text'), 60)}」"
        )

    if kind == "sleep_skip":
        count = int(detail.get("count") or 0)
        if count > 1:
            return f"她在睡觉，又挡下了这期间的 {count} 条消息（其它插件也一起挡了）"
        return f"她在睡觉，没有回复（{_clip(detail.get('reason'), 60)}）"

    if kind == "manual":
        values = detail.get("values") or {}
        labels = {
            "energy": "精力",
            "loneliness": "孤独感",
            "curiosity": "好奇心",
            "affect": "心潮",
            "boredom": "无聊",
        }
        pairs = "、".join(
            f"{labels.get(key, key)} {float(value):.2f}"
            for key, value in values.items()
            if isinstance(value, (int, float))
        )
        return f"手动改了数值：{pairs}" if pairs else "手动改了数值"

    if kind == "soothed":
        cause = _clip(detail.get("cause") or "被安抚了一会儿", 40)
        value = detail.get("valence")
        tail = f"（效价 {float(value):.2f}）" if isinstance(value, (int, float)) else ""
        return f"心情缓过来一点：{cause}{tail}"

    if kind == "drowsy":
        return str(detail.get("note") or "困了，先进临睡期")

    if kind == "goodnight":
        if bool(detail.get("said")):
            return "睡前说了晚安（说给谁由她自己定）"
        return "睡前没说话，安静地睡了"

    if kind == "goodmorning":
        if bool(detail.get("said")):
            return "睡醒说了句早安"
        return "睡醒了，没吭声"

    raise KeyError(kind)
