"""The life journal boundary and a recoverable, one-time database migration.

Contacts, profile facts and the group vocabulary deliberately live outside this
boundary. They are never removed by this migration.
"""
from __future__ import annotations

import copy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time
from typing import Any

LIFE_TYPES = frozenset({"scene", "event", "inner", "dream"})
LIFE_SOURCES = frozenset({"runtime", "life_matter", "life_event", "preset", "manual", "import", "dream", "chat_browse"})
LIFE_DOMAIN = "my_world.life"
LIFE_SQL = "type IN ('scene','event','inner','dream') AND source IN ('runtime','life_matter','life_event','preset','manual','import','dream','chat_browse')"
# Stored JSON arrays are normalized by Database.add_memory. Private progress and
# reading feelings must never be broadened by a shared world or a global recall.
SOURCE_SCOPED_SQL = ("(source='chat_browse' OR (source='life_matter' AND "
    "(coalesce(participants,'[]')!='[]' OR coalesce(related_users,'[]')!='[]')) OR "
    "(source='runtime' AND coalesce(CASE WHEN json_valid(context) THEN json_extract(context,'$.kind') END,'') "
    "IN ('heart_knot','grudge') AND coalesce(CASE WHEN json_valid(context) THEN json_extract(context,'$.source_scoped') END,0)=1))")
RETIRED_MEMORY = {"dialogue_summary", "summary_trigger_messages", "summary_idle_minutes", "summary_on_move", "summary_max_chars"}
RETIRED_CONTEXT = {"chat_overflow", "chat_compress_threshold", "chat_keep_after_compress", "summary_refresh_minutes"}
RETIRED_STATE = {"pending_memory", "pending_memory_node", "memory_hints", "memory_flush_at", "memory_flush_wanted",
                 "chat_summary", "chat_summary_at", "chat_summaries", "chat_dropped", "chat_overflow_warned_at"}


def is_life_memory(row: dict) -> bool:
    kind, source = row.get("type", "scene"), row.get("source", "manual")
    return isinstance(kind, str) and isinstance(source, str) and kind in LIFE_TYPES and source in LIFE_SOURCES


def scoped_emotion(source: str, context: Any) -> bool:
    if source != "runtime":
        return False
    try:
        meta = json.loads(context) if isinstance(context, str) else context
    except (ValueError, TypeError):
        return False
    return (isinstance(meta, dict) and meta.get("kind") in {"heart_knot", "grudge"}
            and meta.get("source_scoped") is True)


def clean_memory_settings(data: dict) -> dict:
    """Drop retired settings even when models allow unknown keys; clean overrides too."""
    result = copy.deepcopy(data)
    result.pop("life_execution", None)
    memory = result.get("memory")
    if isinstance(memory, dict):
        for key in RETIRED_MEMORY:
            memory.pop(key, None)
        if memory.get("automatic_recall") in {"all", "life_only"}:
            memory["automatic_recall"] = "auto"
    context = result.get("context")
    if isinstance(context, dict):
        for key in RETIRED_CONTEXT:
            context.pop(key, None)
    for session in result.get("sessions", []):
        if not isinstance(session, dict):
            continue
        settings = (session.get("config_overrides") or {}).get("settings")
        if isinstance(settings, dict):
            session["config_overrides"]["settings"] = clean_memory_settings(settings)
    return result


def migrate_life_memory(conn: sqlite3.Connection, path: str) -> None:
    """Back up *before* removal, then commit journal/state cleanup atomically.

    An existing backup is never overwritten. A failed backup aborts startup;
    a failed transaction leaves the source intact and can be retried safely.
    """
    marker = "migration:life_journal:1"
    if conn.execute("SELECT 1 FROM kv WHERE key=?", (marker,)).fetchone():
        return
    old = conn.execute("SELECT COUNT(*) FROM node_memory WHERE NOT (" + LIFE_SQL + ") OR source='import'").fetchone()[0]
    states = list(conn.execute("SELECT session_id,payload FROM world_state"))
    batch_exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_batch'").fetchone()
    batches = bool(batch_exists and conn.execute("SELECT 1 FROM memory_batch LIMIT 1").fetchone())
    changed_states = []
    for row in states:
        payload = json.loads(row["payload"])
        if RETIRED_STATE.intersection(payload):
            changed_states.append((json.dumps({k: v for k, v in payload.items() if k not in RETIRED_STATE}, ensure_ascii=False), row["session_id"]))
    backup_path = ""
    if old or changed_states or batches:
        directory = Path(path).parent / "backups"
        directory.mkdir(parents=True, exist_ok=True)
        # mkstemp avoids collisions after a process interruption or a restore.
        import tempfile
        import os
        fd, backup_path = tempfile.mkstemp(prefix="before-life-journal-", suffix=".sqlite3", dir=directory)
        os.close(fd)
        try:
            with closing(sqlite3.connect(backup_path)) as backup:
                conn.backup(backup)
                if backup.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("生活记忆迁移备份校验失败")
        except BaseException:
            Path(backup_path).unlink(missing_ok=True)
            raise
    with conn:
        # Dreams are subjective experiences, never evidence of an outside event.
        conn.execute("UPDATE node_memory SET type='dream' WHERE source='dream'")
        removed = "SELECT id FROM node_memory WHERE NOT (" + LIFE_SQL + ") OR source='import'"
        for table in ("memory_evidence", "memory_vector", "memory_revision", "memory_receipt"):
            conn.execute(f"DELETE FROM {table} WHERE memory_id IN ({removed})")
        archived = conn.execute("SELECT COUNT(*) FROM node_memory WHERE id IN (" + removed + ")").fetchone()[0]
        conn.execute("DELETE FROM node_memory WHERE id IN (" + removed + ")")
        conn.executemany("UPDATE world_state SET payload=? WHERE session_id=?", changed_states)
        conn.execute("DROP TABLE IF EXISTS memory_batch")
        conn.execute("INSERT INTO kv(key,value,updated_at) VALUES(?,?,?)",
                     (marker, json.dumps({"backup": backup_path, "archived": archived, "states": len(changed_states)}), time.time()))
