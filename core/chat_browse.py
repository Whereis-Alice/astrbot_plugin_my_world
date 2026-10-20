"""Source-scoped thoughts and bounded reading, independent of reply watermarks."""
from __future__ import annotations

from .evidence import split_external_memory
from .state import split_annotation


def reading_text(row: dict) -> str:
    """Only the sender's text and an image marker; never attachment data or captions."""
    image = bool(row.get("has_images") or row.get("images"))
    if "reading_text" in row:
        text = str(row["reading_text"] or "")
    elif image:
        # Older image rows have no proven raw text; their body can contain vision reports.
        text = ""
    else:
        text = split_annotation(str(row.get("text") or ""))[0]
    text = split_external_memory(text)[0].strip()
    if image:
        text = f"{text}\n「图片」".strip()
    return text[:1000]


def reading_view(state, session: str, config, now: float) -> list[dict]:
    cursor = int((state.chat_browsing.get("sessions", {}).get(session) or {}).get("seq", 0))
    rows = []
    for row in state.recent_chat:
        if (not isinstance(row, dict) or row.get("internal")
                or str(row.get("origin") or state.session_id) != session
                or int(row.get("seq") or 0) <= cursor
                or not 0 <= now - float(row.get("at") or 0) <= config.window_hours * 3600):
            continue
        text = reading_text(row)
        if text:
            rows.append({"seq": int(row["seq"]), "at": float(row["at"]),
                         "name": str(row.get("name") or row.get("user_id") or "")[:40],
                         "self": bool(row.get("is_self")), "text": text[:1000]})
    kept, remaining = [], config.max_chars
    for row in reversed(rows[-config.max_messages:]):
        if remaining <= 0:
            break
        row = {**row, "text": row["text"][:remaining]}
        kept.append(row)
        remaining -= len(row["text"])
    kept.reverse()
    return kept if any(not row["self"] for row in kept) else []


def dialogue_origin(item: dict) -> str | None:
    if item.get("dialogue_session"):
        return str(item["dialogue_session"])
    if item.get("source") == "聊完一轮" or item.get("quote") or item.get("reply"):
        return None  # Legacy conversation content with no provable source.
    return ""


def thought_visible(
    item: dict, session: str, now: float, *, fallback_session: str = ""
) -> bool:
    origin = dialogue_origin(item)
    if origin is None:
        # Legacy chat reflections had no origin tag. They are safe to show only
        # in the representative session that owns this persisted world; sibling
        # sessions still require an explicit matching source.
        origin = fallback_session if fallback_session == session else "<legacy>"
    if origin and origin != session:
        return False
    return not item.get("browse_session") or (
        item["browse_session"] == session and float(item.get("expires_at") or 0) > now)


def expire_feelings(state, now: float) -> None:
    state.thoughts = [row for row in state.thoughts if not row.get("browse_session")
                      or float(row.get("expires_at") or 0) > now]


def feeling_context(state, session: str, now: float) -> str:
    rows = [row for row in state.thoughts if row.get("browse_session")
            and thought_visible(row, session, now)][-2:]
    if not rows:
        return ""
    return ("\n# 翻消息时留下的感受\n这是你阅读本会话时的主观想法，原有人设优先；"
            "不代表亲历、对方真实动机或新的约定。只在当前交谈相关时自然参考，不必主动提起。\n"
            + "\n".join("- " + str(row.get("content") or "")[:120] for row in rows))
