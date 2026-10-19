"""Shared, deterministic admission for new life work; no changes to character traits."""

from __future__ import annotations


def choose_life(
    state,
    *,
    now: float,
    critical: bool = False,
    schedule_minutes: int | None = None,
    sleep_requested: bool = True,
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
    elif any(m.get("status") == "working" and any(r.get("status") == "running" for r in m.get("executions", [])) for m in state.life_matters):
        current = next(m for m in state.life_matters if m.get("status") == "working" and any(r.get("status") == "running" for r in m.get("executions", [])))
        choice.update(kind="continue", reason="接续独立生活沙盒中的小步，不打断正常聊天", matter_id=current["id"])
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
            and not m.get("execution_review_required")
            and m.get("blocker") not in {"environment", "materials", "capability", "uncertain"}
            and float(m.get("resume_at") or 0) <= now
        ]
        if candidates:
            choice.update(
                kind="matter", reason="有适合接续的生活事项，在原有人设和当前条件下继续一小步"
            )
    return choice
