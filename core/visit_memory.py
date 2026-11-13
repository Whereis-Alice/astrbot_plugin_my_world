"""Monotonic confirmation updates for a trip's single durable memory receipt."""
from __future__ import annotations

import hashlib
import json

ADDITIONS_MARKER = "。本地经历与阶段回顾："
UNFINISHED_LABEL = "当时未完事项，再见时可重选"
PROMISE_LABEL = "我当时记下的约定，成立与否以双方原话为准"


def render_visit_additions(meta: dict) -> str:
    """Local experience stays labelled; a group's idea never becomes a result."""
    additions = meta.get("visit_additions") or {}
    parts = []
    labels = {"read": "我读到群友的说法或建议（未证明完成）", "sent": "我实际回复群里", "sent_image": "我实际向群里发图"}
    for row in additions.get("local_experiences", []):
        if row.get("kind") in labels:
            parts.append(f"[{labels[row['kind']]}，{row['at']:g}] {row['text']}")
    for row in additions.get("milestones", []):
        parts.append(f"[共同确认片刻，{row['at']:g}] {row['summary']}")
    for row in additions.get("personal_moments", []):
        feelings = "；".join(str(row.get(name) or "") for name in ("thought", "reflection") if row.get(name))
        if feelings:
            parts.append(f"[我当时的主观感受，{row['at']:g}] {feelings}")
        if row.get("promise"):
            parts.append(f"[我当时记下的约定，成立与否以双方原话为准，{row['at']:g}] {row['promise']}")
    snapshots = additions.get("unfinished", [])
    if snapshots:
        # Earlier snapshots remain in the receipt for monotonic reconciliation;
        # only the latest state is rendered, including an explicit empty state.
        latest = max(snapshots, key=lambda row: row["id"])
        for item in latest["pending"]:
            parts.append(f"[{UNFINISHED_LABEL}] {item}")
    return ADDITIONS_MARKER + "；".join(parts) if parts else ""


def visit_recall_additions(meta: dict) -> list[str]:
    """Choose a few later opportunities; callers verify the full rendered suffix."""
    additions = meta.get("visit_additions") or {}
    parts = []
    snapshots = additions.get("unfinished", [])
    if snapshots:
        pending = max(snapshots, key=lambda row: row["id"])["pending"]
        if pending:
            parts.append(f"[{UNFINISHED_LABEL}] " + "；".join(pending[:2]))
    moments = additions.get("personal_moments", [])
    promise = next((row["promise"] for row in reversed(moments) if row.get("promise")), "")
    if promise:
        parts.append(f"[{PROMISE_LABEL}] {promise}")
    if not parts and moments:
        latest = next((row for row in reversed(moments) if row.get("reflection") or row.get("thought")), {})
        if latest:
            parts.append("[我当时的主观感受] " + str(latest.get("reflection") or latest["thought"]))
    return parts


def _merge_additions(old: dict, new: dict) -> dict | None:
    """Append by stable local identity; reject rewritten receipts, preserve evicted old rows."""
    previous, incoming = old.get("visit_additions") or {}, new.get("visit_additions") or {}
    if not isinstance(previous, dict) or not isinstance(incoming, dict):
        return None
    merged = {}
    for field, identity in (("local_experiences", "id"), ("milestones", "id"), ("personal_moments", "seq"),
                            ("unfinished", "id")):
        before, after = previous.get(field, []), incoming.get(field, [])
        if not isinstance(before, list) or not isinstance(after, list):
            return None
        rows = {}
        latest = max((row.get("seq", row.get("id", 0)) for row in before if isinstance(row, dict)
                      and type(row.get("seq", row.get("id"))) is int), default=0)
        previous_ids = {str(row[identity]) for row in before if isinstance(row, dict) and identity in row}
        for row in [*before, *after]:
            if not isinstance(row, dict) or identity not in row:
                return None
            if field == "unfinished" and (type(row[identity]) is not int or row[identity] < 0
                    or not isinstance(row.get("pending"), list) or len(row["pending"]) > 8
                    or any(not isinstance(item, str) or len(item) > 160 for item in row["pending"])):
                return None
            key = str(row[identity])
            if key in rows and rows[key] != row:
                return None
            if key not in previous_ids and latest and type(row.get("seq", row.get("id"))) is int and row.get("seq", row.get("id")) <= latest:
                return None
            rows[key] = row
        merged[field] = sorted(rows.values(), key=lambda row: (row.get("seq", row.get("id", 0)), row.get("at", 0)))
    return merged


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _metadata(value: str) -> dict:
    try:
        result = json.loads(value)
    except (ValueError, TypeError):
        return {}
    return result if isinstance(result, dict) else {}


def _facts(content: str) -> str:
    """The legacy adapter already separated confirmed facts from the ending."""
    body = content.partition("已经确认的经过：")[2]
    for ending in ("。这次已告别归家。", "。本次相处已结束："):
        if ending in body:
            return body.split(ending, 1)[0]
    return ""


