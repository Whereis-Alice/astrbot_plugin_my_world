"""Bounded personal goodbyes after the shared clock has released home life."""
from __future__ import annotations

import asyncio
import copy
import math

from astrbot.api import logger

from .decisions import VisitDecisionError, capture_decision_candidate
from .protocol import TERMINAL, digest, new_id

DEPARTURE_VERSION = 1
DEPARTURE_WINDOW_SECONDS = 300
DEPARTURE_RETRY_DELAYS = (30, 60)
MAX_DEPARTURE_WORKERS = 2


def farewell_id(visit_id: str, actor: str, value: str, at: float) -> str:
    return digest({"visit_id": visit_id, "actor_id": actor, "text": value, "at": at})


def validate_farewell(visit: dict, actor: str, value: object, *, now: float | None = None) -> dict:
    fields = {"id", "actor_id", "text", "at"}
    if (not isinstance(value, dict) or set(value) != fields or value.get("actor_id") != actor
            or actor not in {visit["host_id"], visit["guest_id"]}
            or not isinstance(value.get("text"), str) or not value["text"].strip()
            or len(value["text"]) > 400 or type(value.get("at")) not in {int, float}
            or not math.isfinite(value["at"]) or value["at"] < 0
            or now is not None and value["at"] > now + 300
            or value.get("id") != farewell_id(visit["id"], actor, value["text"], value["at"])):
        raise ValueError("告别原话或来源无效")
    return copy.deepcopy(value)


def validate_departure_snapshot(visit: dict, remote: dict, *, now: float | None = None) -> None:
    version = remote.get("departure_version")
    if version is not None and (type(version) is not int or version != DEPARTURE_VERSION):
        raise ValueError("告别快照协议版本无效")
    receipts = remote.get("departure_farewells", {})
    if not isinstance(receipts, dict) or not set(receipts) <= {visit["host_id"], visit["guest_id"]}:
        raise ValueError("告别确认来源无效")
    logs = remote.get("log", [])
    prior = visit.get("departure_farewells", {})
    own = visit["guest_id"] if visit.get("role") == "guest" else visit["host_id"]
    for actor, receipt in receipts.items():
        if not isinstance(receipt, dict) or set(receipt) != {"id", "actor_id", "text", "at", "log_seq"}:
            raise ValueError("告别确认格式无效")
        quote = validate_farewell(visit, actor, {k: receipt[k] for k in ("id", "actor_id", "text", "at")}, now=now)
        number = receipt["log_seq"]
        if (type(number) is not int or not 1 <= number <= len(logs)
                or any(logs[number - 1].get(k) != v for k, v in
                       {"actor_id": actor, "kind": "say", "text": quote["text"], "at": quote["at"]}.items())):
            raise ValueError("告别确认不属于已确认原话")
        if actor in prior and receipt != prior[actor]:
            raise ValueError("已经确认的告别不能改写")
        if actor not in prior:
            old_voice = any(row.get("actor_id") == actor and row.get("kind") == "say"
                            and row.get("text") == quote["text"] and row.get("at") == quote["at"]
                            for row in visit.get("log", []))
            outing = visit.get("outing") or {}
            planned_voice = (outing.get("return_agreed") and outing.get("farewells", {}).get(actor) == quote["text"]
                             and quote["at"] == outing.get("until"))
            leave_voice = (remote.get("end_reason") == quote["text"]
                           and remote.get("turns", {}).get(actor, 0) > visit.get("turns", {}).get(actor, 0))
            ended = remote.get("ended_at")
            if (not (old_voice or planned_voice or leave_voice)
                    and (type(ended) not in {int, float} or not math.isfinite(ended)
                         or not ended <= quote["at"] <= ended + DEPARTURE_WINDOW_SECONDS)):
                raise ValueError("新的告别不能回填到相处结束之前")
        if visit.get("role") == "guest" and actor == own and actor not in prior:
            choice = (visit.get("departure") or {}).get("quote")
            planned = (visit.get("outing") or {}).get("farewells", {}).get(actor)
            agreed = ((visit.get("outing") or {}).get("return_agreed") and planned == quote["text"]
                      and quote["at"] == visit["outing"]["until"])
            confirmed = any(row.get("actor_id") == actor and row.get("kind") == "say"
                            and row.get("text") == quote["text"] and row.get("at") == quote["at"]
                            for row in visit.get("log", []))
            if choice != quote and not agreed and not confirmed:
                raise ValueError("东道主不能补写访客的告别")
    if visit.get("role") == "guest" and any(actor not in receipts for actor in prior):
        raise ValueError("已经确认的告别不能删除")


