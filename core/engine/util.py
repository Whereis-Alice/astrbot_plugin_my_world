"""引擎里的纯函数小工具与它们用的正则：洗话题、说人话的时长、工具参数整形。"""

from __future__ import annotations

import re
from typing import Any
from ..state import strip_mention_note


def _clean_search_topic(reply: Any) -> str:
    """把"想查什么"的那句回答洗干净；看起来不像话题（JSON、代码、空话）就用空串。"""

    text = " ".join(str(reply or "").split()).strip().strip("「」\"'。!！?？")
    if not text or len(text) > 40:
        return ""
    banned = ("{", "}", "query", "参数", "JSON", "json")
    if any(item in text for item in banned):
        return ""
    return text


def _echo_payload(
    event_type: str, detail: dict[str, Any], enabled: set[str]
) -> bool:
    """这条事件要不要发到群里。

    调试回显是为了补上"群里本来看不见的东西"（决定、耗时、工具调用、被跳过的动作），
    所以她自己说过的话不再重复发一遍：可见动作的文本已经作为普通消息发出去了，
    而想事情这类静默动作（visible=False）的文本从不出现在群里，照旧回显。
    """

    if event_type not in enabled:
        return False
    if event_type == "action":
        return not (bool(detail.get("visible")) and bool(detail.get("messages")))
    return True
_CN_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

_DURATION_PATTERN = re.compile(
    r"(?P<num>\d+(?:\.\d+)?|[零一二两三四五六七八九十]+|半)\s*"
    r"(?P<unit>个?\s*小时|个?\s*钟头|分钟|分)"
)

# 日程里读「什么时候」用的两条：相对时间（十分钟后）与口语钟点（晚上八点半）
_RELATIVE_WHEN = re.compile(
    r"(?P<num>\d+|[零一二两三四五六七八九十]+|半)\s*个?\s*"
    r"(?P<unit>小时|钟头|分钟|分)"
)
_SPOKEN_HOUR = re.compile(
    r"(?P<period>凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|夜里|半夜)?\s*"
    r"(?P<num>\d{1,2}|[零一二两三四五六七八九十]+)\s*(?:点|点钟)\s*"
    r"(?:(?P<half>半)|(?P<minutes>\d{1,2})\s*分?)?"
)


def _spoken_hour(text: str) -> tuple[int, int] | None:
    """「晚上八点」「下午三点半」→ (时, 分)；读不出来返回 None。"""

    match = _SPOKEN_HOUR.search(str(text or ""))
    if not match:
        return None
    value = _duration_number(match.group("num"))
    if value is None:
        return None
    hour = int(value)
    minutes = 30 if match.group("half") else int(match.group("minutes") or 0)
    period = str(match.group("period") or "")
    if period in ("下午", "傍晚", "晚上", "夜里") and hour < 12:
        hour += 12
    elif period == "中午" and hour < 11:
        hour += 12
    if not (0 <= hour <= 23) or not (0 <= minutes <= 59):
        return None
    return hour, minutes


def _duration_number(token: str) -> float | None:
    """时长里的数字：阿拉伯数字、汉字数字、还有「半」。"""

    text = str(token or "").strip()
    if not text:
        return None
    if text == "半":
        return 0.5
    try:
        return float(text)
    except ValueError:
        pass
    if text == "十":
        return 10.0
    if text.startswith("十") and len(text) > 1:
        return float(10 + _CN_DIGITS.get(text[1], 0))  # 十二 → 12
    if text.endswith("十") and text[:-1] in _CN_DIGITS:
        return float(_CN_DIGITS[text[:-1]] * 10)  # 二十 → 20
    if len(text) == 1 and text in _CN_DIGITS:
        return float(_CN_DIGITS[text])
    return None


