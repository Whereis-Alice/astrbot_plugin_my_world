"""用大模型批量生成「地点」和「动作」。

只负责**出题、解析、校验**：生成结果一律先交给编辑器预览，用户改完、勾选之后才落库，
所以这里的函数都是纯的，方便单测。
"""

from __future__ import annotations

import json
import re
from typing import Any

from .models import ActionDef, NodeDef, ScheduleDef, ZoneDef

MAX_ACTIONS_PER_NODE = 5
MAX_NODES_PER_RUN = 5
DEFAULT_ACTIONS_PER_NODE = 3
DEFAULT_NODES_PER_RUN = 3
MAX_ZONES_PER_RUN = 4
DEFAULT_ZONES_PER_RUN = 2
MAX_SCHEDULES_PER_RUN = 6
DEFAULT_SCHEDULES_PER_RUN = 3
MAX_EVENTS_PER_RUN = 6
DEFAULT_EVENTS_PER_RUN = 3

DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
_ATTRS = ("energy", "loneliness", "curiosity", "affect", "boredom")
# 群名片里的地点文案：「在书房」这种，短到不会把昵称挤没
NODE_NICKNAME_CHARS = 5


def _node_nickname_text(value: Any, name: str) -> str:
    """地点自己的名片文案；模型没给就用「在<地点名>」兜底。"""

    text = " ".join(str(value or "").split()).strip()
    if not text:
        text = f"在{name}" if name else ""
    return text[:NODE_NICKNAME_CHARS]


def clamp_per_node(value: Any, default: int = DEFAULT_ACTIONS_PER_NODE) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(1, min(MAX_ACTIONS_PER_NODE, number))


def clamp_node_count(value: Any, default: int = DEFAULT_NODES_PER_RUN) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(1, min(MAX_NODES_PER_RUN, number))


def extract_items(text: str, key: str) -> tuple[list[Any], list[str]]:
    """从模型输出里取出数组：容忍 ```json 围栏、前后废话、以及 {"key": [...]} 包装。"""

    raw = str(text or "").strip()
    if not raw:
        return [], ["模型没有返回任何内容"]
    fenced = re.search(r"```(?:json)?\s*(.+?)```", raw, re.S)
    if fenced:
        raw = fenced.group(1).strip()
    payload: Any = None
    try:
        payload = json.loads(raw)
    except Exception:
        # 退一步：截取第一个 [ 到最后一个 ]（或 { 到 }）
        for opener, closer in (("[", "]"), ("{", "}")):
            start, end = raw.find(opener), raw.rfind(closer)
            if start >= 0 and end > start:
                try:
                    payload = json.loads(raw[start : end + 1])
                    break
                except Exception:
                    continue
    if payload is None:
        return [], ["模型输出不是合法 JSON"]
    if isinstance(payload, list):
        return payload, []
    if isinstance(payload, dict):
        items = payload.get(key)
        if isinstance(items, list):
            return items, []
        # 有的模型会返回单个对象
        if payload.get("id") or payload.get("name"):
            return [payload], []
    return [], [f"模型输出里没有 {key} 列表"]


def _slug(value: Any) -> str:
    text = re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower())
    return re.sub(r"_+", "_", text).strip("_")


