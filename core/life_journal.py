"""Persistent year calendars and grounded daily journals.

The journal is kept separate from chat history.  Calendar rows are generated
from the character/world description and public calendar candidates; diary
rows are generated from saved schedules, safe life events and, optionally,
the character's own non-dialogue thoughts.
"""
from __future__ import annotations

import json
import time
from typing import Any, Iterable


JOURNAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS life_calendar (
    scope TEXT NOT NULL,
    persona_key TEXT NOT NULL,
    year INTEGER NOT NULL,
    payload TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (scope, persona_key, year)
);
CREATE INDEX IF NOT EXISTS idx_life_calendar_scope
    ON life_calendar (scope, year);

CREATE TABLE IF NOT EXISTS life_diary (
    scope TEXT NOT NULL,
    persona_key TEXT NOT NULL,
    day TEXT NOT NULL,
    payload TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (scope, persona_key, day)
);
CREATE INDEX IF NOT EXISTS idx_life_diary_scope
    ON life_diary (scope, day);
"""


def _load_payload(raw: Any) -> dict[str, Any]:
    try:
        item = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return item if isinstance(item, dict) else {}


class JournalStoreMixin:
    @staticmethod
    def _journal_key(value: Any) -> str:
        return str(value or "").strip() or "global"

    def calendar_year_get(
        self, scope: str, persona_key: str, year: int
    ) -> dict[str, Any] | None:
        row = self._query(
            "SELECT payload, revision, created_at, updated_at "
            "FROM life_calendar WHERE scope=? AND persona_key=? AND year=?",
            (self._journal_key(scope), str(persona_key), int(year)),
        )
        if not row:
            return None
        item = _load_payload(row[0]["payload"])
        item["_revision"] = int(row[0]["revision"] or 1)
        item["_created_at"] = float(row[0]["created_at"] or 0)
        item["_updated_at"] = float(row[0]["updated_at"] or 0)
        return item

    def calendar_year_save(
        self, scope: str, persona_key: str, year: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        now = time.time()
        scope, persona_key, year = self._journal_key(scope), str(persona_key), int(year)
        with self.atomic():
            prior = self._query(
                "SELECT revision FROM life_calendar "
                "WHERE scope=? AND persona_key=? AND year=?",
                (scope, persona_key, year),
            )
            revision = int((prior[0]["revision"] if prior else 0) or 0) + 1
            self._execute(
                "INSERT INTO life_calendar(scope,persona_key,year,payload,revision,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(scope,persona_key,year) DO UPDATE SET "
                "payload=excluded.payload,revision=excluded.revision,updated_at=excluded.updated_at",
                (scope, persona_key, year, json.dumps(payload, ensure_ascii=False), revision, now, now),
            )
        result = dict(payload)
        result["_revision"] = revision
        result["_updated_at"] = now
        return result

    def calendar_year_list(self, scope: str, persona_key: str) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT payload FROM life_calendar "
            "WHERE scope=? AND persona_key=? ORDER BY year",
            (self._journal_key(scope), str(persona_key)),
        )
        return [{**_load_payload(row["payload"])} for row in rows]

    def diary_get(self, scope: str, persona_key: str, day: str) -> dict[str, Any] | None:
        row = self._query(
            "SELECT payload, revision, created_at, updated_at FROM life_diary "
            "WHERE scope=? AND persona_key=? AND day=?",
            (self._journal_key(scope), str(persona_key), str(day)),
        )
        if not row:
            return None
        item = _load_payload(row[0]["payload"])
        item.update(
            day=str(day),
            _revision=int(row[0]["revision"] or 1),
            _created_at=float(row[0]["created_at"] or 0),
            _updated_at=float(row[0]["updated_at"] or 0),
        )
        return item

    def diary_list(
        self, scope: str, persona_key: str, limit: int = 180
    ) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT day, payload, revision, created_at, updated_at FROM life_diary "
            "WHERE scope=? AND persona_key=? ORDER BY day DESC LIMIT ?",
            (self._journal_key(scope), str(persona_key), max(1, int(limit))),
        )
        result = []
        for row in rows:
            item = _load_payload(row["payload"])
            item.update(
                day=str(row["day"]),
                _revision=int(row["revision"] or 1),
                _created_at=float(row["created_at"] or 0),
                _updated_at=float(row["updated_at"] or 0),
            )
            result.append(item)
        return result

    def diary_latest(
        self, scope: str, persona_key: str, on_or_before: str
    ) -> dict[str, Any] | None:
        """Return the most recent diary at or before a local day."""

        rows = self._query(
            "SELECT day, payload, revision, created_at, updated_at FROM life_diary "
            "WHERE scope=? AND persona_key=? AND day<=? "
            "ORDER BY day DESC LIMIT 1",
            (self._journal_key(scope), str(persona_key), str(on_or_before)),
        )
        if not rows:
            return None
        row = rows[0]
        item = _load_payload(row["payload"])
        item.update(
            day=str(row["day"]),
            _revision=int(row["revision"] or 1),
            _created_at=float(row["created_at"] or 0),
            _updated_at=float(row["updated_at"] or 0),
        )
        return item

    def diary_between(
        self, scope: str, persona_key: str, start_day: str, end_day: str
    ) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT day, payload, revision, created_at, updated_at FROM life_diary "
            "WHERE scope=? AND persona_key=? AND day>=? AND day<=? "
            "ORDER BY day ASC",
            (
                self._journal_key(scope),
                str(persona_key),
                str(start_day),
                str(end_day),
            ),
        )
        result = []
        for row in rows:
            item = _load_payload(row["payload"])
            item.update(
                day=str(row["day"]),
                _revision=int(row["revision"] or 1),
                _created_at=float(row["created_at"] or 0),
                _updated_at=float(row["updated_at"] or 0),
            )
            result.append(item)
        return result

    def diary_upsert(
        self, scope: str, persona_key: str, day: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        now = time.time()
        scope, persona_key, day = self._journal_key(scope), str(persona_key), str(day)
        with self.atomic():
            prior = self._query(
                "SELECT revision, created_at FROM life_diary "
                "WHERE scope=? AND persona_key=? AND day=?",
                (scope, persona_key, day),
            )
            revision = int((prior[0]["revision"] if prior else 0) or 0) + 1
            created = float(prior[0]["created_at"] if prior else now)
            self._execute(
                "INSERT INTO life_diary(scope,persona_key,day,payload,revision,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(scope,persona_key,day) DO UPDATE SET "
                "payload=excluded.payload,revision=excluded.revision,updated_at=excluded.updated_at",
                (scope, persona_key, day, json.dumps(payload, ensure_ascii=False), revision, created, now),
            )
        result = dict(payload)
        result.update(day=day, _revision=revision, _created_at=created, _updated_at=now)
        return result

    def diary_prune(self, scope: str, persona_key: str, keep: int) -> int:
        scope, persona_key, keep = self._journal_key(scope), str(persona_key), max(1, int(keep))
        with self.atomic():
            rows = self._query(
                "SELECT day FROM life_diary WHERE scope=? AND persona_key=? "
                "ORDER BY day DESC LIMIT -1 OFFSET ?",
                (scope, persona_key, keep),
            )
            for row in rows:
                self._execute(
                    "DELETE FROM life_diary WHERE scope=? AND persona_key=? AND day=?",
                    (scope, persona_key, row["day"]),
                )
        return len(rows)

    def event_log_between(
        self,
        session_ids: Iterable[str],
        start: float,
        end: float,
        event_types: Iterable[str] | None = None,
        limit: int = 800,
        persona_key: str = "",
        diary_actions_only: bool = False,
    ) -> list[dict[str, Any]]:
        sessions = sorted({str(item) for item in session_ids if str(item)})
        if not sessions:
            return []
        placeholders = ",".join("?" for _ in sessions)
        sql = (
            f"SELECT id, session_id, event_type, detail, created_at FROM event_log "
            f"WHERE session_id IN ({placeholders}) AND created_at>=? AND created_at<?"
        )
        params: list[Any] = [*sessions, float(start), float(end)]
        kinds = sorted({str(item) for item in (event_types or []) if str(item)})
        if kinds:
            sql += " AND event_type IN (" + ",".join("?" for _ in kinds) + ")"
            params.extend(kinds)
        if persona_key:
            sql += " AND persona_id=?"
            params.append(str(persona_key))
        if diary_actions_only:
            # Filter before LIMIT: chat actions must not crowd out quiet life.
            sql += (
                " AND (event_type!='action' OR CASE WHEN json_valid(detail) "
                "THEN json_extract(detail, '$.diary_life')=1 ELSE 0 END)"
            )
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(max(1, min(2000, int(limit))))
        rows = self._query(sql, params)
        result = []
        for row in reversed(rows):
            item = dict(row)
            item["detail"] = _load_payload(item.get("detail"))
            result.append(item)
        return result
