"""Presentation-only keepsakes for confirmed visits; no model calls or private context.

The caller supplies the complete, locally confirmed log.  Data stays separate
from the Jinja template so record text can never become executable template code.
"""
from __future__ import annotations

from datetime import datetime, timezone as datetime_timezone, tzinfo
from functools import lru_cache
import math
from pathlib import Path
from typing import Any
import unicodedata
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CARD_WIDTH = 900
CARD_SCALE = 1.5
_TERMINAL = {"completed", "declined", "expired", "interrupted", "closed", "ended", "cancelled", "failed"}
_TEXT_TYPES = {"say": "speech", "invitation": "speech", "contact_reply": "speech",
               "contact_status": "status", "action": "intent", "action_plan": "intent",
               "experience": "experience", "result": "result"}
_LABELS = {"speech": "", "intent": "当时的打算", "experience": "当时的感受",
           "result": "发生的结果", "status": "通话前状态", "chronicle": "共同经历"}
_STATUS = {"completed": "已告别归家", "interrupted": "这次暂时中断", "expired": "这次相处已结束",
           "declined": "这次没有成行", "cancelled": "邀请已取消", "failed": "这次没有成行"}


@lru_cache(maxsize=1)
def visit_card_template() -> str:
    """Return the packaged, self-contained template for AstrBot html_render."""
    return (Path(__file__).resolve().parents[1] / "templates" / "visit_card.html").read_text("utf-8")


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _zone(value: str | tzinfo | None) -> tzinfo:
    if isinstance(value, tzinfo):
        return value
    try:
        return ZoneInfo(value or "Asia/Shanghai")
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return datetime_timezone.utc


def _date(value: Any, zone: tzinfo) -> datetime | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            return None
        return datetime.fromtimestamp(number / 1000 if number >= 1e12 else number, zone)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _parts(row: dict) -> list[dict[str, str]]:
    """Mirror the reader's evidence categories, including typed legacy composites."""
    kind = _text(row.get("kind"))
    category = _TEXT_TYPES.get(kind, "chronicle")
    spoken = category == "speech" or not kind
    neutral = bool(kind) and category in {"chronicle", "status"}
    parts: list[dict[str, str]] = []

    def add(part_type: str, value: Any) -> None:
        text = _text(value)
        if text and not any(part["type"] == part_type and part["text"] == text for part in parts):
            parts.append({"type": part_type, "text": text, "label": _LABELS[part_type]})

    if spoken:
        add("speech", row.get("say"))
    add(category, row.get("text"))
    if not spoken:
        add(category, row.get("say"))
    add(category if neutral else "intent", row.get("action"))
    add(category if neutral else "result", row.get("result"))
    return parts


def _identity(visit: dict, row: dict) -> tuple[str, str]:
    actor_id = _text(row.get("actor_id"))
    actors = [(side, actor) for side in ("local", "remote")
              if isinstance(actor := visit.get(side), dict)]
    def public_id(side: str, actor: dict) -> str:
        explicit = _text(actor.get("id"))
        if explicit:
            return explicit
        # Durable local actors have a short actor_id; log rows use the public
        # identity. The confirmed role supplies that identity without guessing
        # from a character's display name or an arbitrary suffix.
        role = _text(visit.get("role"))
        if side == "local" and role in {"host", "guest"}:
            return _text(visit.get("host_id" if role == "host" else "guest_id"))
        return ""

    exact = [(side, actor) for side, actor in actors if actor_id and public_id(side, actor) == actor_id]
    matches = exact or [(side, actor) for side, actor in actors if actor_id and actor.get("actor_id") == actor_id]
    if len(matches) != 1:
        legacy = _text(row.get("speaker")) or _text(row.get("name")) if not actor_id else ""
        return "unknown", "来源未确认" + (f" · {legacy}" if legacy else "")
    side, actor = matches[0]
    name = _text(actor.get("name")) or ("本方" if side == "local" else "对方")
    names = [_text(actor.get("name")) for _, actor in actors]
    if len(names) == 2 and names[0] and names[0] == names[1]:
        name = ("本方" if side == "local" else "对方") + f" · {name}"
    return side, name


