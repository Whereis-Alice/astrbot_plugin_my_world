"""Local receipts of chat actually read or sent while this actor is visiting.

The visit wire never carries these notes. A character may use her own bounded
experience when deciding what to do next; another group's words stay local.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import math
from dataclasses import asdict, dataclass
from typing import Any

from .evidence import direct_text

MAX_NOTES = 64
MAX_VISITS = 96


def _text(value: Any, limit: int = 180) -> str:
    return " ".join(str(value or "").split())[:limit]


def _stamp(value: Any) -> float:
    return float(value) if type(value) in {int, float} and math.isfinite(value) and value > 0 else 0.


def _actor(visit: dict) -> str:
    field = "host_id" if visit.get("role") == "host" else "guest_id"
    return str(visit.get(field) or (visit.get("local") or {}).get("actor_id") or "")


@dataclass(frozen=True)
class VisitChatOwner:
    visit_id: str
    scope: str
    actor_id: str
    persona_key: str
    origin: str
    arrived_at: float
    captured_at: float

    def snapshot(self) -> dict:
        return asdict(self)


def restore_visit_chat_owner(value: Any) -> VisitChatOwner | None:
    if not isinstance(value, dict):
        return None
    names = ("visit_id", "scope", "actor_id", "persona_key", "origin")
    if any(not isinstance(value.get(name), str) or not value[name] for name in names):
        return None
    arrived, at = _stamp(value.get("arrived_at")), _stamp(value.get("captured_at"))
    if not arrived or at < arrived:
        return None
    return VisitChatOwner(*(value[name] for name in names), arrived, at)


def capture_visit_chat_owner(engine: Any, session: str, *, persona_key: str | None = None,
                             expected_state_key: str = "", expected_visit_id: str = "") -> VisitChatOwner | None:
    """Freeze only an arrived, identity-matching group-chat observation."""
    from .visit_presence import active_visit
    session = str(session or "")
    parts = session.split(":", 2)
    if len(parts) != 3 or parts[1] != "GroupMessage":
        return None
    service = getattr(engine, "visits", None)
    if service is None or getattr(service, "_closed", False) or getattr(service, "_loaded", True) is False:
        return None
    try:
        scope = engine.state_key(session)
        if expected_state_key and scope != expected_state_key:
            return None
        visit = active_visit(engine, session)
        if (not visit or visit.get("scope") != scope or not visit.get("arrived_at")
                or expected_visit_id and visit.get("id") != expected_visit_id):
            return None
        local = visit.get("local") or {}
        # No guessed persona: asynchronous callers supply the persona they read;
        # accepted speech can additionally use its existing task-local lease.
        if not persona_key:
            from .engine.life_inference import STATE_LEASE
            lease = STATE_LEASE.get() or {}
            if lease.get("owner") is asyncio.current_task() and str(getattr(lease.get("state"), "session_id", "")) == scope:
                persona_key = hashlib.sha256(str(lease.get("persona", "")).encode()).hexdigest()[:24]
        if not persona_key or local.get("persona_key") != persona_key:
            return None
        at, arrived = _stamp(engine._now()), _stamp(visit.get("arrived_at"))
        identity, actor = str(visit.get("id") or ""), _actor(visit)
        if not identity or not actor or not arrived or at < arrived:
            return None
        return VisitChatOwner(identity, scope, actor, persona_key, session, arrived, at)
    except (AttributeError, KeyError, TypeError, ValueError, RuntimeError):
        return None


def _visit_for_owner(engine: Any, owner: VisitChatOwner) -> dict | None:
    service = getattr(engine, "visits", None)
    saved = getattr(service, "_state", {}).get("visits", {})
    visit = saved.get(owner.visit_id) if isinstance(saved, dict) else None
    if visit is None:
        getter = getattr(service, "active", None)
        visit = getter(owner.scope) if callable(getter) else None
    if (not isinstance(visit, dict) or visit.get("id") != owner.visit_id or visit.get("scope") != owner.scope
            or _actor(visit) != owner.actor_id or (visit.get("local") or {}).get("persona_key") != owner.persona_key
            or _stamp(visit.get("arrived_at")) != owner.arrived_at):
        return None
    return visit


def _bucket_key(scope: str) -> str:
    return "world_visit_chat_v1:" + hashlib.sha256(scope.encode()).hexdigest()[:32]


def _journal_key(owner: VisitChatOwner) -> str:
    return hashlib.sha256((owner.visit_id + "\0" + owner.actor_id).encode()).hexdigest()[:32]


async def record_visit_chat(engine: Any, owner: VisitChatOwner | None, *, kind: str, text: str,
                            source_id: str, at: float | None = None, significant: bool = False) -> bool:
    """Commit one immutable local fact; retries neither duplicate nor wake twice."""
    if owner is None or kind not in {"read", "sent", "sent_image"} or not source_id:
        return False
    stamp = owner.captured_at if at is None else _stamp(at)
    body = _text(text, 220 if kind == "sent" else 180)
    if not body or stamp < owner.arrived_at:
        return False
    locks = getattr(engine, "_visit_chat_bridge_locks", None)
    if locks is None:
        locks = engine._visit_chat_bridge_locks = {}
    key = _bucket_key(owner.scope)
    lock = locks.setdefault(key, asyncio.Lock())
    note_id = hashlib.sha256((kind + "\0" + owner.origin + "\0" + str(source_id)).encode()).hexdigest()[:32]
    async with lock:
        visit = _visit_for_owner(engine, owner)
        if visit is None or _stamp(visit.get("ended_at")) and stamp > _stamp(visit["ended_at"]):
            return False
        payload = await engine.db.call("kv_get", key)
        payload = copy.deepcopy(payload) if isinstance(payload, dict) else {}
        journals = payload.setdefault("visits", {})
        if not isinstance(journals, dict):
            journals = payload["visits"] = {}
        journal_key = _journal_key(owner)
        journal = journals.get(journal_key)
        proof = {name: getattr(owner, name) for name in ("visit_id", "scope", "actor_id", "persona_key", "arrived_at")}
        if not isinstance(journal, dict):
            journal = {**proof, "seq": 0, "notes": []}
        elif any(journal.get(name) != value for name, value in proof.items()):
            return False
        notes = [row for row in journal.get("notes", []) if isinstance(row, dict)]
        # Keep receipt IDs even after bounded text eviction.
        seen = journal.get("seen", [])
        seen = [value for value in seen if isinstance(value, str)] if isinstance(seen, list) else []
        if note_id in seen or any(row.get("id") == note_id for row in notes):
            return False
        seq = int(journal.get("seq") or 0) + 1
        notes.append({"id": note_id, "seq": seq, "at": stamp, "kind": kind, "text": body,
                      "origin": owner.origin})
        journal.update(seq=seq, notes=notes[-MAX_NOTES:], seen=[*seen, note_id][-2048:], updated_at=stamp)
        journals[journal_key] = journal
        if len(journals) > MAX_VISITS:
            inactive = [entry for entry in journals if entry != journal_key]
            for entry in sorted(inactive, key=lambda entry: journals[entry].get("updated_at", 0))[:len(journals) - MAX_VISITS]:
                journals.pop(entry, None)
        await engine.db.call("kv_set", key, payload)
    wake = getattr(getattr(engine, "visits", None), "wake_local_experience", None)
    if callable(wake):
        try:
            await wake(visit_id=owner.visit_id, scope=owner.scope, actor_id=owner.actor_id,
                       persona_key=owner.persona_key, at=stamp, seq=seq, significant=significant)
        except Exception:
            pass  # The immutable receipt is already durable; normal review can still read it.
    return True


def important_visit_chat(engine: Any, owner: VisitChatOwner | None, text: str) -> bool:
    """Only a direct update relevant to this encounter should interrupt quiet company."""
    if owner is None:
        return False
    from .visit_presence import visit_context_relevant
    suggestion = (any(word in text for word in ("要不要", "不如", "可以", "先", "换", "回家"))
                  and any(word in text for word in ("一起", "你们", "你俩", "带她", "带他", "朋友", "同伴")))
    return visit_context_relevant(text, _visit_for_owner(engine, owner)) or suggestion


async def record_visit_reads(engine: Any, owner: VisitChatOwner | None, rows: list[dict]) -> None:
    """Only sources actually included in the completed request, with direct-text labels."""
    if owner is None:
        return
    for row in rows:
        if not isinstance(row, dict) or row.get("is_self") or row.get("user_id") == "__self__":
            continue
        origin = str(row.get("origin") or row.get("session") or owner.origin)
        if origin != owner.origin:
            continue  # Another session's archive is not a new live group interaction.
        text = direct_text(str(row.get("raw_text") or row.get("text") or ""))
        source = str(row.get("event_key") or row.get("event_id") or row.get("message_id") or "")
        if not source:
            source = hashlib.sha256((str(row.get("wall_at") or row.get("seq") or "") + "\0" + text).encode()).hexdigest()
        await record_visit_chat(engine, owner, kind="read", text=text, source_id=source,
                                significant=important_visit_chat(engine, owner, text))


async def local_chat_experiences(engine: Any, visit: dict, *, limit: int = 8) -> dict:
    """Read only this actor's own journey journal, including historical finish."""
    local = visit.get("local") or {}
    owner = VisitChatOwner(str(visit.get("id") or ""), str(visit.get("scope") or ""), _actor(visit),
                          str(local.get("persona_key") or ""), "", _stamp(visit.get("arrived_at")), 0.)
    if not all((owner.visit_id, owner.scope, owner.actor_id, owner.persona_key, owner.arrived_at)):
        return {"seq": 0, "notes": []}
    payload = await engine.db.call("kv_get", _bucket_key(owner.scope))
    journals = payload.get("visits", {}) if isinstance(payload, dict) else {}
    journal = journals.get(_journal_key(owner), {}) if isinstance(journals, dict) else {}
    if (not isinstance(journal, dict) or any(journal.get(name) != getattr(owner, name)
            for name in ("visit_id", "scope", "actor_id", "persona_key", "arrived_at"))):
        return {"seq": 0, "notes": []}
    notes = [dict(row) for row in journal.get("notes", []) if isinstance(row, dict)
             and row.get("kind") in {"read", "sent", "sent_image"} and _stamp(row.get("at")) >= owner.arrived_at]
    end = _stamp(visit.get("ended_at"))
    if end:
        notes = [row for row in notes if row["at"] <= end]
    return {"seq": int(journal.get("seq") or 0), "notes": notes[-max(0, min(MAX_NOTES, int(limit))):] if limit else []}


def local_experience_lines(experiences: dict, *, limit: int = 900) -> str:
    """Deterministic bounded notes preserve read-versus-actual-speech meaning."""
    labels = {"read": "我在群里读到（群友说法/建议，未证明完成）", "sent": "我实际回复群里",
              "sent_image": "我实际向群里发送图片"}
    lines = []
    for row in experiences.get("notes", []):
        if not isinstance(row, dict) or row.get("kind") not in labels:
            continue
        lines.append(f"[{labels[row['kind']]}，时间{_stamp(row.get('at')):g}] {_text(row.get('text'), 180)}")
    selected = []
    remaining = max(0, int(limit))
    for line in reversed(lines):
        if len(line) + bool(selected) > remaining:
            break
        selected.insert(0, line)
        remaining -= len(line) + bool(len(selected) > 1)
    return "；".join(selected)
