"""Bounded local recovery; neither a retry nor a wait proves a new visit fact."""
from __future__ import annotations

import copy
import math

from .decisions import FORMAT_ISSUES, RETRYABLE, WAITING

DELAYS = (30, 60)
WAIT_DELAYS = (30, 60, 120, 300)
ACTIVE_DELAYS = DELAYS + (300, 600, 900)


def recovery_basis(visit: dict, cursor: dict) -> dict:
    """Only confirmed protocol progress and new attributed experiences may wake a pause."""
    return {"cursor": copy.deepcopy(cursor),
            "local_experience_seq": visit.get("local_experience_attention_seq", 0),
            "remote_experience_seq": visit.get("remote_experience_seq", 0)}


def _failure_counts(retry: dict) -> tuple[int, int]:
    failures = retry.get("failures", 0 if retry.get("waiting") else retry.get("attempts", 0))
    legacy_format = failures if retry.get("category") == "format" and not retry.get("waiting") else 0
    formats = retry.get("format_attempts", legacy_format)
    return failures, formats


def _repair_fields(previous: dict, *, repair_text: str = "", repair_choice=None) -> dict:
    """Keep correction drafts only in the local retry journal, never candidate."""
    if isinstance(repair_choice, dict):
        return {"repair_choice": copy.deepcopy(repair_choice)}
    if isinstance(repair_text, str) and repair_text:
        return {"repair_text": repair_text[:8000]}
    text = previous.get("repair_text", "")
    choice = previous.get("repair_choice")
    result = {}
    if isinstance(text, str) and text:
        result["repair_text"] = text[:8000]
    if isinstance(choice, dict):
        result["repair_choice"] = copy.deepcopy(choice)
    return result


def _active_recovery(visit: dict, cursor: dict) -> bool:
    """Only an existing shared encounter can retain its phase through a failure."""
    return (visit.get("outing_version") == 1 and bool(visit.get("arrived_at"))
            and visit.get("status") == "active" and cursor.get("phase") in {"turn", "scene"})


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
    failures, formats = _failure_counts(retry)
    active = _active_recovery(visit, cursor)
    delays = ACTIVE_DELAYS if active and retry.get("category") in {"timeout", "provider"} else DELAYS
    paused = retry.get("paused", False)
    total_limit = len(ACTIVE_DELAYS) + len(DELAYS) if active else len(DELAYS)
    if paused:
        return (active and retry.get("category") == "format" and waiting is False
                and type(attempts) is int and 1 <= attempts <= len(DELAYS) + 1
                and type(formats) is int and 1 <= formats <= len(DELAYS) + 1
                and type(failures) is int and formats <= failures <= total_limit + 1
                and retry.get("phase") == cursor.get("phase")
                and isinstance(retry.get("basis"), dict)
                and type(next_at) in {int, float} and math.isfinite(next_at) and next_at >= 0)
    return (retry.get("phase") == cursor.get("phase")
            and retry.get("category") in RETRYABLE
            and type(waiting) is bool and paused is False
            and type(attempts) is int and 1 <= attempts <= len(WAIT_DELAYS if waiting else delays)
            and type(formats) is int and 0 <= formats <= len(DELAYS)
            and type(failures) is int and formats <= failures <= total_limit
            and failures - formats <= len(ACTIVE_DELAYS if active else DELAYS)
            and type(next_at) in {int, float} and math.isfinite(next_at) and next_at >= 0)


def defer_decision(visit: dict, cursor: dict, *, category: str, now: float, candidate=None,
                   waiting: bool = False, format_issue: str = "", repair_text: str = "", repair_choice=None) -> bool:
    """Unissued waits keep their phase; only unsuccessful inference spends recovery."""
    old = visit.get("model_retry") or {}
    previous = old if valid_retry(visit, cursor) else {}
    failures, formats = _failure_counts(previous)
    if category not in RETRYABLE:
        return False
    active = _active_recovery(visit, cursor) and category in {"timeout", "provider"}
    delays = ACTIVE_DELAYS if active else DELAYS
    if waiting:
        attempts = min(len(WAIT_DELAYS), (previous.get("attempts", 0) if previous.get("waiting") else 0) + 1)
        delay = WAIT_DELAYS[attempts - 1]
    else:
        failures += 1
        if category == "format":
            formats += 1
        attempts = formats if category == "format" else failures - formats
        total_limit = len(ACTIVE_DELAYS) + len(DELAYS) if _active_recovery(visit, cursor) else len(DELAYS)
        if attempts > len(delays) or failures > total_limit:
            return False
        delay = delays[attempts - 1]
    next_at = now + delay
    if next_at >= visit["expires_at"]:
        if not (waiting or active) or visit["expires_at"] <= now:
            return False
        next_at = visit["expires_at"]  # The existing lease ends the wait at its own clock.
    retry = {"phase": cursor["phase"], "cursor": copy.deepcopy(cursor), "attempts": attempts,
             "next_at": next_at, "category": category}
    if category == "format" and isinstance(format_issue, str) and format_issue in FORMAT_ISSUES:
        retry["format_issue"] = format_issue
    if waiting:
        retry["waiting"] = True
    if failures:
        retry["failures"] = failures
    if formats:
        retry["format_attempts"] = formats
    retry.update(_repair_fields(previous, repair_text=repair_text, repair_choice=repair_choice))
    if isinstance(candidate, dict) and category != "format":
        retry["candidate"] = copy.deepcopy(candidate)
    visit["model_retry"] = retry
    visit["next_at"] = max(visit["next_at"], next_at)
    return True


def pause_decision(visit: dict, cursor: dict, *, category: str, now: float,
                   format_issue: str = "", repair_text: str = "", repair_choice=None) -> bool:
    """A failed correction pauses an existing encounter without inventing a choice."""
    if category != "format" or not _active_recovery(visit, cursor) or visit["expires_at"] <= now:
        return False
    old = visit.get("model_retry") or {}
    previous = old if valid_retry(visit, cursor) else {}
    failures, formats = _failure_counts(previous)
    formats = min(len(DELAYS) + 1, formats + 1)
    if "local_experience_attention_seq" not in visit:
        visit["local_experience_attention_seq"] = (visit.get("local_experience_seq", 0)
                                                  if visit.get("local_experience_pending") else 0)
    retry = {"phase": cursor["phase"], "cursor": copy.deepcopy(cursor), "category": "format",
             "paused": True, "attempts": formats, "format_attempts": formats,
             "failures": min(len(ACTIVE_DELAYS) + len(DELAYS) + 1, failures + 1),
             "next_at": visit["expires_at"], "basis": recovery_basis(visit, cursor)}
    if isinstance(format_issue, str) and format_issue in FORMAT_ISSUES:
        retry["format_issue"] = format_issue
    retry.update(_repair_fields(previous, repair_text=repair_text, repair_choice=repair_choice))
    visit["model_retry"] = retry
    visit["next_at"] = visit["expires_at"]
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
    if retry.get("paused") is True:
        return {"reason": "这次模型选择尚未修好，保留现场等待新的实际进展", "paused": True,
                "next_at": retry.get("next_at", 0), "attempts": retry["attempts"]}
    return {"reason": WAITING[retry["category"]] + "，冷却后继续相处",
            "next_at": retry.get("next_at", 0), "attempts": retry["attempts"]}
