"""Scoped, evidence-backed interaction learning; never grants relationship permissions."""

from __future__ import annotations

import json
import math
import time
from typing import Any
from .model_call_runtime import safe_error

SOCIAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS interaction_evaluation (
 id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, user_id TEXT NOT NULL,
 source_key TEXT NOT NULL, delta REAL NOT NULL, reason TEXT NOT NULL, evidence TEXT NOT NULL,
 at REAL NOT NULL, UNIQUE(session_id,user_id,source_key));
CREATE INDEX IF NOT EXISTS idx_interaction_person ON interaction_evaluation(session_id,user_id,id);
CREATE TABLE IF NOT EXISTS interaction_source_receipt (
 session_id TEXT NOT NULL,user_id TEXT NOT NULL,source_id TEXT NOT NULL,
 PRIMARY KEY(session_id,user_id,source_id));
CREATE TABLE IF NOT EXISTS interaction_style (
 session_id TEXT NOT NULL, user_id TEXT NOT NULL, cursor INTEGER NOT NULL DEFAULT 0,
 revision INTEGER NOT NULL DEFAULT 0, habits TEXT NOT NULL DEFAULT '[]', manual INTEGER NOT NULL DEFAULT 0,
 retry_at REAL NOT NULL DEFAULT 0, PRIMARY KEY(session_id,user_id));
CREATE TABLE IF NOT EXISTS session_vocabulary (
 session_id TEXT NOT NULL, term TEXT NOT NULL, description TEXT NOT NULL,
 evidence TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'active', manual INTEGER NOT NULL DEFAULT 0,
 updated_at REAL NOT NULL, PRIMARY KEY(session_id,term));