def departure_pending(visit: dict, now: float) -> bool:
    state = visit.get("departure") or {}
    return bool(state and state.get("state") not in {"done", "failed"}
                and state.get("deadline", 0) > now)


class VisitDeparture:
    @staticmethod
    def _known_departure_farewell(visit: dict, actor: str, quote: dict) -> bool:
        receipt = visit.get("departure_farewells", {}).get(actor)
        if receipt and all(receipt.get(key) == value for key, value in quote.items()):
            return True
        if any(row.get("actor_id") == actor and row.get("kind") == "say"
               and row.get("text") == quote["text"] and row.get("at") == quote["at"]
               for row in visit.get("log", [])):
            return True
        outing = visit.get("outing") or {}
        return bool(outing.get("return_agreed") and outing.get("farewells", {}).get(actor) == quote["text"]
                    and quote["at"] == outing.get("until"))

    def _validate_received_departure(self, visit: dict, actor: str, value: object,
                                     *, sent_at: float | None = None) -> dict:
        quote = validate_farewell(visit, actor, value, now=self._now())
        if self._known_departure_farewell(visit, actor, quote):
            return quote
        state = visit.get("departure") or {}
        ended = visit.get("ended_at")
        deadline = state.get("deadline")
        dispatch = self._now() if sent_at is None else sent_at
        if (not state or any(type(value) not in {int, float} or not math.isfinite(value)
                             for value in (ended, deadline, dispatch))
                or not ended <= quote["at"] <= dispatch <= deadline):
            raise ValueError("新的告别必须属于原收尾时窗，不能回填旧相见")
        return quote

    async def _expire_visit(self, visit: dict, reason: str) -> None:
        natural = visit.get("outing_version") == 1 and bool(visit.get("arrived_at"))
        await self._finish(visit, "completed" if natural else "expired", reason,
                           ended_at=visit["expires_at"])

    def _departure_snapshot_current(self, snapshot: dict) -> bool:
        current = self._state.get("visits", {}).get(snapshot.get("id"))
        peer = self._find_peer(snapshot.get("scope"), (snapshot.get("remote") or {}).get("id"))
        return bool(not self._closed and self.config.enabled and current and peer
                    and self._peer_trusted(peer) and self._life_enabled(snapshot.get("scope"))
                    and current.get("status") in TERMINAL and current.get("local") == snapshot.get("local")
                    and (current.get("departure") or {}).get("state") == "pending"
                    and (current.get("departure") or {}).get("deadline", 0) > self._now()
                    and (current.get("departure") or {}).get("inflight")
                    == (snapshot.get("departure") or {}).get("inflight")
                    and (current.get("departure") or {}).get("inflight"))

    def _prepare_departure(self, visit: dict) -> None:
        if (visit.get("departure") or visit.get("outing_version") != 1 or not visit.get("arrived_at")
                or visit.get("status") != "completed"):
            return
        own = self._local_id(visit)
        now = self._now()
        ended = visit.get("ended_at")
        if type(ended) not in {int, float} or not math.isfinite(ended) or now > ended + DEPARTURE_WINDOW_SECONDS:
            return
        state = {"version": 1, "state": "pending", "attempts": 0, "waits": 0,
                 "next_at": now, "deadline": ended + DEPARTURE_WINDOW_SECONDS}
        visit["departure"] = state
        outing = visit.get("outing") or {}
        agreed = outing.get("farewells", {}) if outing.get("return_agreed") else {}
        for actor, words in agreed.items():
            if not isinstance(words, str) or not words.strip():
                continue
            at = outing["until"]
            quote = {"id": farewell_id(visit["id"], actor, words, at), "actor_id": actor,
                     "text": words, "at": at}
            if visit["role"] == "host":
                self._record_departure_farewell(visit, quote, reuse=True)
            if actor == own:
                state["quote"], state["state"] = quote, "ready"
        # A verified leave already contains this actor's actual goodbye.
        decision = visit.get("decision") or {}
        if not state.get("quote") and decision.get("leave") is True and decision.get("say"):
            row = next((row for row in reversed(visit["log"]) if row.get("actor_id") == own
                        and row.get("kind") == "say" and row.get("text") == decision["say"]), None)
            if row:
                words, at = row["text"], row["at"]
                quote = {"id": farewell_id(visit["id"], own, words, at), "actor_id": own,
                         "text": words, "at": at}
                state["quote"], state["state"] = quote, "ready"
                if visit["role"] == "host":
                    self._record_departure_farewell(visit, quote, reuse=True)

    def _recover_departure(self, visit: dict) -> None:
        state = visit.get("departure") or {}
        if state.pop("inflight", None):
            state["state"] = "pending" if state.get("candidate") else "failed"
        if state.get("deadline", 0) <= self._now() and state.get("state") == "pending":
            state["state"] = "failed"
        self._reconcile_departure(visit)

    def _reconcile_departure(self, visit: dict) -> None:
        state = visit.get("departure") or {}
        if not state:
            return
        own = self._local_id(visit)
        receipts = visit.get("departure_farewells", {})
        if visit.get("role") == "guest" and state.get("quote") and own in receipts:
            state["delivered"] = True
        if state.get("delivered") and (len(receipts) == 2 or state.get("peer_supported") is False):
            state["state"] = "done"

    def _record_departure_farewell(self, visit: dict, quote: dict, *, reuse=False) -> None:
        actor = quote["actor_id"]
        receipts = visit.setdefault("departure_farewells", {})
        previous = receipts.get(actor)
        if previous:
            if {k: previous[k] for k in quote} != quote:
                raise ValueError("已经确认的告别不能改写")
            return
        row = next((row for row in visit["log"] if row.get("actor_id") == actor
                    and row.get("kind") == "say" and row.get("text") == quote["text"]
                    and row.get("at") == quote["at"]), None) if reuse else None
        if row is None:
            self._append(visit, actor, "say", quote["text"])
            row = visit["log"][-1]
            row["at"] = quote["at"]
        receipts[actor] = {**copy.deepcopy(quote), "log_seq": row["seq"]}
        self._invalidate_memory(visit)

    def _accept_departure_farewell(self, visit: dict, sender: str, payload: dict,
                                   *, sent_at: float | None = None) -> str:
        version = payload.get("departure_version")
        if version is not None and (type(version) is not int or version != 1):
            raise ValueError("告别协议版本无效")
        self._prepare_departure(visit)
        state = visit.get("departure") or {}
        if state and version is not None:
            state["peer_supported"] = True
        value = payload.get("farewell")
        if value is None:
            self._reconcile_departure(visit)
            return ""
        if version != 1 or visit.get("status") not in TERMINAL or not visit.get("arrived_at"):
            raise ValueError("当前旅程不能接收新的告别")
        quote = self._validate_received_departure(visit, sender, value, sent_at=sent_at)
        if visit["role"] == "host":
            self._record_departure_farewell(visit, quote)
        else:
            receipt = visit.get("departure_farewells", {}).get(sender, {})
            if {k: receipt.get(k) for k in quote} != quote:
                raise ValueError("东道主告别尚未进入已确认经过")
        self._reconcile_departure(visit)
        return quote["id"]

    async def _advance_departure(self, visit_id: str) -> None:
        async with self._lock:
            visit = self._state["visits"].get(visit_id)
            if not visit or self._closed or not self.config.enabled:
                return
            state = visit.get("departure") or {}
            if not state:
                return
            if state.get("state") == "ready" and state.get("deadline", 0) <= self._now():
                state["state"] = "done"
                await self._save()
                return
            if state.get("state") != "pending":
                return
            peer = self._find_peer(visit["scope"], visit["remote"]["id"])
            if not peer or not self._peer_trusted(peer) or not self._life_enabled(visit["scope"]):
                state["state"] = "failed"
                await self._save()
                return
            if state.get("deadline", 0) <= self._now() or state.get("attempts", 0) >= 3 and not state.get("candidate"):
                state["state"] = "failed"
                await self._save()
                return
            if state.get("next_at", 0) > self._now():
                return
            tasks = self._departure_tasks
            task = tasks.get(visit_id)
            if task and not task.done():
                return
            if state.get("inflight"):
                state.pop("inflight", None)
                state["state"] = "pending" if state.get("candidate") else "failed"
                await self._save()
                if state["state"] != "pending":
                    return
            if sum(not task.done() for task in tasks.values()) >= MAX_DEPARTURE_WORKERS:
                return
            token = new_id()
            state["inflight"] = token
            candidate = state.get("candidate")
            state["attempts"] += 0 if candidate else 1
            own = self._local_id(visit)
            visit["calls"][own] += 0 if candidate else 1
            snapshot = copy.deepcopy(visit)
            snapshot["departure_charged"] = not bool(candidate)
            snapshot["departure_context"] = {"ended_at": visit.get("ended_at"),
                "end_reason": visit.get("end_reason", ""),
                "friend_farewell": next((receipt["text"] for actor, receipt in
                    visit.get("departure_farewells", {}).items() if actor != own), "")}
            try:
                await self._save()
            except (Exception, asyncio.CancelledError):
                state.pop("inflight", None)
                if snapshot["departure_charged"]:
                    state["attempts"] = max(0, state["attempts"] - 1)
                    visit["calls"][own] = max(0, visit["calls"][own] - 1)
                state["waits"] = state.get("waits", 0) + 1
                state["state"] = "failed" if state["waits"] >= 3 else "pending"
                state["next_at"] = self._now() + DEPARTURE_RETRY_DELAYS[min(1, state["waits"] - 1)]
                try:
                    await self._save()
                except (Exception, asyncio.CancelledError):
                    pass
                raise
            task = asyncio.create_task(self._generate_departure(visit_id, token, snapshot),
                                       name="world-visit-departure:" + visit_id)
            tasks[visit_id] = task

            def finished(done):
                if tasks.get(visit_id) is done:
                    tasks.pop(visit_id, None)
                if not done.cancelled() and (error := done.exception()) is not None:
                    logger.warning("串门告别暂未完成（%s），保留有界收尾记录", type(error).__name__)

            task.add_done_callback(finished)
        # Observe only a short interval; slow goodbye inference cannot hold up
        # the other lives, original return clock, or ordinary home activity.
        await asyncio.wait({task}, timeout=.05)

    async def _generate_departure(self, visit_id: str, token: str, snapshot: dict) -> None:
        captured = {}
        result = None
        error = None
        try:
            with capture_decision_candidate() as captured:
                async with asyncio.timeout(self.config.timeout_seconds):
                    local = await self.actor_port.describe(snapshot["scope"])
                    if any(local[k] != snapshot["local"][k] for k in ("actor_id", "persona_key")):
                        raise VisitDecisionError("identity", request_started=False)
                    result = snapshot["departure"].get("candidate") or await self.actor_port.decide(
                        snapshot["scope"], "departure", snapshot)
                    if (not isinstance(result, dict) or not isinstance(result.get("say"), str)
                            or not result["say"].strip() or len(result["say"]) > 400
                            or any(result.get(k) for k in ("action", "action_step", "action_control", "scene_update", "outing"))):
                        raise VisitDecisionError("format", format_issue="departure_fields")
                    latest = await self.actor_port.describe(snapshot["scope"])
                    if any(latest[k] != snapshot["local"][k] for k in ("actor_id", "persona_key")):
                        raise VisitDecisionError("identity")
        except asyncio.CancelledError:
            async def preserve():
                async with self._lock:
                    visit = self._state["visits"].get(visit_id)
                    state = (visit or {}).get("departure") or {}
                    if state.get("inflight") == token:
                        candidate = result or captured.get("candidate")
                        if isinstance(candidate, dict) and isinstance(candidate.get("say"), str):
                            state["candidate"] = {"say": candidate["say"]}
                        await self._save()
            saved = asyncio.create_task(preserve())
            while not saved.done():
                try:
                    await asyncio.shield(saved)
                except asyncio.CancelledError:
                    continue
            saved.result()
            raise
        except Exception as exc:
            error = exc
        async with self._lock:
            visit = self._state["visits"].get(visit_id)
            state = (visit or {}).get("departure") or {}
            if (self._closed or state.get("inflight") != token or visit.get("status") not in TERMINAL
                    or visit.get("local") != snapshot["local"]):
                return
            current = self._departure_snapshot_current(snapshot)
            state.pop("inflight", None)
            own = self._local_id(visit)
            if error is not None:
                if isinstance(error, VisitDecisionError) and not error.request_started and snapshot.get("departure_charged"):
                    visit["calls"][own] = max(0, visit["calls"][own] - 1)
                    state["attempts"] = max(0, state["attempts"] - 1)
                    state["waits"] = state.get("waits", 0) + 1
                category = getattr(error, "category", "provider")
                state["last_issue"] = category
                count = max(state["attempts"], state.get("waits", 0))
                if not current or category in {"identity", "budget"} or count >= 3 or self._now() >= state["deadline"]:
                    state["state"] = "failed"
                else:
                    state["next_at"] = self._now() + DEPARTURE_RETRY_DELAYS[max(0, count - 1)]
                await self._save()
                return
            if not current or state.get("state") != "pending" or self._now() >= state.get("deadline", 0):
                state["state"] = "failed"
                await self._save()
                return
            words = result["say"].strip()
            at = self._now()
            quote = {"id": farewell_id(visit_id, own, words, at), "actor_id": own, "text": words, "at": at}
            state.pop("candidate", None)
            state["quote"], state["state"] = quote, "ready"
            if visit["role"] == "host":
                self._record_departure_farewell(visit, quote)
            visit["end_pending"] = {"attempts": 0, "next_at": at}
            await self._save()

    async def _close_departure_tasks(self) -> None:
        tasks = tuple(self._departure_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await self._await_memory_tasks(tasks)
        self._departure_tasks.clear()