def _guess_duration_seconds(text: str) -> int:
    """从她自己的话里读时长（「小睡三小时」「半小时」「20 分钟」），读不出返回 0。

    模型经常把"睡多久"写在 intent 或台词里，却忘了 duration 字段；这一层只做兜底，
    真正的上下限仍然由动作配置的 duration_min / duration_max 夹住。
    """

    body = str(text or "")
    if not body:
        return 0
    # 「一个半小时 / 一小时半」先单独认，免得被后面的「一小时」抢走
    if re.search(r"(?:一|1)\s*个?\s*半\s*(?:小时|钟头)", body) or re.search(
        r"(?:一|1)\s*个?\s*(?:小时|钟头)\s*半", body
    ):
        return 5400
    for match in _DURATION_PATTERN.finditer(body):
        value = _duration_number(match.group("num"))
        if value is None:
            continue
        unit = match.group("unit")
        per_unit = 3600 if ("小时" in unit or "钟头" in unit) else 60
        seconds = int(round(value * per_unit))
        if seconds > 0:
            return seconds
    return 0
_ARGUMENT_ERROR_MARKERS = (
    "required positional argument",
    "unexpected keyword argument",
    "missing 1 required",
    "required argument",
    "got an unexpected keyword",
    "positional argument",
    "参数",
)


def _looks_like_argument_error(error: Any) -> bool:
    """这次失败像不像"参数给少了 / 给错了"。

    很多第三方工具的 schema 写着参数可选，实现却必须要；调用当场报
    ``missing 1 required positional argument``。这种错值得带回去重新补一次参数。
    """

    text = str(error or "").lower()
    if not text:
        return False
    return any(marker in text for marker in _ARGUMENT_ERROR_MARKERS)


# 「工具自己坏了」的迹象：只有这些才计入熔断——参数写错是补参的问题，不算工具坏。
_BROKEN_TOOL_MARKERS = (
    "没有可调用的 handler",
    "没有可调用",
    "not callable",
    "no such tool",
    "tool not found",
    "找不到工具",
    "无法调用",
    "timeout",
    "timed out",
    "超时",
    "connection",
    "connect",
    "refused",
    "gateway",
    "502",
    "503",
    "500",
    "rate limit",
    "quota",
    "unauthorized",
    "401",
    "403",
)


def _looks_like_tool_broken(error: Any) -> bool:
    """这次失败像不像"工具本身不可用"（而不是参数/意图的问题）。"""

    text = str(error or "").lower()
    if not text:
        # 没有报错信息、也没返回内容：当成工具没给出结果，不计入熔断
        return False
    if _looks_like_argument_error(text):
        return False
    return any(marker in text for marker in _BROKEN_TOOL_MARKERS)


_SCHEMA_RESERVED_KEYS = {
    "type",
    "properties",
    "required",
    "title",
    "description",
    "additionalProperties",
    "$schema",
    "definitions",
    "$defs",
}


def normalize_param_schema(schema: Any) -> dict[str, Any]:
    """把各家工具写的参数描述统一成 ``{"properties": {...}, "required": [...]}``。

    AstrBot 里工具的参数定义至少有四种写法，真实插件里全都遇到过：

    - 标准 JSON Schema：``{"type": "object", "properties": {...}, "required": [...]}``；
    - 省掉外层、直接给键值表：``{"city": {"description": "地点"}}``；
    - 参数列表：``[{"name": "city", "description": "地点", "required": true}]``；
    - 属性值是字符串：``{"city": "地点，例如杭州"}``。

    统一之后，"哪些参数要填、哪些是必填"就有唯一答案了。
    """

    if isinstance(schema, list):
        properties: dict[str, Any] = {}
        required: list[str] = []
        for item in schema:
            if isinstance(item, str):
                properties[item] = {}
                continue
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("key") or "").strip()
            if not name:
                continue
            properties[name] = {
                "type": str(item.get("type") or "string"),
                "description": str(item.get("description") or ""),
            }
            if item.get("required"):
                required.append(name)
        return {"properties": properties, "required": required}

    if not isinstance(schema, dict):
        return {"properties": {}, "required": []}

    raw_properties = schema.get("properties")
    if not isinstance(raw_properties, dict):
        # 没有 properties 时，把顶层那些"不是 schema 关键字"的键当成参数表
        guessed = {
            str(key): value
            for key, value in schema.items()
            if key not in _SCHEMA_RESERVED_KEYS
        }
        raw_properties = guessed if guessed else {}

    properties = {}
    required = {str(name) for name in (schema.get("required") or []) if str(name)}
    for name, spec in raw_properties.items():
        key = str(name)
        if isinstance(spec, str):
            properties[key] = {"description": spec}
            continue
        if not isinstance(spec, dict):
            properties[key] = {}
            continue
        properties[key] = dict(spec)
        # 有的工具把"必填"写在属性自己身上
        if spec.get("required") is True:
            required.add(key)
    return {"properties": properties, "required": sorted(required)}


