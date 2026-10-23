"""世界事件与求助的日志正文。"""
from __future__ import annotations

from typing import Any

from ..models import WorldConfig
from .common import _clip, node_name, EVENT_SOURCE_LABELS


def render(kind: str, detail: dict[str, Any], world: WorldConfig | None, *, compact: bool) -> str:
    if kind == "event":
        title = _clip(detail.get("title"), 40) or "一件小事"
        where = node_name(str(detail.get("node") or ""), world)
        head = f"遇到「{title}」"
        if where:
            head += f"（{where}）"
        if detail.get("imagined"):
            head += "｜虚构场景"
        if detail.get("need_help"):
            head += "｜需要协助"
        source = str(detail.get("source") or "")
        label = EVENT_SOURCE_LABELS.get(source, source)
        if label:
            head += f"｜{label}"
        if compact:
            return head
        return f"{head}：{_clip(detail.get('hook'), 80)}"

    if kind == "event_choice":
        desc = _clip(detail.get("desc"), 60) or "照原计划"
        abilities = _clip(detail.get("abilities"), 20)
        head = f"她选：{desc}"
        if abilities:
            head += f"（{abilities}）"
        if compact:
            return head
        reason = _clip(detail.get("reason"), 60)
        return f"{head}　〔{reason}〕" if reason else head

    if kind == "event_check":
        ability = _clip(detail.get("ability"), 12)
        if detail.get("skip"):
            # 她自己选了「先不做」：没有骰子可掷，别显示成概率 0
            return f"{ability or '这件事'}：她主动放弃了，不掷骰"
        value = float(detail.get("ability_value") or 0.0)
        difficulty = float(detail.get("difficulty") or 0.0)
        probability = float(detail.get("probability") or 0.0)
        roll = float(detail.get("roll") or 0.0)
        tier = _clip(detail.get("tier"), 12)
        mods = [
            f"{_clip(item.get('label'), 10)} ×{float(item.get('factor') or 1):.2f}"
            for item in list(detail.get("modifiers") or [])
            if isinstance(item, dict)
        ]
        head = f"{ability} {value:.2f}"
        if mods and not compact:
            head += " × " + " × ".join(mods)
        # 成功条件写清楚：掷出的点数 ≤ 概率才算成——只给"概率"看不出差多少
        head += f" × 难度 {difficulty:.2f} = 要掷到 ≤ {probability:.2f}"
        return f"{head}，掷出 {roll:.2f} → {tier}"

    if kind == "event_result":
        outcome = _clip(detail.get("outcome"), 90)
        parts = [f"结果：{outcome}"] if outcome else ["结果"]
        changes = detail.get("ability_delta") or {}
        if isinstance(changes, dict) and changes:
            parts.append(
                "能力值 "
                + "、".join(
                    f"{key} {'+' if float(value) >= 0 else ''}{float(value):.2f}"
                    for key, value in changes.items()
                )
            )
        if detail.get("followup"):
            parts.append("还有后续")
        elif detail.get("closed"):
            parts.append("这条线索到此为止")
        return "｜".join(parts)

    if kind == "help":
        stage = str(detail.get("stage") or "")
        if stage == "ask":
            return f"她开口求助：{_clip(detail.get('text'), 60)}"
        if stage == "reply":
            body = _clip(detail.get("text"), 80) or _clip(detail.get("digest"), 80)
            dropped = int(detail.get("dropped") or 0)
            kept = int(detail.get("kept") or 0)
            tail = f"（有效 {kept} 条 / 无关 {dropped} 条）" if kept or dropped else ""
            return f"收到群友回应：{body}{tail}"
        if stage == "resolve":
            return f"她把建议听进去了：{_clip(detail.get('note'), 80)}"
        if stage == "timeout":
            return f"等不到人，她自己收尾：{_clip(detail.get('note'), 60)}"
        if stage == "remind":
            return f"轻提醒一句：{_clip(detail.get('text'), 60)}"
        return f"求助：{_clip(detail.get('note'), 60)}"

    if kind == "event_idle":
        stage = str(detail.get("stage") or "")
        title = _clip(detail.get("title"), 40) or "这件事"
        if stage == "hold":
            return f"「{title}」先挂起来，她去做别的了"
        if stage == "expired":
            return f"「{title}」放太久，告一段落"
        if stage == "abandon":
            return f"「{title}」她自己收尾了（没人搭手）"
        if stage == "stopped":
            return f"「{title}」在编辑器里被手动完结了"
        return f"「{title}」暂时搁下"

    raise KeyError(kind)
