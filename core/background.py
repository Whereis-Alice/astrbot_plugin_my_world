"""Persistent daily reservations; unknown usage remains distinct from measured tokens."""
from __future__ import annotations

import json
from typing import Any


class BackgroundStoreMixin:
    def reserve_background(self, day: str, *, calls: int, tokens: int, reserve: int) -> dict[str, Any]:
        key = "background_budget:" + day
        with self.atomic():
            row = self._query("SELECT value FROM kv WHERE key=?", (key,))
            item = json.loads(row[0]["value"]) if row else {"calls": 0, "tokens": 0, "unknown": 0, "reserved": 0}
            if (calls and item["calls"] >= calls) or (tokens and item["tokens"] + item["reserved"] + reserve > tokens):
                return {**item, "allowed": False, "reason": "今日后台预算已用完，生活规则继续运行"}
            item["calls"] += 1
            item["reserved"] += reserve
            self._execute("INSERT INTO kv(key,value,updated_at) VALUES(?,?,strftime('%s','now')) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                          (key, json.dumps(item)))
            return {**item, "allowed": True}

    def settle_background(self, day: str, *, reserve: int, measured: int | None) -> dict:
        key = "background_budget:" + day
        with self.atomic():
            row = self._query("SELECT value FROM kv WHERE key=?", (key,))
            item = json.loads(row[0]["value"]) if row else {"calls": 0, "tokens": 0, "unknown": 0, "reserved": 0}
            if measured is None:
                # Keep a conservative budget hold; never report it as actual consumption.
                item["unknown"] += 1
            else:
                item["reserved"] = max(0, item["reserved"] - reserve)
                item["tokens"] += max(0, measured)
            self._execute("UPDATE kv SET value=? WHERE key=?", (json.dumps(item), key))
            return item