def render_param_text(schema: dict[str, Any]) -> str:
    """把工具自带的参数 schema 渲染成一行中文说明（提示词与编辑器共用）。"""

    normalized = normalize_param_schema(schema)
    properties = normalized.get("properties") or {}
    required = set(normalized.get("required") or [])
    if not properties:
        return "（这个工具不需要参数）"
    parts: list[str] = []
    for name, spec in properties.items():
        if not isinstance(spec, dict):
            spec = {}
        desc = str(spec.get("description") or "").strip()
        flag = "必填" if name in required else "可选"
        text = f"{name}（{flag}"
        if desc:
            text += f"，{desc}"
        text += "）"
        parts.append(text)
    return "、".join(parts)


def _as_float(value: Any, default: float = 0.0) -> float:
    """尽量转成 float，坏值退回默认值。"""

    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return number


def _summarize_batch(items: list[dict[str, Any]], *, limit: int = 240) -> str:
    """把一批群聊压成一条概览：「小明：123；你：来了来了」。

    她回应过的那批不再原样重复给模型，但也不能凭空消失——这一行就是下一轮的背景。
    """

    parts: list[str] = []
    for item in items or []:
        # 先切掉「［这条消息 @ 了：…］」：短消息（「蓝蓝你在吗」）按 40 字截断时
        # 会把整段注解带进来，再被 240 字砍成半个括号
        text = strip_mention_note(item.get("text") or "")
        if not text:
            continue
        who = "你" if item.get("is_self") else str(
            item.get("name") or item.get("user_id") or "有人"
        )
        parts.append(f"{who}：{text[:40]}")
    text = "；".join(parts)
    return text if len(text) <= limit else text[:limit] + "…"


def _signed(value: float) -> str:
    """把数值写成带正负号的增量（效果表里 "+0.08" / "-0.05" 才是"加减"）。"""

    return f"{float(value or 0.0):+.4f}"


def _clip_text(value: Any, limit: int = 200) -> str:
    """把任意值压成单行短文本，用于事件日志。"""

    if value is None:
        return ""
    text = str(value).strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def _clip_log(value: Any, limit: int = 1200) -> str:
    """写进日志页的正文：留长一些，并且**标明是被截断的**（免得以为工具只返回了这么点）。"""

    if value is None:
        return ""
    text = str(value).strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"…（完整 {len(text)} 字，日志里只留存前 {limit} 字）"


def _to_int_or_default(value: Any, default: int = 0) -> int:
    """能转成整数就用它（0 也算有效值），转不出来才用默认值。"""

    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _same_text(left: str, right: str) -> bool:
    """两段文本算不算"同一条消息"：忽略空白标点，也容忍一方被管线改写。"""

    def squeeze(text: str) -> str:
        return "".join(ch for ch in str(text or "").lower() if ch.isalnum())

    a = squeeze(left)
    b = squeeze(right)
    if not a or not b:
        return False
    if a == b:
        return True
    return len(min(a, b, key=len)) >= 4 and (a in b or b in a)


def ago_text(minutes: float) -> str:
    """把「多少分钟以前」说成人话：3 小时 / 2 天。"""

    minutes = max(0, int(minutes))
    if minutes >= 1440:
        return f"{minutes // 1440} 天"
    if minutes >= 60:
        return f"{minutes // 60} 小时"
    return f"{max(1, minutes)} 分钟"