CREATE TABLE IF NOT EXISTS decision_trace (
 id INTEGER PRIMARY KEY AUTOINCREMENT, trace_id TEXT NOT NULL, session_id TEXT NOT NULL,
 stage TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL, details TEXT NOT NULL,
 at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_decision_session ON decision_trace(session_id,at DESC,id DESC);
"""


def clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


class SocialStoreMixin:
    def record_interaction(
        self,
        session: str,
        user: str,
        source_key: str,
        delta: float,
        reason: str,
        evidence: list[dict],
        *,
        now: float,
    ) -> bool:
        if not session or not user or not source_key or not evidence or not math.isfinite(delta):
            return False
        ids = list(dict.fromkeys(row["id"] for row in evidence))
        with self.atomic():
            if any(
                self._query(
                    "SELECT 1 FROM interaction_source_receipt WHERE session_id=? AND user_id=? AND source_id=?",
                    (session, user, sid),
                )
                for sid in ids
            ):
                return False
            cursor = self._execute(
                "INSERT OR IGNORE INTO interaction_evaluation"
                "(session_id,user_id,source_key,delta,reason,evidence,at) VALUES(?,?,?,?,?,?,?)",
                (
                    session,
                    user,
                    source_key,
                    delta,
                    clean(reason, 200),
                    json.dumps(evidence, ensure_ascii=False),
                    now,
                ),
            )
            if cursor.rowcount:
                for sid in ids:
                    self._execute(
                        "INSERT INTO interaction_source_receipt VALUES(?,?,?)", (session, user, sid)
                    )
                if cursor.lastrowid % 50 == 0:
                    self._prune_interactions(session, user)
            return bool(cursor.rowcount)

    def interaction_detail(self, session: str, user: str) -> dict:
        rows = self._query(
            "SELECT * FROM interaction_style WHERE session_id=? AND user_id=?", (session, user)
        )
        style = (
            dict(rows[0])
            if rows
            else {"cursor": 0, "revision": 0, "habits": "[]", "manual": 0, "retry_at": 0}
        )
        style["habits"] = json.loads(style["habits"])
        history = self._query(
            "SELECT * FROM interaction_evaluation WHERE session_id=? AND user_id=? ORDER BY id DESC LIMIT 50",
            (session, user),
        )
        return {
            **style,
            "history": [{**dict(row), "evidence": json.loads(row["evidence"])} for row in history],
        }

    def pending_interaction_styles(self, session: str, now: float) -> list[dict]:
        return [
            dict(row)
            for row in self._query(
                """SELECT e.user_id, COUNT(*) AS count
            FROM interaction_evaluation e LEFT JOIN interaction_style s
            ON s.session_id=e.session_id AND s.user_id=e.user_id
            WHERE e.session_id=? AND e.id>COALESCE(s.cursor,0) AND e.delta<>0
            AND COALESCE(s.manual,0)=0 AND COALESCE(s.retry_at,0)<=?
            GROUP BY e.user_id HAVING COUNT(*)>=5 ORDER BY MIN(e.id) LIMIT 2""",
                (session, now),
            )
        ]

    def pending_interactions(self, session: str, user: str, cursor: int) -> list[dict]:
        return [
            {**dict(row), "evidence": json.loads(row["evidence"])}
            for row in self._query(
                "SELECT * FROM interaction_evaluation WHERE session_id=? AND user_id=? AND id>? AND delta<>0 ORDER BY id LIMIT 50",
                (session, user, cursor),
            )
        ]

    def save_interaction_style(
        self,
        session: str,
        user: str,
        habits: list,
        *,
        revision: int,
        through: int = 0,
        manual: bool = False,
    ) -> bool:
        with self.atomic():
            old = self.interaction_detail(session, user)
            if old["revision"] != revision or (old["manual"] and not manual):
                return False
            self._execute(
                "INSERT INTO interaction_style(session_id,user_id,cursor,revision,habits,manual) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(session_id,user_id) DO UPDATE SET "
                "cursor=excluded.cursor,revision=excluded.revision,habits=excluded.habits,manual=excluded.manual,retry_at=0",
                (
                    session,
                    user,
                    max(old["cursor"], through),
                    revision + 1,
                    json.dumps(habits[:3], ensure_ascii=False),
                    int(manual),
                ),
            )
            self._prune_interactions(session, user)
            return True

    def _prune_interactions(self, session: str, user: str) -> None:
        # Keep evidence used by a current habit, the newest 500 evaluations and
        # unprocessed automatic-learning work. Compact receipts keep replay protection.
        detail = self.interaction_detail(session, user)
        referenced = [
            i for h in detail["habits"] for i in h.get("source_ids", []) if isinstance(i, int)
        ]
        reference_filter = (
            "AND id NOT IN (" + ",".join("?" for _ in referenced) + ") " if referenced else ""
        )
        self._execute(
            "DELETE FROM interaction_evaluation WHERE session_id=? AND user_id=? "
            "AND id<(SELECT id FROM interaction_evaluation WHERE session_id=? AND user_id=? ORDER BY id DESC LIMIT 1 OFFSET 499) "
            + reference_filter
            + "AND (? OR id<=? OR delta=0)",
            [session, user, session, user, *referenced, int(detail["manual"]), detail["cursor"]],
        )

    def resume_interaction_style(self, session: str, user: str, revision: int) -> bool:
        with self.atomic():
            current = self.interaction_detail(session, user)
            if current["revision"] != revision or not current["manual"]:
                return False
            self._execute(
                "UPDATE interaction_style SET manual=0,revision=revision+1,retry_at=0 WHERE session_id=? AND user_id=?",
                (session, user),
            )
            return True

    def defer_interaction_style(self, session: str, user: str, now: float) -> None:
        self._execute(
            "INSERT INTO interaction_style(session_id,user_id,retry_at) VALUES(?,?,?) "
            "ON CONFLICT(session_id,user_id) DO UPDATE SET retry_at=excluded.retry_at",
            (session, user, now + 300),
        )

    def vocabulary(self, session: str) -> list[dict]:
        return [
            {**dict(row), "evidence": json.loads(row["evidence"])}
            for row in self._query(
                "SELECT * FROM session_vocabulary WHERE session_id=? ORDER BY updated_at DESC LIMIT 100",
                (session,),
            )
        ]

    def set_vocabulary(
        self,
        session: str,
        term: str,
        description: str,
        *,
        evidence: list | None = None,
        manual: bool = False,
        status: str = "active",
    ) -> bool:
        term, description = clean(term, 32), clean(description, 160)
        if (
            not term
            or any(c.isspace() for c in term)
            or not description
            or status not in {"active", "disabled"}
        ):
            return False
        with self.atomic():
            old = next((row for row in self.vocabulary(session) if row["term"] == term), None)
            if not manual and (
                not evidence or (old and (old["manual"] or old["status"] == "disabled"))
            ):
                return False
            if old is None and len(self.vocabulary(session)) >= 100:
                return False
            self._execute(
                "INSERT INTO session_vocabulary(session_id,term,description,evidence,status,manual,updated_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(session_id,term) DO UPDATE SET description=excluded.description,"
                "evidence=excluded.evidence,status=excluded.status,manual=excluded.manual,updated_at=excluded.updated_at",
                (
                    session,
                    term,
                    description,
                    json.dumps(
                        evidence if evidence is not None else (old["evidence"] if old else []),
                        ensure_ascii=False,
                    ),
                    status,
                    int(manual),
                    time.time(),
                ),
            )
            return True

    def append_decision_trace(
        self,
        session: str,
        trace: str,
        stage: str,
        status: str,
        reason: str,
        details: dict | None = None,
    ) -> None:
        with self.atomic():
            self._execute(
                "INSERT INTO decision_trace(trace_id,session_id,stage,status,reason,details,at) VALUES(?,?,?,?,?,?,?)",
                (
                    trace,
                    session,
                    clean(stage, 40),
                    clean(status, 40),
                    clean(safe_error(reason) if status == "error" else reason, 200),
                    json.dumps(details or {}, ensure_ascii=False),
                    time.time(),
                ),
            )
            self._execute("DELETE FROM decision_trace WHERE at<?", (time.time() - 30 * 86400,))
            self._execute(
                "DELETE FROM decision_trace WHERE id<(SELECT id FROM decision_trace ORDER BY id DESC LIMIT 1 OFFSET 9999)"
            )

    def decision_traces(self, session: str, *, trace: str = "", limit: int = 100) -> list[dict]:
        clause, args = (" AND trace_id=?", [session, trace]) if trace else ("", [session])
        return [
            {**dict(row), "details": json.loads(row["details"])}
            for row in self._query(
                "SELECT * FROM decision_trace WHERE session_id=?"
                + clause
                + " ORDER BY id DESC LIMIT ?",
                [*args, max(1, min(200, limit))],
            )
        ]

    def delete_social_person(self, session: str, user: str) -> None:
        for table in ("interaction_evaluation", "interaction_style", "interaction_source_receipt"):
            self._execute(f"DELETE FROM {table} WHERE session_id=? AND user_id=?", (session, user))
