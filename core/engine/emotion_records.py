"""Management of a conversation's current and archived feelings."""
from __future__ import annotations

import asyncio
from hashlib import sha256
import json
import math
import time
from typing import Any

from ..emotional_memory import EMOTION_NARRATIVE_LIMIT, EMOTION_SUMMARY_LIMIT, emotion_narrative
from ..memory import INNER
from ..state import WorldState


EMOTION_FIELDS = {"heart_knot": ("heart_knots", "text", "about", "since"),
                  "grudge": ("grudges", "reason", "user_id", "at")}
_ARCHIVE_LABELS = {
    ("heart_knot", "resolved"): "（心里那件事放下了）",
    ("heart_knot", "expired"): "（心里搁过的事）",
    ("grudge", "resolved"): "（跟他算清了一笔账）",
    ("grudge", "expired"): "（气过一阵就算了）",
}


class EmotionRecordConflict(ValueError):
    """The editor's revision no longer describes this record."""


def _stamp(value: Any) -> float:
    try:
        result = float(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return result if math.isfinite(result) and result > 0 else 0.0


def _revision(value: Any) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]


def _archive_content(kind: str, status: str, text: str, name: str) -> str:
    prefix = _ARCHIVE_LABELS[kind, status]
    return prefix + (name + "：" if kind == "grudge" and name else "") + text


def _evidence_rows(value: Any) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [{"text": row["text"][:240], "at": _stamp(row.get("at")),
             "user_name": str(row.get("user_name") or "")[:80]}
            for row in value[:3] if isinstance(row, dict) and isinstance(row.get("text"), str) and row["text"]]


