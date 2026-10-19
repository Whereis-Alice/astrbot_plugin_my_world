"""Immutable source snapshots, durable frozen batches and idempotent memory receipts."""
from __future__ import annotations

import hashlib
import json
import time
import re
from html.parser import HTMLParser
from typing import Any

EVIDENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_source(id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
    snapshot TEXT NOT NULL, eligible INTEGER NOT NULL DEFAULT 0, observed_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS memory_evidence(memory_id INTEGER NOT NULL, source_id TEXT NOT NULL,
    PRIMARY KEY(memory_id, source_id));
CREATE TABLE IF NOT EXISTS memory_receipt(batch_key TEXT PRIMARY KEY, memory_id INTEGER NOT NULL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS memory_batch(batch_key TEXT PRIMARY KEY, session_id TEXT NOT NULL,
    payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS memory_revision(id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id INTEGER NOT NULL,
    before_payload TEXT NOT NULL, after_payload TEXT NOT NULL, at REAL NOT NULL, manual INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS memory_vector(memory_id INTEGER NOT NULL, model_key TEXT NOT NULL,
    revision TEXT NOT NULL, vector TEXT NOT NULL, PRIMARY KEY(memory_id, model_key));
CREATE INDEX IF NOT EXISTS idx_memory_source_age ON memory_source(observed_at);
CREATE INDEX IF NOT EXISTS idx_memory_evidence_source ON memory_evidence(source_id);
CREATE INDEX IF NOT EXISTS idx_memory_batch_pending ON memory_batch(session_id,status,at);
"""


EXTERNAL_MEMORY = re.compile(r"<(Anamnesis-Memory|RAG-Faiss-Memory)>.*?</\1>", re.S | re.I)


def split_external_memory(text: str) -> tuple[str, str]:
    """Separate known injected blocks; never mutate another plugin's request history."""
    blocks = []
    def collect(match):
        blocks.append(match.group(0))
        return ""
    body = EXTERNAL_MEMORY.sub(collect, str(text or ""))
    # A truncated injected block must not turn into direct speech either.
    match = re.search(r"<(?:Anamnesis-Memory|RAG-Faiss-Memory)>", body, re.I)
    if match:
        blocks.append(body[match.start():])
        body = body[:match.start()]
    return body.strip(), "\n\n".join(blocks)


class _DirectText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"quote", "blockquote", "quoted_message"}:
            if not self.depth:
                self.parts.append("\n")
            self.depth += 1

    def handle_endtag(self, tag):
        if tag in {"quote", "blockquote", "quoted_message"} and self.depth:
            self.depth -= 1
            if not self.depth:
                self.parts.append("\n")

    def handle_data(self, data):
        if not self.depth:
            self.parts.append(data)


def direct_text(text: str) -> str:
    body, _ = split_external_memory(text)
    body = body.split("[我的世界·此刻]", 1)[0]
    body = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith(">"))
    # Only parse explicit quote markup; ordinary comparisons such as 'a < b' remain intact.
    if re.search(r"</?(?:quote|blockquote|quoted_message)\b", body, re.I):
        parser = _DirectText()
        parser.feed(body)
        body = "".join(parser.parts)
    return body.strip()


def source_snapshot(record: dict, session_id: str) -> dict:
    raw = {key: record.get(key) for key in ("user_id", "name", "text", "at", "message_id", "event_id", "is_self")}
    raw["session"] = str(record.get("origin") or record.get("session") or session_id)
    raw["kind"] = "self_statement" if record.get("is_self") else "direct_speech"
    if record.get("is_recalled") or record.get("kind") in {"recalled_memory", "tool_result", "model_summary", "operation_log", "quote"}:
        raw["kind"] = str(record.get("kind") or "recalled_memory")
    explicit = str(record.get("source_id") or record.get("event_key") or "")
    identity = [raw["session"], explicit] if explicit else raw
    raw["id"] = explicit if explicit.startswith("src_") else "src_" + hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:32]
    return raw


class EvidenceStoreMixin:
    def memory_by_id(self, memory_id: int) -> dict | None:
        rows = self._query("SELECT * FROM node_memory WHERE id=?", (memory_id,))
        return self._memory_row(rows[0]) if rows else None

    def observe_source(self, source: dict, *, eligible: bool = False) -> str:
        self._execute("INSERT INTO memory_source(id,session_id,snapshot,eligible,observed_at) VALUES(?,?,?,?,?) "
                      "ON CONFLICT(id) DO UPDATE SET eligible=MAX(eligible,excluded.eligible)",
                      (source["id"], source["session"], json.dumps(source, ensure_ascii=False), int(eligible), time.time()))
        if time.time() - getattr(self, "_source_cleanup_at", 0) > 86400:
            self._source_cleanup_at = time.time()
            self._execute("DELETE FROM memory_source WHERE observed_at<? AND NOT EXISTS "
                          "(SELECT 1 FROM memory_evidence WHERE source_id=memory_source.id) AND NOT EXISTS "
                          "(SELECT 1 FROM memory_batch WHERE status='pending' AND instr(payload,memory_source.id)>0)",
                          (time.time() - 90 * 86400,))
        return source["id"]

    def source_record(self, source_id: str) -> dict | None:
        rows = self._query("SELECT snapshot,eligible FROM memory_source WHERE id=?", (source_id,))
        return {**json.loads(rows[0]["snapshot"]), "eligible": bool(rows[0]["eligible"])} if rows else None

    def attach_evidence(self, memory_id: int, source_ids: list[str]) -> None:
        for source_id in source_ids:
            self._execute("INSERT OR IGNORE INTO memory_evidence(memory_id,source_id) "
                          "SELECT ?,id FROM memory_source WHERE id=? AND eligible=1", (memory_id, source_id))

    def memory_sources(self, memory_id: int) -> dict[str, Any]:
        rows = self._query("SELECT s.snapshot,s.eligible FROM memory_source s JOIN memory_evidence e ON e.source_id=s.id WHERE e.memory_id=?", (memory_id,))
        revisions = self._query("SELECT before_payload,after_payload,at,manual FROM memory_revision WHERE memory_id=? ORDER BY id DESC LIMIT 20", (memory_id,))
        return {"sources": [{**json.loads(row["snapshot"]), "eligible": bool(row["eligible"])} for row in rows],
                "revisions": [{"before": json.loads(row["before_payload"]), "after": json.loads(row["after_payload"]),
                               "at": row["at"], "manual": bool(row["manual"])} for row in revisions]}

    def freeze_memory_batch(self, key: str, session_id: str, payload: dict) -> None:
        self._execute("INSERT OR IGNORE INTO memory_batch(batch_key,session_id,payload,at) VALUES(?,?,?,?)",
                      (key, session_id, json.dumps(payload, ensure_ascii=False), time.time()))

    def pending_memory_batch(self, session_id: str) -> dict | None:
        rows = self._query("SELECT batch_key,payload FROM memory_batch WHERE session_id=? AND status='pending' ORDER BY at LIMIT 1", (session_id,))
        return {"key": rows[0]["batch_key"], **json.loads(rows[0]["payload"])} if rows else None

    def has_pending_memory_batch(self, session_ids: list[str]) -> bool:
        if not session_ids:
            return False
        marks = ",".join("?" for _ in session_ids)
        return bool(self._query(f"SELECT 1 FROM memory_batch WHERE status='pending' AND session_id IN ({marks}) LIMIT 1", session_ids))

    def finish_memory_batch(self, key: str) -> None:
        self._execute("UPDATE memory_batch SET status='done' WHERE batch_key=?", (key,))
        self._execute("DELETE FROM memory_batch WHERE status='done' AND at < ?", (time.time() - 30*86400,))

    def revise_memory(self, memory_id: int, fields: dict, *, manual: bool = True) -> None:
        with self.atomic():
            old = [self._memory_row(row) for row in self._query("SELECT * FROM node_memory WHERE id=?", (memory_id,))]
            if not old:
                return
            self._execute("INSERT INTO memory_revision(memory_id,before_payload,after_payload,at,manual) VALUES(?,?,?,?,?)",
                          (memory_id, json.dumps(old[0], ensure_ascii=False), json.dumps({**old[0], **fields}, ensure_ascii=False), time.time(), int(manual)))
            self.update_memory(memory_id, **fields)
            self._execute("DELETE FROM memory_vector WHERE memory_id=?", (memory_id,))
