"""Calendar bounds for explicit retrospective questions, without model calls."""
from __future__ import annotations

from datetime import datetime, timedelta
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def recall_day(query: str, now: float, timezone: str) -> tuple[float, float] | None:
    # Mixed retrospective/prospective questions have no single calendar scope.
    # Keep semantic recall available rather than assigning every clause to today.
    if any(word in query for word in ("上次", "那次", "以前", "之前", "上周", "上个月", "前几天", "打算", "计划", "明天")):
        return None
    # Several different days form a comparison, not a single-day filter.
    named = [(word, days) for word, days in (("今天", 0), ("今日", 0), ("昨天", 1), ("昨日", 1), ("前天", 2))
             if word in query]
    offsets = {days for _, days in named}
    dates = re.findall(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)", query)
    if len(offsets) + len(set(dates)) != 1:
        return None
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    try:
        day = (datetime.strptime(dates[0], "%Y-%m-%d").date() if dates else
               datetime.fromtimestamp(now, zone).date() - timedelta(days=next(iter(offsets))))
        start = datetime.combine(day, datetime.min.time(), zone)
        return start.timestamp(), (start + timedelta(days=1)).timestamp()
    except (ValueError, OverflowError, OSError):
        return None
