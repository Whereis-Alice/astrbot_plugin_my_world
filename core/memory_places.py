"""Keep a memory's map index separate from a visit's confirmed whereabouts."""
from __future__ import annotations

import math
from typing import Any, Mapping

from .context_sources import life_memory_metadata


def _text(value: Any, limit: int) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def visit_location_metadata(visit: dict) -> dict:
    """Retain accepted locations only after their recorded arrival time.

    A scene or an offer may still be a proposal. The local home node remains
    the durable map index, but does not describe where this encounter happened.
    """
    role = _text(visit.get("role"), 10)
    local, remote = visit.get("local") or {}, visit.get("remote") or {}
    host = local if role == "host" else remote if role == "guest" else {}
    result = {"version": 1, "role": role if role in {"host", "guest"} else "",
              "world": _text(host.get("world"), 80), "route": []}
    outing, expires = visit.get("outing"), visit.get("expires_at")
    if (role not in {"host", "guest"} or visit.get("outing_version") != 1 or not isinstance(outing, dict)
            or type(expires) not in {int, float} or not math.isfinite(expires)):
        return result
    from .visits.outings import valid_activity
    try:
        valid = valid_activity(outing, expires_at=expires)
    except (KeyError, TypeError, ValueError):
        valid = False
    if valid:
        def reached(stop: dict) -> bool:
            end = stop.get("ended_at", visit.get("ended_at"))
            # Older receipts may lack an ending. Preserve their existing
            # geography, but never treat a known future arrival as visited.
            return (type(end) not in {int, float} or not math.isfinite(end) or end <= 0
                    or stop["started_at"] <= end)

        result["route"] = [{"place": _text(stop.get("place"), 80),
                            "activity": _text(stop.get("activity"), 120),
                            "started_at": stop["started_at"]} for stop in outing["route"] if reached(stop)]
    return result


def memory_location(memory: Mapping[str, Any], node_map: Mapping[str, Any] | None = None) -> dict:
    """A readable location for the editor, recall and sleep review.

    Legacy visits cannot recover a host role or concrete place from dialogue.
    Recognise their explicit visit provenance without treating home_node as
    the occurrence location, and leave missing geography missing.
    """
    meta = life_memory_metadata(str(memory.get("source") or ""), memory.get("context"))
    if meta.get("kind") == "world_visit":
        saved = meta.get("visit_location")
        saved = saved if (isinstance(saved, dict) and saved.get("version") == 1
                          and _text(saved.get("role"), 10) in {"host", "guest"}) else {}
        world = _text(saved.get("world"), 80)
        stops = []
        route = saved.get("route")
        if isinstance(route, list):
            for stop in route:
                if isinstance(stop, dict) and (place := _text(stop.get("place"), 80)):
                    stops.append({"place": place, "activity": _text(stop.get("activity"), 120)})
        places = [stop["place"] for stop in stops]
        route = " → ".join(places[:4])
        if len(places) > 4:
            route += f" → …（共{len(places)}站）"
        label = (world + " · " if world else "") + (route or "串门 · 具体地点未记")
        return {"kind": "visit", "label": label, "world": world, "stops": stops,
                "peer_world": _text(meta.get("peer_world"), 80)}
    node_id = str(memory.get("node_id") or "")
    node = (node_map or {}).get(node_id)
    name = node.get("name") if isinstance(node, dict) else getattr(node, "name", "")
    label = str(name or node_id or "无地点")
    return {"kind": "node", "label": label, "world": "", "stops": []}


def retain_visit_location(previous: Any, current: Any) -> dict | None:
    """Preserve confirmed geography on delayed packets; reject a rewritten route."""
    old = previous if isinstance(previous, dict) and previous.get("version") == 1 else {}
    new = current if isinstance(current, dict) and current.get("version") == 1 else {}
    if not old:
        return new
    if not new:
        return old
    for key in ("role", "world"):
        if old.get(key) and new.get(key) != old[key]:
            return None
    route, before = new.get("route", []), old.get("route", [])
    if (not isinstance(route, list) or not isinstance(before, list)
            or len(route) < len(before) or route[:len(before)] != before):
        return None
    return new
