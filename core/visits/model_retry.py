"""Bounded local recovery; neither a retry nor a wait proves a new visit fact."""
from __future__ import annotations

import copy
import math

from .decisions import RETRYABLE, WAITING

DELAYS = (30, 60)
WAIT_DELAYS = (30, 60, 120, 300)


def _state_checkpoints(visit: dict) -> dict:
    """Read bounded step counters, including journals with one legacy counter."""
    old = visit.get("state_retry") or {}
    if not isinstance(old, dict):
        return {}
    rows = old.get("checkpoints")
    if not isinstance(rows, dict):
        checkpoint = old.get("checkpoint", "tick")
        rows = {checkpoint: old} if isinstance(checkpoint, str) else {}
    return {key: {"attempts": row["attempts"], "next_at": row["next_at"]}
            for key, row in rows.items()
            if key in {"tick", "exchange"} and isinstance(row, dict)
            and type(row.get("attempts")) is int and 1 <= row["attempts"] <= len(DELAYS)
            and type(row.get("next_at")) in {int, float}
            and math.isfinite(row["next_at"]) and row["next_at"] >= 0}


def clear_state_read(visit: dict, *, checkpoint: str) -> bool:
    """A successful read clears only that step's failure streak."""
    rows = _state_checkpoints(visit)
    if rows.pop(checkpoint, None) is None:
        return False
    if rows:
        pending = max(rows, key=lambda key: rows[key]["next_at"])
        visit["state_retry"] = {**rows[pending], "checkpoint": pending, "checkpoints": rows}
    else:
        visit.pop("state_retry", None)
    return True


def defer_state_read(visit: dict, *, now: float, checkpoint: str) -> bool:
    """Keep each read checkpoint's streak across earlier successful prechecks."""
    rows = _state_checkpoints(visit)
    attempts = rows.get(checkpoint, {}).get("attempts", 0) + 1
    if attempts > len(DELAYS):
        return False
    next_at = now + DELAYS[attempts - 1]
    rows[checkpoint] = {"attempts": attempts, "next_at": next_at}
    visit["state_retry"] = {**rows[checkpoint], "checkpoint": checkpoint, "checkpoints": rows}
    visit["next_at"] = max(visit["next_at"], next_at)
    return True


def valid_retry(visit: dict, cursor: dict) -> bool:
    retry = visit.get("model_retry")
    if not isinstance(retry, dict) or retry.get("cursor") != cursor:
        return False
    attempts, next_at = retry.get("attempts"), retry.get("next_at")
    waiting = retry.get("waiting", False)
    failures = retry.get("failures", 0 if waiting else attempts)
    return (retry.get("phase") == cursor.get("phase")
            and retry.get("category") in RETRYABLE
            and type(waiting) is bool
            and type(attempts) is int and 1 <= attempts <= len(WAIT_DELAYS if waiting else DELAYS)
            and type(failures) is int and 0 <= failures <= len(DELAYS)
            and type(next_at) in {int, float} and math.isfinite(next_at) and next_at >= 0)


def defer_decision(visit: dict, cursor: dict, *, category: str, now: float, candidate=None,
                   waiting: bool = False) -> bool:
    """Unissued waits keep their phase; only unsuccessful inference spends recovery."""
    old = visit.get("model_retry") or {}
    previous = old if valid_retry(visit, cursor) else {}
    failures = previous.get("failures", 0 if previous.get("waiting") else previous.get("attempts", 0))
    if category not in RETRYABLE:
        return False
    if waiting:
        attempts = min(len(WAIT_DELAYS), (previous.get("attempts", 0) if previous.get("waiting") else 0) + 1)
        delay = WAIT_DELAYS[attempts - 1]
    else:
        failures += 1
        if failures > len(DELAYS):
            return False
        attempts, delay = failures, DELAYS[failures - 1]
    next_at = now + delay
    if next_at >= visit["expires_at"]:
        if not waiting or visit["expires_at"] <= now:
            return False
        next_at = visit["expires_at"]  # The existing lease ends the wait at its own clock.
    retry = {"phase": cursor["phase"], "cursor": copy.deepcopy(cursor), "attempts": attempts,
             "next_at": next_at, "category": category}
    if waiting:
        retry["waiting"] = True
    if failures:
        retry["failures"] = failures
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
