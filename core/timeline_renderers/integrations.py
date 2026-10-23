"""工具、搜索与平台交互的日志正文。"""
from __future__ import annotations

from typing import Any

from ..models import WorldConfig
from .common import _clip


def render(kind: str, detail: dict[str, Any], world: WorldConfig | None, *, compact: bool) -> str:
    if kind in ("tool_call", "tool"):
        name = str(detail.get("tool") or detail.get("action") or "工具")
        if compact:
            return f"调用「{name}」"
        params = _clip(detail.get("params"), 80)
        return f"调用「{name}」参数 {params}" if params else f"调用「{name}」"

    if kind == "tool_result":
        name = str(detail.get("tool") or detail.get("action") or "工具")
        if compact:
            return f"「{name}」返回"
        result = _clip(detail.get("result"), 80)
        if detail.get("ok") is False:
            reason = _clip(detail.get("error"), 80) or "没有返回结果"
            return f"「{name}」没成功：{reason}"
        return f"「{name}」返回：{result}" if result else f"「{name}」没有返回内容"

    if kind == "nickname":
        if detail.get("manual"):
            to = detail.get("to") or ""
            if detail.get("ok") is False:
                return f"重新读群名片失败：{_clip(detail.get('note'), 80)}"
            return f"重新读了她的群名片：「{to}」"
        if detail.get("base"):
            return f"记下了她原来的群名片「{detail.get('base')}」"
        to = detail.get("to") or ""
        if detail.get("ok") is False:
            return f"群名片没改成「{to}」：{_clip(detail.get('note'), 80)}"
        return f"群名片改成「{to}」"

    if kind == "vision":
        count = int(detail.get("images") or 0)
        suffix = f"（{count} 张）" if count else ""
        if compact:
            return f"图片内容{suffix}"
        if detail.get("ok") is False:
            return f"图片没看成{suffix}：{_clip(detail.get('detail'), 80)}"
        return f"图片内容{suffix}：{_clip(detail.get('detail'), 80)}"

    if kind in ("command", "command_call", "command_result"):
        line = _clip(detail.get("command"), 60)
        action = _clip(detail.get("action"), 20)
        head = f"执行指令「{line}」" + (f"（动作：{action}）" if action else "")
        if kind == "command_call" or compact:
            return head
        if detail.get("ok") is False:
            return f"{head}失败：{_clip(detail.get('error'), 60)}"
        result = _clip(detail.get("result"), 80)
        return f"{head}　→ {result}" if result else head

    if kind == "ext_fields":
        # 扩展自己声明的字段（主插件不解释内容，只记下"模型标了什么"）
        pairs = "、".join(
            f"{key}={_clip(value, 30) if not isinstance(value, list) else '/'.join(_clip(item, 12) for item in value)}"
            for key, value in (detail.get("fields") or {}).items()
        )
        return f"记下了扩展要的信息：{pairs}" if pairs else "记下了扩展要的信息"

    if kind == "poke":
        who = _clip(detail.get("name") or detail.get("target") or "对方", 20)
        if detail.get("ok") is False:
            return f"想戳 {who} 但没戳成：{_clip(detail.get('note'), 90)}"
        return f"戳了 {who} 一下"

    if kind == "search":
        queries = [str(item) for item in (detail.get("queries") or []) if str(item)]
        head = "正在联网搜索"
        # 这一行就靠查询词说明她在查什么，精简模式下也留着
        if queries:
            head += f"「{'、'.join(_clip(item, 20) for item in queries[:4])}」"
            if len(queries) > 4:
                head += " 等"
        return head

    if kind == "search_sources":
        count = int(detail.get("count") or 0)
        queries = [str(item) for item in (detail.get("queries") or []) if str(item)]
        head = f"查到 {count} 条可用来源"
        if queries:
            head = f"查了「{'、'.join(_clip(item, 20) for item in queries)}」：{count} 条来源"
        sources = [str(item) for item in (detail.get("sources") or []) if str(item)]
        if not sources:
            return head
        if compact:
            return f"{head}（{sources[0]}）"
        return head + "\n" + "\n".join(f"　　· {item}" for item in sources[:5])

    if kind == "search_digest":
        topic = _clip(detail.get("topic"), 30)
        head = (
            f"检索整理：{topic}｜材料 {int(detail.get('materials') or 0)} 条 / "
            f"{int(detail.get('material_chars') or 0)} 字"
        )
        if compact:
            return head
        preview = _clip(detail.get("preview"), 60)
        digest = _clip(detail.get("digest"), 120)
        if preview:
            head += f"\n　　材料开头：{preview}"
        if digest:
            head += f"\n　　压成：{digest}"
        return head

    raise KeyError(kind)
