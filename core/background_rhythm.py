"""Accumulate routine model allowance across the local day without extra counters."""
from __future__ import annotations

from datetime import datetime, timedelta
from math import ceil, floor
from typing import Any

EXHAUSTED = "今日后台预算已用完，生活规则继续运行"
PACED = "后台预算按全天节奏释放，先留些余量给后续时段，生活规则继续运行"


def background_window(local: datetime, *, calls: int, tokens: int, enabled: bool) -> dict[str, Any]:
    """Allow one hour of startup room; unused allowance carries into later hours."""
    start = local.replace(hour=0, minute=0, second=0, microsecond=0, fold=0)
    begin, end = start.timestamp(), (start + timedelta(days=1)).timestamp()
    fraction = min(1., max(0., (local.timestamp() - begin + 3600) / (end - begin)))
    return {
        "start": begin, "end": end, "paced": enabled,
        "call_allowance": max(1, floor(calls * fraction)) if calls and enabled else calls,
        "token_allowance": max(1, floor(tokens * fraction)) if tokens and enabled else tokens,
    }


def background_admission(item: dict[str, Any], *, calls: int, tokens: int, reserve: int = 0,
                         window: dict[str, Any] | None = None) -> dict[str, Any]:
    """The store uses this inside its transaction; preflight uses it read-only."""
    spent_calls = int(item.get("calls") or 0)
    held_tokens = int(item.get("tokens") or 0) + int(item.get("reserved") or 0)
    result = {**item, "allowed": True, "reason": "", "paced": False, "next_at": 0.}
    if (calls and spent_calls >= calls) or (tokens and held_tokens + reserve > tokens
                                            or tokens and not reserve and held_tokens >= tokens):
        return {**result, "allowed": False, "reason": EXHAUSTED,
                "next_at": float((window or {}).get("end") or 0)}
    if not window or not window["paced"]:
        return result
    retry = []
    duration = window["end"] - window["start"]
    if calls and spent_calls >= window["call_allowance"]:
        retry.append(window["start"] + (spent_calls + 1) / calls * duration - 3600)
    if tokens and (held_tokens + reserve > window["token_allowance"]
                   or not reserve and held_tokens >= window["token_allowance"]):
        retry.append(window["start"] + (held_tokens + max(1, reserve)) / tokens * duration - 3600)
    if retry:
        return {**result, "allowed": False, "reason": PACED, "paced": True,
                # Stay just beyond a fractional threshold: float rounding must
                # not reschedule the exact same already-reached release time.
                "next_at": min(window["end"], (ceil(max(retry) * 1000) + 1) / 1000)}
    return result
