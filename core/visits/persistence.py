"""A bounded atomic protocol journal backed by the plugin's existing SQLite KV."""
from __future__ import annotations

import asyncio
import copy
from typing import Any

from astrbot.api import logger

from .connections import WorldConnections
from .protocol import RESERVED, TERMINAL, new_id, public_actor

KEY = "world_visits.v1"


class VisitJournal(WorldConnections):
    def __init__(self, db, config, actor_port, clock, random):
        self.db, self.config, self.actor_port = db, config, actor_port
        self._now, self._random = clock, random
        self._lock = asyncio.Lock()
        self._state: dict[str, Any] = {}
        self._loaded = False
        self._closed = False
        self._touched: dict[str, float] = {}
        self._remembering: set[str] = set()
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
            self._migrate_connections()
            if "presence_epochs" not in self._state:
                self._state["presence_epochs"] = {}
                for visit in self._state["visits"].values():
                    if visit.get("arrived_at"):
                        self._mark_arrival(visit)
            self._loaded = True
            # An inference interrupted by process exit must never be regenerated.
            for visit in list(self._state["visits"].values()):
                if visit.pop("model_pending", None):
                    await self._finish(visit, "interrupted", "上次思考被中断，只保留已确认经过")
                elif visit.get("proposal_stage") == "pending" and (
                        visit.get("decision_cursor") != self._decision_cursor(visit, "propose")
                        or not isinstance(visit.get("decision"), dict)):
                    await self._finish(visit, "interrupted", "上次邀请尚未形成可确认的决定，这次联系结束")
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
            if not self._settlement_pending(visit):
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

    def presence_epoch(self, scope: str) -> int:
        """A persistent arrival cursor, retained when journey history is trimmed."""
        return self._state.get("presence_epochs", {}).get(scope, 0)

    @staticmethod
    def _settlement_pending(visit: dict) -> bool:
        return visit["status"] in TERMINAL and bool(visit.get("end_pending") or
            (visit.get("arrived_at") and not visit.get("remembered")))

    def _settlement_backlog_full(self, scope: str) -> bool:
        # Bound protected records during a persistent storage/network failure.
        # Existing visits can finish; only new invitations wait for settlement.
        return sum(v["scope"] == scope and self._settlement_pending(v)
                   for v in self._state["visits"].values()) >= 32

    @staticmethod
    def _invalidate_memory(visit: dict) -> None:
        for key in ("remembered", "remember_failures", "remember_retry_at", "remember_error"):
            visit.pop(key, None)

    def _mark_arrival(self, visit) -> None:
        self._note_automatic_success(visit)
        if not visit.get("arrived_at") or visit.get("presence_marked"):
            return
        epochs = self._state.setdefault("presence_epochs", {})
        epochs[visit["scope"]] = epochs.get(visit["scope"], 0) + 1
        visit["presence_marked"] = True

    def _peer(self, scope, peer_id):
        peer = self._find_peer(scope, peer_id)
        if not peer or not self._peer_trusted(peer) or not self._life_enabled(scope):
            raise ValueError("请先开启这个生活的串门并选择已配对的友邻")
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
                      calls_used=sum(visit["calls"].values()),
                      contact_error=visit.get("contact_error", ""))
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
                    "connections": [self._safe_connection(c) for c in self._state["connections"].values()],
                    "lives": self._available_lives(), "enabled_scopes": self._participating(),
                    "scope_enabled": self._life_enabled(scope),
                    "peers": self._scoped_candidates(scope), "visits": visits, "active": current}

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

    async def _finish(self, visit, status, reason="", *, notify=True):
        contacted = visit.get("contacted") or visit.get("arrived_at") or visit["status"] == "offered"
        visit["status"], visit["end_reason"] = status, reason
        visit["ended_at"] = self._now()
        visit.pop("model_pending", None)
        visit.pop("outbox", None)
        visit.pop("unsent_turn", None)
        visit.pop("proposal_offer", None)
        visit.pop("proposal_cursor", None)
        if "proposal_stage" in visit:
            visit["proposal_stage"] = "done"
        if notify and contacted:
            visit["end_pending"] = {"attempts": 0, "next_at": self._now()}
        else:
            visit.pop("end_pending", None)
        await self._save()

    async def _remember(self, visit_id):
        async with self._lock:
            visit = self._state["visits"].get(visit_id)
            if (self._closed or not visit or visit["status"] not in TERMINAL or visit.get("remembered")
                    or visit_id in self._remembering or visit.get("remember_retry_at", 0) > self._now()):
                return
            snapshot = copy.deepcopy(visit)
            self._remembering.add(visit_id)
        try:
            try:
                await self.actor_port.remember(snapshot["scope"], snapshot)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                async with self._lock:
                    visit = self._state["visits"].get(visit_id)
                    if not self._closed and self._same_memory(visit, snapshot):
                        failures = min(8, visit.get("remember_failures", 0) + 1)
                        visit.update(remember_failures=failures, remember_error=type(exc).__name__,
                                     remember_retry_at=self._now() + min(3600, 60 * 2 ** (failures - 1)))
                        await self._save_memory_progress()
                return
            async with self._lock:
                visit = self._state["visits"].get(visit_id)
                if not self._closed and self._same_memory(visit, snapshot):
                    self._invalidate_memory(visit)
                    visit["remembered"] = True
                    await self._save_memory_progress()
        finally:
            self._remembering.discard(visit_id)

    @staticmethod
    def _same_memory(visit: dict | None, snapshot: dict) -> bool:
        return visit is not None and all(visit.get(key) == snapshot.get(key)
                                        for key in ("log", "status", "end_reason"))

    async def _save_memory_progress(self):
        # A failed settlement acknowledgement may be retried after restart.
        # New invitations/model calls still require their normal durable admission.
        try:
            await self._save()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("串门记忆入账进度暂未保存（%s），等待后续保存或重载后重试", type(exc).__name__)
