"""A bounded atomic protocol journal backed by the plugin's existing SQLite KV."""
from __future__ import annotations

import asyncio
import copy
from typing import Any

from .protocol import RESERVED, TERMINAL, new_id, public_actor

KEY = "world_visits.v1"


class VisitJournal:
    def __init__(self, db, config, actor_port, clock, random):
        self.db, self.config, self.actor_port = db, config, actor_port
        self._now, self._random = clock, random
        self._lock = asyncio.Lock()
        self._state: dict[str, Any] = {}
        self._loaded = False
        self._closed = False
        self._touched: dict[str, float] = {}
        self.error = ""

    async def _load(self):
        async with self._lock:
            if self._loaded:
                return
            value = await self.db.call("kv_get", KEY, {})
            self._state = value if isinstance(value, dict) else {}
            for name, default in (("instance_id", new_id()), ("peers", {}), ("visits", {}),
                                  ("codes", {}), ("receipts", {}), ("draws", {})):
                self._state.setdefault(name, default)
            self._loaded = True
            # An inference interrupted by process exit must never be regenerated.
            for visit in self._state["visits"].values():
                if visit.pop("model_pending", None):
                    visit["status"] = "interrupted"
                    visit["ended_at"] = self._now()
                    visit["end_reason"] = "上次思考被中断，只保留已确认经过"
            await self._save()

    async def _save(self):
        now = self._now()
        self._state["codes"] = {k: v for k, v in self._state["codes"].items() if v["expires_at"] > now}
        receipts = sorted(self._state["receipts"].items(), key=lambda item: item[1]["at"], reverse=True)
        self._state["receipts"] = dict(receipts[:512])
        visits = self._state["visits"]
        past = sorted((v for v in visits.values() if v["status"] in TERMINAL),
                      key=lambda v: v.get("ended_at", v["created_at"]), reverse=True)
        for visit in past[self.config.history_keep:]:
            visits.pop(visit["id"], None)
        await self.db.call("kv_set", KEY, self._state)

    def active(self, scope: str) -> dict[str, Any] | None:
        if not self.config.enabled or self._closed:
            return None
        visit = next((v for v in self._state.get("visits", {}).values()
                      if v["scope"] == scope and v["status"] in RESERVED), None)
        return copy.deepcopy(visit) if visit else None

    def touch(self, scope: str) -> None:
        if self.config.enabled:
            self._touched[scope] = self._now() + 180

    def _peer(self, scope, peer_id):
        peer = self._state["peers"].get(peer_id)
        if not peer or peer["scope"] != scope:
            raise ValueError("请先选择本生活已配对的友邻")
        return peer

    @staticmethod
    def _safe_peer(peer):
        return {k: copy.deepcopy(peer.get(k)) for k in ("id", "scope", "alias", "url", "name", "world", "enabled")}

    @staticmethod
    def _wire(visit):
        keys = ("id", "status", "created_at", "arrived_at", "expires_at", "scene", "scene_stage", "log",
                "host_id", "guest_id", "next_actor", "next_at", "turns", "calls", "max_calls",
                "turn_limit", "event_stage", "event_after", "end_reason", "ended_at", "intro")
        return {key: copy.deepcopy(visit[key]) for key in keys if key in visit}

    def _visible(self, visit):
        result = self._wire(visit)
        result.update(role=visit["role"], reason=visit["reason"],
                      local=public_actor(visit["local"], self._state["instance_id"]),
                      remote=copy.deepcopy(visit["remote"]),
                      calls_used=sum(visit["calls"].values()))
        return result

    async def overview(self, scope: str = "") -> dict[str, Any]:
        await self._load()
        async with self._lock:
            visits = [self._visible(v) for v in self._state["visits"].values() if not scope or v["scope"] == scope]
            visits.sort(key=lambda row: row["created_at"], reverse=True)
            current = next((v for v in visits if v["status"] in RESERVED), None)
            return {"enabled": self.config.enabled, "instance_id": self._state["instance_id"],
                    "listen_port": getattr(getattr(self, "transport", None), "bound_port", self.config.listen_port),
                    "public_url": self.config.public_url, "error": self.error,
                    "peers": [self._safe_peer(p) for p in self._state["peers"].values()
                              if not scope or p["scope"] == scope], "visits": visits, "active": current}

    def _append(self, visit, actor_id, kind, value):
        from .protocol import text
        value = text(value, 500)
        if value:
            visit["log"].append({"seq": len(visit["log"]) + 1, "actor_id": actor_id,
                                 "kind": kind, "text": value, "at": self._now()})

    def _local_id(self, visit):
        return self._state["instance_id"] + ":" + visit["local"]["actor_id"]

    def _allocation(self, visit, actor_id):
        total = visit["max_calls"]
        return total // 2 + (total % 2 if actor_id == visit["host_id"] else 0)

    async def _finish(self, visit, status, reason=""):
        visit["status"], visit["end_reason"] = status, reason
        visit["ended_at"] = self._now()
        visit.pop("model_pending", None)
        visit.pop("outbox", None)
        visit.pop("unsent_turn", None)
        await self._save()

    async def _remember(self, visit_id):
        async with self._lock:
            visit = self._state["visits"].get(visit_id)
            if not visit or visit["status"] not in TERMINAL or visit.get("remembered"):
                return
            snapshot = copy.deepcopy(visit)
        await self.actor_port.remember(snapshot["scope"], snapshot)
        async with self._lock:
            visit = self._state["visits"].get(visit_id)
            if visit:
                visit["remembered"] = True
                await self._save()
