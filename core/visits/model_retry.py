"""Bounded local recovery; neither a retry nor a wait proves a new visit fact."""
from __future__ import annotations

import copy
import math

from .decisions import RETRYABLE, WAITING

DELAYS = (30, 60)


def valid_retry(visit: dict, cursor: dict) -> bool:
    retry = visit.get("model_retry")
    if not isinstance(retry, dict) or retry.get("cursor") != cursor:
        return False
    attempts, next_at = retry.get("attempts"), retry.get("next_at")
    return (retry.get("phase") == cursor.get("phase")
            and retry.get("category") in RETRYABLE
            and type(attempts) is int and 1 <= attempts <= len(DELAYS)
            and type(next_at) in {int, float} and math.isfinite(next_at) and next_at >= 0)


def defer_decision(visit: dict, cursor: dict, *, category: str, now: float, candidate=None) -> bool:
    old = visit.get("model_retry") or {}
    attempts = (old.get("attempts", 0) if valid_retry(visit, cursor) else 0) + 1
    if category not in RETRYABLE or attempts > len(DELAYS):
        return False
    next_at = now + DELAYS[attempts - 1]
    if next_at >= visit["expires_at"]:
        return False
    retry = {"phase": cursor["phase"], "cursor": copy.deepcopy(cursor), "attempts": attempts,
             "next_at": next_at, "category": category}
    if isinstance(candidate, dict):
        retry["candidate"] = copy.deepcopy(candidate)
    visit["model_retry"] = retry
    visit["next_at"] = max(visit["next_at"], next_at)
    return True


def visible_wait(visit: dict) -> dict:
    state = visit.get("state_retry") or {}
    if (type(state.get("attempts")) is int and 1 <= state["attempts"] <= len(DELAYS)
            and type(state.get("next_at")) in {int, float} and math.isfinite(state["next_at"])):
        return {"reason": "暂时无法确认当前生活状态，稍后继续", "attempts": state["attempts"],
                "next_at": state["next_at"]}
    retry = visit.get("model_retry") or {}
    if retry.get("category") not in WAITING or type(retry.get("attempts")) is not int:
        return {}
    return {"reason": WAITING[retry["category"]] + "，冷却后继续相处",
            "next_at": retry.get("next_at", 0), "attempts": retry["attempts"]}
