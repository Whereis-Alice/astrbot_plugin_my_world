"""Bound the wire, retaining stable sequence numbers and every local confirmed fact."""
from __future__ import annotations

import copy

from .protocol import digest

LOG_PAGE_SIZE = 192  # Worst-case Chinese text, actor IDs and route64 fit the 512 KiB envelope.
ROUTE_PAGE_SIZE = 64


def extended_history(visit: dict) -> bool:
    return type(visit.get("outing_version")) is int and visit["outing_version"] == 1 or visit.get("manual") is True


def write_page(target: dict, name: str, rows: list, size: int) -> None:
    offset = max(0, len(rows) - size)
    target[name] = copy.deepcopy(rows[offset:])
    target[name + "_offset"] = offset
    target[name + "_count"] = len(rows)
    target[name + "_prefix"] = digest(rows[:offset])


def read_page(local: list, source: dict, name: str, size: int, *, advancing: bool) -> list:
    rows = source.get(name, [])
    if not isinstance(rows, list) or len(rows) > size:
        raise ValueError("旅程同步页过长")
    keys = tuple(name + suffix for suffix in ("_offset", "_count", "_prefix"))
    if any(key in source for key in keys):
        offset, count, prefix = (source.get(key) for key in keys)
        if (type(offset) is not int or type(count) is not int or offset < 0
                or count != offset + len(rows) or offset > len(local)
                or not isinstance(prefix, str) or prefix != digest(local[:offset])):
            raise ValueError("旅程同步缺页或前缀不一致")
    else:
        offset, count = 0, len(rows)  # Pre-pagination saves and shorter first exchanges.
    overlap = min(len(local) - offset, len(rows))
    if rows[:overlap] != local[offset:offset + overlap] or advancing and count < len(local):
        raise ValueError("已确认经过不能被改写或回退")
    return copy.deepcopy(local[:offset] + rows)


def expand_snapshot(visit: dict, remote: dict) -> dict:
    """Reconstruct only from independently known prefix plus the authenticated new page."""
    if not extended_history(visit):
        return remote
    result = copy.deepcopy(remote)
    advancing = visit["role"] == "guest"
    if "log" in result:
        result["log"] = read_page(visit["log"], result, "log", LOG_PAGE_SIZE, advancing=advancing)
        for key in ("log_offset", "log_count", "log_prefix"):
            result.pop(key, None)
    if isinstance(result.get("outing"), dict):
        prior = (visit.get("outing") or {}).get("route", [])
        result["outing"]["route"] = read_page(prior, result["outing"], "route", ROUTE_PAGE_SIZE,
                                                advancing=advancing)
        for key in ("route_offset", "route_count", "route_prefix"):
            result["outing"].pop(key, None)
    return result
