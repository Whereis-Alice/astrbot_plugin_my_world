"""把 AstrBot 里已经注册的工具批量转成「她会的动作」。

这个模块只有纯函数：喂进去工具元信息（名字 / 说明 / 参数 schema / 来源插件），
吐出来一批可以直接写进世界配置的动作草稿。它不碰 AstrBot，也不碰数据库，
所以编辑器、命令行和单测走的是同一套逻辑。

为什么要单独做一层：AstrBot 装了几十个插件、上百个工具时，
「给每个工具建一个动作」这件事手点一遍要花一个晚上。这里把它变成一次调用，
命名、分组、范围、参数提示全部按规则推出来，用户之后在动作库里再慢慢调。
"""

from __future__ import annotations

import re
import time
from typing import Any, Iterable

# 动作 id 的前缀：`tool_搜索工具` 一眼能看出是导入来的，也方便和内置动作区分
DEFAULT_ID_PREFIX = "tool_"
# 动作 id 里只留这些字符，别的（空格、emoji、中文标点）统一压成下划线
_ID_SAFE = re.compile(r"[^0-9a-zA-Z_]+")
# 名字切句用的分隔符：工具说明经常是「一句话；补充说明。」这种结构
_SENTENCE_SPLIT = re.compile(r"[。；;！!？?\n\r｜|]+")
# 说明切句（更宽松，句号也算）
_DESC_SPLIT = re.compile(r"(?<=[。！？!?;；])")

# 名字最多这么长（按字符算），超了截断。动作名是写给模型看的，
# 太长会把动作清单撑得很啰嗦，而且她本来也只需要看个大概。
NAME_MAX = 16
DESCRIPTION_MAX = 220

# 来源 → 分组名前缀（group_mode="source" 时用）
SOURCE_GROUP_LABELS: dict[str, str] = {
    "official": "官方工具",
    "plugin": "插件工具",
    "mcp": "MCP 工具",
}

