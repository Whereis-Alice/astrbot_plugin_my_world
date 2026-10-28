"""A short-lived reading opportunity, bound to its life and original messages."""
from __future__ import annotations

import time
from dataclasses import dataclass, replace


def message_identity(row: dict) -> tuple:
    return (int(row.get("seq") or 0), float(row.get("at") or 0),
            str(row.get("user_id") or ""), str(row.get("message_id") or ""),
            str(row.get("event_id") or ""), str(row.get("text") or ""))


def available_rows(engine, state, target: str) -> list[dict]:
    """Retained, unanswered source messages; age is left to the reading decision."""
    from .state import chat_item_is_fresh
    until, seq = engine._watermark(state, target)
    return [row for row in state.recent_chat if engine._chat_origin(row, state) == target
            and not row.get("is_self") and not row.get("internal")
            and float(row.get("at") or 0) <= engine._now()
            and chat_item_is_fresh(row, replied_until=until, replied_seq=seq)
            and not engine.is_plugin_command(str(row.get("text") or ""))]


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
    def capture(cls, engine, state, target: str, browse_id: str, *, read_rows=None) -> BrowseReply:
        if read_rows is None:
            # Older/public callers can use only the reading's retained evidence,
            # never broaden a past reading to everything currently in the archive.
            thought = next((row for row in reversed(state.thoughts) if row.get("browse_id") == browse_id
                            and row.get("browse_session") == target), {})
            read_rows = thought.get("browse_sources") or []
        read_seqs = {int(row.get("seq") or 0) for row in read_rows}
        rows = [row for row in available_rows(engine, state, target) if int(row.get("seq") or 0) in read_seqs]
        return cls(state.session_id, target, browse_id,
                   frozenset(message_identity(row) for row in rows),
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
