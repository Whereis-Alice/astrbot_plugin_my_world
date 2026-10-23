"""记忆、回想与上下文的日志正文。"""
from __future__ import annotations

from typing import Any

from ..models import WorldConfig
from .common import _clip, node_name


def render(kind: str, detail: dict[str, Any], world: WorldConfig | None, *, compact: bool) -> str:
    if kind == "recall_start":
        query = detail.get("query") or {}
        where = "、".join(query.get("node_names") or []) or query.get("zone") or "任何地方"
        keyword = str(query.get("keyword") or "").strip()
        text = f"开始回想：{_clip(detail.get('intent'), 60)}（范围：{where}"
        text += f"；主题：{keyword}）" if keyword else "）"
        return text

    if kind == "recall_done":
        if compact:
            return f"回想起 {int(detail.get('count') or 0)} 件事"
        if not int(detail.get("count") or 0):
            return f"回想完了：什么都没想起来（找的是 {_clip(detail.get('keyword'), 20) or '旧事'}）"
        return f"回想起来 {int(detail.get('count') or 0)} 件事：{_clip(detail.get('detail'), 120)}"

    if kind == "memory":
        where = node_name(str(detail.get("where") or ""), world)
        text = _clip(detail.get("content"), 60)
        suffix = f"（{_clip(detail.get('reason'), 20)}）" if detail.get("reason") else ""
        return f"记住了一件事{suffix}：{text}" + (f"　@ {where}" if where else "")

    if kind == "context":
        # 群聊上下文的动作（压缩成摘要 / 留档满了）：日志页要能一眼看懂
        note = str(detail.get("note") or "").strip() or "上下文有变化"
        compressed = int(detail.get("compressed") or 0)
        if compressed:
            note += f"（{compressed} 条）"
        hint = str(detail.get("hint") or "").strip()
        return note + (f"\n　　💡 {hint}" if hint else "")

    if kind == "remember":
        # 她当场用「记住」动作写进通讯录的东西（不是睡前整理补的）
        written = [str(item) for item in (detail.get("written") or []) if str(item)]
        skipped = [str(item) for item in (detail.get("skipped") or []) if str(item)]
        head = "；".join(written) if written else "这一条没记下什么"
        text = f"她主动记进了通讯录：{head}"
        if skipped:
            text += f"　〔{'；'.join(skipped)}〕"
        return text

    raise KeyError(kind)