# 说明里出现这些词时，说明这个工具会「自己把消息发出去」：
# 走它等于绕过本插件的回复管线（分段、表情包、发送前钩子都会失效），
# 所以默认跳过，但在返回值里说清楚为什么，用户想要也能手动加回来。
_SELF_SEND_HINTS = (
    "发送消息到",
    "直接发送",
    "把消息发给",
    "send a message",
    "send message to",
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def slugify(value: str, *, fallback: str = "tool") -> str:
    """把工具名压成动作 id 片段：只留字母数字下划线。"""

    text = str(value or "").strip()
    # 带命名空间的工具名（``某插件.tool_name``）只取最后一段
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    text = _ID_SAFE.sub("_", text).strip("_")
    text = re.sub(r"_{2,}", "_", text)
    if not text:
        return fallback
    if text[0].isdigit():
        text = f"t{text}"
    return text[:48]


def _clean_text(value: Any) -> str:
    """把说明压成一行：去换行、去多余空白。"""

    return " ".join(str(value or "").split())


def _first_sentence(text: str, *, limit: int = NAME_MAX) -> str:
    """取说明的第一句，作为动作名的候选。"""

    body = _clean_text(text)
    if not body:
        return ""
    # 说明经常以「从插件索引中搜索表情包候选；不要直接浏览…」开头，
    # 取分号前那一截就是干净的动作名
    piece = _SENTENCE_SPLIT.split(body, maxsplit=1)[0].strip()
    piece = piece.strip(" 　:：,，。.-—")
    if len(piece) > limit:
        piece = piece[:limit].rstrip(" 　,，。.、和与及")
    return piece


def humanize_tool_name(name: str, *, plugin: str = "") -> str:
    """工具名兜底成名字：``magpie_search_meme`` -> ``search meme``。

    工具名前面常常挂着插件自己的短名（``magpie_`` / ``demo_``），
    而那个词对"她会做什么"没有信息量，能认出来就去掉。
    """

    text = str(name or "").strip()
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    text = re.sub(r"^(?:astrbot_plugin_|tool_)+", "", text)
    first, sep, rest = text.partition("_")
    plugin_key = re.sub(r"^(?:astrbot_plugin_)+", "", str(plugin or "")).strip().lower()
    plugin_flat = plugin_key.replace("_", "").replace("-", "")
    if (
        sep
        and rest
        and len(first) >= 4
        and plugin_key
        and (first.lower() in plugin_key or first.lower() in plugin_flat)
    ):
        text = rest
    text = text.replace("_", " ").replace("-", " ")
    return " ".join(text.split())[:NAME_MAX] or str(name or "")


def guess_action_name(tool: dict[str, Any], *, naming: str = "auto") -> str:
    """按工具元信息猜一个动作名。

    ``naming`` 三档：
    - ``auto``（默认）：优先用说明的第一句，太短或没有就用工具名
    - ``description``：只用说明
    - ``tool``：只用工具名
    """

    name = str(tool.get("name") or "").strip()
    description = str(tool.get("description") or "")
    plugin = str(tool.get("plugin") or "")
    if naming == "tool":
        return humanize_tool_name(name, plugin=plugin)
    first = _first_sentence(description)
    if naming == "description":
        return first or humanize_tool_name(name, plugin=plugin)
    # auto：说明的那一句得有点信息量才配当名字（「搜索。」这种不要）
    if len(first) >= 2:
        return first
    return humanize_tool_name(name, plugin=plugin)


def guess_group(tool: dict[str, Any], *, mode: str = "source", fixed: str = "") -> str:
    """按来源把工具动作分组，方便动作库一大堆的时候扫得动。"""

    if mode == "none":
        return ""
    if mode == "fixed":
        return str(fixed or "").strip()
    source = str(tool.get("source") or "plugin").strip() or "plugin"
    if source == "official":
        return SOURCE_GROUP_LABELS["official"]
    plugin = str(tool.get("plugin") or "").strip()
    if source == "mcp":
        return f"{SOURCE_GROUP_LABELS['mcp']} · {plugin}" if plugin else SOURCE_GROUP_LABELS["mcp"]
    return f"{SOURCE_GROUP_LABELS['plugin']} · {plugin}" if plugin else "插件工具 · 其它"


def describe_action(tool: dict[str, Any], *, limit: int = DESCRIPTION_MAX) -> str:
    """动作说明：直接沿用工具自己的说明（那是工具作者写给人看的）。"""

    text = _clean_text(tool.get("description"))
    if not text:
        text = f"调用 {tool.get('name') or '工具'} 做点事情。"
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def param_rows(tool: dict[str, Any]) -> list[dict[str, Any]]:
    """把工具的 JSON Schema 参数摊成表格行，给编辑器和导入预览用。

    返回的每一行：``name`` / ``type`` / ``required`` / ``description`` /
    ``enum``（可选取值）/ ``default``（schema 里写的默认值）。
    """

    schema = tool.get("parameters")
    if not isinstance(schema, dict):
        return []
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return []
    required = {str(item) for item in (schema.get("required") or []) if str(item).strip()}
    rows: list[dict[str, Any]] = []
    for name, spec in properties.items():
        spec = spec if isinstance(spec, dict) else {}
        param_type = spec.get("type") or ("object" if "properties" in spec else "string")
        if isinstance(param_type, list):
            param_type = "/".join(str(item) for item in param_type)
        row: dict[str, Any] = {
            "name": str(name),
            "type": str(param_type),
            "required": str(name) in required,
            "description": _clean_text(spec.get("description"))[:160],
        }
        enum = spec.get("enum")
        if isinstance(enum, list) and enum:
            row["enum"] = [str(item) for item in enum[:12]]
        if "default" in spec:
            row["default"] = spec.get("default")
        rows.append(row)
    return rows


def looks_like_self_send(tool: dict[str, Any]) -> bool:
    """这个工具是不是「直接把消息发到会话」的那种（这类动作会被引擎跳过）。"""

    text = f"{_clean_text(tool.get('description')).lower()} {str(tool.get('name') or '').lower()}"
    return any(hint.lower() in text for hint in _SELF_SEND_HINTS)


def unique_action_id(base: str, taken: set[str]) -> str:
    """撞 id 就加后缀：``tool_xxx`` → ``tool_xxx_2``。"""

    candidate = base
    index = 2
    while candidate in taken:
        candidate = f"{base}_{index}"
        index += 1
    taken.add(candidate)
    return candidate


def existing_action_index(actions: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """工具名 -> 已经绑了它的动作（用来判断「已经导入过了」）。"""

    index: dict[str, dict[str, Any]] = {}
    for action in actions or []:
        if not isinstance(action, dict):
            continue
        if str(action.get("llm_level") or "") != "tool":
            continue
        names = action.get("tool_names")
        if not isinstance(names, list):
            names = [action.get("tool_name")] if action.get("tool_name") else []
        for name in names:
            key = str(name or "").strip()
            if key and key not in index:
                index[key] = action
    return index


def build_draft(
    tool: dict[str, Any],
    *,
    options: dict[str, Any],
    taken_ids: set[str],
    existing: dict[str, Any] | None = None,
    plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """单个工具 → 动作草稿。

    ``plan`` 是可选的人工 / 模型命名结果：``{"name": ..., "group": ...,
    "description": ...}``，有就优先用它。
    """

    tool_name = str(tool.get("name") or "").strip()
    plan = plan if isinstance(plan, dict) else {}
    naming = str(options.get("naming") or "auto")
    prefix = str(options.get("id_prefix") if options.get("id_prefix") is not None else DEFAULT_ID_PREFIX)
    scope = str(options.get("scope") or "global")
    node_ids = [str(item) for item in (options.get("node_ids") or []) if str(item).strip()]
    followup = options.get("followup")
    followup = True if followup is None else bool(followup)

    row = dict(existing) if isinstance(existing, dict) else {}
    base_id = row.get("id") if existing else None
    action_id = str(base_id or "").strip() or unique_action_id(
        f"{prefix}{slugify(tool_name)}", taken_ids
    )

    name = str(plan.get("name") or "").strip() or guess_action_name(tool, naming=naming)
    group = str(plan.get("group") or "").strip() or guess_group(
        tool,
        mode=str(options.get("group_mode") or "source"),
        fixed=str(options.get("group") or ""),
    )
    description = str(plan.get("description") or "").strip() or describe_action(tool)

    pinned: dict[str, Any] = {}
    pins = options.get("param_pins")
    if isinstance(pins, dict):
        raw_pins = pins.get(tool_name)
        if isinstance(raw_pins, dict):
            for key, value in raw_pins.items():
                if str(key).strip() and value not in ("", None):
                    pinned[str(key)] = {"type": "string", "value": str(value)}

    on_complete: dict[str, Any] = {"trigger": "llm_followup" if followup else "none"}
    if followup:
        on_complete["prompt_hint"] = (
            f"你刚用「{name}」做完了一件事。用你自己的话把结果讲给群里听一句："
            "别念参数、别念网址、别照抄工具返回的格式；没成功就直说，不要编。"
        )

    draft: dict[str, Any] = {
        "id": action_id,
        "name": name,
        "description": description,
        "group": group,
        "category": "instant",
        "llm_level": "tool",
        "scope": "node" if scope == "node" and node_ids else "global",
        "allowed_nodes": list(node_ids) if scope == "node" else [],
        "target_type": "none",
        "duration": 0,
        "duration_mode": "fixed",
        "interruptible": True,
        # 明确写 0：导入的工具动作不算肢体接触，别让引擎再花一次调用去猜
        "intimacy": 0,
        "preconditions": {},
        "params": pinned,
        "during": {},
        "on_complete": on_complete,
        "template": "",
        "desc_mode": "full",
        "tool_name": tool_name,
        "tool_names": [tool_name],
        "tool_fallbacks": [],
        "tool_mode": "sequence",
        "tool_flow": "simple",
        "visible": bool(options.get("visible", True)),
        "priority": int(options.get("priority", 5) or 5),
        "enabled": bool(options.get("enabled", True)),
        "builtin": False,
        "import_source": "tool_import",
        "created_at": int(row.get("created_at") or 0) or _now_ms(),
    }
    return draft


def build_drafts(
    tools: Iterable[dict[str, Any]],
    *,
    options: dict[str, Any] | None = None,
    existing_actions: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """批量生成动作草稿。

    返回：``{"drafts": [...], "warnings": [...], "skipped": [...],
    "already": [...], "updated": [...]}``——
    - ``drafts``：要新建 / 要覆盖的草稿（``update`` 模式会带上已有动作的 id）
    - ``already``：已经导入过、这次按策略跳过或更新的工具名
    - ``warnings``：人话提示（工具说明为空、参数多、直发消息工具…）
    - ``skipped``：这次一个都没导入的工具名
    """

    options = dict(options or {})
    naming = str(options.get("naming") or "auto")
    duplicate = str(options.get("duplicate") or "skip")
    skip_self_send = bool(options.get("skip_self_send", True))
    plans = options.get("plans") if isinstance(options.get("plans"), dict) else {}

    actions = [item for item in (existing_actions or []) if isinstance(item, dict)]
    taken_ids = {str(item.get("id") or "").strip() for item in actions if item.get("id")}
    by_tool = existing_action_index(actions)
    # 同一个工具名在同一份列表里出现两次时只处理一次（老配置里可能真有）
    seen_tools: set[str] = set()

    drafts: list[dict[str, Any]] = []
    warnings: list[str] = []
    skipped: list[str] = []
    already: list[str] = []
    updated: list[str] = []

    for raw in tools or []:
        tool = dict(raw) if isinstance(raw, dict) else {}
        name = str(tool.get("name") or "").strip()
        if not name:
            warnings.append("有一个工具没有名字，已跳过。")
            continue
        if name in seen_tools:
            continue
        seen_tools.add(name)
        if skip_self_send and looks_like_self_send(tool):
            warnings.append(
                f"「{name}」看起来是「直接把消息发到会话」的工具，"
                "用它等于绕过本插件的回复管线（分段、表情包、发送前钩子都会失效），"
                "已跳过。确实要用的话，在动作里手动把工具挂上就行。"
            )
            skipped.append(name)
            continue

        existing = by_tool.get(name)
        if existing is not None:
            already.append(name)
            if duplicate != "update":
                continue

        plan = plans.get(name) if isinstance(plans.get(name), dict) else None
        draft = build_draft(
            tool,
            options=options,
            taken_ids=taken_ids,
            existing=existing,
            plan=plan,
        )
        rows = param_rows(tool)
        if rows:
            required = [row["name"] for row in rows if row["required"]]
            draft["param_rows"] = rows
            draft["param_summary"] = (
                f"{len(rows)} 个参数"
                + (f"（必填 {'、'.join(required[:4])}{'…' if len(required) > 4 else ''}）" if required else "（都可选）")
            )
        else:
            draft["param_rows"] = []
            draft["param_summary"] = "不需要参数"
        if existing is not None:
            updated.append(name)
        if not str(tool.get("description") or "").strip():
            warnings.append(f"「{name}」没有写说明，动作名与说明是按工具名推的，记得改一下。")
        drafts.append(draft)

    return {
        "drafts": drafts,
        "warnings": warnings,
        "skipped": skipped,
        "already": already,
        "updated": updated,
        "naming": naming,
        "duplicate": duplicate,
    }


def merge_drafts(
    actions: Iterable[dict[str, Any]], drafts: Iterable[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """把草稿并进动作列表：同 id 的覆盖，新的追加。返回（新列表, 统计）。"""

    result = [dict(item) for item in (actions or []) if isinstance(item, dict)]
    index = {str(item.get("id") or ""): pos for pos, item in enumerate(result)}
    created = 0
    updated = 0
    for draft in drafts or []:
        draft = dict(draft) if isinstance(draft, dict) else {}
        action_id = str(draft.get("id") or "").strip()
        if not action_id:
            continue
        # 编辑器表格用的字段不写进配置里
        for key in ("param_rows", "param_summary"):
            draft.pop(key, None)
        if action_id in index:
            result[index[action_id]] = draft
            updated += 1
        else:
            index[action_id] = len(result)
            result.append(draft)
            created += 1
    return result, {"created": created, "updated": updated}


def summary_text(result: dict[str, Any]) -> str:
    """给前端 / 日志用的一句话总结。"""

    drafts = result.get("drafts") or []
    created = len([item for item in drafts if item.get("id") not in (result.get("existing_ids") or [])])
    updated = len(result.get("updated") or [])
    parts = []
    if created:
        parts.append(f"新增 {created} 个")
    if updated:
        parts.append(f"更新 {updated} 个")
    if result.get("skipped"):
        parts.append(f"跳过 {len(result['skipped'])} 个")
    return "、".join(parts) or "没有可导入的工具"
