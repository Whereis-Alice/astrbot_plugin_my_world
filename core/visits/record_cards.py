"""Optional encounter postcards, delivered outside the life/protocol clock.

Only newly finished, actually arrived visits are admitted. A durable page claim
precedes every external send: an ambiguous acknowledgement is never replayed.
"""
from __future__ import annotations

import asyncio
import copy
from hashlib import sha256
from typing import Any

from astrbot.api import logger

from .protocol import TERMINAL, digest, public_actor

MAX_RECORD_CARD_WORKERS = 1
MAX_RECORD_CARD_QUEUE = 32
MAX_RECORD_CARD_FAILURES = 5
RECORD_CARD_RENDER_TIMEOUT = 180
RECORD_CARD_SEND_TIMEOUT = 45
PUBLIC_LOG_KINDS = frozenset({
    "invitation", "arrival", "activity", "say", "action", "action_plan", "experience",
    "result", "outcome", "scene", "event", "contact_reply", "contact_status", "phone", "message", "", None,
})


class RecordCardRetryableError(RuntimeError):
    """The adapter proved that no external send was attempted or accepted."""


class VisitRecordCards:
    def _record_card_targets(self, visit: dict) -> list[str]:
        """The synchronous adapter checks exact session and current life identity."""
        select = getattr(self.actor_port, "record_card_targets", None)
        if not callable(select):
            return []
        targets = select(visit["scope"], visit)
        if not isinstance(targets, (list, tuple)):
            return []
        return list(dict.fromkeys(value for value in targets
                                  if isinstance(value, str) and value.strip() and len(value) <= 2048))

    @staticmethod
    def _record_card_pending(visit: dict) -> bool:
        card = visit.get("record_card")
        return isinstance(card, dict) and card.get("state") == "pending"

    def _prepare_record_cards(self) -> None:
        """Called by journal saves, including direct protocol terminal transitions."""
        visits = self._state["visits"].values()
        queued = sum(self._record_card_pending(visit) for visit in visits)
        for visit in visits:
            if visit["status"] not in TERMINAL or "record_card" in visit:
                continue
            card = {"version": 1, "state": "skipped", "checked_at": self._now()}
            visit["record_card"] = card
            if not visit.get("arrived_at"):
                card["reason"] = "not_arrived"
                continue
            if not all(callable(getattr(self.actor_port, name, None)) for name in
                       ("record_card_targets", "render_record_card", "send_record_card")):
                card["reason"] = "disabled"
                continue
            try:
                targets = self._record_card_targets(visit)
            except Exception as exc:
                card["reason"] = "targets_unavailable"
                logger.warning("相见长卡片会话暂不可用（%s），保留原始记录", type(exc).__name__)
                continue
            if not targets:
                card["reason"] = "disabled"
                continue
            if queued >= MAX_RECORD_CARD_QUEUE:
                card["reason"] = "queue_full"
                logger.warning("相见长卡片等待队列已满，保留原始记录供查看")
                continue
            queued += 1
            card.update(state="pending", failures=0, next_at=self._now(),
                        targets={target: {"state": "pending", "next_page": 0} for target in targets})

    @staticmethod
    def _record_card_state(card: dict) -> None:
        states = {value["state"] for value in card.get("targets", {}).values()}
        if "pending" in states:
            card["state"] = "pending"
        elif states == {"done"}:
            card["state"] = "done"
        elif "done" in states:
            card["state"] = "partial"
        elif "uncertain" in states:
            card["state"] = "uncertain"
        elif "failed" in states:
            card["state"] = "failed"
        else:
            card["state"] = "skipped"

    def _recover_record_card(self, visit: dict) -> None:
        """Do not flood old history; do not replay a page with an unknown receipt."""
        card = visit.get("record_card")
        if not isinstance(card, dict):
            visit["record_card"] = {"version": 1, "state": "skipped", "reason": "historical"}
            return
        claim = card.pop("claim", None)
        if isinstance(claim, dict):
            target = card.get("targets", {}).get(claim.get("session"))
            if target and target.get("state") == "pending":
                target.update(state="uncertain", reason="receipt_unknown")
                self._record_card_state(card)

    def _record_card_snapshot(self, visit: dict) -> dict[str, Any]:
        """Export the complete confirmed record, never a wire page or inner life."""
        keys = ("id", "scope", "role", "reason", "manual", "outing_version", "created_at",
                "arrived_at", "ended_at", "status", "end_reason", "host_id", "guest_id")
        snapshot = {key: copy.deepcopy(visit[key]) for key in keys if key in visit}
        persona_key = str(visit["local"].get("persona_key") or "")
        if persona_key:
            snapshot["life_identity"] = sha256(persona_key.encode("utf-8")).hexdigest()
        snapshot["local"] = public_actor(visit["local"], self._state["instance_id"])
        snapshot["remote"] = {key: str(visit["remote"].get(key) or "")
                              for key in ("id", "actor_id", "instance_id", "name", "world")}
        snapshot["scene"] = {key: copy.deepcopy(visit.get("scene", {}).get(key, ""))
                             for key in ("place", "activity", "event")}
        snapshot["log"] = [{key: copy.deepcopy(row[key]) for key in
                            ("seq", "actor_id", "kind", "text", "at", "say", "action", "result", "speaker", "name")
                            if key in row}
                           for row in visit.get("log", [])
                           if isinstance(row, dict) and row.get("kind") in PUBLIC_LOG_KINDS]
        outing = visit.get("outing")
        if isinstance(outing, dict):
            snapshot["outing"] = {key: copy.deepcopy(outing[key]) for key in
                                  ("place", "activity", "started_at", "until") if key in outing}
            snapshot["outing"]["route"] = [
                {key: copy.deepcopy(stop[key]) for key in
                 ("place", "activity", "started_at", "until") if key in stop}
                for stop in outing.get("route", []) if isinstance(stop, dict)]
        return snapshot

    async def _observe_record_card(self, visit_id: str) -> None:
        """Own a single worker; observing it never waits for browser or QQ IO."""
        async with self._lock:
            current = self._record_card_tasks.get(visit_id)
            if current and not current.done():
                return
            visit = self._state["visits"].get(visit_id)
            if visit and (visit.get("record_card") or {}).get("claim"):
                self._recover_record_card(visit)
                await self._save_record_card_progress()
            if (self._closed or not self.config.enabled or not visit
                    or visit["status"] not in TERMINAL or visit.get("end_pending")
                    or not self._record_card_pending(visit)
                    or visit["record_card"].get("next_at", 0) > self._now()
                    or sum(not task.done() for task in self._record_card_tasks.values()) >= MAX_RECORD_CARD_WORKERS):
                return
            task = asyncio.create_task(self._write_record_card(visit_id), name="world-visit-card:" + visit_id)
            self._record_card_tasks[visit_id] = task

            def finished(done):
                if self._record_card_tasks.get(visit_id) is done:
                    self._record_card_tasks.pop(visit_id, None)
                if not done.cancelled() and (error := done.exception()) is not None:
                    logger.warning("相见长卡片任务暂未完成（%s），保留发送进度", type(error).__name__)

            task.add_done_callback(finished)

    async def _save_record_card_progress(self) -> None:
        try:
            await self._save()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("相见长卡片发送进度暂未保存（%s），保留页码等待重试", type(exc).__name__)

    async def _retry_record_card(self, visit_id: str, error: Exception) -> None:
        async with self._lock:
            visit = self._state["visits"].get(visit_id)
            if not visit or not self._record_card_pending(visit):
                return
            card = visit["record_card"]
            # This path is used only before an external send, or when the
            # adapter explicitly proves that it never attempted that send.
            card.pop("claim", None)
            failures = card.get("failures", 0) + 1
            card.update(failures=failures, error=type(error).__name__,
                        next_at=self._now() + min(600, 30 * 2 ** (failures - 1)))
            if failures >= MAX_RECORD_CARD_FAILURES:
                for target in card["targets"].values():
                    if target["state"] == "pending":
                        target.update(state="failed", reason="retry_exhausted")
                self._record_card_state(card)
            await self._save_record_card_progress()

    async def _card_snapshot_current(self, visit_id: str, fingerprint: str) -> bool:
        """Under the lock, discard stale rendering before any external delivery."""
        visit = self._state["visits"].get(visit_id)
        if (self._closed or not self.config.enabled or not visit or visit.get("end_pending")
                or not self._record_card_pending(visit)):
            return False
        if digest(self._record_card_snapshot(visit)) == fingerprint:
            return True
        card = visit["record_card"]
        if any(target.get("next_page", 0) for target in card["targets"].values()):
            for target in card["targets"].values():
                if target["state"] == "pending":
                    target.update(state="failed", reason="record_changed")
            self._record_card_state(card)
        else:
            card.pop("fingerprint", None)
            card.pop("page_count", None)
            card["next_at"] = self._now()
        await self._save_record_card_progress()
        return False

    async def _write_record_card(self, visit_id: str) -> None:
        owned_pages: list[Any] = []
        try:
            await self._deliver_record_card(visit_id, owned_pages)
        except asyncio.CancelledError:
            # A durable claim survives cancellation. Reload conservatively
            # treats it as an unknown receipt, never as permission to resend.
            raise
        except Exception as exc:
            await self._retry_record_card(visit_id, exc)
        finally:
            release = getattr(self.actor_port, "release_record_card", None)
            if callable(release):
                try:
                    release(owned_pages)
                except Exception as exc:
                    logger.warning("相见长卡片临时图片暂未清理（%s）", type(exc).__name__)

    async def _deliver_record_card(self, visit_id: str, owned_pages: list[Any]) -> None:
        async with self._lock:
            visit = self._state["visits"].get(visit_id)
            if (self._closed or not visit or visit.get("end_pending")
                    or not self._record_card_pending(visit)):
                return
            card = visit["record_card"]
            allowed = self._record_card_targets(visit)
            for session, target in card["targets"].items():
                if target["state"] == "pending" and session not in allowed:
                    target.update(state="skipped", reason="session_disabled")
            self._record_card_state(card)
            await self._save_record_card_progress()
            if not self._record_card_pending(visit):
                return
            snapshot = self._record_card_snapshot(visit)
            fingerprint = digest(snapshot)
        async with asyncio.timeout(RECORD_CARD_RENDER_TIMEOUT):
            pages = await self.actor_port.render_record_card(snapshot["scope"], copy.deepcopy(snapshot))
        if isinstance(pages, (list, tuple)):
            owned_pages.extend(pages)
        if not isinstance(pages, (list, tuple)) or not pages:
            raise RecordCardRetryableError("render_empty")
        async with self._lock:
            if not await self._card_snapshot_current(visit_id, fingerprint):
                return
            card = visit["record_card"]
            prior_fingerprint = card.get("fingerprint")
            prior_count = card.get("page_count")
            delivered = any(target.get("next_page", 0) for target in card["targets"].values())
            if delivered and (prior_fingerprint != fingerprint or prior_count != len(pages)):
                for target in card["targets"].values():
                    if target["state"] == "pending":
                        target.update(state="failed", reason="layout_changed")
                self._record_card_state(card)
                await self._save_record_card_progress()
                return
            card.update(fingerprint=fingerprint, page_count=len(pages))
            await self._save_record_card_progress()
            sessions = list(card["targets"])
        for session in sessions:
            for index, page in enumerate(pages):
                async with self._lock:
                    if not await self._card_snapshot_current(visit_id, fingerprint):
                        return
                    target = card["targets"][session]
                    if target["state"] != "pending":
                        break
                    if index < target.get("next_page", 0):
                        continue
                    if session not in self._record_card_targets(visit):
                        target.update(state="skipped", reason="session_disabled")
                        self._record_card_state(card)
                        await self._save_record_card_progress()
                        break
                    card["claim"] = {"session": session, "page": index}
                    try:
                        # Never invoke an external send without its durable
                        # claim. A failed write is known to precede the send.
                        await self._save()
                    except BaseException:
                        card.pop("claim", None)
                        raise
                try:
                    async with asyncio.timeout(RECORD_CARD_SEND_TIMEOUT):
                        await self.actor_port.send_record_card(snapshot["scope"], copy.deepcopy(snapshot), session, page)
                except asyncio.CancelledError:
                    raise
                except RecordCardRetryableError:
                    raise
                except Exception as exc:
                    async with self._lock:
                        card.pop("claim", None)
                        target.update(state="uncertain", reason="receipt_unknown")
                        card["error"] = type(exc).__name__
                        self._record_card_state(card)
                        await self._save_record_card_progress()
                    logger.warning("相见长卡片发送回执不明（%s），避免重复发送；原始记录仍可查看", type(exc).__name__)
                    break
                async with self._lock:
                    card.pop("claim", None)
                    target["next_page"] = index + 1
                    if target["next_page"] == len(pages):
                        target["state"] = "done"
                    self._record_card_state(card)
                    await self._save_record_card_progress()

    async def _close_record_card_tasks(self) -> None:
        tasks = tuple(self._record_card_tasks.values())
        for task in tasks:
            task.cancel()
        try:
            await self._await_memory_tasks(tasks)
        finally:
            self._record_card_tasks = {key: task for key, task in self._record_card_tasks.items() if not task.done()}