def _units(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in text)


def _lines(text: str, width: int = 49) -> int:
    return sum(max(1, math.ceil(_units(line) / width)) for line in text.split("\n"))


def _entry_height(entry: dict) -> int:
    if entry["category"] == "route":
        return 76 + sum(48 + _lines(stop["place"] + " · " + stop["activity"], 62) * 27
                        for stop in entry["stops"])
    return 92 + sum(_lines(part["text"]) * (44 if part["type"] == "speech" else 39)
                    + (30 if part["label"] else 8) for part in entry["parts"])


def _continues(previous: dict, row: dict) -> bool:
    if row.get("node_id") and previous.get("node_id") and row["node_id"] != previous["node_id"]:
        return False
    seq, before = row.get("seq"), previous.get("seq")
    sequenced = type(seq) is int and seq > 0 and type(before) is int and before > 0
    if sequenced and seq != before + 1:
        return False
    times = [value if type(value) in {int, float} and math.isfinite(value) and value > 0 else 0
             for value in (row.get("at"), previous.get("at"))]
    if all(times) and abs(times[0] - times[1]) >= 3:
        return False
    return sequenced or all(times)


def _entries(visit: dict, zone: tzinfo) -> list[dict]:
    entries: list[dict] = []
    outing = visit.get("outing") if isinstance(visit.get("outing"), dict) else {}
    route = outing.get("route") if isinstance(outing.get("route"), list) else []
    stops = [{"place": _text(row.get("place")) or _text(row.get("location")),
              "activity": _text(row.get("activity")) or _text(row.get("title")), "index": index + 1}
             for index, row in enumerate(route) if isinstance(row, dict)
             and (_text(row.get("place")) or _text(row.get("location"))
                  or _text(row.get("activity")) or _text(row.get("title")))]
    if stops:
        entries.append({"category": "route", "side": "shared", "stops": stops,
                        "continued": False, "parts": [], "seq": None})
    logs = visit.get("log") if isinstance(visit.get("log"), list) else []
    moments: list[dict] = []
    can_continue = False
    for row in logs:
        if not isinstance(row, dict):
            can_continue = False
            continue
        kind = _text(row.get("kind"))
        if kind == "contact_status" or kind == "activity" and _text(row.get("text")).startswith(("开始相处：", "开始相处:")):
            can_continue = False
            continue
        parts = _parts(row)
        if not parts:
            can_continue = False
            continue
        category = "speech" if any(part["type"] == "speech" for part in parts) else parts[0]["type"]
        side, speaker = _identity(visit, row)
        date = _date(row.get("at"), zone)
        entry = {"category": category, "kind": kind,
                 "kind_class": kind if kind in {"arrival", "say", "invitation", "contact_reply", "action", "action_plan", "experience", "result", "activity", "event"} else "record",
                 "side": side,
                 "speaker": speaker, "initial": speaker.split(" · ")[-1][:1],
                 "time": date.strftime("%H:%M") if date else "", "day": date.strftime("%m.%d") if date else "",
                 "seq": row.get("seq"), "parts": parts, "continued": False,
                 "tag": {"arrival": "相见", "invitation": "约见电话", "contact_reply": "电话答复",
                         "activity": "一起做的事", "event": "途中小事", "result": "发生的结果"}.get(kind, "共同经历")}
        if kind == "arrival":
            # The system's stock arrival sentence is represented by the neutral
            # milestone, just as in the journal reader. Keep any actual detail.
            entry["parts"] = [part for part in parts if part["text"].rstrip("。！!")
                              != "双方接受邀请，开始在东道主世界相处"]
        previous = moments[-1] if moments else None
        if (can_continue and kind in {"action", "action_plan", "experience"} and side != "unknown" and previous
                and previous["category"] == "speech" and previous["side"] == side
                and kind not in previous["source_kinds"] and previous["source_row"].get("actor_id") == row.get("actor_id")
                and _continues(previous["source_row"], row)):
            previous["parts"].extend(parts)
            previous["source_kinds"].append(kind)
            previous["source_row"] = row
        else:
            entry.update(source_row=row, source_kinds=[kind])
            moments.append(entry)
        can_continue = True

    for moment in moments:
        moment.pop("source_row")
        moment.pop("source_kinds")
        # One complete keepsake: paragraphs and source moments need no page
        # boundaries. Let the renderer capture their natural full height.
        if moment["parts"] or moment["kind"] == "arrival":
            entries.append(moment)
    return entries


