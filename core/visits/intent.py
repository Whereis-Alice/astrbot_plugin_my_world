"""Bounded personal wishes; never shared facts or executable activity controls."""
from __future__ import annotations

import math
from typing import Any


def clean_activity_intent(value: Any) -> dict[str, str]:
    """An optional private note cannot invalidate an otherwise legal choice."""
    if not isinstance(value, dict):
        return {}
    return {key: value[key].strip()[:limit] for key, limit in
            (("want", 160), ("enough", 160), ("feeling", 100))
            if isinstance(value.get(key), str) and value[key].strip()}


def _stage(visit: dict) -> int:
    stage = (visit.get("outing") or {}).get("stage", -1)
    return stage if type(stage) is int else -1


def _target(offer: dict) -> dict:
    return {key: offer.get(key, 0 if key == "travel_seconds" else "")
            for key in ("place", "activity", "minutes", "travel_seconds")}


def _accepted(row: dict, visit: dict) -> bool:
    activity = visit.get("outing") or {}
    target = row.get("target")
    if not isinstance(target, dict) or row.get("stage") != _stage(visit):
        return False
    start, until = activity.get("started_at"), activity.get("until")
    if any(type(value) not in {int, float} or not math.isfinite(value)
           for value in (start, until, row.get("at"), target.get("minutes"))):
        return False
    return (row["at"] <= start and target.get("place") == activity.get("place")
            and target.get("activity") == activity.get("activity")
            and target.get("travel_seconds", 0) == activity.get("travel_seconds", 0)
            and math.isclose(target["minutes"] * 60, until - start, abs_tol=0.000001))


def activity_intent_context(visit: dict) -> dict:
    """Select only this person's wish for the accepted stage or pending offer."""
    current = visit.get("activity_intent") or {}
    proposed = visit.get("activity_intent_proposal") or {}
    result = {}
    if isinstance(current, dict) and current.get("stage") == _stage(visit):
        result["current"] = clean_activity_intent(current)
    if isinstance(proposed, dict) and _accepted(proposed, visit) and not result.get("current"):
        result["current"] = clean_activity_intent(proposed)
    elif (isinstance(proposed, dict) and proposed.get("stage") == _stage(visit) + 1
          and _target(visit.get("outing_offer") or {}) == proposed.get("target")):
        result["pending_proposal"] = clean_activity_intent(proposed)
    return {key: value for key, value in result.items() if value}


def retain_activity_intent(visit: dict, value: Any, *, phase: str, now: float,
                           offer: dict | None = None) -> None:
    if phase not in {"propose", "receive", "turn"}:
        return
    note = clean_activity_intent(value)
    if not note:
        return
    stage = _stage(visit)
    key = "activity_intent_proposal" if offer else "activity_intent"
    current, proposed = visit.get("activity_intent") or {}, visit.get("activity_intent_proposal") or {}
    if (offer and isinstance(proposed, dict) and _accepted(proposed, visit)
            and not (isinstance(current, dict) and current.get("stage") == stage
                     and clean_activity_intent(current))):
        visit["activity_intent"] = {k: v for k, v in proposed.items() if k != "target"}
    binding = {"stage": stage + 1, "target": _target(offer)} if offer else {"stage": stage}
    previous = visit.get(key) or {}
    same = isinstance(previous, dict) and all(previous.get(k) == v for k, v in binding.items())
    # A just-accepted proposal belongs to this stage, not the previous one.
    if not offer and not same:
        previous = (activity_intent_context(visit).get("current") or {})
    content = {**(clean_activity_intent(previous) if same or not offer else {}), **note}
    if same and content == clean_activity_intent(previous):
        return
    visit[key] = {**content, **binding, "phase": phase, "at": now,
                  "log_seq": len(visit.get("log", []))}