def parse_generated_actions(
    text: str,
    *,
    node_ids: list[str],
    tool_names: set[str] | None = None,
    max_per_node: int = MAX_ACTIONS_PER_NODE,
) -> tuple[list[dict[str, Any]], list[str]]:
    """把模型输出解析成一串动作草稿。返回 (动作列表, 提示信息)。"""

    raw_items, problems = extract_items(text, "actions")
    if not raw_items:
        return [], problems
    tool_names = {str(name) for name in (tool_names or set())}
    known_nodes = {str(node) for node in node_ids}
    per_node: dict[str, int] = {}
    used_ids: set[str] = set()
    actions: list[dict[str, Any]] = []

    for index, item in enumerate(raw_items, 1):
        if not isinstance(item, dict):
            problems.append(f"第 {index} 条不是对象，已跳过")
            continue
        node_id = str(item.get("node_id") or item.get("node") or "").strip()
        if node_id and node_id not in known_nodes:
            problems.append(f"第 {index} 条指向了不存在的地点 {node_id}，已跳过")
            continue
        if not node_id:
            # 没写归属就落在第一个地点上；用户在预览里可以改
            node_id = node_ids[0] if node_ids else ""
        if not node_id:
            problems.append(f"第 {index} 条没有可归属的地点，已跳过")
            continue

        action_id = _slug(item.get("id") or item.get("name"))
        if not action_id or not _ID_RE.match(action_id):
            problems.append(f"第 {index} 条的 id「{item.get('id')}」不合法，已跳过")
            continue
        if action_id in used_ids:
            problems.append(f"动作 id 重复：{action_id}，已跳过")
            continue
        if per_node.get(node_id, 0) >= max(1, int(max_per_node)):
            problems.append(f"{node_id} 的动作超过上限，后面的已跳过")
            continue

        payload = dict(item)
        payload["id"] = action_id
        payload["name"] = str(payload.get("name") or action_id).strip()
        # 生成的动作一律先绑到它所属的地点；用户可以在预览里改成多个地点
        payload["scope"] = "node"
        payload["allowed_nodes"] = [node_id]
        payload["node_id"] = node_id
        payload.setdefault("category", "instant")
        payload.setdefault("target_type", "none")
        payload.setdefault("visible", True)
        payload.setdefault("enabled", True)
        payload.setdefault("interruptible", True)
        level = str(payload.get("llm_level") or "single")
        if level not in ("template", "single", "tool"):
            level = "single"
        if level == "tool":
            raw_tools = payload.get("tool_names")
            if not isinstance(raw_tools, list):
                raw_tools = [payload.get("tool_name")]
            wanted = [str(item or "").strip() for item in raw_tools]
            kept = [name for name in dict.fromkeys(wanted) if name in tool_names]
            dropped = [name for name in dict.fromkeys(wanted) if name and name not in tool_names]
            if not kept:
                # 工具不存在就降级成"让她自己说"，并提醒用户去挑一个工具
                level = "single"
                payload.pop("tool_name", None)
                payload.pop("tool_names", None)
                problems.append(
                    f"{action_id}：工具「{'、'.join(dropped) or '（没写）'}」在 AstrBot 里没注册，"
                    "已改成「让她自己说」"
                )
            else:
                payload["tool_names"] = kept
                payload["tool_name"] = kept[0]
                if dropped:
                    problems.append(
                        f"{action_id}：工具「{'、'.join(dropped)}」在 AstrBot 里没注册，已去掉"
                    )
        payload["llm_level"] = level
        payload.setdefault("during", {})
        payload.setdefault("params", {})
        payload.setdefault("preconditions", {})
        completed = payload.get("on_complete")
        if not isinstance(completed, dict):
            completed = {}
        completed.setdefault("trigger", "llm_followup")
        completed.setdefault("effects", {})
        completed.setdefault("effects_per_minute", {})
        completed["effects"] = _clean_effects(completed.get("effects"))
        completed["effects_per_minute"] = _clean_effects(
            completed.get("effects_per_minute")
        )
        payload["on_complete"] = completed
        # 「亲密程度」：只有**碰到人**的动作才有意义。模型写 0 或者没写都当"没提"，
        # 交给内置对照表 / 运行时分类去判——免得它顺手写个 0 把抱抱判成不亲密。
        raw_intimacy = payload.get("intimacy")
        if raw_intimacy in (None, "", 0, 0.0, "0"):
            payload.pop("intimacy", None)
        else:
            try:
                value = max(0.0, min(1.0, float(raw_intimacy)))
            except (TypeError, ValueError):
                payload.pop("intimacy", None)
            else:
                if value > 0:
                    payload["intimacy"] = value
                else:
                    payload.pop("intimacy", None)
        payload.pop("node", None)

        try:
            ActionDef.model_validate(payload)
        except Exception as exc:  # 结构不对就丢掉，别把脏数据带进配置
            problems.append(f"{action_id} 的结构不合法，已跳过（{exc}）")
            continue
        used_ids.add(action_id)
        per_node[node_id] = per_node.get(node_id, 0) + 1
        actions.append(payload)

    return actions, problems