def build_visit_card_pages(visit: dict, *, timezone: str | tzinfo | None = None,
                           max_page_height: int = 5200) -> list[dict]:
    """Build one complete tall card from an ended visit's confirmed transcript.

    The list-shaped API and ``max_page_height`` remain compatible with callers;
    height is never a pagination or cropping boundary. Render the entire card.
    No invitations, private thoughts or unconfirmed plans outside ``log`` are read.
    """
    if not isinstance(visit, dict) or _text(visit.get("status")) not in _TERMINAL:
        return []
    zone = _zone(timezone)
    start = _date(visit.get("arrived_at") or visit.get("started_at") or visit.get("created_at"), zone)
    end = _date(visit.get("ended_at"), zone)
    local = visit.get("local") if isinstance(visit.get("local"), dict) else {}
    remote = visit.get("remote") if isinstance(visit.get("remote"), dict) else {}
    outing = visit.get("outing") if isinstance(visit.get("outing"), dict) else {}
    scene = visit.get("scene") if isinstance(visit.get("scene"), dict) else {}
    met = _date(visit.get("arrived_at"), zone) is not None or visit.get("contact_success") is True
    place = (_text(outing.get("place")) or _text(scene.get("place"))) if met else ""
    activity = (_text(outing.get("activity")) or _text(scene.get("activity"))) if met else ""
    interval = start.strftime("%H:%M") if start else "时刻未记"
    if end and start and end >= start:
        interval += " — " + (end.strftime("%H:%M") if end.date() == start.date() else end.strftime("%m.%d %H:%M"))
    duration = ""
    if end and start and end >= start:
        minutes = int((end - start).total_seconds() // 60)
        duration = f"{minutes // 60} 小时 {minutes % 60} 分钟" if minutes >= 60 else f"{minutes} 分钟"
    base = {"title": place or ("一次相见，留在这里" if met else "一次约见的记录"),
            "activity": activity, "local_name": _text(local.get("name")) or "本方",
            "remote_name": _text(remote.get("name")) or _text(visit.get("peer_name")) or "友邻",
            "date": start.strftime("%Y.%m.%d") if start else "日期未记", "interval": interval,
            "duration": duration, "status": _STATUS.get(_text(visit.get("status")), "这次相处已结束"),
            "end_time": end.strftime("%H:%M") if end else "", "end_reason": _text(visit.get("end_reason")),
            "width": CARD_WIDTH, "met": met}
    rows = _entries(visit, zone)
    if not rows:
        rows = [{"category": "chronicle", "kind": "empty", "kind_class": "empty", "side": "shared", "tag": "相见记录",
                 "seq": None, "time": "", "day": "", "continued": False,
                 "parts": [{"type": "chronicle", "label": "", "text": "这次相见没有留下逐句经过。" if met else "这次约见没有留下逐句经过。"}]}]
    return [{**base, "entries": rows, "estimated_height": 920 + sum(_entry_height(row) for row in rows),
             "page": 1, "pages": 1, "first": True, "last": True}]


def render_visit_card_html(page: dict) -> str:
    """Render a page for local browser screenshots; AstrBot already bundles Jinja2."""
    from jinja2 import Environment, StrictUndefined
    return Environment(autoescape=True, undefined=StrictUndefined).from_string(visit_card_template()).render(**page)
