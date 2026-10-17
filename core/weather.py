"""天气：记录怎么存、"多久之前"怎么说、提示词与编辑器横幅共用的那一份数据。

天气是现实世界的属性，跟她所在的会话、地图区域都无关，所以整份记录全局共享，
存在 DB 的 kv 表里；提示词、横幅、静默刷新读的都是同一份。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Any

# kv 里的两个 key：一份是查到的结果，一份是"上次尝试是什么时候"（节流用）
WEATHER_KEY = "weather"
WEATHER_TRY_KEY = "weather.last_try"

# 归一化之后的字段顺序：城市｜温度｜天气｜湿度｜风力｜预报
PART_KEYS = ("city", "temp", "desc", "humidity", "wind", "forecast")
PART_SEP = "｜"
_PART_SEPARATORS = ("｜", "|")

# 工具名 / 说明里出现这些词，就当成「这是一把查天气的工具」。
# 主回复的工具调用已经交回 AstrBot 原生，她用的天气工具可能是任何插件装的
# （uapi_weather、get_weather、天气查询…），所以按名字和说明认，不写死工具名。
WEATHER_TOOL_HINTS = ("weather", "forecast", "天气", "氣象", "气象")

# 名字 / 说明里带这些词的多半是「分析、日报、总结」类工具：它们常在说明里
# 顺带提到"天气"，光凭一个词就认领会把群聊分析报告当成天气（见 #9）。
_WEATHER_TOOL_ANTI_HINTS = (
    "群聊",
    "聊天记录",
    "分析报告",
    "日报",
    "周报",
    "月报",
    "总结",
    "摘要",
    "画像",
    "活跃度",
    "发言排行",
    "统计",
)

# 工具参数里这些键最可能是城市。
_CITY_ARG_KEYS = (
    "city",
    "location",
    "place",
    "region",
    "area",
    "district",
    "城市",
    "地点",
    "地区",
)


def weather_tool_score(name: str, description: str = "") -> int:
    """这把工具像不像天气工具：名字里带 → 3 分，说明里带 → 1 分，像报告 → 0 分。

    0 表示"别认它"。挑工具时按分数从高到低排，避免随便撞上一个说明里
    提过"天气"的分析类工具就被当成天气源。
    """

    tool_name = str(name or "").lower()
    desc = str(description or "").lower()
    if not tool_name and not desc:
        return 0
    has_anti = any(word in f"{tool_name} {desc}" for word in _WEATHER_TOOL_ANTI_HINTS)
    if has_anti and not any(hint in tool_name for hint in WEATHER_TOOL_HINTS):
        # 名字本身没说天气、内容又是报告类的：不认
        return 0
    if any(hint in tool_name for hint in WEATHER_TOOL_HINTS):
        return 3
    if any(hint in desc for hint in WEATHER_TOOL_HINTS):
        return 1
    return 0


def looks_like_weather_tool(name: str, description: str = "") -> bool:
    """工具名或说明里带 weather / 天气 这类词，就认它是天气工具。"""

    return weather_tool_score(name, description) > 0


# 「这段文字到底像不像天气」：报告类的工具结果常常夹着"天气"两个字，
# 所以不看到温度/湿度这类带数字的线索就不当真。
_WEATHER_STRONG_RE = re.compile(
    r"-?\d{1,3}\s*(?:℃|°C|摄氏度|度)"
    r"|(?:气温|温度|体感)\s*[:：]?\s*-?\d"
)
_WEATHER_MEDIUM_RE = re.compile(
    r"(?:湿度|风力|风速|降水|降雨|空气质量|AQI|紫外线)"
    r"\s*[:：]?\s*[\d一二三四五六七八九十]"
)
_WEATHER_PHENOMENA = (
    "晴",
    "多云",
    "阴",
    "阵雨",
    "雷阵雨",
    "小雨",
    "中雨",
    "大雨",
    "暴雨",
    "雪",
    "雾",
    "霾",
    "沙尘",
    "冰雹",
)
_WEATHER_NOISE = ("群聊", "分析报告", "发言排行", "活跃度", "话题榜", "聊天记录", "总结报告")


def looks_like_weather_text(text: str) -> bool:
    """粗判一段工具结果像不像天气：带数字的温度 / 湿度是硬线索，报告类直接扣分。"""

    body = str(text or "")
    if not body.strip():
        return False
    score = 0
    if _WEATHER_STRONG_RE.search(body):
        score += 2
    if _WEATHER_MEDIUM_RE.search(body):
        score += 1
    if any(word in body for word in _WEATHER_PHENOMENA):
        score += 1
    if any(word in body for word in _WEATHER_NOISE):
        score -= 3
    return score >= 2


def city_from_tool_args(args: Any) -> str:
    """从工具参数里挑出城市；看不清就返回空串，绝不让调用方自己猜。"""

    if not isinstance(args, dict):
        return ""
    for key in _CITY_ARG_KEYS:
        for raw_key, raw_value in args.items():
            if str(raw_key).strip().lower() != key:
                continue
            text = " ".join(str(raw_value or "").split()).strip()
            if text:
                return text[:40]
    return ""


@dataclass
class WeatherRecord:
    """一次查到的天气。``at`` 是查到它的时刻——提示词里的"多久之前"按它算。"""

    text: str = ""
    parts: dict[str, str] = field(default_factory=dict)
    at: float = 0.0
    source: str = ""
    raw: str = ""
    images: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.line()

    def line(self) -> str:
        """给模型看的那一行：优先用归一化结果，没有就把字段拼回去。"""

        text = str(self.text or "").strip()
        return text or format_parts(self.parts)

    def to_payload(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "parts": {key: value for key, value in self.parts.items() if value},
            "at": float(self.at or 0.0),
            "source": self.source,
            "raw": self.raw,
            "images": list(self.images),
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "WeatherRecord":
        """读 kv 里的值；坏数据当没有，不要让编辑器页面跟着炸掉。"""

        if not isinstance(payload, dict):
            return cls()
        raw_parts = payload.get("parts")
        parts: dict[str, str] = {}
        if isinstance(raw_parts, dict):
            for key, value in raw_parts.items():
                text = str(value or "").strip()
                if text:
                    parts[str(key)] = text
        try:
            at = float(payload.get("at") or 0.0)
        except (TypeError, ValueError):
            at = 0.0
        images = payload.get("images")
        return cls(
            text=str(payload.get("text") or ""),
            parts=parts,
            at=at,
            source=str(payload.get("source") or ""),
            raw=str(payload.get("raw") or ""),
            images=[str(item) for item in images if str(item).strip()]
            if isinstance(images, list)
            else [],
        )


# 工具原文也常用 ``|`` 分隔，但字段又多又长；归一化那一行才有这个上限
_NORMALIZED_MAX_CHUNK = 24


def _normalized_shape(chunks: list[str], separator: str) -> bool:
    if len(chunks) < 2:
        return False
    if separator == "｜":
        # 归一化那一行允许缺字段（"内容里没提到的字段留空"），所以两段也认
        return True
    # 半角竖线：字段数对得上、每段都不长，才当成"其实已经归一化过"
    return 4 <= len(chunks) <= 6 and all(len(item) <= _NORMALIZED_MAX_CHUNK for item in chunks)


def parse_parts(text: str) -> dict[str, str]:
    """把 ``城市｜温度｜天气｜湿度｜风力｜预报`` 拆成字段；形状不对返回空字典。

    只认归一化过的那一行：工具原文经常也用 ``|`` 分隔一堆长字段，
    按下标硬套会把「今日温差：最低23℃~最高32℃」当成天气（见 #8）。
    """

    body = str(text or "").strip()
    separator = ""
    for candidate in _PART_SEPARATORS:
        if candidate in body:
            separator = candidate
            break
    if not separator:
        return {}
    chunks = [item.strip() for item in body.split(separator)]
    if not _normalized_shape(chunks, separator):
        return {}
    parts: dict[str, str] = {}
    for index, key in enumerate(PART_KEYS):
        value = chunks[index] if index < len(chunks) else ""
        if value:
            parts[key] = value
    return parts


_LABELED_CITY_RE = re.compile(r"(?:城市|地点|地区|位置|所在地)\s*[:：]\s*([^\s|｜,，;；]{2,16})")
_CITY_SUFFIX_RE = re.compile(r"([\u4e00-\u9fa5]{2,8}(?:市|区|县|州|省))")
_TEMP_LABEL_RE = re.compile(
    r"(?:气温|温度|实时温度|体感温度)\s*[:：]?\s*(-?\d{1,3}\s*(?:℃|°C|度))"
)
_TEMP_RE = re.compile(r"(-?\d{1,3}\s*(?:℃|°C|度))")
_DESC_LABEL_RE = re.compile(r"(?:实时天气|天气|状况|气况)\s*[:：]?\s*([^\s|｜,，;；]{1,10})")
_DESC_RE = re.compile("|".join(_WEATHER_PHENOMENA))
_HUMIDITY_RE = re.compile(r"湿度\s*[:：]?\s*(\d{1,3}\s*%)")
_WIND_RE = re.compile(r"(?:风力|风速|风向)\s*[:：]?\s*([^\s|｜,，;；]{1,16})")
_FORECAST_RE = re.compile(r"(?:预报|降水|降雨|未来[^|｜:：]{0,6})\s*[:：]?\s*([^|｜]{2,60})")
_PROVINCE_PREFIX_RE = re.compile(r"^[\u4e00-\u9fa5]{2,8}?省")


def _trim_city(text: str) -> str:
    """``广东省广州市`` → ``广州市``：横幅上不需要整条行政区划。"""

    body = str(text or "").strip()
    trimmed = _PROVINCE_PREFIX_RE.sub("", body, count=1)
    return trimmed or body


def extract_parts(text: str, city_hint: str = "") -> dict[str, str]:
    """从任意一段天气原文里挑字段；挑不到的留空，不编。

    工具返回的常常是带标签的一长串（``实时天气:多云|26℃(体感28℃)|湿度77%``），
    按下标套不进固定顺序，所以这里按标签和数值线索分别捞。
    """

    body = " ".join(str(text or "").split())
    if not body:
        return {}
    parts: dict[str, str] = {}

    city = ""
    match = _LABELED_CITY_RE.search(body)
    if match:
        city = match.group(1).strip()
    if not city:
        match = _CITY_SUFFIX_RE.search(body)
        if match:
            city = match.group(1)
    if not city:
        city = " ".join(str(city_hint or "").split())[:16]
    if city:
        parts["city"] = _trim_city(city)

    match = _TEMP_LABEL_RE.search(body) or _TEMP_RE.search(body)
    if match:
        parts["temp"] = match.group(1).strip()[:24]

    desc = ""
    match = _DESC_LABEL_RE.search(body)
    if match:
        desc = match.group(1).strip()
    if not desc or not _DESC_RE.search(desc):
        match = _DESC_RE.search(body)
        desc = match.group(0) if match else desc
    if desc:
        parts["desc"] = desc[:24]

    match = _HUMIDITY_RE.search(body)
    if match:
        parts["humidity"] = match.group(1).strip()

    match = _WIND_RE.search(body)
    if match:
        parts["wind"] = match.group(1).strip()

    match = _FORECAST_RE.search(body)
    if match:
        parts["forecast"] = match.group(1).strip()[:60]

    return parts


def parts_for(text: str, city_hint: str = "") -> dict[str, str]:
    """先按归一化那一行拆；拆不出来再从原文里挑标签。"""

    return parse_parts(text) or extract_parts(text, city_hint)


def format_parts(parts: dict[str, str]) -> str:
    """字段拼回一行（末尾的空字段不补分隔符）。"""

    values = [str(parts.get(key) or "").strip() for key in PART_KEYS]
    while values and not values[-1]:
        values.pop()
    return PART_SEP.join(values)


def age_text(at: float, now: float, *, tz: tzinfo | None = None) -> str:
    """多久之前：刚刚 / X 小时前 / 昨天 / 前天 / X 天前。

    按自然日算：同一天说"几小时前"，昨天前天直说，更久就说天数。
    """

    try:
        at_value = float(at or 0.0)
        now_value = float(now or 0.0)
    except (TypeError, ValueError):
        return ""
    if at_value <= 0 or now_value <= 0:
        return ""
    delta = max(0.0, now_value - at_value)
    try:
        day_now = datetime.fromtimestamp(now_value, tz).date()
        day_at = datetime.fromtimestamp(at_value, tz).date()
        days = (day_now - day_at).days
    except (OverflowError, OSError, ValueError):
        # 时间戳离谱时退回按 24 小时算，至少不会抛
        days = int(delta // 86400)
    if days <= 0:
        hours = int(delta // 3600)
        return "刚刚" if hours < 1 else f"{hours} 小时前"
    if days == 1:
        return "昨天"
    if days == 2:
        return "前天"
    return f"{days} 天前"


def prompt_block(
    record: WeatherRecord,
    now: float,
    *,
    stale_hours: float = 24.0,
    tz: tzinfo | None = None,
) -> str:
    """写进提示词的那一段；没有记录、或者旧到不值得提就返回空串。"""

    line = record.line()
    if not line:
        return ""
    at = float(record.at or 0.0)
    if at > 0 and stale_hours > 0 and (float(now) - at) > stale_hours * 3600:
        return ""
    age = age_text(at, now, tz=tz)
    head = f"外面的天气（{age}查的）：" if age else "外面的天气："
    return f"# 外面的天气\n{head}{line}"


def banner(record: WeatherRecord, now: float, *, tz: tzinfo | None = None) -> dict[str, Any]:
    """编辑器横幅要的数据（前端自己算"多久之前"，所以这里只给原始时间）。"""

    parts = dict(record.parts)
    return {
        "text": record.line(),
        "parts": parts,
        "city": parts.get("city", ""),
        "temp": parts.get("temp", ""),
        "desc": parts.get("desc", ""),
        "at": float(record.at or 0.0),
        "age": age_text(record.at, now, tz=tz),
        "source": record.source,
    }
