"""Bounded synthetic scenes for evaluating the shared participation prompt."""
from __future__ import annotations

from copy import copy, deepcopy
from dataclasses import dataclass
from hashlib import sha256
import math
import json
from typing import Any


def _label(value: Any, limit: int = 120) -> str:
    return str(value or "").strip()[:limit]


def _flag(row: dict, key: str) -> bool:
    value = row.get(key, False)
    if type(value) is not bool:
        raise ValueError(f"chat_rows 的 {key} 必须是布尔值")
    return value


def _body(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) > 20000:
        raise ValueError("合成正文或引用最多 20000 字；请保留完整边界后再提交")
    return text


def visible_eval_scene(prompt: str) -> list[dict]:
    """Extract the exact scene serialized by the shared production builder."""
    start = prompt.find("\n[")
    if start < 0:
        return []
    scene, _ = json.JSONDecoder().raw_decode(prompt[start + 1:])
    return scene if isinstance(scene, list) else []


@dataclass
class EvalScene:
    engine: Any
    state: Any
    rows: list[dict]
    source: str
    reply_seqs: frozenset[int]
    answered_seqs: frozenset[int]
    message_aliases: dict[str, str]

    def metadata(self) -> dict:
        return {"isolated": True, "source": self.source, "rows": len(self.rows),
                "state_key": self.state.session_id, "message_aliases": self.message_aliases,
                "memory": "disabled", "profiles": "disabled", "native_context": False,
                "tools": False, "life": "copied_current_state"}


def prepare_eval_scene(engine, state, target: str, item: dict, *, index: int, now: float) -> EvalScene:
    """Copy life facts, replace all chat context, and never bind real identities.

    Only ages and whitelisted message metadata are accepted. The original
    session is still the provider/config selection target; the copied state's
    identity cannot borrow its persisted speech receipts.
    """
    supplied = item.get("chat_rows")
    if not isinstance(supplied, list) or not 1 <= len(supplied) <= 80:
        raise ValueError("isolated_scene 需要 1 至 80 条 chat_rows")
    source = _label(item.get("source") or "message")
    if source not in {"message", "browse"}:
        raise ValueError("隔离测评 source 只能是 message 或 browse")
    prefix = "eval:" + sha256(f"{target}:{index}:{now}".encode()).hexdigest()[:16]

    def identity(value: Any, *, kind: str, default: str = "") -> str:
        alias = _label(value) or default
        return f"{prefix}:{kind}:{alias}" if alias else ""

    copied = deepcopy(state)
    copied.session_id = prefix
    copied.recent_chat = []
    copied.chat_seq = 0
    rows, aliases = [], {}
    for offset, raw in enumerate(supplied, 1):
        if not isinstance(raw, dict):
            raise ValueError("chat_rows 每条必须是对象")
        text = _body(raw.get("text"))
        if not text:
            raise ValueError("chat_rows 每条需要 text")
        age = raw.get("age_seconds", (len(supplied) - offset) * 5)
        if type(age) not in {int, float} or not math.isfinite(age) or not 0 <= age <= 2592000:
            raise ValueError("age_seconds 必须是 0 至 2592000 的有限数字")
        is_self, answered = _flag(raw, "is_self"), _flag(raw, "answered")
        alias = _label(raw.get("message_id")) or f"row-{offset}"
        mid = identity(alias, kind="message")
        if mid in aliases:
            raise ValueError("chat_rows 的 message_id 不能重复")
        aliases[mid] = alias
        name = _label(raw.get("name"), 80) or ("我" if is_self else "群友")
        row = {"origin": target, "text": text, "raw_text": text, "at": now - age,
               "seq": offset, "message_id": mid, "name": name, "is_self": is_self,
               "answered": answered, "user_id": identity(raw.get("user_id"), kind="user",
                   default="self" if is_self else _label(raw.get("name"), 80) or f"person-{offset}")}
        if raw.get("speech_turn"):
            row["speech_turn"] = identity(raw["speech_turn"], kind="turn")
        if raw.get("addressing") in {"me", "others"}:
            row["addressing"] = raw["addressing"]
        targets = raw.get("at_targets", [])
        if not isinstance(targets, list) or len(targets) > 20:
            raise ValueError("at_targets 必须是最多 20 项的数组")
        if targets:
            row["at_targets"] = []
            for entry in targets:
                if not isinstance(entry, dict):
                    raise ValueError("at_targets 每项必须是对象")
                row["at_targets"].append({"id": identity(entry.get("id"), kind="user"),
                    "name": _label(entry.get("name"), 80), "self": _flag(entry, "self")})
        quoted = raw.get("reply_to")
        if quoted is not None:
            if not isinstance(quoted, dict):
                raise ValueError("reply_to 必须是对象")
            quote = {}
            for key in ("id", "message_id"):
                if quoted.get(key):
                    quote[key] = identity(quoted[key], kind="message" if key == "message_id" else "user")
            for key in ("name", "text"):
                if quoted.get(key):
                    quote[key] = _body(quoted[key]) if key == "text" else _label(quoted[key], 80)
            if "is_self" in quoted:
                quote["is_self"] = _flag(quoted, "is_self")
            row["reply_to"] = quote
        # Use the same archive envelope and bounded excerpts as actual input.
        retained = copied.note_chat(user_id=row["user_id"], name=name, text=text, now=row["at"],
            keep=80, is_self=is_self, origin=target, raw_text=text,
            at=row.get("at_targets"), reply_to=row.get("reply_to"),
            addressing=row.get("addressing", ""), message_id=mid)
        if retained is None:
            raise ValueError("合成消息归一化失败")
        retained["answered"] = answered
        if row.get("speech_turn"):
            retained["speech_turn"] = row["speech_turn"]
        rows.append(retained)
    for field in ("thoughts", "recent_events", "recent_replies", "open_topics", "grudges",
                  "heart_knots", "pending_images", "review_visits"):
        setattr(copied, field, [])
    for field in ("chat_notes", "chat_previews", "chat_watermarks", "chat_browsing", "user_presence",
                  "session_activity", "last_reasoning", "last_reflection"):
        setattr(copied, field, {})
    copied.chat_note = copied.chat_preview = copied.pending_review = ""
    copied.chat_replied_until = copied.chat_replied_seq = 0

    # Instance overrides affect only this local copy. The production prompt's
    # selection, formatting, life evidence and cadence rendering remain shared.
    view = copy(engine)
    view.visits = None
    view.context_memories = lambda *args, **kwargs: []
    view.context_profiles = lambda *args, **kwargs: ""
    view.context_emotions = lambda *args, **kwargs: ""
    view.restore_context_semantics = lambda *args, **kwargs: None
    view.chat_speech_cadence = lambda *args, **kwargs: (0.0, [])
    eligible = frozenset(row["seq"] for row in rows if not row["is_self"] and not row["answered"])
    answered = frozenset(row["seq"] for row in rows if row["answered"])
    return EvalScene(view, copied, rows, source, eligible, answered, aliases)
