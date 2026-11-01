"""Read-only presence overlay; travelling never imports a foreign home map."""
from __future__ import annotations

from typing import Any


def presence_epoch(engine: Any, session_id: str) -> int:
    """A scope's persisted arrival generation, including already finished visits."""
    service = getattr(engine, "visits", None)
    getter = getattr(service, "presence_epoch", None)
    return int(getter(engine.state_key(session_id))) if callable(getter) else 0


def active_visit(engine: Any, session_id: str) -> dict | None:
    service = getattr(engine, "visits", None)
    getter = getattr(service, "active", None)
    if not callable(getter):
        return None
    visit = getter(engine.state_key(session_id))
    # An invitation reserves a turn, but has not taken her out of her life yet.
    if not isinstance(visit, dict) or visit.get("status") not in {"active", "finishing"}:
        return None
    return visit


def pause_intervals(engine: Any, session_id: str, start: float, end: float,
                    *, persona_key: str) -> list[tuple[float, float]]:
    """Read only retained, confirmed visit windows for this original life."""
    getter = getattr(getattr(engine, "visits", None), "pause_intervals", None)
    return getter(engine.state_key(session_id), start, end, persona_key=persona_key) if callable(getter) else []


def presence_text(visit: dict) -> str:
    remote = visit.get("remote") or {}
    scene = visit.get("scene") or {}
    name = str(remote.get("name") or "另一位朋友")[:60]
    world = str(remote.get("world") or "友邻世界")[:80]
    place = str(scene.get("place") or "约好的地方")[:80]
    activity = str(scene.get("activity") or "一起待一会儿")[:120]
    if visit.get("role") == "host":
        return f"正在自己的世界接待来自{world}的{name}；在{place}，{activity}。"
    return f"正在{world}拜访{name}；在{place}，{activity}。自己的住处与原安排保留，归家后再接续。"
