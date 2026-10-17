"""用大模型批量生成「地点」和「动作」。

只负责**出题、解析、校验**：生成结果一律先交给编辑器预览，用户改完、勾选之后才落库，
所以这里的函数都是纯的，方便单测。
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

from .models import ActionDef, NodeDef, ScheduleDef, ZoneDef

MAX_ACTIONS_PER_NODE = 5
MAX_WORLD_ACTIONS_PER_RUN = 24
DEFAULT_WORLD_ACTIONS_PER_RUN = 8
# 一个区域里的地点上限：入口 / 门户类地点也占名额，所以别卡得太死。
MAX_NODES_PER_RUN = 8
DEFAULT_ACTIONS_PER_NODE = 3
DEFAULT_NODES_PER_RUN = 3
MAX_ZONES_PER_RUN = 4
DEFAULT_ZONES_PER_RUN = 2
MAX_SCHEDULES_PER_RUN = 6
DEFAULT_SCHEDULES_PER_RUN = 3
MAX_EVENTS_PER_RUN = 6
DEFAULT_EVENTS_PER_RUN = 3
# 一键重建整张地图：区域更多、每个区域自带地点，单独一套上限，避免和"增量生成"
# 的额度互相牵制。
MAX_MAP_ZONES = 6
MIN_MAP_ZONES = 2
DEFAULT_MAP_ZONES = 4
MAX_MAP_NODES_PER_ZONE = 8
MIN_MAP_NODES_PER_ZONE = 1
DEFAULT_MAP_NODES_PER_ZONE = 3

DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
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


def clamp_world_action_count(
    value: Any, default: int = DEFAULT_WORLD_ACTIONS_PER_RUN
) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    if number <= 0:
        number = default
    return max(1, min(MAX_WORLD_ACTIONS_PER_RUN, number))


def clamp_node_count(value: Any, default: int = DEFAULT_NODES_PER_RUN) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(1, min(MAX_NODES_PER_RUN, number))


def _strip_trailing_commas(text: str) -> str:
    """去掉对象 / 数组末尾多余的逗号（``{"a": 1,}`` / ``[1, 2,]``）。

    用一个小状态机跳过字符串内容，免得把正文里的 ``,}"`` 也改掉。
    """

    out: list[str] = []
    in_string = False
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == ",":
            look = index + 1
            while look < len(text) and text[look] in " \t\r\n":
                look += 1
            if look < len(text) and text[look] in "}]":
                index += 1
                continue
        out.append(char)
        index += 1
    return "".join(out)


def _balance_json(text: str) -> str:
    """把被截断的 JSON 补上结尾的 ``}`` / ``]``（字符串没收尾就别硬补）。"""

    stack: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append(char)
        elif char in "}]" and stack:
            stack.pop()
    if in_string or not stack:
        return text
    return text + "".join("}" if opener == "{" else "]" for opener in reversed(stack))


def parse_json_payload(text: str) -> tuple[Any, list[str]]:
    """把模型输出解析成 JSON 对象 / 数组：容忍围栏、前后废话、尾逗号与截断。"""

    raw = str(text or "").strip()
    if not raw:
        return None, ["模型没有返回任何内容"]
    fenced = re.search(r"```(?:json)?\s*(.+?)```", raw, re.S)
    if fenced:
        raw = fenced.group(1).strip()
    candidates = [raw]
    # 退一步：截取第一个 [ 到最后一个 ]（或 { 到 }）
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = raw.find(opener), raw.rfind(closer)
        if start >= 0 and end > start:
            sliced = raw[start : end + 1]
            if sliced not in candidates:
                candidates.append(sliced)
    payload: Any = None
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
            break
        except Exception:
            continue
    if payload is None:
        # 最后一招：尾逗号 / 被截断的括号。只在标准解析全失败时才动，
        # 修坏了也只是回到"不是合法 JSON"，不会把好数据改没。
        for candidate in candidates:
            repaired = _balance_json(_strip_trailing_commas(candidate))
            try:
                payload = json.loads(repaired)
                break
            except Exception:
                continue
    if payload is None:
        return None, ["模型输出不是合法 JSON"]
    return payload, []


def extract_items(text: str, key: str) -> tuple[list[Any], list[str]]:
    """从模型输出里取出数组：容忍 ```json 围栏、前后废话、以及 {"key": [...]} 包装。"""

    payload, problems = parse_json_payload(text)
    if payload is None:
        return [], problems
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


# 模型经常用近义写法填字面量字段；这里统一收口，别让一条小偏差把整个动作丢掉
_CATEGORY_ALIASES = {
    "instant": "instant",
    "one_shot": "instant",
    "瞬时": "instant",
    "瞬间": "instant",
    "continuous": "continuous",
    "sustained": "continuous",
    "持续": "continuous",
}
_TARGET_ALIASES = {
    "none": "none",
    "no": "none",
    "self": "none",
    "无": "none",
    "自己": "none",
    "user": "user",
    "person": "user",
    "someone": "user",
    "对方": "user",
    "别人": "user",
    "group": "group",
    "all": "group",
    "群": "group",
    "群聊": "group",
}
_TRIGGER_ALIASES = {
    "none": "none",
    "no": "none",
    "无": "none",
    "llm_followup": "llm_followup",
    "followup": "llm_followup",
    "follow_up": "llm_followup",
    "续说": "llm_followup",
    "说一句": "llm_followup",
    "schedule": "schedule",
    "日程": "schedule",
}


def _pick_literal(value: Any, aliases: dict[str, str], default: str) -> str:
    text = str(value or "").strip().lower()
    return aliases.get(text, default)


def parse_generated_actions(
    text: str,
    *,
    node_ids: list[str],
    tool_names: set[str] | None = None,
    max_per_node: int = MAX_ACTIONS_PER_NODE,
    existing_ids: set[str] | None = None,
    max_total: int = 0,
) -> tuple[list[dict[str, Any]], list[str]]:
    """把模型输出解析成一串动作草稿。返回 (动作列表, 提示信息)。"""

    raw_items, problems = extract_items(text, "actions")
    if not raw_items:
        return [], problems
    tool_names = {str(name) for name in (tool_names or set())}
    known_nodes = {str(node) for node in node_ids}
    per_node: dict[str, int] = {}
    used_ids: set[str] = {str(item) for item in (existing_ids or set())}
    actions: list[dict[str, Any]] = []

    for index, item in enumerate(raw_items, 1):
        if max_total and len(actions) >= int(max_total):
            problems.append(f"超过一次最多 {max_total} 个动作的上限，后面的已跳过")
            break
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
        category = _pick_literal(payload.get("category"), _CATEGORY_ALIASES, "instant")
        payload["category"] = category
        payload["target_type"] = _pick_literal(
            payload.get("target_type"), _TARGET_ALIASES, "none"
        )
        # duration_mode：模型给瞬间动作写 none / 空都见过，而 ActionDef 只认
        # fixed / llm。瞬间动作一律 fixed（时长清零），持续动作认不出就按 llm。
        mode = str(payload.get("duration_mode") or "").strip().lower()
        if mode not in ("fixed", "llm"):
            mode = "fixed" if category == "instant" else "llm"
        payload["duration_mode"] = mode
        if category == "instant":
            payload["duration"] = 0
            payload["duration_min"] = 0
            payload["duration_max"] = 0
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
        completed["trigger"] = _pick_literal(
            completed.get("trigger"), _TRIGGER_ALIASES, "llm_followup"
        )
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


def auto_layout_ring(
    count: int,
    *,
    center: tuple[float, float] | None = None,
    radius: float = 0.0,
) -> list[tuple[float, float]]:
    """把整张地图的区域摆成一个圆环。

    圆环比两列网格更适合一眼看全：任意两个相邻区域都隔着一段弧，
    自动连线时也更自然地连到隔壁，而不是在中间交叉成 X。
    """

    total = max(0, int(count))
    if total <= 0:
        return []
    chosen_radius = (
        float(radius) if radius > 0 else max(165.0, 80.0 + 22.0 * total)
    )
    center_x, center_y = (
        (float(center[0]), float(center[1]))
        if center
        else (chosen_radius + 70.0, chosen_radius + 70.0)
    )
    if total == 1:
        return [(center_x, center_y)]
    # 区域框宽约 104px；半径随数量增长，保证 6 个区域也不挤在一起。
    positions: list[tuple[float, float]] = []
    for index in range(total):
        angle = -math.pi / 2 + (2 * math.pi * index / total)
        positions.append(
            (
                round(center_x + chosen_radius * math.cos(angle), 1),
                round(center_y + chosen_radius * math.sin(angle), 1),
            )
        )
    return positions


#: 画布宽高比的可接受范围。窗口再极端也按边界处理，
#: 不然一个超宽屏能把地图拉成一条横线、一个竖屏能把它挤成一列。
ASPECT_MIN = 0.45
ASPECT_MAX = 3.0


def normalize_aspect(value: Any) -> float:
    """把前端传来的「画布宽 ÷ 画布高」收进可用范围；拿不准就当作正方形。"""

    try:
        ratio = float(value)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(ratio) or ratio <= 0:
        return 1.0
    return max(ASPECT_MIN, min(ASPECT_MAX, ratio))


def layout_frame(
    count: int, aspect: Any = None, *, unit: float = 240.0
) -> tuple[float, float]:
    """给 ``count`` 个节点算一块「够摆得下、形状跟画布一致」的画布。

    面积只跟节点数有关，宽高比跟着编辑器的画布走：宽窗口铺得宽、高窗口铺得高。
    生成完的地图因此能真正占满整块画布，而不是一条扁带贴在上半截、
    下面空出一大片。
    """

    try:
        total = max(1, int(count))
    except (TypeError, ValueError):
        total = 1
    # 每个节点按 unit × unit 的面积留位置，但整张地图至少有这么大方圆，
    # 免得只有两三个区域时挤成一小团、一放大就糊在一起。
    area = max(560.0 * 560.0, total * unit * unit)
    ratio = normalize_aspect(aspect)
    width = math.sqrt(area * ratio)
    return width, width / ratio


def grid_layout_nodes(
    count: Any,
    *,
    aspect: Any = None,
    step_x: float = 190.0,
    step_y: float = 150.0,
) -> list[tuple[float, float]]:
    """把区域里的地点排成一块网格，列数跟着画布宽高比走。

    每个区域的坐标系是独立的（只在它的区域地图里显示），所以这里不用力导：
    规规矩矩的网格更好认，也不会让新加的入口类地点飞到角落。
    宽画布多排几列、高画布多排几行，最后一行不满时居中，免得右下角空一块。
    """

    try:
        total = max(0, int(count))
    except (TypeError, ValueError):
        total = 0
    if total <= 0:
        return []
    ratio = normalize_aspect(aspect)
    columns = max(1, min(total, int(round(math.sqrt(total * ratio))) or 1))
    rows = math.ceil(total / columns)
    positions: list[tuple[float, float]] = []
    for index in range(total):
        row, column = divmod(index, columns)
        row_count = columns if row < rows - 1 else total - row * columns
        centered = (columns - row_count) * step_x / 2
        positions.append(
            (
                round(70.0 + column * step_x + centered, 1),
                round(70.0 + row * step_y, 1),
            )
        )
    return positions


def spring_layout_zones(
    zones: list[dict[str, Any]],
    edges: list[dict[str, Any]] | None = None,
    *,
    iterations: int = 180,
    ideal: float = 240.0,
    aspect: Any = None,
    seed: dict[str, tuple[float, float]] | None = None,
) -> dict[str, tuple[float, float]]:
    """按跨区路线摆放区域：有路线的区域互相靠近，没路线的互相推开。

    用圆环做确定性种子，再跑一套简化版 Fruchterman-Reingold：
    路线是弹簧，区域之间是斥力，``is_home`` 的区域额外往重心收一点。
    纯计算、无随机数，同一张地图每次得到的布局一致。

    ``aspect`` 是画布宽高比（宽 ÷ 高）：算完会把整张图铺到同形状的画框里，
    窗口多宽地图就多宽，不会剩半个屏幕。
    ``seed`` 是「这些区域现在就在这儿」：既有区域从原位起步、并留一点回位力，
    新区域从整张图外圈起步再被弹簧拉进合适的位置——增量生成时不会把
    用户手排好的地图整个翻掉。
    """

    total = len(zones)
    ids = [str(zone.get("id") or "") for zone in zones if str(zone.get("id") or "")]
    if total <= 0 or not ids:
        return {}
    anchors: dict[str, tuple[float, float]] = {}
    for zone_id, point in (seed or {}).items():
        if zone_id not in ids or not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        try:
            anchors[zone_id] = (float(point[0]), float(point[1]))
        except (TypeError, ValueError):
            continue
    if len(ids) == 1:
        return {ids[0]: (260.0, 260.0)}
    ring = auto_layout_ring(len(ids))
    pos = {zone_id: [ring[index][0], ring[index][1]] for index, zone_id in enumerate(ids)}
    if anchors:
        # 既有区域从原位起步；新区域围着既有地图的轮廓排一圈，
        # 再交给下面的力导收敛，比从远处硬拽过来稳得多。
        center_x = sum(point[0] for point in anchors.values()) / len(anchors)
        center_y = sum(point[1] for point in anchors.values()) / len(anchors)
        spread = max(
            (
                ((point[0] - center_x) ** 2 + (point[1] - center_y) ** 2) ** 0.5
                for point in anchors.values()
            ),
            default=ideal,
        )
        for zone_id, point in anchors.items():
            pos[zone_id] = [point[0], point[1]]
        fresh = [zone_id for zone_id in ids if zone_id not in anchors]
        radius = max(ideal, spread + ideal * 0.9)
        for index, zone_id in enumerate(fresh):
            angle = -math.pi / 2 + (2 * math.pi * index / max(1, len(fresh)))
            pos[zone_id] = [
                center_x + radius * math.cos(angle),
                center_y + radius * math.sin(angle),
            ]
    links: set[tuple[str, str]] = set()
    for edge in edges or []:
        if not isinstance(edge, dict):
            continue
        left = str(edge.get("from_zone") or "")
        right = str(edge.get("to_zone") or "")
        if left in pos and right in pos and left != right:
            links.add(tuple(sorted((left, right))))
    homes = {str(zone.get("id") or "") for zone in zones if zone.get("is_home")}
    homes &= set(ids)
    k = max(80.0, float(ideal))
    temperature = k * 0.9
    for _step in range(max(1, int(iterations))):
        disp = {zone_id: [0.0, 0.0] for zone_id in ids}
        for index, left in enumerate(ids):
            for right in ids[index + 1 :]:
                dx = pos[left][0] - pos[right][0]
                dy = pos[left][1] - pos[right][1]
                distance = max(1.0, (dx * dx + dy * dy) ** 0.5)
                force = (k * k) / distance
                ux, uy = dx / distance, dy / distance
                disp[left][0] += ux * force
                disp[left][1] += uy * force
                disp[right][0] -= ux * force
                disp[right][1] -= uy * force
        for left, right in links:
            dx = pos[left][0] - pos[right][0]
            dy = pos[left][1] - pos[right][1]
            distance = max(1.0, (dx * dx + dy * dy) ** 0.5)
            force = (distance * distance) / k
            ux, uy = dx / distance, dy / distance
            disp[left][0] -= ux * force
            disp[left][1] -= uy * force
            disp[right][0] += ux * force
            disp[right][1] += uy * force
        center_x = sum(point[0] for point in pos.values()) / len(ids)
        center_y = sum(point[1] for point in pos.values()) / len(ids)
        for zone_id in homes:
            disp[zone_id][0] += (center_x - pos[zone_id][0]) * 0.08
            disp[zone_id][1] += (center_y - pos[zone_id][1]) * 0.08
        # 手排过的区域只给一点点回位力：允许让路，但不跟着新区域乱跑。
        for zone_id, anchor in anchors.items():
            disp[zone_id][0] += (anchor[0] - pos[zone_id][0]) * 0.05
            disp[zone_id][1] += (anchor[1] - pos[zone_id][1]) * 0.05
        for zone_id in ids:
            dx, dy = disp[zone_id]
            length = max(1e-6, (dx * dx + dy * dy) ** 0.5)
            step = min(length, temperature)
            pos[zone_id][0] += (dx / length) * step
            pos[zone_id][1] += (dy / length) * step
        temperature *= 0.97
    xs = [point[0] for point in pos.values()]
    ys = [point[1] for point in pos.values()]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    width = max(1.0, max_x - min_x)
    height = max(1.0, max_y - min_y)
    frame_w, frame_h = layout_frame(len(ids), aspect, unit=ideal)
    # 两个轴各自铺满画框：地图的形状从此跟窗口一致，
    # 放大到填满画布时不会有半边空着，也不会把有内容的那半边裁掉。
    scale_x = frame_w / width
    scale_y = frame_h / height
    return {
        zone_id: (
            round((pos[zone_id][0] - min_x) * scale_x + 70.0, 1),
            round((pos[zone_id][1] - min_y) * scale_y + 70.0, 1),
        )
        for zone_id in ids
    }


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


def _zone_xy(zone: dict[str, Any]) -> tuple[float, float]:
    try:
        return float(zone.get("x") or 0), float(zone.get("y") or 0)
    except (TypeError, ValueError):
        return 0.0, 0.0


def _zone_distance(left: dict[str, Any], right: dict[str, Any]) -> float:
    lx, ly = _zone_xy(left)
    rx, ry = _zone_xy(right)
    return ((lx - rx) ** 2 + (ly - ry) ** 2) ** 0.5


PORTAL_HINTS = (
    "入口", "门口", "大门", "门前", "玄关", "门厅", "出口", "站台", "车站",
    "月台", "码头", "渡口", "港口", "桥头", "路口", "街口", "广场", "大厅",
    "前台", "走廊", "楼梯", "电梯", "通道", "院门", "门廊", "gate",
    "entrance", "exit", "station", "platform", "pier", "dock", "bridge",
    "crossing", "lobby", "hall",
)
PRIVATE_HINTS = (
    "厨房", "卧室", "浴室", "厕所", "洗手间", "书房", "衣柜", "床", "沙发",
    "餐桌", "private", "bedroom", "kitchen", "bathroom",
)


def _portal_score(node: dict[str, Any]) -> int:
    """这个地方像不像"跨区门户"：入口 / 门口 / 站台加分，私密房间减分。"""

    text = " ".join(
        str(node.get(key) or "") for key in ("id", "name", "prompt")
    ).lower()
    score = 0
    for hint in PORTAL_HINTS:
        if hint and hint in text:
            score += 2
    for hint in PRIVATE_HINTS:
        if hint and hint in text:
            score -= 3
    return score


def looks_like_portal(node: dict[str, Any]) -> bool:
    """给引擎做连线校验用：名字 / 描述像入口、门口、站台就算门户。"""

    return _portal_score(node) > 0


def _anchor_node(nodes: list[dict[str, Any]]) -> dict[str, Any] | None:
    """取区域里最适合当门户的地点（入口 / 门口 / 站台优先），没有地点就返回 None。"""

    usable = [item for item in (nodes or []) if isinstance(item, dict) and item.get("id")]
    if not usable:
        return None
    return max(
        usable,
        key=lambda item: (
            _portal_score(item),
            -float(item.get("y") or 0),
            -float(item.get("x") or 0),
        ),
    )


def connect_zone_graph(
    zones: list[dict[str, Any]],
    nodes_by_zone: dict[str, list[dict[str, Any]]],
    *,
    existing_edges: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """补全跨区连线，返回**新增**的连线，保证所有有地点的区域连成一片。

    已有连线会被当成图的边参与连通性判断；缺的那几刀按"离已经连上的片区最近的
    区域"补，拓扑稳定，也不会给同一对区域叠重复线。
    """

    by_id = {
        str(zone.get("id") or ""): zone
        for zone in (zones or [])
        if str(zone.get("id") or "")
    }
    usable = [zone_id for zone_id in by_id if nodes_by_zone.get(zone_id)]
    if len(usable) < 2:
        return []

    parent = {zone_id: zone_id for zone_id in usable}

    def find(zone_id: str) -> str:
        while parent[zone_id] != zone_id:
            parent[zone_id] = parent[parent[zone_id]]
            zone_id = parent[zone_id]
        return zone_id

    def union(left: str, right: str) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    for edge in existing_edges or []:
        if not isinstance(edge, dict):
            continue
        left = str(edge.get("from_zone") or "").strip()
        right = str(edge.get("to_zone") or "").strip()
        if left in parent and right in parent:
            union(left, right)

    root = usable[0]
    added: list[dict[str, Any]] = []
    used_edge_ids: set[str] = set()
    while True:
        main = [zone_id for zone_id in usable if find(zone_id) == find(root)]
        missing = [zone_id for zone_id in usable if find(zone_id) != find(root)]
        if not missing:
            break
        # 每次只接入「离已连通区域最近的那个区域」，接完立刻重算。
        # 一次把整批都接到同一个 root 上会得到放射状的线，交叉得很难看。
        best: tuple[float, str, str] | None = None
        for zone_id in missing:
            candidates = [item for item in main if nodes_by_zone.get(item)]
            if not candidates:
                continue
            target = min(
                candidates,
                key=lambda item: _zone_distance(by_id[zone_id], by_id[item]),
            )
            distance = _zone_distance(by_id[zone_id], by_id[target])
            if best is None or distance < best[0]:
                best = (distance, zone_id, target)
        if best is None:
            break
        distance, zone_id, target = best
        src_node = _anchor_node(nodes_by_zone.get(zone_id, []))
        dst_node = _anchor_node(nodes_by_zone.get(target, []))
        if not src_node or not dst_node:
            break
        ticks = max(1, int(round(distance / 150.0)))
        edge_id = f"z_{src_node['id']}_{dst_node['id']}"
        if edge_id in used_edge_ids:
            index = 2
            while f"{edge_id}_{index}" in used_edge_ids:
                index += 1
            edge_id = f"{edge_id}_{index}"
        used_edge_ids.add(edge_id)
        added.append(
            {
                "id": edge_id,
                "from_zone": zone_id,
                "from_node": str(src_node["id"]),
                "to_zone": target,
                "to_node": str(dst_node["id"]),
                "ticks": ticks,
                "bidirectional": True,
            }
        )
        union(zone_id, target)
    return added


def starter_node_for_zone(
    zone: dict[str, Any], *, used_ids: set[str] | None = None
) -> dict[str, Any]:
    """给没有地点的区域放一个可编辑的落脚点，跨区连线才有两端可以引用。"""

    zone_id = _slug(zone.get("id") or zone.get("name")) or "zone"
    name = str(zone.get("name") or zone_id).strip() or zone_id
    base_id = f"{zone_id}_entrance"
    node_id = base_id
    used = {str(item) for item in (used_ids or set())}
    index = 2
    while node_id in used:
        node_id = f"{base_id}_{index}"
        index += 1
    label = name if name.endswith(("入口", "门口", "大门", "站")) else f"{name}入口"
    return {
        "id": node_id,
        "name": label,
        "prompt": (
            f"「{name}」的门户入口，也是跨区路线进出这一带的地方；"
            "写成有标志性的门口、站口或玄关。"
        ),
        "icon": str(zone.get("icon") or "").strip(),
        "color": str(zone.get("color") or "#8B7DD8").strip(),
        "atmosphere": {},
        "preset_memories": [],
        "nickname_text": _node_nickname_text("", name),
        "x": 60.0,
        "y": 60.0,
    }


def clamp_zone_count(value: Any, default: int = DEFAULT_ZONES_PER_RUN) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(1, min(MAX_ZONES_PER_RUN, number))


def clamp_map_zone_count(value: Any, default: int = DEFAULT_MAP_ZONES) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    if number <= 0:
        number = default
    return max(MIN_MAP_ZONES, min(MAX_MAP_ZONES, number))


def clamp_map_nodes_per_zone(
    value: Any, default: int = DEFAULT_MAP_NODES_PER_ZONE
) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    if number <= 0:
        number = default
    return max(MIN_MAP_NODES_PER_ZONE, min(MAX_MAP_NODES_PER_ZONE, number))


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


def parse_generated_zone_pack(
    text: str,
    *,
    existing_zone_ids: set[str] | None = None,
    existing_node_ids: set[str] | None = None,
    max_zones: int = MAX_ZONES_PER_RUN,
    max_nodes_per_zone: int = MAX_MAP_NODES_PER_ZONE,
    add_starter: bool = True,
) -> dict[str, Any]:
    """解析「区域 + 每个区域的地点」这一层草稿。

    「生成区域」和「一键重建地图」共用同一套校验：区域 id、地点 id、
    每区地点数量都挡一遍；缺地点的区域按 ``add_starter`` 决定是否补落脚点。
    """

    payload, problems = parse_json_payload(text)
    empty = {"zones": [], "nodes": [], "problems": []}
    if not isinstance(payload, dict):
        empty["problems"] = problems or ["模型输出不是 JSON 对象"]
        return empty
    raw_zones = payload.get("zones")
    if not isinstance(raw_zones, list) or not raw_zones:
        empty["problems"] = [*problems, "模型输出里没有 zones 列表"]
        return empty

    zones, zone_problems = parse_generated_zones(
        json.dumps({"zones": raw_zones}, ensure_ascii=False),
        existing_ids=existing_zone_ids,
        max_zones=max_zones,
    )
    problems.extend(zone_problems)
    if not zones:
        empty["problems"] = problems
        return empty

    raw_by_zone: dict[str, dict[str, Any]] = {}
    for item in raw_zones:
        if not isinstance(item, dict):
            continue
        key = _slug(item.get("id") or item.get("name"))
        if key:
            raw_by_zone.setdefault(key, item)

    nodes: list[dict[str, Any]] = []
    used_nodes = {str(item) for item in (existing_node_ids or set())}
    for zone in zones:
        zone_id = str(zone.get("id") or "")
        raw = raw_by_zone.get(zone_id) or {}
        raw_nodes = raw.get("nodes")
        parsed, node_problems = parse_generated_nodes(
            json.dumps(
                {"nodes": raw_nodes if isinstance(raw_nodes, list) else []},
                ensure_ascii=False,
            ),
            existing_ids=used_nodes,
            max_nodes=max_nodes_per_zone,
        )
        problems.extend(node_problems)
        if not parsed and add_starter:
            parsed = [starter_node_for_zone(zone, used_ids=used_nodes)]
            problems.append(f"{zone_id} 没有可用地点，已自动补一个落脚点")
        for node in parsed:
            node["zone_id"] = zone_id
            nodes.append(node)
            used_nodes.add(str(node.get("id") or ""))

    return {"zones": zones, "nodes": nodes, "problems": problems}


def parse_generated_world_map(
    text: str,
    *,
    max_zones: int = MAX_MAP_ZONES,
    max_nodes_per_zone: int = MAX_MAP_NODES_PER_ZONE,
    known_nodes: dict[str, str] | None = None,
    known_zones: set[str] | None = None,
) -> dict[str, Any]:
    """解析"一键重建整张地图"的模型输出。

    只信任结构合规、id 合法、区域归属正确的部分；缺地点或漏连线的区域会在
    引擎里自动补落脚点与连通线，保证确认后拿到的地图一定能走通。

    ``known_nodes`` / ``known_zones`` 是**分块生成**时已有的地图：这样新区域
    连到已有区域的门户节点也能通过校验，同时保证新 id 不和旧的撞。
    """

    pack = parse_generated_zone_pack(
        text,
        existing_zone_ids=known_zones,
        existing_node_ids=set((known_nodes or {}).keys()),
        max_zones=max_zones,
        max_nodes_per_zone=max_nodes_per_zone,
    )
    zones = pack["zones"]
    nodes = pack["nodes"]
    problems = list(pack["problems"])
    empty = {"zones": zones, "nodes": nodes, "connections": [], "problems": problems}
    if not zones:
        return empty
    payload, _payload_problems = parse_json_payload(text)
    if not isinstance(payload, dict):
        return empty
    zone_ids = {str(zone.get("id") or "") for zone in zones} | {
        str(item) for item in (known_zones or set())
    }
    node_zone = {
        str(node.get("id") or ""): str(node.get("zone_id") or "") for node in nodes
    }
    node_zone.update(
        {str(key): str(value) for key, value in (known_nodes or {}).items()}
    )
    connections: list[dict[str, Any]] = []
    used_connections: set[tuple[str, str]] = set()
    raw_connections = payload.get("connections")
    if raw_connections is None:
        raw_connections = payload.get("zone_edges")
    for index, item in enumerate(
        raw_connections if isinstance(raw_connections, list) else [], 1
    ):
        if not isinstance(item, dict):
            problems.append(f"第 {index} 条跨区连线不是对象，已跳过")
            continue
        from_node = _slug(item.get("from_node") or item.get("from_point"))
        to_node = _slug(item.get("to_node") or item.get("to_point"))
        from_zone = _slug(item.get("from_zone") or "")
        to_zone = _slug(item.get("to_zone") or "")
        if from_node not in node_zone or to_node not in node_zone:
            problems.append(f"第 {index} 条跨区连线指向不存在的地点，已跳过")
            continue
        if not from_zone:
            from_zone = node_zone[from_node]
        if not to_zone:
            to_zone = node_zone[to_node]
        if from_zone not in zone_ids or to_zone not in zone_ids:
            problems.append(f"第 {index} 条跨区连线指向不存在的区域，已跳过")
            continue
        if node_zone[from_node] != from_zone or node_zone[to_node] != to_zone:
            problems.append(
                f"第 {index} 条跨区连线两端的地点不属于对应区域，已跳过"
            )
            continue
        if from_node == to_node:
            problems.append(f"第 {index} 条跨区连线的两端是同一个地点，已跳过")
            continue
        pair = tuple(sorted((from_node, to_node)))
        if pair in used_connections:
            problems.append(f"第 {index} 条跨区连线重复，已跳过")
            continue
        used_connections.add(pair)
        try:
            ticks = max(1, min(60, int(item.get("ticks") or 1)))
        except (TypeError, ValueError):
            ticks = 1
        connections.append(
            {
                "id": f"z_{from_node}_{to_node}",
                "from_zone": from_zone,
                "from_node": from_node,
                "to_zone": to_zone,
                "to_node": to_node,
                "ticks": ticks,
                "bidirectional": item.get("bidirectional") is not False,
            }
        )

    return {
        "zones": zones,
        "nodes": nodes,
        "connections": connections,
        "problems": problems,
    }


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
        # 一次性安排：跑完那一遍就删掉。日期只认 YYYY-MM-DD，写歪了就当循环日程，
        # 免得一条写错日期的一次性日程永远不会到来、也永远不会消失。
        once = bool(item.get("once") or item.get("one_off"))
        date_text = str(item.get("date") or "").strip()
        if once and not _DATE_RE.match(date_text):
            once, date_text = False, ""
            problems.append(
                f"{schedule_id}：写了一次性安排但日期看不懂（{item.get('date')!r}），"
                "已按每天循环处理"
            )
        payload = {
            "id": schedule_id,
            "enabled": True,
            "time": str(item.get("time") or "08:00").strip(),
            "days": [] if once else (list(dict.fromkeys(days)) or list(DAY_KEYS)),
            "once": once,
            "date": date_text if once else "",
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
        # 味道（好事 / 糟心事 / 怪事 / 日常）和发生在哪儿：给编辑器做标记，
        # 也让"这一批要什么味道"这个要求看得见有没有被落实。
        kind = str(item.get("kind") or "").strip().lower()
        if kind not in ("good", "bad", "weird", "daily"):
            kind = ""
        place = " ".join(str(item.get("place") or "").split())[:40]
        draft = {"title": title[:40], "text": body[:300]}
        if kind:
            draft["kind"] = kind
        if place:
            draft["place"] = place
        events.append(draft)
    return events, problems
