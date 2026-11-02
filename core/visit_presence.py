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


def visit_activity_text(visit: dict, *, now: float | None = None) -> str:
    """Only a jointly confirmed stage describes what she is doing now."""
    remote = visit.get("remote") or {}
    name = str(remote.get("name") or "朋友")[:60]
    scene = visit.get("scene") or {}
    outing = visit.get("outing") or {}
    if visit.get("outing_version") == 1 and not outing:
        return f"刚与{name}见面，正在商量接下来一起做什么"
    place = str(outing.get("place") or scene.get("place") or "约好的地方")[:80]
    activity = str(outing.get("activity") or scene.get("activity") or "一起待一会儿")[:120]
    if (outing.get("version") == 1 and now is not None
            and now >= float(outing.get("until") or 0)):
        return f"与{name}在{place}，刚一起{activity}，下一步还在商量"
    return f"与{name}在{place}，{activity}"


def presence_text(visit: dict, *, now: float | None = None) -> str:
    remote = visit.get("remote") or {}
    scene = visit.get("scene") or {}
    name = str(remote.get("name") or "另一位朋友")[:60]
    world = str(remote.get("world") or "友邻世界")[:80]
    outing = visit.get("outing") or {}
    if visit.get("outing_version") == 1 and not outing:
        location = (f"正在自己的世界接待来自{world}的{name}"
                    if visit.get("role") == "host" else f"已经来到{world}，与{name}见面")
        return location + "；接下来一起去哪里、做什么尚未共同确认，不能把邀请中的计划当作正在进行。"
    place = str(outing.get("place") or scene.get("place") or "约好的地方")[:80]
    activity = str(outing.get("activity") or scene.get("activity") or "一起待一会儿")[:120]
    stage_note = ""
    if outing.get("version") == 1 and now is not None:
        started, until = float(outing.get("started_at") or 0), float(outing.get("until") or 0)
        if now >= until:
            stage_note = "约好的这段活动时间已过，双方下一步尚未确认；没有额外的游玩结果可以补述。"
        elif started <= now < until:
            elapsed = max(0, int((now - started) / 60))
            stage_note = f"这段共同活动仍在进行，已经一起待了约{elapsed}分钟；不把计划中的结果说成已经发生。"
    if visit.get("role") == "host":
        return f"正在自己的世界接待来自{world}的{name}；在{place}，{activity}。{stage_note}"
    return f"正在{world}拜访{name}；在{place}，{activity}。{stage_note}自己的住处与原安排保留，归家后再接续。"
