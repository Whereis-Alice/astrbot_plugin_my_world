"""计划、动作和日程的日志正文。"""
from __future__ import annotations

from typing import Any

from ..models import WorldConfig
from .common import _clip, _join, action_name, node_name


def render(kind: str, detail: dict[str, Any], world: WorldConfig | None, *, compact: bool) -> str:
    if kind == "plan":
        steps = detail.get("steps") or []
        names = [
            action_name(str(step.get("action", "")), world)
            if isinstance(step, dict)
            else str(step)
            for step in steps
        ]
        reason = _clip(detail.get("reason"), 40)
        source = {"rule": "规则", "llm": "大模型", "schedule": "日程", "extreme": "极端保护"}.get(
            str(detail.get("source") or ""), str(detail.get("source") or "")
        )
        text = f"决定：{' → '.join(names) or '（空计划）'}"
        where = " ".join(str(detail.get("send_to") or "").split())
        if where:
            # 这些话准备在哪儿说：和末尾的〔在 …〕（决定发生在哪）分开看
            text += f"（说给 {where}）"
        if reason:
            text += f"　〔原因：{reason}"
            text += f"；来源：{source}〕" if source else "〕"
        elif source:
            text += f"　〔来源：{source}〕"
        if compact:
            return text
        raw = _clip(detail.get("raw"), 160)
        return f"{text}\n　　模型原话：{raw}" if raw else text

    if kind == "action_start":
        name = action_name(str(detail.get("type", "")), world)
        ticks = detail.get("duration_ticks")
        where = detail.get("target_node")
        text = f"开始「{name}」"
        if ticks:
            text += f"，预计 {ticks} 个 tick"
        if where:
            text += f"，目标「{node_name(str(where), world)}」"
        return text

    if kind == "action_done":
        name = action_name(str(detail.get("type", "")), world)
        text = f"做完「{name}」"
        if compact:
            return text
        result = _clip(detail.get("tool_result"), 80)
        return f"{text}：{result}" if result else text

    if kind == "action":
        name = action_name(str(detail.get("type", "")), world)
        said = _join(detail.get("messages"), sep="\n　　")
        thought = _clip(detail.get("content"), 80)
        visible = detail.get("visible")
        text = f"执行「{name}」"
        if thought:
            text += f"　〔内心：{thought}〕"
        if said:
            text += f"：{said}"
        elif visible is False:
            text += "（静默）"
        return text

    if kind == "move":
        return f"走到「{node_name(str(detail.get('to', '')), world)}」"

    if kind == "schedule":
        who = "你点了「立即执行」" if detail.get("manual") else "到点触发"
        when = _clip(detail.get("time"), 8)
        head = f"日程「{detail.get('id', '')}」{who}"
        if when:
            head = f"{head}（{when}）"
        status = {"running": "本轮启动", "completed": "本轮完成", "partial": "部分跳过", "skipped": "未执行"}.get(detail.get("status"))
        if status:
            head += f"〔{status}〕"
        actions = _clip(detail.get("actions"), 80)
        return f"{head}：{actions}" if actions else head

    if kind == "extreme":
        label = {
            "force_sleep": "精力透支，强制休息",
            "force_reach_out": "太久没和人说话，强制找人",
            "need_change": "想换换环境",
        }.get(str(detail.get("flag") or ""), str(detail.get("flag") or ""))
        return f"极端保护触发：{label}"

    if kind == "interrupt":
        return f"动作被打断：{action_name(str(detail.get('action') or ''), world)}"

    if kind == "cancel":
        return f"按对方说的停了：{_clip(detail.get('note'), 60)}"

    if kind == "schedule_edit":
        what = {"add": "加了一条日程", "remove": "删掉了一条日程", "list": "翻了自己的日程"}.get(
            str(detail.get("op") or ""), "改了日程"
        )
        if detail.get("ok") is False:
            return f"日程没改成（{what}）：{_clip(detail.get('note'), 80)}"
        return f"{what}：{_clip(detail.get('note'), 80)}"

    if kind == "chain":
        return f"接着执行日程「{detail.get('schedule', '')}」"

    if kind == "skip":
        return f"跳过：{_clip(detail.get('note'), 120)}"

    raise KeyError(kind)
