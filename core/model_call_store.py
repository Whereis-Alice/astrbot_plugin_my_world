"""Bounded, indexed model-call history in the plugin database."""

from __future__ import annotations

import json
import math
import time
from typing import Any


MODEL_CALL_SCHEMA = """
CREATE TABLE IF NOT EXISTS model_call (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    session_id TEXT NOT NULL DEFAULT '',
    provider_id TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    purpose TEXT NOT NULL DEFAULT 'other',
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'success',
    input_tokens INTEGER,
    cached_tokens INTEGER,
    output_tokens INTEGER,
    total_tokens INTEGER,
    metadata TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_model_call_time ON model_call(started_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_model_call_session ON model_call(session_id, started_at DESC);
"""


class ModelCallStoreMixin:
    def append_model_call(self, row: dict[str, Any]) -> int:
        usage = row.get("usage") or {}
        with self.atomic():
            cursor = self._execute(
                "INSERT INTO model_call(started_at,duration_ms,session_id,provider_id,model,purpose,reason,status,"
                "input_tokens,cached_tokens,output_tokens,total_tokens,metadata) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["started_at"], row["duration_ms"], row["session_id"], row["provider_id"], row["model"],
                 row["purpose"], row["reason"], row["status"], usage.get("input"), usage.get("input_cached"),
                 usage.get("output"), usage.get("total"), json.dumps(row, ensure_ascii=False)))
            self._execute("DELETE FROM model_call WHERE started_at < ?", (time.time() - 30 * 86400,))
            self._execute("DELETE FROM model_call WHERE id < (SELECT id FROM model_call ORDER BY id DESC LIMIT 1 OFFSET 9999)")
        return int(cursor.lastrowid)

    def model_calls(self, *, hours: int = 24, page: int = 1, size: int = 40, session: str = "",
                    purpose: str = "", provider: str = "", status: str = "", q: str = "") -> dict[str, Any]:
        hours = hours if hours in (24, 168, 720) else 24
        now = time.time()
        clauses, values = ["started_at >= ?"], [now - hours * 3600]
        for field, value in (("session_id", session), ("purpose", purpose), ("provider_id", provider), ("status", status)):
            if value:
                clauses.append(f"{field} = ?")
                values.append(str(value)[:200])
        if q.strip():
            # Search literal text; '%' and '_' are not user-controlled wildcards.
            query = q.strip()[:200].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("(reason LIKE ? ESCAPE '\\' OR model LIKE ? ESCAPE '\\' OR provider_id LIKE ? ESCAPE '\\' OR session_id LIKE ? ESCAPE '\\')")
            values.extend([f"%{query}%"] * 4)
        where = " AND ".join(clauses)
        summary = dict(self._query(f"""SELECT COUNT(*) AS calls,
            COALESCE(SUM(status='error'),0) AS errors, COALESCE(SUM(status='cancelled'),0) AS cancelled,
            COALESCE(SUM(total_tokens IS NOT NULL),0) AS reported,
            COALESCE(SUM(input_tokens IS NOT NULL),0) AS input_reported,
            COALESCE(SUM(output_tokens IS NOT NULL),0) AS output_reported,
            COALESCE(SUM(cached_tokens IS NOT NULL),0) AS cached_reported,
            COALESCE(SUM(json_extract(metadata,'$.coverage') <> 'direct'),0) AS final_only,
            COALESCE(SUM(total_tokens),0) AS total, COALESCE(SUM(input_tokens),0) AS input,
            COALESCE(SUM(output_tokens),0) AS output, COALESCE(SUM(cached_tokens),0) AS cached,
            COALESCE(AVG(duration_ms),0) AS duration_ms FROM model_call WHERE {where}""", values)[0])
        size = max(1, min(100, size))
        pages = max(1, math.ceil(summary["calls"] / size))
        page = max(1, min(page, pages))
        rows = self._query(f"SELECT id,metadata FROM model_call WHERE {where} ORDER BY started_at DESC,id DESC LIMIT ? OFFSET ?",
                           [*values, size, (page - 1) * size])
        series = [dict(row) for row in self._query(f"SELECT CAST(started_at / 3600 AS INTEGER)*3600 AS at, COUNT(*) AS calls, COALESCE(SUM(total_tokens),0) AS tokens FROM model_call WHERE {where} GROUP BY at ORDER BY at", values)]
        facets = {}
        for column, label in (("session_id", "sessions"), ("purpose", "purposes"), ("provider_id", "providers")):
            facets[label] = [row[0] for row in self._query(f"SELECT DISTINCT {column} FROM model_call WHERE started_at>=? AND {column}<>'' ORDER BY {column}", (now - hours * 3600,))]
        return {"items": [{**json.loads(row["metadata"]), "id": row["id"]} for row in rows],
                "summary": summary, "series": series, "facets": facets,
                "page": page, "pages": pages, "hours": hours,
                "retention": {"days": 30, "limit": 10000}}
