"""Real and fictional-world calendars with explicit provenance.

The public ``holidays`` package is used when available.  A character still
gets a useful calendar without it, but dates are then marked as inferred
instead of pretending that a model knows a national calendar.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date
from typing import Any
from zoneinfo import ZoneInfo


WORLD_TYPES = {"real", "fanfiction", "original"}
EVENT_KINDS = {"festival", "vacation", "activity", "anniversary"}
EVENT_SOURCES = {"public_calendar", "model", "inferred", "manual"}
COUNTRY_RE = re.compile(r"^[A-Z]{2}$")


def normalize_countries(countries: list[str] | tuple[str, ...] | None) -> list[str]:
    """Return up to eight unique ISO-like country codes."""

    result: list[str] = []
    for item in countries or []:
        code = str(item or "").strip().upper()
        if COUNTRY_RE.fullmatch(code) and code not in result:
            result.append(code)
    return result[:8]


def stable_event_id(*parts: Any, prefix: str = "event") -> str:
    digest = hashlib.sha256(
        "\x1f".join(str(part or "") for part in parts).encode("utf-8")
    ).hexdigest()[:20]
    return f"{prefix}-{digest}"


def _country_calendar(country: str, year: int):
    import holidays

    try:
        return holidays.country_holidays(country, years=int(year), language="zh_CN")
    except NotImplementedError:
        # Not every country ships a Chinese translation. Public dates are more
        # useful in the package's default language than not appearing at all.
        return holidays.country_holidays(country, years=int(year))


def public_calendar_candidates(countries: list[str], year: int) -> tuple[list[dict[str, Any]], list[str]]:
    """Return public holidays without treating them as character activities."""

    candidates: list[dict[str, Any]] = []
    notes: list[str] = []
    try:
        import holidays
    except ImportError:
        return [], ["未安装 holidays；本次日历只能使用模型推定，日期不会伪装成公共日历"]
    for country in sorted(normalize_countries(countries)):
        try:
            calendar = _country_calendar(country, year)
            for day, name in sorted(calendar.items()):
                title = str(name or "").strip()
                if not title:
                    continue
                candidates.append(
                    {
                        "id": stable_event_id(country, day.isoformat(), title, prefix="public"),
                        "title": title[:160],
                        "start": day.isoformat(),
                        "end": day.isoformat(),
                        "kind": "festival",
                        "day_off": True,
                        "description": f"{country} 公共日历；只表示官方假日，不推断角色一定休息",
                        "source": "public_calendar",
                        "source_urls": [],
                    }
                )
        except Exception as exc:
            notes.append(f"{country} 公共日历不可用（{type(exc).__name__}）")
    return candidates, notes


def validate_calendar(
    raw: dict[str, Any],
    *,
    year: int,
    configured_type: str = "auto",
    configured_countries: list[str] | None = None,
    configured_notes: str = "",
    preferred_timezone: str = "",
    preferred_world_name: str = "",
    include_public: bool = True,
    max_events: int = 120,
    public_candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate a model calendar and add deterministic provenance."""

    if not isinstance(raw, dict):
        raise ValueError("日历必须是 JSON 对象")
    incoming_events = raw.get("events")
    if not isinstance(incoming_events, list):
        raise ValueError("日历 events 必须是列表；空日历需要明确返回 []")
    world_type = str(raw.get("world_type") or configured_type or "original").lower()
    if configured_type in WORLD_TYPES:
        world_type = configured_type
    if world_type not in WORLD_TYPES:
        world_type = "original"
    allowed_countries = normalize_countries(configured_countries)
    countries = allowed_countries or [
        str(item).upper() for item in (raw.get("countries") or [])
        if COUNTRY_RE.fullmatch(str(item).strip().upper())
    ]
    countries = normalize_countries(countries)
    timezone = str(
        preferred_timezone or raw.get("timezone") or "Asia/Shanghai"
    ).strip()[:80] or "Asia/Shanghai"
    try:
        ZoneInfo(timezone)
    except Exception:
        timezone = "Asia/Shanghai"
    public = list(public_candidates or [])
    public_keys = {
        (str(row.get("start") or ""), str(row.get("title") or ""))
        for row in public
        if isinstance(row, dict)
    }
    events: list[dict[str, Any]] = []
    seen_event_ids: set[str] = set()
    for incoming in incoming_events[: max(1, int(max_events))]:
        if not isinstance(incoming, dict):
            continue
        try:
            start = date.fromisoformat(str(incoming.get("start") or ""))
            end = date.fromisoformat(str(incoming.get("end") or start.isoformat()))
        except (TypeError, ValueError):
            continue
        if start > end or end.year < year or start.year > year:
            continue
        title = " ".join(str(incoming.get("title") or "").split())[:160]
        if not title:
            continue
        kind = str(incoming.get("kind") or "activity").lower()
        if kind not in EVENT_KINDS:
            kind = "activity"
        source = str(incoming.get("source") or "inferred").lower()
        if source not in {"public_calendar", "model", "inferred"}:
            source = "inferred"
        if source == "public_calendar" and (start.isoformat(), title) not in public_keys:
            source = "inferred"
        urls = [
            str(item)[:1500] for item in (incoming.get("source_urls") or [])
            if isinstance(item, str) and item.startswith("https://")
        ][:8]
        event_id = stable_event_id(
            start.isoformat(), end.isoformat(), title, source, prefix="event"
        )
        if event_id in seen_event_ids:
            continue
        seen_event_ids.add(event_id)
        events.append(
            {
                "id": event_id,
                "title": title,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "kind": kind,
                "day_off": bool(incoming.get("day_off", False)),
                "description": " ".join(str(incoming.get("description") or "").split())[:1000],
                "source": source,
                "source_urls": urls,
            }
        )
    if incoming_events and not events:
        raise ValueError("日历没有有效日期，保留重试机会")
    # In a real-world calendar, official public dates are facts. In fanfiction
    # and original worlds they remain candidates for the model to explicitly
    # adopt; otherwise a real holiday could leak into an unrelated setting.
    combined: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    public_rows = public if include_public and world_type == "real" else []
    for row in [*public_rows, *events]:
        key = (
            str(row.get("start") or ""),
            str(row.get("end") or ""),
            str(row.get("title") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        combined.append(dict(row))
        if len(combined) >= max(10, int(max_events)):
            break
    return {
        "year": int(year),
        "world_type": world_type,
        "world_name": " ".join(
            str(preferred_world_name or raw.get("world_name") or "").split()
        )[:160],
        "countries": sorted(set(countries)),
        "timezone": timezone,
        "culture_notes": " ".join(
            str(configured_notes or raw.get("culture_notes") or "").split()
        )[:2500],
        "events": sorted(combined, key=lambda row: (row["start"], row["title"])),
    }


def calendar_events_for_day(calendar: dict[str, Any] | None, day: str) -> list[dict[str, Any]]:
    if not isinstance(calendar, dict):
        return []
    result = []
    for row in calendar.get("events") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("start") or "") <= day <= str(row.get("end") or ""):
            result.append(dict(row))
    return result


def calendar_context_lines(calendar: dict[str, Any] | None, day: str) -> list[str]:
    rows = calendar_events_for_day(calendar, day)
    if not rows:
        return []
    world_type = str((calendar or {}).get("world_type") or "original")
    world_label = {
        "real": "现实世界",
        "fanfiction": "同人/原作世界",
        "original": "原创虚构世界",
    }.get(world_type, "角色世界")
    source_label = {
        "public_calendar": "公共日历事实",
        "model": "世界设定整理",
        "inferred": "依据设定的推定",
        "manual": "手动设置",
    }
    lines = [
        f"# 今天的日历与文化背景（{world_label}；是背景，不代表已经发生或她一定参加）"
    ]
    for row in rows[:12]:
        source = str(row.get("source") or "inferred")
        lines.append(
            f"- {row.get('title')}（{row.get('start')}"
            + (f"～{row.get('end')}" if row.get("end") != row.get("start") else "")
            + f"；{source_label.get(source, source)}）："
            + f"{str(row.get('description') or '')[:200]}"
        )
    return lines
