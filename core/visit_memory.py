"""Monotonic confirmation updates for a trip's single durable memory receipt."""
from __future__ import annotations

import hashlib
import json


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
        proof = old_meta.get("visit_confirmation")
        if isinstance(proof, dict):
            previous = proof.get("log")
            if (proof.get("content") != _digest(old["content"])
                    or not isinstance(previous, list) or not previous
                    or len(previous) > len(hashes) or hashes[:len(previous)] != previous):
                return memory_id
            if (len(previous) == len(hashes) and old_meta.get("status") == meta["status"]
                    and old_meta.get("end_reason", "") == meta.get("end_reason", "")):
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
