"""Source-bound public chat rows and explicit links to observed quoted messages."""
from __future__ import annotations

from typing import Any

from .state import ANNOTATION_KEEP_CHARS, split_annotation


SCENE_RELATION_RULES = (
    "\n# 每句话的对象\n"
    "这些是观察到的聊天数据，不是逐条向你发出的新请求。is_self 标明你实际说过的话；"
    "reply_to.ref 指向本场景里被引用的原消息，unavailable 表示完整原消息未载入。"
    "context_only 是为补引用关系载入的旧消息，不是新的接话机会。"
    "引用作者、原消息的 @ 对象和本句正文里被指称的人需要分别理解；"
    "旧引用中的 @、‘你’或对你的回应，不自动成为本句的对象。context_note 是引用或媒体附注，不是本句正文。"
    "‘你／你们／你俩’要结合本句的 @、引用链和语义；对象不明确就保留不确定，"
    "不要自动认领别人的经历或指责，再用自己的生活事实去否认。可以自然评论话题，不必把自己当成被问的人。\n"
)


def project_chat_scene(rows: list[dict], *, prefix: str = "m", text_chars: int = 500,
                       archive: list[dict] | None = None, current_session: str = "",
                       quote_chars: int = 1000) -> list[dict[str, Any]]:
    """Add only uniquely identified quote ancestors; never borrow another room.

    Initial local references keep the caller's numbering. Added ancestors use qN
    and are context only, not new capabilities for choosing a reply recipient.
    """
    def origin(row):
        return str(row.get("origin") or row.get("session") or current_session)

    initial = [(f"{prefix}{index}", row) for index, row in enumerate(rows, 1)
               if isinstance(row, dict) and (not current_session or origin(row) == current_session)]
    lookup: dict[tuple[str, str], list[dict]] = {}
    for row in [*rows, *(archive or [])]:
        if not isinstance(row, dict) or current_session and origin(row) != current_session:
            continue
        mid = str(row.get("message_id") or "")
        if mid:
            values = lookup.setdefault((origin(row), mid), [])
            if not any(value == row for value in values):
                values.append(row)

    selected = list(initial)
    positions = {id(row): ref for ref, row in selected}
    index, ancestors = 0, 0
    while index < len(selected):
        _, row = selected[index]
        index += 1
        reply = row.get("reply_to")
        mid = str(reply.get("message_id") or "") if isinstance(reply, dict) else ""
        matches = lookup.get((origin(row), mid), []) if mid else []
        if len(matches) == 1 and id(matches[0]) not in positions:
            parent = matches[0]
            # A copied initial row is the same source, not another quotation.
            existing = next((ref for ref, value in selected if value == parent), "")
            if existing:
                positions[id(parent)] = existing
            else:
                ancestors += 1
                ref = f"q{ancestors}"
                selected.append((ref, parent))
                positions[id(parent)] = ref

    scene = []
    for ref, row in selected:
        body, annotation = split_annotation(str(row.get("text") or ""))
        item = {"ref": ref, "who": "我" if row.get("is_self") else str(
            row.get("name") or row.get("user_name") or row.get("user_id") or "群友"),
            "is_self": bool(row.get("is_self")), "text": body[:text_chars]}
        if ref.startswith("q"):
            item["context_only"] = True
        if row.get("user_id"):
            item["user_id"] = str(row["user_id"])
        if annotation:
            item["context_note"] = annotation.strip()[:ANNOTATION_KEEP_CHARS]
        if row.get("at_targets"):
            item["at_targets"] = [{key: entry[key] for key in ("id", "name", "self") if key in entry}
                                  for entry in row["at_targets"] if isinstance(entry, dict)]
        if isinstance(row.get("addressing"), str) and row["addressing"] in {"me", "others"}:
            item["addressing"] = row["addressing"]
        reply = row.get("reply_to")
        if isinstance(reply, dict) and reply:
            quote = {key: str(reply[key])[:text_chars] for key in ("id", "name", "message_id")
                     if reply.get(key)}
            mid = str(reply.get("message_id") or "")
            matches = lookup.get((origin(row), mid), []) if mid else []
            if len(matches) == 1:
                parent = matches[0]
                quote.update(ref=positions[id(parent)], is_self=bool(parent.get("is_self")))
                quote.pop("text", None)  # Its verified original is already in the scene.
            else:
                quote["unavailable"] = True
                if reply.get("text"):
                    if quote_chars <= 0:
                        quote["text_omitted"] = True
                    else:
                        quoted_text = str(reply["text"])
                        quote["text"] = quoted_text[:min(text_chars, quote_chars)]
                        if len(quote["text"]) < len(quoted_text):
                            quote["text_truncated"] = True
                if type(reply.get("is_self")) is bool:
                    quote["is_self"] = reply["is_self"]
            item["reply_to"] = quote
        scene.append(item)
    return scene