def _clean_effects(effects: Any) -> dict[str, str]:
    """只保留认识的属性，值统一成 "+0.05" / "=0.5" / "mood:满足" 这种写法。"""

    if not isinstance(effects, dict):
        return {}
    cleaned: dict[str, str] = {}
    for key, value in effects.items():
        field = str(key).strip()
        if field == "mood":
            text = str(value).strip()
            cleaned["mood"] = text if text.startswith("mood:") else f"mood:{text}"
            continue
        if field not in _ATTRS:
            continue
        text = str(value).strip()
        if not text:
            continue
        if text[0] not in "+-=*×":
            text = f"+{text}"
        cleaned[field] = text
    return cleaned


def parse_generated_nodes(
    text: str,
    *,
    existing_ids: set[str] | None = None,
    max_nodes: int = MAX_NODES_PER_RUN,
) -> tuple[list[dict[str, Any]], list[str]]:
    """把模型输出解析成一串地点草稿（不给坐标，前端自动摆位）。"""

    raw_items, problems = extract_items(text, "nodes")
    if not raw_items:
        return [], problems
    used = {str(item) for item in (existing_ids or set())}
    nodes: list[dict[str, Any]] = []
    for index, item in enumerate(raw_items, 1):
        if len(nodes) >= max(1, int(max_nodes)):
            problems.append(f"超过一次最多 {max_nodes} 个地点的上限，后面的已跳过")
            break
        if not isinstance(item, dict):
            problems.append(f"第 {index} 条不是对象，已跳过")
            continue
        node_id = _slug(item.get("id") or item.get("name"))
        if not node_id or not _ID_RE.match(node_id):
            problems.append(f"第 {index} 条的 id「{item.get('id')}」不合法，已跳过")
            continue
        if node_id in used:
            problems.append(f"地点 id 重复：{node_id}，已跳过")
            continue
        payload = {
            "id": node_id,
            "name": str(item.get("name") or node_id).strip(),
            "prompt": str(item.get("prompt") or item.get("description") or "").strip(),
            "icon": str(item.get("icon") or "").strip(),
            "color": str(item.get("color") or "#8B7DD8").strip(),
            "atmosphere": item.get("atmosphere") if isinstance(item.get("atmosphere"), dict) else {},
            "preset_memories": item.get("preset_memories")
            if isinstance(item.get("preset_memories"), list)
            else [],
            "nickname_text": _node_nickname_text(
                item.get("nickname_text"), str(item.get("name") or node_id).strip()
            ),
            "x": 60,
            "y": 60,
        }
        try:
            NodeDef.model_validate({**payload, "zone_id": "tmp"})
        except Exception as exc:
            problems.append(f"{node_id} 的结构不合法，已跳过（{exc}）")
            continue
        used.add(node_id)
        nodes.append(payload)
    return nodes, problems


def auto_layout(
    existing: list[tuple[float, float]], count: int, *, step: int = 130
) -> list[tuple[float, float]]:
    """给新地点算一组不重叠的坐标：接在已有节点右边，一行两个。"""

    base_x = max((x for x, _ in existing), default=40)
    base_y = min((y for _, y in existing), default=60)
    positions: list[tuple[float, float]] = []
    for index in range(max(0, int(count))):
        column = index % 2
        row = index // 2
        positions.append((base_x + 60 + column * step, base_y + row * step))
    return positions


