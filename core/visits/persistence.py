"""A bounded atomic protocol journal backed by the plugin's existing SQLite KV."""
from __future__ import annotations

import asyncio
import copy
import math
from typing import Any

from astrbot.api import logger

from .connections import WorldConnections
from .protocol import RESERVED, TERMINAL, new_id, public_actor
from .model_retry import valid_retry, visible_wait
from .history_pages import LOG_PAGE_SIZE, ROUTE_PAGE_SIZE, extended_history, write_page

KEY = "world_visits.v1"
MAX_MEMORY_WORKERS = 2
MEMORY_WAIT_SECONDS = .05


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
        self._memory_tasks: dict[str, asyncio.Task] = {}
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
            limits = [self.config.daily_calls, self.config.daily_tokens]
            if self._state.get("budget_limits") != limits:
                # An old remote refusal is a cached observation, not current
                # authority. A settings change permits one fresh authenticated
                # check while retaining all pairings and actual usage ledgers.
                for connection in self._state.get("connections", {}).values():
                    connection.pop("availability", None)
                self._state["budget_limits"] = limits
            if "presence_epochs" not in self._state:
                self._state["presence_epochs"] = {}
                for visit in self._state["visits"].values():
                    if visit.get("arrived_at"):
                        self._mark_arrival(visit)
            self._loaded = True
            # Unknown in-flight inference cannot be regenerated. A paid candidate
            # under read-only verification can resume its existing recovery phase.
            for visit in list(self._state["visits"].values()):
                if visit["status"] in TERMINAL:
                    # A confirmed goodbye wins over stale inference markers.
                    self._clear_decision_wait(visit)
                    if "proposal_stage" in visit:
                        visit["proposal_stage"] = "done"
                    continue
                retry = visit.get("model_retry")
                retry = retry if isinstance(retry, dict) else {}
                phase = retry.get("phase")
                verifying_candidate = (phase in {"propose", "receive", "turn", "scene"}
                                       and isinstance(retry.get("candidate"), dict)
                                       and valid_retry(visit, self._decision_cursor(visit, phase)))
                if visit.pop("model_pending", None) and not verifying_candidate:
                    await self._finish(visit, "interrupted", "上次思考被中断，只保留已确认经过")
                elif visit.get("proposal_stage") == "pending" and (
                        visit.get("decision_cursor") != self._decision_cursor(visit, "propose")
                        or not isinstance(visit.get("decision"), dict)) and not valid_retry(visit, self._decision_cursor(visit, "propose")):
                    await self._finish(visit, "interrupted", "上次邀请尚未形成可确认的决定，这次联系结束")
            await self._save()

    async def _save(self):
        now = self._now()
        self._state["codes"] = {k: v for k, v in self._state["codes"].items() if v["expires_at"] > now}
        receipts = sorted(self._state["receipts"].items(), key=lambda item: item[1]["at"], reverse=True)
        self._state["receipts"] = dict(receipts[:512])
        if "manual_attempts" in self._state:
            self._state["manual_attempts"] = self._state["manual_attempts"][-64:]
        visits = self._state["visits"]
        past = sorted((v for v in visits.values() if v["status"] in TERMINAL),
                      key=lambda v: v.get("ended_at", v["created_at"]), reverse=True)
        # Failed calls are diagnostics; they must not evict actual encounters.
        # Both collections stay bounded by the configured retained history.
        for arrived in (True, False):
            entries = [visit for visit in past if bool(visit.get("arrived_at")) is arrived]
            for visit in entries[self.config.history_keep:]:
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

    def pause_intervals(self, scope: str, start: float, end: float,
                        *, persona_key: str) -> list[tuple[float, float]]:
        """The retained arrival/end facts bound time away from a paused home."""
        def timestamp(value):
            return type(value) in {int, float} and math.isfinite(value) and value > 0

        if not persona_key or not timestamp(start) or not timestamp(end) or end <= start:
            return []
        windows = []
        for visit in self._state.get("visits", {}).values():
            if (visit.get("scope") != scope or visit.get("local", {}).get("persona_key") != persona_key
                    or visit.get("status") not in {"active", "finishing", "completed", "interrupted", "expired"}):
                continue
            arrived, expires = visit.get("arrived_at"), visit.get("expires_at")
            ended = visit.get("ended_at") if visit.get("status") in TERMINAL else end
            if not all(timestamp(value) for value in (arrived, expires, ended)):
                continue
            first, last = max(start, arrived), min(end, ended, expires)
            if last > first:
                windows.append((first, last))
        merged = []
        for first, last in sorted(windows):
            if merged and first <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(last, merged[-1][1]))
            else:
                merged.append((first, last))
        return merged

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

    @staticmethod
    def _clear_decision_wait(visit: dict) -> None:
        """Discard local inference waits without disturbing goodbye delivery."""
        for key in ("model_pending", "model_retry", "state_retry"):
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
                "turn_limit", "event_stage", "event_after", "end_reason", "ended_at", "intro",
                "manual", "outing_version", "outing", "outing_offer", "action_plans", "budget_holds",
                "outing_requests", "outing_requests_handled", "outing_request_turn")
        result = {key: copy.deepcopy(visit[key]) for key in keys if key in visit}
        if extended_history(visit):
            write_page(result, "log", visit["log"], LOG_PAGE_SIZE)
            if isinstance(result.get("outing"), dict):
                write_page(result["outing"], "route", visit["outing"]["route"], ROUTE_PAGE_SIZE)
        return result

    def _visible(self, visit):
        result = self._wire(visit)
        result.update(role=visit["role"], reason=visit["reason"],
                      local=public_actor(visit["local"], self._state["instance_id"]),
                      remote=copy.deepcopy(visit["remote"]),
                      calls_used=sum(visit["calls"].values()),
                      contact_error=visit.get("contact_error", ""))
        result["observed_at"] = self._now()
        if isinstance(result.get("outing"), dict):
            result["outing"]["phase"] = "playing" if self._now() < result["outing"]["until"] else "checkpoint"
        result["budget_side"] = visit.get("budget_side", "")
        result["model_wait"] = visible_wait(visit) if visit["status"] in RESERVED else {}
        result["progress"] = self._visible_progress(visit, result["model_wait"])
        return result

    def _visible_progress(self, visit: dict, model_wait: dict) -> dict:
        """Project local continuation evidence without exporting decisions or packets."""
        if visit["status"] not in RESERVED:
            return {}
        now = self._now()
        def timestamp(value):
            return value if type(value) in {int, float} and math.isfinite(value) and value > 0 else 0
        next_at = timestamp(visit.get("next_at"))
        if model_wait:
            return {"kind": "model_wait", **model_wait}
        if visit.get("outbox") or visit.get("contact_error"):
            failures = visit.get("network_failures", 0)
            return {"kind": "delivery_wait", "reason": "联系暂未送达，正在重试", "next_at": next_at,
                    "attempts": failures if type(failures) is int and failures >= 0 else 0}
        if visit.get("model_pending"):
            return {"kind": "thinking", "reason": "正在组织这一步的回应", "next_at": 0}
        outing = visit.get("outing")
        if isinstance(outing, dict):
            until, quiet = timestamp(outing.get("until")), timestamp(outing.get("quiet_until"))
            if until > now and (quiet > now or outing.get("return_agreed")):
                return {"kind": "activity_wait", "reason": "正在一起活动",
                        "next_at": min(quiet, until) if quiet > now else until}
        return {"kind": "next_turn", "reason": "等待下一步回应", "next_at": next_at,
                "actor_id": str(visit.get("next_actor") or "")}

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
                    "peers": self._scoped_candidates(scope), "visits": visits, "active": current,
                    "queued_invitation": copy.deepcopy(self._state.get("manual_invites", {}).get(scope)),
                    "manual_attempts": [copy.deepcopy(item) for item in self._state.get("manual_attempts", [])
                                        if not scope or item.get("scope") == scope]}

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

    async def _finish(self, visit, status, reason="", *, notify=True, ended_at: float | None = None):
        contacted = visit.get("contacted") or visit.get("arrived_at") or visit["status"] == "offered"
        visit["status"], visit["end_reason"] = status, reason
        visit["ended_at"] = self._now() if ended_at is None else ended_at
        self._clear_decision_wait(visit)
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

    async def _start_memory(self, visit_id):
        """Keep at most two owned writes; each trip retains its original task."""
        async with self._lock:
            current = self._memory_tasks.get(visit_id)
            if current and not current.done():
                return current, False
            visit = self._state["visits"].get(visit_id)
            if (self._closed or not visit or visit["status"] not in TERMINAL or visit.get("remembered")
                    or visit_id in self._remembering or visit.get("remember_retry_at", 0) > self._now()
                    or sum(not task.done() for task in self._memory_tasks.values()) >= MAX_MEMORY_WORKERS):
                return None, False
            task = asyncio.create_task(self._write_memory(visit_id), name="world-visit-memory:" + visit_id)
            self._memory_tasks[visit_id] = task

            def finished(done):
                if self._memory_tasks.get(visit_id) is done:
                    self._memory_tasks.pop(visit_id, None)
                if not done.cancelled() and (error := done.exception()) is not None:
                    logger.warning("串门记忆任务暂未完成（%s），保留原记录等待重试", type(error).__name__)

            task.add_done_callback(finished)
            return task, True

    async def _observe_memory(self, visit_id):
        """A slow write releases the visit clock without cancelling its transaction."""
        task, _ = await self._start_memory(visit_id)
        if task and not task.done():
            await asyncio.wait({task}, timeout=MEMORY_WAIT_SECONDS)

    @staticmethod
    async def _await_memory_tasks(tasks):
        """Cancellation cannot abandon a SQLite worker which still owns a write."""
        if not tasks:
            return
        joined = asyncio.gather(*tasks, return_exceptions=True)
        cancelled = False
        while not joined.done():
            try:
                await asyncio.shield(joined)
            except asyncio.CancelledError:
                cancelled = True
        joined.result()
        if cancelled:
            raise asyncio.CancelledError()

    async def _close_memory_tasks(self):
        tasks = tuple(self._memory_tasks.values())
        for task in tasks:
            task.cancel()
        try:
            await self._await_memory_tasks(tasks)
        finally:
            self._memory_tasks = {key: task for key, task in self._memory_tasks.items() if not task.done()}

    async def _remember(self, visit_id):
        """A direct first call waits; duplicate calls retain the existing single flight."""
        task, started = await self._start_memory(visit_id)
        if not started:
            return
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            task.cancel()
            try:
                await self._await_memory_tasks((task,))
            finally:
                raise

    async def _write_memory(self, visit_id):
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
