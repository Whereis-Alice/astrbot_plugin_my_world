"""Render execution order without promoting speech or plans into present facts."""
from __future__ import annotations

import copy
import math
from typing import Any


_SPEECH_ACTIONS = {"say", "share", "think", "poke"}
_ENDED = {"done", "completed", "skipped", "deferred", "cancelled", "interrupted", "expired"}


def _number(value: Any, default: float = 0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _index(value: Any, default: int = -1) -> int:
    if isinstance(value, bool):
        return default
    number = _number(value, -1)
    return int(number) if number >= 0 and number.is_integer() else default


def _instant_record(record: Any, definitions: dict, persona_key: str | None) -> dict:
    if not isinstance(record, dict):
        return {}
    if persona_key is not None and record.get("persona_key") and record["persona_key"] != persona_key:
        return {}
    definition = definitions.get(str(record.get("type") or ""))
    if (definition is None or not definition.enabled
            or (definition.category != "instant" and record.get("execution_kind") != "instant")
            or definition.llm_level != "single" or definition.id in _SPEECH_ACTIONS):
        return {}
    return dict(record)


def state_execution_view(state: Any, world: Any, *, persona_key: str | None = None, now: float = 0,
                         instant_action: dict | None = None) -> Any:
    """Sanitise only render fields in a shallow copy; never repair persisted life data."""
    view = copy.copy(state)
    definitions = world.action_map()
    stamp = _number(now)
    view.world_time = _index(getattr(state, "world_time", 0), 0)

    def own(record: dict) -> bool:
        return persona_key is None or not record.get("persona_key") or record["persona_key"] == persona_key

    action = getattr(state, "current_action", None)
    action = dict(action) if isinstance(action, dict) and own(action) else {}
    definition = definitions.get(str(action.get("type") or ""))
    duration = _index(action.get("duration_ticks", 1))
    elapsed = _index(action.get("elapsed_ticks", 0))
    if (definition is None or not definition.enabled or definition.idle
            or str(action.get("status") or "") in _ENDED or duration < 1 or elapsed < 0):
        action = {}
    else:
        action.update(duration_ticks=duration, elapsed_ticks=elapsed)
    view.current_action = action or None

    plan = getattr(state, "current_plan", None)
    plan = dict(plan) if isinstance(plan, dict) and own(plan) else {}
    steps = plan.get("steps")
    current = _index(plan.get("current_step", 0))
    deadline = _number(plan.get("expires_at", 0), -1)
    until = _index(plan.get("valid_until", 0))
    created = _number(plan.get("created_ts", 0), -1)
    expired = bool(deadline > 0 and stamp > 0 and stamp >= deadline)
    if not deadline and until > 0:
        expired = view.world_time >= until
    if (not isinstance(steps, (list, tuple)) or current < 0 or current >= len(steps)
            or deadline < 0 or until < 0 or created < 0 or (stamp > 0 and created > stamp)
            or expired or str(plan.get("status") or "") in _ENDED | {"paused", "abandoned"}):
        plan = {}
    else:
        # Keep indices stable: an invalid row cannot shift a later activity forward.
        plan["steps"] = [dict(step) if isinstance(step, dict) else {} for step in steps]
        for step in plan["steps"][current:]:
            if str(step.get("status") or "") in _ENDED:
                step.clear()
        plan["current_step"] = current
    view.current_plan = plan or None
    instant = _instant_record(instant_action, definitions, persona_key)
    if instant and plan and instant.get("from_plan") is True:
        identity = str(plan.get("id") or "")
        same = (str(instant.get("plan_id") or "") == identity if identity or instant.get("plan_id")
                else _index(instant.get("plan_created_at")) == _index(plan.get("created_at")))
        if (same and _index(instant.get("plan_index")) == current
                and plan["steps"][current].get("action") == instant.get("type")):
            plan["steps"][current].clear()  # Only presentation; the actual step remains pending until it succeeds.

    last = getattr(state, "last_plan", None)
    last = dict(last) if isinstance(last, dict) and own(last) else {}
    last["at"] = _index(last.get("at", 0), 0)
    last_steps = last.get("steps")
    last["steps"] = [dict(step) for step in last_steps if isinstance(step, dict)] if isinstance(last_steps, (list, tuple)) else []
    view.last_plan = last

    events = getattr(state, "recent_events", None)
    view.recent_events = []
    for event in events if isinstance(events, (list, tuple)) else []:
        if not isinstance(event, dict) or not own(event):
            continue
        detail = event.get("detail")
        at = _number(event.get("at")) or _number(event.get("created_at"))
        at = at if at > 0 and (not stamp or at <= stamp) else 0
        view.recent_events.append({**event, "detail": dict(detail) if isinstance(detail, dict) else {},
                                   "at": at, "created_at": at})
    idle = getattr(state, "idle_activity", None)
    idle = dict(idle) if isinstance(idle, dict) and own(idle) else {}
    until_tick = _index(idle.get("until_tick", 0))
    view.idle_activity = {**idle, "until_tick": until_tick} if until_tick > view.world_time else {}
    return view


def action_facts(state: Any, world: Any, *, persona_key: str | None = None, now: float = 0,
                 instant_action: dict | None = None) -> str:
    """Keep completed, active and pending personal activities distinct and bounded."""
    state = state_execution_view(state, world, persona_key=persona_key, now=now, instant_action=instant_action)
    now = _number(now)
    definitions = world.action_map()

    def label(record: dict) -> str:
        identity = str(record.get("type") or record.get("action") or "")
        definition = definitions.get(identity)
        # Tools have separate result evidence; completing their clock is not success.
        if identity in _SPEECH_ACTIONS or definition is None or definition.llm_level in {"tool", "command"}:
            return ""
        text = str(record.get("action_name") or definition.name or identity)
        if identity == "life_simulation":
            text = str(record.get("intent") or record.get("desc") or text)
        return " ".join(text.split())[:120]

    completed = []
    for event in state.recent_events or []:
        if str(event.get("kind") or "") not in {"action", "action_done"}:
            continue
        detail = event.get("detail") or {}
        if not isinstance(detail, dict) or not (name := label(detail)):
            continue
        status = str(detail.get("status") or "")
        if status and status not in {"done", "completed"}:
            continue
        definition = definitions.get(str(detail.get("type") or detail.get("action") or ""))
        if event.get("kind") == "action" and definition.category == "continuous" and not status:
            continue  # A legacy ambiguous activity record is not completion evidence.
        at = event["at"]
        # Old records still establish order; they must not masquerade as "just now".
        age = f"约 {max(0, int((now - at) / 60))} 分钟前" if at and now else "较早的执行记录"
        completed.append(f"{age}完成：{name}")

    action = state.current_action if isinstance(state.current_action, dict) else {}
    active = label(action) if action else ""
    plan = state.current_plan if isinstance(state.current_plan, dict) else {}
    current = int(plan.get("current_step") or 0)
    pending = []
    for index, step in enumerate((plan.get("steps") or [])[current:], current):
        if not isinstance(step, dict) or str(step.get("status") or "") in _ENDED:
            continue
        if action and index == current and step.get("action") == action.get("type"):
            continue
        if name := label(step):
            pending.append(name)

    lines = [*completed[-3:]]
    if active:
        lines.append("正在进行，尚未结束：" + active)
    instant = _instant_record(instant_action, definitions, persona_key)
    if instant and (name := label(instant)):
        lines.append("本轮正在做，结果尚未确认：" + name)
    if pending:
        lines.append("尚未开始的安排：" + " → ".join(pending[:3]))
    if not lines:
        return ""
    return ("# 自己行动的执行事实（以此核对当前自述）\n" + "\n".join(lines)
            + "\n旧活动结束不证明其附带状态已切换；设备开关、连接和身体姿态以明确执行或当前状态记录为准。"
            "尚未开始的事只能作为打算。场景陈设、旧台词和心里话都不证明此刻正在做什么。")