def link_plan(node_ids: list[str], *, anchor: str = "") -> list[tuple[str, str]]:
    """新地点的自动连线（方案 A）：第一个连到区域内最近的既有地点，之后依次串成链。"""

    if not node_ids:
        return []
    pairs: list[tuple[str, str]] = []
    if anchor:
        pairs.append((anchor, node_ids[0]))
    for index in range(len(node_ids) - 1):
        pairs.append((node_ids[index], node_ids[index + 1]))
    return pairs


def clamp_zone_count(value: Any, default: int = DEFAULT_ZONES_PER_RUN) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(1, min(MAX_ZONES_PER_RUN, number))


def clamp_schedule_count(value: Any, default: int = DEFAULT_SCHEDULES_PER_RUN) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(1, min(MAX_SCHEDULES_PER_RUN, number))


def clamp_event_count(value: Any, default: int = DEFAULT_EVENTS_PER_RUN) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(1, min(MAX_EVENTS_PER_RUN, number))


def parse_generated_zones(
    text: str,
    *,
    existing_ids: set[str] | None = None,
    max_zones: int = MAX_ZONES_PER_RUN,
) -> tuple[list[dict[str, Any]], list[str]]:
    """把模型输出解析成一串区域草稿（坐标由调用方自动摆位）。"""

    raw_items, problems = extract_items(text, "zones")
    if not raw_items:
        return [], problems
    used = {str(item) for item in (existing_ids or set())}
    zones: list[dict[str, Any]] = []
    homes: list[str] = []
    for index, item in enumerate(raw_items, 1):
        if len(zones) >= max(1, int(max_zones)):
            problems.append(f"超过一次最多 {max_zones} 个区域的上限，后面的已跳过")
            break
        if not isinstance(item, dict):
            problems.append(f"第 {index} 条不是对象，已跳过")
            continue
        zone_id = _slug(item.get("id") or item.get("name"))
        if not zone_id or not _ID_RE.match(zone_id):
            problems.append(f"第 {index} 条的 id「{item.get('id')}」不合法，已跳过")
            continue
        if zone_id in used:
            problems.append(f"区域 id 重复：{zone_id}，已跳过")
            continue
        payload = {
            "id": zone_id,
            "name": str(item.get("name") or zone_id).strip(),
            "note": " ".join(str(item.get("note") or item.get("description") or "").split()),
            "icon": str(item.get("icon") or "").strip(),
            "color": str(item.get("color") or "#7FB2E5").strip(),
            "is_home": bool(item.get("is_home")),
            "x": 40.0,
            "y": 60.0,
        }
        try:
            ZoneDef.model_validate(payload)
        except Exception as exc:
            problems.append(f"{zone_id} 的结构不合法，已跳过（{exc}）")
            continue
        if payload["is_home"]:
            homes.append(zone_id)
        used.add(zone_id)
        zones.append(payload)
    if len(homes) > 1:
        # 一个世界只认一个「家」：多出来的清掉，避免久待逻辑找不到北
        for zone in zones:
            if zone["id"] in homes[1:]:
                zone["is_home"] = False
        problems.append("生成了多个「家」，只保留第一个，其余已清掉")
    return zones, problems


