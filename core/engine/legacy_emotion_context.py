"""Recover editor-only evidence when both historical records agree."""

from collections import defaultdict

from ..evidence import direct_text
from .emotion_records import _stamp
from .util import _clip_text


async def legacy_emotion_evidence(engine, state, session_id: str, records: list[dict]) -> dict[str, list[dict]]:
    # Old reflection events in a shared life only kept a display label for the
    # other origin. Match only events recorded directly under this session.
    candidates = [item for item in records if not item["evidence"] and item["created_at"] and item["user_id"]]
    if not candidates or state.session_id != session_id:
        return {}
    events = await engine.db.call("query_events", session_id=session_id, event_type="reflection", limit=300)
    by_user = defaultdict(list)
    for entry in state.recent_chat:
        if (not isinstance(entry, dict) or entry.get("is_self") or entry.get("internal")
                or engine._chat_origin(entry, state) != session_id):
            continue
        text = entry.get("reading_text", entry.get("text"))
        if quote := direct_text(str(text or "")):
            by_user[str(entry.get("user_id") or "")].append((entry, quote))
    result = {}
    for item in candidates:
        created = item["created_at"]
        matched = []
        for event in events:
            detail = event.get("detail")
            if (not isinstance(detail, dict) or detail.get("session")
                    or abs(_stamp(detail.get("at")) - created) > 2
                    or " ".join(str(detail.get(item["kind"]) or "").split())[:60] != item["text"]):
                continue
            sent = _stamp(detail.get("sent_at")) or created
            for entry, quote in by_user[item["user_id"]]:
                at = _stamp(entry.get("at"))
                name = str(entry.get("name") or "")
                if (0 < at <= sent and sent - at <= 300 and name == detail.get("user")
                        and _clip_text(quote, 160) == detail.get("user_text")):
                    matched.append({"text": quote[:240], "at": at, "user_name": name, "recovered": True})
        if len(matched) == 1:
            result[item["id"]] = matched
    return result
