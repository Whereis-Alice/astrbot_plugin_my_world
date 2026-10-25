"""A short-lived reading opportunity, bound to its life and original messages."""
from __future__ import annotations

import time
from dataclasses import dataclass, replace


def message_identity(row: dict) -> tuple:
    return (int(row.get("seq") or 0), float(row.get("at") or 0),
            str(row.get("user_id") or ""), str(row.get("message_id") or ""),
            str(row.get("event_id") or ""), str(row.get("text") or ""))


@dataclass(frozen=True)
class BrowseReply:
    life: str
    target: str
    browse_id: str
    messages: frozenset[tuple]
    wait_seconds: int
    created_at: float
    started: float
    deferred: bool = False

    @classmethod
    def capture(cls, engine, state, target: str, browse_id: str) -> BrowseReply:
        rows = engine.live_chat(state, target, window=engine._participation_window("browse"))
        checked = (state.participation.get(target) or {}).get("checked_seq", 0)
        return cls(state.session_id, target, browse_id,
                   frozenset(message_identity(row) for row in rows if int(row.get("seq") or 0) > checked),
                   engine.world.chat_browse.reply_wait_seconds, engine._now(), time.monotonic())

    def queued(self) -> BrowseReply:
        return replace(self, deferred=True)

    @property
    def deadline(self) -> float:
        return self.created_at + self.wait_seconds

    def remaining(self, now: float) -> float:
        return min(self.deadline - now, self.started + self.wait_seconds - time.monotonic())

    def eligible(self, rows: list[dict]) -> list[dict]:
        return [row for row in rows if message_identity(row) in self.messages]

    def error(self, engine, state) -> tuple[str, str]:
        if engine.state_key(self.target) != self.life or state.session_id != self.life:
            return "cancelled", "生活归属已变化，本次阅读机会已结束"
        if self.deferred and self.remaining(engine._now()) <= 0:
            return "expired", "短暂等待已到上限，本次不再接话"
        if not engine.world.chat_browse.enabled or self.target not in engine._browse_sources(state):
            return "cancelled", "浏览已停用或来源范围已变化，本次不再接话"
        progress = (state.chat_browsing.get("sessions") or {}).get(self.target) or {}
        thought = next((row for row in reversed(state.thoughts)
                        if row.get("browse_id") == self.browse_id and row.get("browse_session") == self.target), None)
        if progress.get("browse_id") != self.browse_id or thought is None:
            return "cancelled", "本次阅读记录已清除或被新阅读替代"
        if (thought.get("detail") or {}).get("speech"):
            return "already_considered", "本次阅读已经有发言决定，不重复判断"
        return "", ""


RETRYABLE = frozenset({"reply_pending", "busy"})


def reply_status(state, ticket: BrowseReply) -> str:
    thought = next((row for row in reversed(state.thoughts) if row.get("browse_id") == ticket.browse_id
                    and row.get("browse_session") == ticket.target), {})
    return str(((thought.get("detail") or {}).get("browse_reply") or {}).get("status") or "")