def parse_generated_schedules(
    text: str,
    *,
    existing_ids: set[str] | None = None,
    action_ids: set[str] | None = None,
    node_ids: set[str] | None = None,
    max_schedules: int = MAX_SCHEDULES_PER_RUN,
) -> tuple[list[dict[str, Any]], list[str]]:
    """把模型输出解析成一串日程草稿；步骤只认当前世界真有的动作与地点。"""

    raw_items, problems = extract_items(text, "schedules")
    if not raw_items:
        return [], problems
    used = {str(item) for item in (existing_ids or set())}
    allowed_actions = {str(item) for item in (action_ids or set()) if str(item)}
    allowed_nodes = {str(item) for item in (node_ids or set()) if str(item)}
    schedules: list[dict[str, Any]] = []
    for index, item in enumerate(raw_items, 1):
        if len(schedules) >= max(1, int(max_schedules)):
            problems.append(f"超过一次最多 {max_schedules} 条日程的上限，后面的已跳过")
            break
        if not isinstance(item, dict):
            problems.append(f"第 {index} 条不是对象，已跳过")
            continue
        schedule_id = _slug(item.get("id") or item.get("name"))
        if not schedule_id or not _ID_RE.match(schedule_id):
            problems.append(f"第 {index} 条的 id「{item.get('id')}」不合法，已跳过")
            continue
        if schedule_id in used:
            problems.append(f"日程 id 重复：{schedule_id}，已跳过")
            continue
        steps: list[dict[str, Any]] = []
        for step in item.get("action_chain") or item.get("steps") or []:
            if not isinstance(step, dict):
                continue
            step_type = str(step.get("type") or "").strip()
            if step_type == "walk_to":
                target = str(
                    step.get("target_node") or step.get("target") or ""
                ).strip()
                if target and allowed_nodes and target not in allowed_nodes:
                    problems.append(f"{schedule_id}：地点 {target} 不存在，这一步已丢掉")
                    continue
                if not target:
                    problems.append(f"{schedule_id}：walk_to 没有目标，这一步已丢掉")
                    continue
                steps.append({"type": "walk_to", "target_node": target})
                continue
            if step_type and allowed_actions and step_type not in allowed_actions:
                problems.append(f"{schedule_id}：动作 {step_type} 不可用，这一步已丢掉")
                continue
            if not step_type:
                continue
            payload_step: dict[str, Any] = {"type": step_type}
            for key in ("intent", "content", "duration"):
                value = step.get(key)
                if value not in (None, "", 0):
                    payload_step[key] = value
            messages = step.get("messages")
            if isinstance(messages, list) and messages:
                payload_step["messages"] = [str(item) for item in messages if str(item)]
            steps.append(payload_step)
        if not steps:
            problems.append(f"{schedule_id}：一步可用的动作都没有，已跳过")
            continue
        days = [
            str(day).strip().lower()[:3]
            for day in (item.get("days") or [])
            if str(day).strip().lower()[:3] in DAY_KEYS
        ]
        payload = {
            "id": schedule_id,
            "enabled": True,
            "time": str(item.get("time") or "08:00").strip(),
            "days": list(dict.fromkeys(days)) or list(DAY_KEYS),
            "note": " ".join(str(item.get("note") or "").split()),
            "smart": bool(item.get("smart")),
            "action_chain": steps,
            "auto_travel": True,
        }
        try:
            ScheduleDef.model_validate(payload)
        except Exception as exc:
            problems.append(f"{schedule_id} 的结构不合法，已跳过（{exc}）")
            continue
        used.add(schedule_id)
        schedules.append(payload)
    return schedules, problems


def parse_generated_events(
    text: str,
    *,
    max_events: int = MAX_EVENTS_PER_RUN,
) -> tuple[list[dict[str, Any]], list[str]]:
    """把模型输出解析成一串事件草稿：``{title, text}``，投递时用 text。"""

    raw_items, problems = extract_items(text, "events")
    if not raw_items:
        return [], problems
    events: list[dict[str, Any]] = []
    for index, item in enumerate(raw_items, 1):
        if len(events) >= max(1, int(max_events)):
            problems.append(f"超过一次最多 {max_events} 件事的上限，后面的已跳过")
            break
        if isinstance(item, str):
            text_value = " ".join(item.split())
            if text_value:
                events.append({"title": text_value[:20], "text": text_value})
            continue
        if not isinstance(item, dict):
            problems.append(f"第 {index} 条不是对象，已跳过")
            continue
        body = " ".join(
            str(item.get("text") or item.get("event") or item.get("description") or "").split()
        )
        if not body:
            problems.append(f"第 {index} 条没有事件内容，已跳过")
            continue
        title = " ".join(str(item.get("title") or "").split()) or body[:20]
        events.append({"title": title[:40], "text": body[:300]})
    return events, problems