class EmotionRecordsMixin:
    def _emotion_enabled(self, kind: str) -> bool:
        if kind == "heart_knot":
            return self.world.state_dynamics.heart_knot_enabled
        return self.world.profile.grudge_enabled

    def _emotion_strength(self, item: dict, now: float) -> float:
        last = _stamp(item.get("at")) or _stamp(item.get("since")) or now
        hours = max(0.0, (now - last) / 3600)
        rate = max(0.0, self.world.state_dynamics.heart_knot_decay_per_hour)
        return _stamp(item.get("strength")) - rate * hours

    def _emotion_status(self, kind: str, item: dict, now: float) -> str:
        until = self._emotion_expiry(kind, item)
        if (until and until <= now) or (kind == "heart_knot" and self._emotion_strength(item, now) <= .02):
            return "expired"
        return "paused" if item.get("paused") else "active"

    def _emotion_expiry(self, kind: str, item: dict) -> float:
        until = _stamp(item.get("until"))
        if kind == "grudge" and (started := _stamp(item.get("at"))):
            days = max(1, self.world.profile.grudge_days)
            if not until:
                return started + days * 86400
            # Old saves had no duration marker. Apply the new default to rows
            # carrying the old default window, leaving other saved windows intact.
            if days == 1 and "duration_days" not in item and math.isclose(
                    until - started, 7 * 86400, rel_tol=0, abs_tol=1):
                return started + 86400
        return until

    def _emotion_record_id(self, state: WorldState, kind: str, item: dict) -> str:
        _, text, user, created = EMOTION_FIELDS[kind]
        return str(item.get("id") or "legacy_" + _revision([
            kind, item.get("session") or state.session_id, item.get("persona_key") or "",
            item.get(user) or "", _stamp(item.get(created)), item.get(text) or "",
        ]))

    def _emotion_row(self, state: WorldState, kind: str, item: dict, persona_key: str | None, now: float) -> dict:
        _, text, user, created = EMOTION_FIELDS[kind]
        row = {
            "id": self._emotion_record_id(state, kind, item), "kind": kind,
            "text": str(item.get(text) or ""), "user_id": str(item.get(user) or ""),
            "narrative": emotion_narrative(item.get("narrative"))[:EMOTION_NARRATIVE_LIMIT],
            "evidence": _evidence_rows(item.get("evidence")),
            "user_name": str(item.get("who_name") or ""),
            "session": str(item.get("session") or state.session_id),
            "persona_key": str(item.get("persona_key") or ""),
            "created_at": _stamp(item.get(created)), "expires_at": self._emotion_expiry(kind, item),
            "updated_at": _stamp(item.get("edited_at")) or _stamp(item.get(created)),
            "status": self._emotion_status(kind, item, now), "archived": False,
        }
        return self._describe_emotion_row(row, persona_key)

    def _describe_emotion_row(self, row: dict, persona_key: str | None) -> dict:
        row["revision"] = _revision(row)
        row["current_persona"] = not row["persona_key"] or row["persona_key"] == persona_key
        row["feature_enabled"] = self._emotion_enabled(row["kind"])
        row["affects_dialogue"] = row["status"] == "active" and row["current_persona"] and row["feature_enabled"]
        return row

    def _archived_emotion_row(self, memory: dict, meta: dict, persona_key: str | None) -> dict:
        status = str(meta.get("emotion_status") or "")
        if status not in {"resolved", "expired"}:
            status = "resolved" if str(memory["content"]).startswith(_ARCHIVE_LABELS["grudge", "resolved"]) else "expired"
        text = str(meta.get("original") or memory["content"])
        name = str(meta.get("emotion_name") or "")
        if not name and meta.get("kind") == "grudge" and meta.get("original"):
            name = str(memory["content"]).removeprefix(_ARCHIVE_LABELS["grudge", status]).removesuffix(text).rstrip("：")
        return self._describe_emotion_row({
            "id": str(meta.get("emotion_id") or f"memory_{memory['id']}"), "kind": meta["kind"],
            "text": text, "user_id": str(meta.get("emotion_user") or ""), "user_name": name,
            "narrative": emotion_narrative(meta.get("emotion_narrative"))[:EMOTION_NARRATIVE_LIMIT],
            "evidence": _evidence_rows(meta.get("emotion_evidence")),
            "session": str(memory["session_id"]), "persona_key": str(meta.get("persona_key") or ""),
            "created_at": _stamp(meta.get("emotion_created_at")), "expires_at": _stamp(meta.get("emotion_expires_at")),
            "updated_at": _stamp(meta.get("emotion_updated_at")) or _stamp(memory.get("created_at")),
            "status": status, "archived": True,
        }, persona_key)

    def _emotion_sources(self, state: WorldState, session_id: str, persona_key: str | None, *,
                         include_shadowed: bool = False):
        persona = self._matter_persona_key(state) if persona_key is None else persona_key
        now = self._now()
        active_ids = set()
        for kind, (field, _, _, _) in EMOTION_FIELDS.items():
            for item in getattr(state, field):
                if str(item.get("session") or state.session_id) != session_id:
                    continue
                row = self._emotion_row(state, kind, item, persona, now)
                active_ids.add(row["id"])
                yield row, item, None
        for memory in self.memory.db.query_emotion_memories(session_id=session_id):
            meta = json.loads(memory["context"])
            row = self._archived_emotion_row(memory, meta, persona)
            if include_shadowed or row["id"] not in active_ids:
                yield row, meta, memory

    def emotion_records(self, state: WorldState, session_id: str, *, persona_key: str | None = None) -> list[dict]:
        """List editable records for this origin, including other personas and archives."""
        rows = [row for row, _, _ in self._emotion_sources(state, session_id, persona_key)]
        return sorted(rows, key=lambda row: (row["status"] not in {"active", "paused"}, -row["updated_at"], row["id"]))

    def _archive_emotion(self, state: WorldState, item: dict, *, kind: str, status: str, now: float) -> int | None:
        row = self._emotion_row(state, kind, item, "", now)
        if not row["text"]:
            return None
        uid, name = row["user_id"], row["user_name"]
        return self.memory.remember(session_id=row["session"], persona_id="", node_id=state.node_id,
            content=_archive_content(kind, status, row["text"], name), memory_type=INNER,
            emotion=state.mood, weight=.35 if status == "resolved" else .3,
            affect=state.affect, valence=state.valence, participants=[uid] if uid else [], source="runtime",
            conflict_policy="newest", idempotency_key="emotion:" + row["id"],
            context=json.dumps({"kind": kind, "persona_key": row["persona_key"], "emotion_user": uid,
                "emotion_name": name, "original": row["text"], "emotion_id": row["id"], "source_scoped": True,
                "emotion_status": status, "emotion_created_at": row["created_at"],
                "emotion_narrative": row["narrative"], "emotion_evidence": row["evidence"],
                "emotion_expires_at": row["expires_at"], "emotion_updated_at": now}, ensure_ascii=False))

    def update_emotion_record(self, state: WorldState, session_id: str, record_id: str, operation: str,
                              *, text: str | None = None, narrative: str | None = None,
                              revision: str | None = None) -> dict:
        """Apply an editor mutation while the caller holds the shared world's state lock."""
        source = next((entry for entry in self._emotion_sources(state, session_id, None)
                       if entry[0]["id"] == record_id), None)
        if source is None:
            raise ValueError("这条未释怀记录已不存在")
        row, item, memory = source
        if revision is not None and revision != row["revision"]:
            raise EmotionRecordConflict("这条记录已经变化，请刷新后重试")
        kind = row["kind"]
        field, text_field, _, _ = EMOTION_FIELDS[kind]
        entries = getattr(state, field)
        now = self._now()
        persona = self._matter_persona_key(state)
        if operation == "edit":
            if not isinstance(text, str):
                raise ValueError("请填写短标题")
            body = " ".join(text.split())
            if not 1 <= len(body) <= EMOTION_SUMMARY_LIMIT:
                raise ValueError(f"短标题须为 1 至 {EMOTION_SUMMARY_LIMIT} 字")
            account = row["narrative"] if narrative is None else emotion_narrative(narrative)
            if len(account) > EMOTION_NARRATIVE_LIMIT:
                raise ValueError(f"第一人称记录最多 {EMOTION_NARRATIVE_LIMIT} 字")
            if memory is not None:
                item.update(original=body, emotion_narrative=account, emotion_updated_at=now, source_scoped=True)
                content = _archive_content(kind, row["status"], body, row["user_name"])
                self.memory.db.revise_memory(memory["id"], {"content": content,
                    "context": json.dumps(item, ensure_ascii=False)}, reason="管理员整理未释怀记录")
                return self._archived_emotion_row({**memory, "content": content}, item, persona)
            item.update({"id": record_id, text_field: body, "narrative": account, "edited_at": now})
        elif operation == "delete":
            if memory is not None:
                self.memory.db.delete_memories_by_ids([memory["id"]])
            else:
                setattr(state, field, [entry for entry in entries if entry is not item])
            return {**row, "status": "deleted", "affects_dialogue": False}
        elif operation == "resolve" and row["status"] in {"active", "paused"}:
            memory_id = self._archive_emotion(state, item, kind=kind, status="resolved", now=now)
            archived = self.memory.db.memory_by_id(memory_id) if memory_id is not None else None
            if archived is None:
                raise ValueError("归档暂未保存，原记录仍保留")
            saved = self._archived_emotion_row(archived, json.loads(archived["context"]), persona)
            setattr(state, field, [entry for entry in entries if entry is not item])
            return saved
        elif (operation, row["status"]) in {("pause", "active"), ("resume", "paused")}:
            if operation == "resume" and not row["feature_enabled"]:
                raise ValueError("当前配置已关闭这类记录")
            if operation == "resume" and not self._has_emotion_capacity(state, kind, row["persona_key"], now):
                raise ValueError("生效中的同类记录已达上限，请先整理其中一条")
            item.update(id=record_id, paused=operation == "pause", edited_at=now)
        else:
            raise ValueError("该记录的当前状态不支持此操作")
        return self._emotion_row(state, kind, item, persona, now)

    async def delete_emotion_records(self, state: WorldState, records: list[dict]) -> int:
        """Delete a reviewed batch from one locked life and its exact source sessions."""
        from .life_inference import STATE_LEASE

        lease = STATE_LEASE.get() or {}
        if lease.get("state") is not state or lease.get("owner") is not asyncio.current_task() or lease.get("released"):
            raise RuntimeError("删除未释怀记录需要持有生活状态锁")
        selected: dict[str, dict[str, str]] = {}
        for record in records:
            origin, record_id, revision = (record.get(key) for key in ("session", "id", "revision"))
            if not all(isinstance(value, str) and value for value in (origin, record_id, revision)):
                raise ValueError("记录标识或版本无效，请刷新后重试")
            if self.state_key(origin) != state.session_id:
                raise EmotionRecordConflict("记录来源不在当前生活范围")
            expected = selected.setdefault(origin, {})
            if record_id in expected:
                raise ValueError("所选记录重复，请刷新后重试")
            expected[record_id] = revision
        if not selected:
            raise ValueError("请选择要删除的记录")

        # This operation owns the only state write. A cancelled caller still
        # waits for the transaction via AsyncDatabase.run before releasing the lock.
        lease["save"] = False
        changes, stamp = await self.db.run(self._delete_emotion_batch, state, selected)
        for field, remaining in changes.items():
            setattr(state, field, remaining)
        if changes:
            state.updated_at = stamp
        return len(records)

    def _delete_emotion_batch(self, state: WorldState, selected: dict[str, dict[str, str]]) -> tuple[dict, float]:
        db = self.memory.db
        live_ids: dict[str, set] = {}
        archived_ids: set[int] = set()
        with db.atomic():
            for origin, expected in selected.items():
                matches: dict[str, list] = {}
                with self.config_scope(origin):
                    for row, item, memory in self._emotion_sources(state, origin, None, include_shadowed=True):
                        if row["id"] in expected:
                            matches.setdefault(row["id"], []).append((row, item, memory))
                for record_id, revision in expected.items():
                    entries = matches.get(record_id, [])
                    if not entries or entries[0][0]["revision"] != revision:
                        raise EmotionRecordConflict("有记录已经变化，请刷新后重新选择")
                    for row, item, memory in entries:
                        if memory is None:
                            field = EMOTION_FIELDS[row["kind"]][0]
                            live_ids.setdefault(field, set()).add(id(item))
                        else:
                            archived_ids.add(memory["id"])

            ids = sorted(archived_ids)
            for offset in range(0, len(ids), 400):
                db.delete_memories_by_ids(ids[offset:offset + 400])
            changes = {field: [entry for entry in getattr(state, field) if id(entry) not in doomed]
                       for field, doomed in live_ids.items()}
            stamp = time.time()
            if changes:
                db.save_state(state.session_id, {**state.to_payload(), **changes, "updated_at": stamp}, stamp)
        return changes, stamp

    def _has_emotion_capacity(self, state: WorldState, kind: str, persona_key: str, now: float) -> bool:
        limit = self.world.profile.grudge_max if kind == "grudge" else self.world.state_dynamics.heart_knot_max
        field, _, _, _ = EMOTION_FIELDS[kind]
        count = sum(self._emotion_belongs(state, item, persona_key) and self._emotion_status(kind, item, now) == "active"
                    for item in getattr(state, field))
        return count < max(1, limit)