def remember_visit(memory, *, confirmed_log: list[dict], idempotency_key: str, **kwargs) -> int | None:
    """Accept only confirmed extensions of the original trip and ownership.

    Generic remember() receipts keep their existing idempotency semantics.
    The content digest detects editor changes even when they bypass revisions;
    revisions additionally preserve deliberate edits that restore the old text.
    """
    meta = _metadata(kwargs.get("context", ""))
    if (not idempotency_key or meta.get("kind") != "world_visit"
            or not all(meta.get(key) for key in ("visit_id", "peer_id", "persona_key"))
            or meta.get("status") not in {"completed", "interrupted", "expired"}
            or meta.get("confirmed") is not True):
        return None
    content = str(kwargs.get("content") or "").strip()
    if not content or not confirmed_log:
        return None
    hashes = [_digest(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
              for row in confirmed_log]
    meta["visit_confirmation"] = {"log": hashes, "content": _digest(content)}
    kwargs["context"] = json.dumps(meta, ensure_ascii=False)
    with memory.db.atomic():
        receipt = memory.db._query("SELECT memory_id FROM memory_receipt WHERE batch_key=?",
                                   (idempotency_key,))
        if not receipt:
            return memory.remember(idempotency_key=idempotency_key, **kwargs)
        memory_id = int(receipt[0]["memory_id"])
        old = memory.db.memory_by_id(memory_id)
        # Deleted memories remain deleted, even when a delayed packet arrives.
        if old is None:
            return memory_id
        old_meta = _metadata(old.get("context", ""))
        if (old.get("source") != "life_event" or old.get("type") != "event"
                or old.get("pinned") or old.get("tier") != "raw" or old.get("folded_at")
                or any(old.get(key) != kwargs.get(key) for key in
                       ("session_id", "persona_id", "node_id", "scope"))
                or old.get("related_users") != (kwargs.get("related_users") or [])
                or old.get("participants") != (kwargs.get("participants") or [])
                or old_meta.get("kind") != "world_visit" or old_meta.get("confirmed") is not True
                or any(old_meta.get(key) != meta.get(key) for key in
                       ("visit_id", "peer_id", "persona_key"))):
            return memory_id
        if memory.db._query("SELECT 1 FROM memory_revision WHERE memory_id=? AND manual=1 LIMIT 1",
                            (memory_id,)):
            return memory_id
        if old_meta.get("status") == "completed" and meta["status"] != "completed":
            return memory_id
        from .memory_places import retain_visit_location
        location = retain_visit_location(old_meta.get("visit_location"), meta.get("visit_location"))
        if location is None:
            return memory_id
        if location:
            meta["visit_location"] = location
            kwargs["context"] = json.dumps(meta, ensure_ascii=False)
        from .visit_recall import visit_occurrence_interval
        if visit_occurrence_interval(old_meta) is not None:
            # An incomplete legacy snapshot cannot erase a previously recorded
            # occurrence. Complete newer confirmations may correct both times.
            previous_times = {key: old_meta[key] for key in ("occurred_start", "occurred_end") if key in old_meta}
            for key, value in previous_times.items():
                meta.setdefault(key, value)
            if visit_occurrence_interval(meta) is None:
                meta.update(previous_times)
            kwargs["context"] = json.dumps(meta, ensure_ascii=False)
        proof = old_meta.get("visit_confirmation")
        if isinstance(proof, dict):
            previous = proof.get("log")
            if (proof.get("content") != _digest(old["content"])
                    or not isinstance(previous, list) or not previous
                    or len(previous) > len(hashes) or hashes[:len(previous)] != previous):
                return memory_id
            incoming_suffix = render_visit_additions(meta)
            additions = _merge_additions(old_meta, meta)
            if additions is None:
                return memory_id
            if additions:
                if incoming_suffix and not content.endswith(incoming_suffix):
                    return memory_id
                base = content[:-len(incoming_suffix)] if incoming_suffix else content
                meta["visit_additions"] = additions
                content = base + render_visit_additions(meta)
                meta["visit_confirmation"]["content"] = _digest(content)
                kwargs.update(content=content, context=json.dumps(meta, ensure_ascii=False))
            if (len(previous) == len(hashes) and old_meta.get("status") == meta["status"]
                    and old_meta.get("end_reason", "") == meta.get("end_reason", "")
                    and all(old_meta.get(key) == meta.get(key) for key in ("occurred_start", "occurred_end"))
                    and old_meta.get("visit_location", {}) == location and old["content"] == content):
                return memory_id
        else:
            # A pre-upgrade receipt has no fingerprints. Upgrade only when its
            # rendered facts are still a prefix of the newly confirmed facts.
            before, after = _facts(old["content"]), _facts(content)
            if not before or not after or not (after == before or after.startswith(before + "；")):
                return memory_id
        memory.db.revise_memory(memory_id, {"content": content, "context": kwargs["context"]},
                                manual=False, reason="补全同一次串门的最终确认经过")
        return memory_id
