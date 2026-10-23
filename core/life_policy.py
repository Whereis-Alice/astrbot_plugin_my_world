"""Shared, deterministic admission for new life work; no changes to character traits."""

from __future__ import annotations


def choose_life(
    state,
    *,
    now: float,
    critical: bool = False,
    schedule_minutes: int | None = None,
    sleep_requested: bool = True,
    persona_key: str | None = None,
) -> dict:
    choice = {"kind": "idle", "reason": "暂时没有必须开始的事，留一点自由时间", "at": now}
    if critical:
        choice.update(kind="event", reason="先处理眼前的重要事件，其他安排保留续作位置")
    elif (
        state.is_sleeping
        or state.state == "drowsy"
        or (sleep_requested and state.sleep_rhythm.get("decision") == "sleep")
    ):
        choice.update(kind="rest", reason="已经进入休息安排，新的生活事项等醒来再继续")
    elif schedule_minutes is not None and 0 <= schedule_minutes <= 10:
        choice.update(kind="schedule", reason="日程即将开始，暂不启动新的长事项")
    elif state.energy < 0.4 and not state.current_action:
        choice.update(kind="rest", reason="现在精力不足，先休息，保留事项进度")
    elif state.current_action or state.current_plan:
        choice.update(
            kind="continue",
            reason="先接续手上已经开始的事情",
            matter_id=str(
                (state.current_action or {}).get("matter_id")
                or (state.current_plan or {}).get("matter_id")
                or ""
            ),
        )
    else:
        candidates = [
            m
            for m in state.life_matters
            if m.get("status") in {"active", "working", "waiting", "blocked"}
            and m.get("blocker") != "evidence"
            and float(m.get("resume_at") or 0) <= now
            and (not m.get("persona_key") or m["persona_key"] == persona_key)
        ]
        if candidates:
            choice.update(
                kind="matter", reason="有适合接续的生活事项，在原有人设和当前条件下继续一小步"
            )
    return choice


def admit_life(state, kind: str, *, critical: bool = False, force: bool = False) -> dict:
    """One interruption policy for events, appointments, rest and free activity."""
    current = state.current_action or {}
    resting = state.is_sleeping or state.state == "drowsy"
    busy = bool(current or state.current_plan)
    if resting and not force:
        return {"allow": False, "interrupt": False, "reason": "正在休息，醒来后再安排"}
    if kind == "event" and not critical and busy:
        return {"allow": True, "interrupt": False, "reason": "普通经历随当前生活发生，保留手上的事情"}
    if busy and kind in {"matter", "free"}:
        return {"allow": False, "interrupt": False, "reason": "先接续已经开始的事情"}
    if busy and kind == "rest" and not force:
        return {"allow": False, "interrupt": False, "reason": "先收尾这一小步，再去休息"}
    if busy and current.get("type") == "walk_to" and kind == "rest":
        return {"allow": False, "interrupt": False, "reason": "先走完这一段路，再去休息"}
    return {"allow": True, "interrupt": busy and (force or critical or kind == "schedule"),
            "reason": {"event": "眼前的重要事件先处理", "schedule": "到约好的时间了，保存进度后赴约",
                       "rest": "需要休息，保存进度后再继续"}.get(kind, "有空闲，可以继续一小步")}
