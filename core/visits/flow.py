"""Invitation-first state machine; the host owns scene ordering and settlement."""
from __future__ import annotations

import asyncio
import copy
import math
from typing import Any

from .protocol import RESERVED, TERMINAL, clean_decision, clean_scene, new_id, public_actor, digest
from .activities import (record_action, advance_activity, apply_scene_update,
                         apply_action_control, next_activity_due, activity_context)
from .history_pages import LOG_PAGE_SIZE, read_page
from .outings import CHECKPOINT_LEASE_SECONDS, activity_waiting, begin_activity, outing_enabled, pause_activity, pending_outing_requests, proposed_activity
from .rhythm import VisitRhythm
from .snapshots import validate_snapshot
from .transport import VisitTransportError
from .decisions import VisitDecisionError, capture_decision_candidate
from .model_retry import clear_state_read, defer_decision, defer_state_read, valid_retry
from .ports import VisitStateError, describe_local


class VisitFlow(VisitRhythm):
    def _activity_waiting(self, visit):
        if visit.get("local_experience_pending") or visit.get("remote_experience_pending"):
            return False
        if (visit.get("next_actor") == visit.get("guest_id")
                and visit.get("next_at", 0) <= self._now()):
            return False  # A received new turn is an invitation to respond.
        if activity_context(visit, self._now()).get("ready_action_ids"):
            return False
        due = next_activity_due(visit, self._now())
        return activity_waiting(visit, self._now()) and (due is None or due > self._now())

    async def wake_local_experience(self, visit_id, scope, actor_id, persona_key, at, seq=None, significant=True):
        """A locally read interaction requests attention without sharing its contents."""
        await self._load()
        async with self._lock:
            visit = self._state.get("visits", {}).get(visit_id)
            if (self._closed or not visit or visit.get("status") not in {"active"} | TERMINAL
                    or visit.get("scope") != scope or not outing_enabled(visit)
                    or actor_id not in {self._local_id(visit), visit.get("local", {}).get("actor_id")}
                    or visit.get("local", {}).get("persona_key") != persona_key
                    or type(at) not in {int, float} or not math.isfinite(at)
                    or at < visit.get("arrived_at", 0) or at > self._now() + 300
                    or visit.get("ended_at") and at > visit["ended_at"]):
                return False
            previous = visit.get("local_experience_seq", 0)
            if type(seq) is int and seq <= previous:
                return False
            visit["local_experience_seq"] = seq if type(seq) is int else previous + 1
            if visit["status"] in TERMINAL:
                self._invalidate_memory(visit)
            elif significant:
                visit["local_experience_pending"] = True
                visit["next_at"] = min(visit.get("next_at", self._now()), self._now())
            await self._save()
            return True

    def _accept_experience_wake(self, visit, sender, value):
        if value is None:
            return
        if (not outing_enabled(visit) or visit["role"] != "host" or sender != visit["guest_id"]
                or not isinstance(value, dict) or set(value) != {"seq", "at"}
                or type(value.get("seq")) is not int or value["seq"] < 1
                or type(value.get("at")) not in {int, float} or not math.isfinite(value["at"])
                or not visit.get("arrived_at", 0) <= value["at"] <= self._now() + 300):
            raise ValueError("本次外界互动提醒无效")
        if value["seq"] > visit.get("remote_experience_seq", 0):
            visit["remote_experience_seq"] = value["seq"]
            visit["remote_experience_pending"] = True
            visit["next_at"] = min(visit["next_at"], self._now())

    @staticmethod
    def _retain_private(visit, result, now):
        moment = {key: result[key] for key in ("thought", "reflection", "promise") if result.get(key)}
        if not moment:
            return
        visit.setdefault("private", {}).update(moment)
        moments = visit.setdefault("private_moments", [])
        if not moments or any(moments[-1].get(key) != value for key, value in moment.items()):
            number = max((row.get("seq", 0) for row in moments), default=0) + 1
            moments.append({"at": now, "seq": number, "log_seq": len(visit.get("log", [])), **moment})
            del moments[:-12]

    def _outing_wait_at(self, visit):
        outing = visit.get("outing") or {}
        holds = visit.get("budget_holds", {})
        if holds and outing.get("return_agreed") and outing.get("until", 0) > self._now():
            return outing["until"]
        if holds:
            return max(hold["reset_at"] for hold in holds.values())
        if pending_outing_requests(visit):
            return self._now() + (self.config.poll_seconds if visit["next_actor"] != self._local_id(visit) else 0)
        if visit.get("local_experience_pending") or visit.get("remote_experience_pending"):
            return self._now()
        if activity_context(visit, self._now()).get("ready_action_ids"):
            return self._now()
        if activity_waiting(visit, self._now()):
            due = next_activity_due(visit, self._now())
            return min(outing.get("quiet_until", outing["until"]), due) if due is not None else outing.get("quiet_until", outing["until"])
        return self._now()

    async def _refresh_outing_budget_hold(self, visit_id):
        """Release only our own obsolete allowance wait before the time gate."""
        visit = self._state.get("visits", {}).get(visit_id)
        if (not visit or visit["status"] in TERMINAL or not outing_enabled(visit)
                or visit.get("manual")):
            return False
        own, scope = self._local_id(visit), visit["scope"]
        if own not in visit.get("budget_holds", {}):
            return False
        budget = await self._automatic_budget_snapshot(scope)
        if budget is None or not budget.get("allowed"):
            return False
        async with self._lock:
            visit = self._state.get("visits", {}).get(visit_id)
            if (not visit or self._closed or not self.config.enabled or visit["status"] in TERMINAL
                    or visit["scope"] != scope or self._local_id(visit) != own
                    or own not in visit.get("budget_holds", {})):
                return False
            record = self._state["draws"].get(scope, {})
            if (not budget.get("unlimited") and record.get("budget_hold") is not None
                    and record["budget_hold"] == budget.get("snapshot")):
                return False
            if visit.get("outbox"):
                # Settle the saved signed packet first; its cached reply can
                # still acknowledge our old hold. Keep network backoff intact.
                if not visit.get("budget_notice_pending"):
                    visit["budget_notice_pending"] = True
                    visit["next_at"] = self._now()
                    await self._save()
                return False
            visit["budget_holds"].pop(own)
            record.pop("budget_hold", None)
            if not visit["budget_holds"]:
                visit.pop("outing_budget_hold", None)
            # Publish our withdrawal even if the peer's cached hold still
            # waits until tomorrow. Only its authenticated reply can clear it.
            visit["budget_notice_pending"] = True
            visit["next_at"] = self._now()
            await self._save()
            return True

    async def _hold_outing_budget(self, visit, budget):
        """Retain the last shared place and publish a single reset checkpoint."""
        budget = copy.deepcopy(budget or {})
        budget["allowed"] = False  # Reservation exhaustion can precede the read-only soft ceiling.
        reset = budget.get("reset_at") or self._now() + 86400
        reset = max(self._now() + 60, min(reset, self._now() + 172800))
        visit["budget_side"], visit["automatic_budget"] = "local", budget
        visit.setdefault("budget_holds", {})[self._local_id(visit)] = {
            "reset_at": reset, "reason": str(budget.get("reason") or "今日自主相处额度已用完")[:240]}
        for key in ("day", "used", "limit"):
            if key in budget:
                visit["budget_holds"][self._local_id(visit)][key] = copy.deepcopy(budget[key])
        visit["outing_budget_hold"] = True
        if budget.get("snapshot") is not None:
            self._state["draws"].setdefault(visit["scope"], {})["budget_hold"] = budget["snapshot"]
        if visit.get("arrived_at"):
            visit["expires_at"] = max(visit["expires_at"], reset + CHECKPOINT_LEASE_SECONDS)
            visit["budget_notice_pending"] = True
            visit["next_at"] = self._now()
            await self._save()
        else:
            await self._finish(visit, "declined", str(budget.get("reason") or "今日自主邀请额度已用完"))

    async def _publish_outing_hold(self, visit_id):
        visit = self._state["visits"][visit_id]
        if not visit.get("budget_notice_pending"):
            return
        reply = await self._send(visit_id, {"op": "sync", "visit": self._wire(visit)})
        async with self._lock:
            if self._closed or visit["status"] in TERMINAL:
                return
            self._synchronize(visit, reply["visit"], visit["remote"]["id"])
            if reply.get("delivered_op") == "sync":
                visit.pop("budget_notice_pending", None)
            visit["next_at"] = self._outing_wait_at(visit)
            await self._save()

    async def _outing_budget_ready(self, visit_id, phase):
        """Daily exhaustion stops new inference, never an agreed activity in progress."""
        visit = self._state.get("visits", {}).get(visit_id)
        if (not visit or not outing_enabled(visit) or visit.get("manual")
                or phase not in {"turn", "scene"}):
            return True
        budget = await self._automatic_budget_snapshot(visit["scope"])
        saved_hold = self._state.get("draws", {}).get(visit["scope"], {}).get("budget_hold")
        if budget is None or budget.get("allowed") and (saved_hold is None or budget.get("snapshot") != saved_hold):
            return True
        async with self._lock:
            visit = self._state.get("visits", {}).get(visit_id)
            if not visit or self._closed or visit["status"] in TERMINAL:
                return False
            await self._hold_outing_budget(visit, budget)
        return False

    def _decision_snapshot_current(self, phase, snapshot):
        """A provider dispatch belongs to this exact still-live admission."""
        visit = self._state.get("visits", {}).get(snapshot.get("id"))
        return bool(visit and not self._closed and self.config.enabled
                    and visit["status"] not in TERMINAL and visit["expires_at"] > self._now()
                    and visit["scope"] == snapshot.get("scope") and self._life_enabled(visit["scope"])
                    and snapshot.get("model_pending")
                    and visit.get("model_pending") == snapshot["model_pending"]
                    and visit["local"] == snapshot.get("local")
                    and self._decision_cursor(visit, phase) == self._decision_cursor(snapshot, phase))

    def _refund_unstarted_decision(self, visit, snapshot, token):
        """Withdraw our known unsent admission, even after a live goodbye cleared it."""
        own = self._local_id(snapshot)
        if (self._closed or visit["scope"] != snapshot["scope"] or visit["local"] != snapshot["local"]
                or visit["calls"].get(own) != snapshot["calls"][own]
                or not (visit.get("model_pending") == token
                        or visit["status"] in TERMINAL and not visit.get("model_pending"))):
            return False
        visit["calls"][own] = max(0, visit["calls"][own] - 1)
        if visit.get("model_pending") == token:
            visit.pop("model_pending", None)
        return True

    async def _decision_still_current(self, visit, phase, cursor):
        """Under the journal lock, renew life/phase proof after a successful await."""
        if (self._closed or visit["status"] in TERMINAL
                or self._decision_cursor(visit, phase) != cursor):
            return False
        if not self.config.enabled or not self._life_enabled(visit["scope"]):
            await self._finish(visit, "interrupted", "这个生活已关闭串门或归属发生变化")
            return False
        if visit["expires_at"] <= self._now():
            await self._finish(visit, "expired", "本次停留已经到期，旧决定作废")
            return False
        return True

    @staticmethod
    def _decision_cursor(visit, phase):
        cursor = {"phase": phase, "status": visit["status"], "log_seq": len(visit["log"]),
                  "turns": copy.deepcopy(visit["turns"]), "event_stage": visit["event_stage"],
                  "scene_stage": visit.get("scene_stage", ""), "next_actor": visit["next_actor"],
                  "manual": bool(visit.get("manual")), "manual_continuation": bool(visit.get("manual_continuation")),
                  "outing_request": copy.deepcopy(visit.get("outing_request"))}
        # Omit absent additions so older durable decision cursors still match.
        for name in ("outing_requests", "outing_requests_handled", "outing_request_turn"):
            if visit.get(name):
                cursor[name] = copy.deepcopy(visit[name])
        if phase == "propose":
            cursor["proposal_stage"] = visit.get("proposal_stage", "")
        if outing_enabled(visit):
            cursor["outing_stage"] = (visit.get("outing") or {}).get("stage", -1)
            cursor["quiet_until"] = (visit.get("outing") or {}).get("quiet_until", 0)
            cursor["outing_offer"] = copy.deepcopy(visit.get("outing_offer"))
            if visit.get("shared_activity"):
                cursor["shared_revision"] = digest(visit["shared_activity"])
        return cursor

    async def _resume_saved_decision(self, visit_id, cursor, scope, phase="receive"):
        """Recheck current life gates before consuming its paid saved choice."""
        async with self._lock:
            visit = self._state["visits"].get(visit_id)
            if not visit or self._closed or not self.config.enabled or visit["status"] in TERMINAL:
                return None
            if valid_retry(visit, cursor) and visit["model_retry"]["next_at"] > self._now():
                return None
        try:
            current = await describe_local(self.actor_port, scope, timeout_seconds=self.config.timeout_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            current = None
        async with self._lock:
            visit = self._state["visits"].get(visit_id)
            if not visit or self._closed or not self.config.enabled or visit["status"] in TERMINAL:
                return None
            marker = "proposal_cursor" if phase == "propose" and visit.get("proposal_stage") == "prepared" else "decision_cursor"
            if (self._decision_cursor(visit, phase) != cursor or visit.get(marker) != cursor):
                return None
            if visit["expires_at"] <= self._now():
                await self._finish(visit, "expired", "确认当前生活状态期间本次停留到期，旧决定作废")
                return None
            if not self._life_enabled(scope):
                await self._finish(visit, "interrupted", "这个生活已关闭串门或归属发生变化")
                return None
            if current is None:
                if defer_decision(visit, cursor, category="state", now=self._now(),
                                  candidate=visit["decision"], waiting=True):
                    await self._save()
                else:
                    await self._finish(visit, "interrupted", str(VisitDecisionError("state")))
                return None
            if any(current[key] != visit["local"][key] for key in ("persona_key", "actor_id")):
                await self._finish(visit, "interrupted", "生活身份变化，本次联系结束")
                return None
            if phase in {"turn", "scene"}:
                if not visit.get("manual") and not current["ready"]:
                    if defer_decision(visit, cursor, category="life_busy", now=self._now(),
                                      candidate=visit["decision"], waiting=True):
                        await self._save()
                    else:
                        await self._finish(visit, "interrupted", str(VisitDecisionError("life_busy")))
                    return None
                if (phase == "turn" and not visit.get("unsent_turn")
                        and not self._shared_choice_executable(visit, visit["decision"], self._now())):
                    visit.pop("decision", None)
                    visit.pop("decision_cursor", None)
                    if defer_decision(visit, cursor, category="format", now=self._now(), format_issue="action_references"):
                        await self._save()
                    else:
                        await self._finish(visit, "interrupted", str(VisitDecisionError("format")))
                    return None
                if visit.pop("model_retry", None) is not None:
                    await self._save()
                if not await self._decision_still_current(visit, phase, cursor):
                    return None
                return copy.deepcopy(visit["decision"])
            declining = visit["decision"].get("accept") is False or visit["decision"].get("leave") is True
            if (not visit.get("manual") and not declining
                    and (not current["ready"] and current.get("readiness_wait") is True
                         or current.get("chat_busy") and (phase == "receive" or visit["automatic"]))):
                if defer_decision(visit, cursor, category="life_busy", now=self._now(),
                                  candidate=visit["decision"], waiting=True):
                    await self._save()
                else:
                    await self._finish(visit, "interrupted", str(VisitDecisionError("life_busy")))
                return None
            reason = (current.get("ready_reason") or "现在不方便，稍后再约") if not visit.get("manual") and not declining and not current["ready"] else ""
            if (not visit.get("manual") and not declining and not reason and phase == "propose"
                    and not current.get("reception_open", True)):
                reason = "现在不在接待时间，稍后再约"
            if reason:
                decision = clean_decision({"accept": False, "say": reason, "leave": True})
                decision["contact_kind"] = "contact_status"
                visit["decision"] = decision
                await self._save()
                if not await self._decision_still_current(visit, phase, cursor):
                    return None
            if visit.pop("model_retry", None) is not None:
                await self._save()
            if not await self._decision_still_current(visit, phase, cursor):
                return None
            return copy.deepcopy(visit["decision"])

    def _new_visit(self, scope, peer, local, visit_id, role, reason, automatic):
        own = self._state["instance_id"] + ":" + local["actor_id"]
        host, guest = (own, peer["id"]) if role == "host" else (peer["id"], own)
        duration = (self.config.outing_duration_minutes if self.config.outings_enabled else self.config.duration_minutes) * 60
        return {"id": visit_id, "scope": scope, "role": role, "local": copy.deepcopy(local),
                "connection_id": peer.get("connection_id", peer["actor"]["instance_id"]),
                "remote": copy.deepcopy(peer["actor"]), "reason": str(reason)[:300],
                "status": "inviting", "automatic": automatic, "manual": False,
                "outing_version": 1 if self.config.outings_enabled else 0, "created_at": self._now(),
                "expires_at": self._now() + duration,
                "host_id": host, "guest_id": guest, "scene": {}, "log": [], "private": {},
                "turns": {host: 0, guest: 0}, "calls": {host: 0, guest: 0},
                "max_calls": self.config.max_calls, "turn_limit": self.config.turns_per_actor,
                "next_actor": guest, "next_at": self._now(), "event_stage": "none"}

    async def _decide(self, visit_id, phase):
        if not await self._outing_budget_ready(visit_id, phase):
            return None
        saved_choice = None
        async with self._lock:
            visit = self._state["visits"][visit_id]
            if visit["status"] in TERMINAL or self._closed:
                return None
            if visit["expires_at"] <= self._now():
                await self._finish(visit, "expired", "本次停留已经到期，旧决定作废")
                return None
            cursor = self._decision_cursor(visit, phase)
            retry = visit.get("model_retry") or {}
            if retry and not valid_retry(visit, cursor):
                visit.pop("model_retry", None)
                retry = {}
            if retry and retry["next_at"] > self._now():
                return None
            candidate = copy.deepcopy(retry.get("candidate"))
            resumable = phase in {"receive", "turn", "scene"} or (
                phase == "propose" and visit.get("proposal_stage") == "pending")
            if (resumable and visit.get("decision_cursor") == cursor
                    and isinstance(visit.get("decision"), dict)):
                saved_choice = (cursor, visit["scope"])
            else:
                own = self._local_id(visit)
                if (not visit.get("manual") and not outing_enabled(visit) and candidate is None
                        and visit["calls"][own] >= self._allocation(visit, own)):
                    await self._finish(visit, "completed" if visit.get("arrived_at") else "declined", "本次相见的预算已用完")
                    return None
                if visit.get("model_pending"):
                    return None
                token = new_id()
                visit["model_pending"] = token
                charged = candidate is None
                if charged:
                    visit["calls"][own] += 1
                snapshot = copy.deepcopy(visit)
                try:
                    await self._save()  # Admission is durable before inference.
                except (Exception, asyncio.CancelledError):
                    if (visit.get("model_pending") == token and not self._closed
                            and visit["status"] not in TERMINAL):
                        # No actor call has started: this exact reservation is refundable.
                        visit.pop("model_pending", None)
                        if charged:
                            visit["calls"][own] = max(0, visit["calls"][own] - 1)
                        try:
                            if defer_decision(visit, cursor, category="state", now=self._now(), candidate=candidate):
                                await self._save()
                            else:
                                await self._finish(visit, "interrupted", str(VisitDecisionError("state")))
                        except Exception:
                            pass  # Keep in-memory recovery while propagating the storage failure.
                    raise
        if saved_choice is not None:
            return await self._resume_saved_decision(visit_id, *saved_choice, phase=phase)
        async with self._lock:
            visit = self._state["visits"].get(visit_id)
            if not visit:
                return None
            if visit.get("model_pending") != token:
                if charged and self._refund_unstarted_decision(visit, snapshot, token):
                    await self._save()
                return None
            if not await self._decision_still_current(visit, phase, cursor):
                changed = charged and self._refund_unstarted_decision(visit, snapshot, token)
                if not self._closed and visit.get("model_pending") == token:
                    visit.pop("model_pending", None)
                    changed = True
                if changed:
                    await self._save()
                return None
        generated = {}
        try:
            with capture_decision_candidate() as generated:
                async with asyncio.timeout(self.config.timeout_seconds):
                    if candidate is None:
                        raw = await self.actor_port.decide(snapshot["scope"], phase, snapshot)
                    else:
                        current = await self.actor_port.describe(snapshot["scope"])
                        if any(current[key] != snapshot["local"][key] for key in ("actor_id", "persona_key")):
                            raise VisitDecisionError("identity", request_started=False)
                        if phase in {"propose", "receive"} and (candidate.get("accept") is False or candidate.get("leave") is True):
                            raw = candidate  # A refusal needs identity proof, not an open reception.
                        elif not snapshot.get("manual") and not current["ready"]:
                            raise VisitDecisionError("life_busy", request_started=False, candidate=candidate)
                        elif not snapshot.get("manual") and phase == "propose" and not current.get("reception_open", True):
                            raw = {"accept": False, "leave": True, "say": "现在不在接待时间，稍后再约",
                                   "contact_kind": "contact_status"}
                        elif not snapshot.get("manual") and phase == "receive" and current.get("chat_busy"):
                            raise VisitDecisionError("life_busy", request_started=False, candidate=candidate)
                        else:
                            raw = candidate
                    result = clean_decision(raw)
                    if phase == "turn" and not self._shared_choice_executable(snapshot, result, self._now()):
                        raise VisitDecisionError("format", format_issue="action_references")
                    # Only trusted local prechecks can explicitly refund a successful status result.
                    request_started = not isinstance(raw, dict) or raw.get("_request_started") is not False
                    result["contact_kind"] = ("contact_status" if isinstance(raw, dict)
                                              and raw.get("contact_kind") == "contact_status" else "contact_reply")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            budget = None
            if (isinstance(error, VisitDecisionError) and error.category == "budget"
                    and not snapshot.get("manual") and (snapshot["automatic"] or outing_enabled(snapshot))):
                try:
                    budget = await self._automatic_budget_snapshot(snapshot["scope"])
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass  # The confirmed failure still needs its normal settlement.
            async with self._lock:
                visit = self._state["visits"].get(visit_id)
                if not visit or self._closed:
                    return None
                if visit["status"] in TERMINAL or visit.get("model_pending") != token:
                    if (charged and isinstance(error, VisitDecisionError) and not error.request_started
                            and self._refund_unstarted_decision(visit, snapshot, token)):
                        await self._save()
                    return None
                if visit.get("model_pending") == token and not self._closed:
                    visit.pop("model_pending", None)
                    if charged and isinstance(error, VisitDecisionError) and not error.request_started:
                        visit["calls"][own] = max(0, visit["calls"][own] - 1)
                    if isinstance(error, VisitDecisionError) and error.category == "budget" and outing_enabled(visit):
                        await self._hold_outing_budget(visit, budget)
                        return None
                    if self._decision_cursor(visit, phase) != cursor:
                        await self._save()
                        return None
                    saved_candidate = getattr(error, "candidate", None)
                    if saved_candidate is None:
                        saved_candidate = candidate if candidate is not None else generated.get("candidate")
                    category = (error.category if isinstance(error, VisitDecisionError) else
                                "state" if saved_candidate is not None else
                                "timeout" if isinstance(error, TimeoutError) else "provider")
                    if category == "format":
                        saved_candidate = None  # An invalid choice needs a fresh bounded correction.
                    if defer_decision(visit, cursor, category=category, now=self._now(),
                                      candidate=saved_candidate,
                                      format_issue=getattr(error, "format_issue", ""),
                                      waiting=(saved_candidate is not None or isinstance(error, VisitDecisionError)
                                               and not error.request_started
                                               and category in {"busy", "backoff", "life_busy", "state", "provider"})):
                        await self._save()
                        return None
                    if budget is not None:
                        self._state["draws"].setdefault(visit["scope"], {})["budget_hold"] = budget["snapshot"]
                    reason = str(error) if isinstance(error, VisitDecisionError) else (
                        str(VisitDecisionError("state")) if category == "state" else
                        str(VisitDecisionError("timeout")) if isinstance(error, TimeoutError) else
                        "这次思考未完成，只保留已确认经过")
                    await self._finish(visit, "interrupted", reason)
            return None
        async with self._lock:
            visit = self._state["visits"].get(visit_id)
            if not visit or self._closed:
                return None
            if visit.get("model_pending") != token or visit["status"] in TERMINAL:
                if charged and not request_started and self._refund_unstarted_decision(visit, snapshot, token):
                    await self._save()
                return None
            if charged and not request_started:
                visit["calls"][own] = max(0, visit["calls"][own] - 1)
            if visit["expires_at"] <= self._now():
                await self._finish(visit, "expired", "思考期间本次停留到期，旧决定作废")
                return None
            visit.pop("model_pending", None)
            if self._decision_cursor(visit, phase) != cursor:
                await self._save()
                return None  # Another confirmed step has already advanced this phase.
            self._retain_private(visit, result, self._now())
            visit.pop("model_retry", None)
            # Durable output lets a request retry reuse this decision.
            visit["decision"] = result
            visit["decision_experience_seq"] = snapshot.get("local_experience_seq", 0)
            if resumable:
                visit["decision_cursor"] = cursor
            else:
                visit.pop("decision_cursor", None)
            await self._save()
            if not await self._decision_still_current(visit, phase, cursor):
                return None
        return result

    async def invite(self, scope: str, peer_id: str, reason: str = "", direction: str = "outgoing",
                     automatic: bool = False, manual: bool = False) -> dict[str, Any]:
        self._enabled()
        await self._load()
        if direction not in {"outgoing", "incoming"}:
            raise ValueError("请选择去做客或邀请来访")
        scope = self._canonical_life(scope)
        if manual:
            queued = await self._manual_during_current_visit(scope, peer_id, reason, direction)
            if queued is not None:
                if not queued.get("queued"):
                    return await self._confirm_manual_continuation(queued["id"])
                return queued
        if automatic and self.config.auto_probability <= 0:
            raise ValueError("自主邀请概率为 0，今天先留在自己的世界")
        if automatic and not await self._automatic_budget_available(scope):
            raise VisitDecisionError("budget")
        if automatic:
            cached = self._find_peer(scope, peer_id)
            if cached and self._peer_automatic_hold(cached):
                raise ValueError("友邻今天的自动相处额度已经用完")
        peer_id = await self._prepare_peer(scope, peer_id)
        if automatic:
            peer_available = getattr(self, "_automatic_peer_budget_available", None)
            if callable(peer_available) and not await peer_available(scope, peer_id):
                raise ValueError("友邻今天的自动相处额度已经用完")
        local = await describe_local(self.actor_port, scope, timeout_seconds=self.config.timeout_seconds)
        async with self._lock:
            self._enabled()
            peer = self._peer(scope, peer_id)
            if not self._routing_binding_current(peer):
                raise ValueError("建立联系期间会话归属已变化，请重新联系")
            if not peer.get("enabled", True):
                raise ValueError("这位友邻暂未开启联系")
            current = self.active(scope)
            if current:
                if not manual:
                    raise ValueError("她已经有邀请或串门，先等这次结束")
                if current["remote"]["id"] == peer_id:
                    ongoing = self._state["visits"][current["id"]]
                    if reason and (ongoing.get("outing") or {}).get("return_agreed"):
                        return await self._queue_manual_after_return(scope, ongoing, peer_id, reason, direction)
                    self._mark_manual_request(ongoing, reason)
                    if reason:
                        ongoing["outing_request"] = {"text": str(reason)[:300], "at": self._now()}
                    await self._save()
                    return self._visible(ongoing)
                queued = {"peer_id": peer_id, "reason": str(reason)[:300], "direction": direction,
                          "created_at": self._now(), "manual": True}
                self._state.setdefault("manual_invites", {})[scope] = queued
                await self._save()
                return {"queued": True, "status": "queued", **queued}
            if self._settlement_backlog_full(scope):
                raise ValueError("还有先前的联系需要收尾，请稍后再邀请")
            if (local["persona_key"] != peer["local"]["persona_key"]
                    or local["actor_id"] != peer["local"]["actor_id"]):
                raise ValueError("建立联系期间人设已变化，请稍后重试")
            if automatic and not self._admit_automatic(scope, peer, local):
                await self._save()
                raise ValueError("今天先留在自己的世界，稍后再看其他友邻")
            visit_id = new_id()
            visit = self._new_visit(scope, peer, local, visit_id,
                                    "guest" if direction == "outgoing" else "host", reason, automatic)
            visit["manual"] = bool(manual and not automatic)
            visit["proposal_stage"] = "pending"
            self._state["visits"][visit_id] = visit
            await self._save()
        result = await self._decide(visit_id, "propose")
        return await self._submit_proposal(visit_id, result)

    async def _manual_during_current_visit(self, scope, peer_id, reason, direction):
        async with self._lock:
            current = self.active(scope)
            if not current:
                return None
            if not self._life_enabled(scope):
                raise ValueError("这个生活已关闭串门或归属发生变化")
            if current["remote"]["id"] == peer_id:
                ongoing = self._state["visits"][current["id"]]
                if reason and (ongoing.get("outing") or {}).get("return_agreed"):
                    return await self._queue_manual_after_return(scope, ongoing, peer_id, reason, direction)
                self._mark_manual_request(ongoing, reason)
                if reason:
                    ongoing["outing_request"] = {"text": str(reason)[:300], "at": self._now()}
                await self._save()
                return self._visible(ongoing)
            candidates = self._scoped_candidates(scope)
            if (not any(row["id"] == peer_id for row in candidates)
                    and not self._find_peer(scope, peer_id)
                    and not self._state["connections"].get(peer_id, {}).get("enabled")):
                raise ValueError("请先选择已经认识的友邻")
            queued = {"peer_id": peer_id, "reason": str(reason)[:300], "direction": direction,
                      "created_at": self._now(), "manual": True}
            self._state.setdefault("manual_invites", {})[scope] = queued
            await self._save()
            return {"queued": True, "status": "queued", **queued}

    async def _queue_manual_after_return(self, scope, current, peer_id, reason, direction):
        queued = {"peer_id": peer_id, "reason": str(reason)[:300], "direction": direction,
                  "created_at": self._now(), "manual": True}
        self._state.setdefault("manual_invites", {})[scope] = queued
        await self._save()
        return {"queued": True, "status": "queued", "current_visit_id": current["id"], **queued}

    def _mark_manual_request(self, visit, reason):
        if reason and outing_enabled(visit) and visit.get("arrived_at"):
            requests = visit.setdefault("outing_requests", {})
            actor = self._local_id(visit)
            request = {"id": new_id(), "text": str(reason)[:300], "at": self._now(),
                       "seq": requests.get(actor, {}).get("seq", 0) + 1}
            requests[actor] = request
            visit["manual_continuation"] = True
            visit["next_at"] = self._now()
        if visit.get("manual"):
            return
        if visit["status"] == "inviting" and not visit.get("contacted") and not visit.get("outbox"):
            # No other world has received this proposal. Only a fresh human
            # invitation will cross the signed channel; already paid calls stay recorded.
            visit["manual"], visit["manual_restart_proposal"] = True, True
            if reason:
                visit["reason"] = str(reason)[:300]
            visit["proposal_stage"] = "pending"
            for key in ("proposal_offer", "proposal_cursor", "decision_cursor", "model_retry"):
                visit.pop(key, None)
        else:
            visit["manual_continuation"] = True

    async def _confirm_manual_continuation(self, visit_id):
        """A human request upgrades both signed participants before any fresh inference."""
        visit = self._state["visits"][visit_id]
        if visit.get("manual_restart_proposal"):
            async with self._lock:
                if visit.get("model_pending") or self._closed or visit["status"] in TERMINAL:
                    return self._visible(visit)
                visit.pop("manual_restart_proposal", None)
                duration = self.config.outing_duration_minutes if outing_enabled(visit) else self.config.duration_minutes
                visit["expires_at"] = max(visit["expires_at"], self._now() + duration * 60)
                await self._save()
            result = await self._decide(visit_id, "propose")
            return await self._submit_proposal(visit_id, result)
        if not visit.get("manual_continuation"):
            return self._visible(visit)
        attempted_request_id = (visit.get("outing_requests", {}).get(self._local_id(visit)) or {}).get("id")
        try:
            reply = await self._send(visit_id, {"op": "status"})
            request = visit.get("outing_requests", {}).get(self._local_id(visit))
            pending_delivery = bool(visit.get("manual_continuation") and request
                                    and reply.get("delivered_request") != request["id"])
            if visit.get("manual_continuation") and (reply.get("delivered_op") != "status" or pending_delivery):
                reply = await self._send(visit_id, {"op": "status"})
        except (ValueError, VisitStateError) as error:
            async with self._lock:
                if self._closed or visit["status"] in TERMINAL or not visit.get("manual_continuation"):
                    return self._visible(visit)
                current_request_id = (visit.get("outing_requests", {}).get(self._local_id(visit)) or {}).get("id")
                if current_request_id != attempted_request_id:
                    # A late failure belongs to the earlier suggestion. The
                    # newer human request still needs its own signed delivery.
                    visit["next_at"] = self._now()
                    await self._save()
                    return self._visible(visit)
                visit["continuation_error"] = str(error)[:240]
                visit["next_at"] = self._now() + 120
                await self._save()
            return self._visible(visit)
        async with self._lock:
            if not self._closed and visit["status"] not in TERMINAL:
                self._synchronize(visit, reply["visit"], visit["remote"]["id"])
                request = visit.get("outing_requests", {}).get(self._local_id(visit))
                if (visit.get("manual_continuation") and request
                        and reply.get("delivered_request") != request["id"]):
                    if request["id"] == attempted_request_id:
                        visit["continuation_error"] = "友邻尚未确认收到这条游玩建议，可能需要更新插件"
                        visit["next_at"] = self._now() + 120
                    else:
                        visit["next_at"] = self._now()
                    await self._save()
                    return self._visible(visit)
                if visit.get("manual"):
                    for key in ("budget_holds", "outing_budget_hold", "budget_notice_pending", "continuation_error",
                                "model_retry", "decision_cursor", "proposal_cursor"):
                        visit.pop(key, None)
                    visit["next_at"] = self._outing_wait_at(visit)
                    await self._save()
        return self._visible(visit)

    async def _complete_outing_if_due(self, visit):
        """Execute both previously confirmed farewells at the agreed shared clock."""
        outing = visit.get("outing") or {}
        if (not outing_enabled(visit) or visit["status"] != "active" or not outing.get("return_agreed")
                or outing["until"] > self._now()):
            return False
        shared = visit.get("shared_activity") or {}
        if visit["role"] == "guest" and (outing.get("travel_seconds") or shared.get("action_steps") or shared.get("milestones")):
            # Return on the agreed clock, then obtain the host's final log.
            # Adding local farewells to a stale log would break its prefix.
            await self._finish(visit, "completed", "", ended_at=outing["until"])
            return True
        self._advance_shared_clock(visit)
        for actor in (visit["host_id"], visit["guest_id"]):
            previous = len(visit["log"])
            self._append(visit, actor, "say", outing.get("farewells", {}).get(actor, ""))
            if len(visit["log"]) != previous:
                visit["log"][-1]["at"] = outing["until"]
        # The farewell and agreed return clock are one durable transition.
        # Cancellation after SQLite commits must not leave a later poll time
        # as the permanent end, overstating how long home life was paused.
        await self._finish(visit, "completed", "", ended_at=outing["until"])
        return True

    def _advance_shared_clock(self, visit):
        if visit["role"] != "host" or visit.get("status") != "active":
            return False
        outing = visit.get("outing") or {}
        changed = False
        if (outing.get("travel_seconds") and outing.get("started_at", math.inf) <= self._now()
                and visit.get("outing_logged_stage") != outing.get("stage")):
            self._append(visit, visit["host_id"], "activity", outing["activity"])
            visit["log"][-1]["at"] = outing["started_at"]
            visit["outing_logged_stage"] = outing["stage"]
            changed = True
        return advance_activity(visit, self._now()) or changed

    async def _drain_manual_invites(self):
        for scope, request in list(self._state.get("manual_invites", {}).items()):
            if self._closed or not self.config.enabled:
                return
            if self.active(scope):
                continue
            try:
                result = await self.invite(scope, request["peer_id"], request["reason"], request["direction"], manual=True)
                if result.get("queued"):
                    continue
            except (ValueError, OSError) as error:
                async with self._lock:
                    self._state.setdefault("manual_attempts", []).append({
                        "scope": scope, "peer_id": request["peer_id"], "created_at": self._now(),
                        "status": "declined", "end_reason": str(error)[:500]})
            async with self._lock:
                if self._state.get("manual_invites", {}).get(scope) == request:
                    self._state["manual_invites"].pop(scope, None)
                    await self._save()

    async def _submit_proposal(self, visit_id, result=None, *, retry=False):
        async with self._lock:
            visit = self._state["visits"][visit_id]
            if visit["status"] != "inviting" or self._closed or not self.config.enabled:
                return self._visible(visit)
            if visit.get("proposal_stage") == "pending":
                if not result or visit.get("decision_cursor") != self._decision_cursor(visit, "propose"):
                    return self._visible(visit)
                if not result["accept"] or result["leave"]:
                    self._append(visit, self._local_id(visit), result["contact_kind"], result["say"])
                    await self._finish(visit, "declined", result["say"] or "现在不方便，稍后再约")
                    return self._visible(visit)
                visit["scene"] = clean_scene(result["scene"]) if visit["role"] == "host" else {}
                if outing_enabled(visit) and visit["role"] == "host":
                    self._offer_activity(visit, result)
                self._append(visit, self._local_id(visit), "invitation", result["say"])
                visit["proposal_offer"] = {
                    "op": "offer", "role": "host" if visit["role"] == "guest" else "guest",
                    "reason": visit["reason"], "say": result["say"], "max_calls": visit["max_calls"],
                    "turn_limit": visit["turn_limit"], "duration": self.config.duration_minutes * 60,
                    "manual": visit.get("manual", False), "outing_version": visit.get("outing_version", 0),
                    "invitation": copy.deepcopy(visit["log"][0]) if visit["log"] else None}
                visit["proposal_stage"] = "prepared"
                if outing_enabled(visit):
                    visit["proposal_offer"]["duration"] = self.config.outing_duration_minutes * 60
                visit["proposal_cursor"] = self._decision_cursor(visit, "propose")
                await self._save()
            if visit.get("proposal_stage") != "prepared":
                return self._visible(visit)
            cursor = copy.deepcopy(visit.get("proposal_cursor"))
            scope = visit["scope"]
        result = await self._resume_saved_decision(visit_id, cursor, scope, phase="propose")
        async with self._lock:
            visit = self._state["visits"][visit_id]
            if (not result or self._closed or not self.config.enabled or visit["status"] != "inviting"
                    or visit.get("proposal_stage") != "prepared"
                    or self._decision_cursor(visit, "propose") != cursor):
                return self._visible(visit)
            if not result["accept"] or result["leave"]:
                self._append(visit, self._local_id(visit), result["contact_kind"], result["say"])
                await self._finish(visit, "declined", result["say"] or "现在不方便，稍后再约")
                return self._visible(visit)
            offer = copy.deepcopy(visit["proposal_offer"])
        try:
            reply = await self._send(visit_id, offer)
            async with self._lock:
                visit = self._state["visits"][visit_id]
                if visit["status"] in TERMINAL or self._closed:
                    return self._visible(visit)
                visit["proposal_stage"] = "sent"
                visit.pop("proposal_offer", None)
                visit.pop("proposal_cursor", None)
                if reply.get("declined"):
                    # The signed refusal already settles this contact. The
                    # receiver may never have created a journey to say goodbye to.
                    await self._finish(visit, "declined", str(reply.get("reason") or "对方暂时不方便"), notify=False)
                else:
                    offered = reply.get("visit") or {}
                    if visit.get("outing_version") == 1 and offered.get("outing_version") != 1:
                        visit["outing_version"] = 0
                    visit["max_calls"] = min(visit["max_calls"], offered.get("max_calls", visit["max_calls"]))
                    visit["turn_limit"] = min(visit["turn_limit"], offered.get("turn_limit", visit["turn_limit"]))
                    visit["expires_at"] = min(visit["expires_at"], offered.get("expires_at", visit["expires_at"]))
                    visit["next_at"] = self._now() + self.config.poll_seconds
                    await self._save()
        except VisitStateError:
            if retry:
                raise
            async with self._lock:
                visit = self._state["visits"].get(visit_id)
                if visit and visit["status"] not in TERMINAL and not self._closed:
                    if defer_state_read(visit, now=self._now(), checkpoint="exchange"):
                        await self._save()
                    else:
                        await self._finish(visit, "interrupted", "暂时无法确认当前生活状态，只保留已确认经过")
        except ValueError as error:
            if retry:
                raise
            async with self._lock:
                visit = self._state["visits"].get(visit_id)
                if visit and visit["status"] not in TERMINAL and not self._closed:
                    if isinstance(error, VisitTransportError):
                        visit["contact_error"] = str(error)
                    else:
                        visit.pop("contact_error", None)
                    visit["next_at"] = self._now() + self.config.poll_seconds
                    await self._save()
        async with self._lock:
            return self._visible(self._state["visits"][visit_id])

    def _activate(self, visit, intro=""):
        if visit["status"] not in {"inviting", "offered"}:
            return
        visit["status"] = "active"
        visit["arrived_at"] = self._now()
        self._mark_arrival(visit)
        if visit["role"] == "host":
            if not outing_enabled(visit):
                self._append(visit, visit["host_id"], "arrival", "双方接受邀请，开始在东道主世界相处")
            scene = visit["scene"]
            if not outing_enabled(visit):
                self._append(visit, visit["host_id"], "activity",
                             "，".join(value for value in (scene.get("place"), scene.get("activity")) if value))
            self._append(visit, visit["guest_id"], "say", intro)
        visit["next_at"] = self._now() + self.config.poll_seconds

    def _synchronize(self, visit, remote, sender, *, allow_terminal=False):
        remote = validate_snapshot(visit, remote, now=self._now())
        if visit["status"] in TERMINAL and not allow_terminal:
            self._clear_decision_wait(visit)
            return
        if allow_terminal and (visit["role"] != "guest" or sender != visit["host_id"]
                               or remote.get("status") not in TERMINAL):
            raise ValueError("结束后的确认只能来自东道主的最终经过")
        if remote.get("status") in {"offered", "inviting"}:
            return  # Their pending acceptance does not turn our proposal into a received invitation.
        if visit["role"] == "host":
            if outing_enabled(visit):
                holds = visit.setdefault("budget_holds", {})
                remote_hold = remote.get("budget_holds", {}).get(sender)
                if remote_hold:
                    holds[sender] = copy.deepcopy(remote_hold)
                    visit["expires_at"] = max(visit["expires_at"], remote_hold["reset_at"] + CHECKPOINT_LEASE_SECONDS)
                    visit["next_at"] = self._outing_wait_at(visit)
                elif sender in holds:
                    holds.pop(sender, None)
                    visit["next_at"] = self._outing_wait_at(visit)
            if remote.get("status") == "active":
                self._activate(visit, remote.get("intro", ""))
            elif remote.get("status") in TERMINAL:
                visit["status"] = "declined" if not visit.get("arrived_at") else "interrupted"
                visit["end_reason"] = remote.get("end_reason") or "对方已结束本次联系"
                visit["ended_at"] = remote.get("ended_at", self._now())
                if not visit.get("arrived_at"):
                    visit["log"] = copy.deepcopy(remote.get("log", []))
                self._clear_decision_wait(visit)
            return
        if sender != visit["host_id"] or remote.get("status") not in RESERVED | TERMINAL:
            raise ValueError("无效东道主状态")
        logs = remote.get("log", [])
        prior = visit["log"]
        if len(logs) < len(prior) or logs[:len(prior)] != prior:
            raise ValueError("已确认的经过不能被改写")
        for index, row in enumerate(logs):
            if not isinstance(row, dict) or row.get("seq") != index + 1 or row.get("actor_id") not in {visit["host_id"], visit["guest_id"]}:
                raise ValueError("旅程经过来源无效")
        calls = remote.get("calls", {})
        if not isinstance(calls, dict):
            raise ValueError("东道主调用额度无效")
        remote_calls = calls.get(sender, 0)
        if (type(remote_calls) is not int or remote_calls < 0 or
                not visit.get("manual") and not outing_enabled(visit) and remote_calls > self._allocation(visit, sender)):
            raise ValueError("东道主调用额度无效")
        own_calls = visit["calls"][self._local_id(visit)]
        for key in ("status", "arrived_at", "scene", "log", "next_actor", "next_at", "turns", "event_stage", "ended_at", "end_reason",
                    "outing", "outing_offer", "outing_version", "action_plans", "budget_holds",
                    "outing_requests", "outing_requests_handled", "outing_request_turn", "shared_activity"):
            if key in remote:
                visit[key] = copy.deepcopy(remote[key])
        if "expires_at" in remote:
            visit["expires_at"] = remote["expires_at"] if outing_enabled(visit) and visit.get("arrived_at") else min(
                visit["expires_at"], remote["expires_at"])
        if outing_enabled(visit) and "outing_offer" not in remote:
            visit.pop("outing_offer", None)
        if visit.get("manual"):
            visit.pop("budget_holds", None)
        visit["calls"][sender] = max(visit["calls"][sender], remote_calls)
        visit["calls"][self._local_id(visit)] = own_calls
        self._mark_arrival(visit)
        if visit["status"] in TERMINAL:
            self._clear_decision_wait(visit)

    async def _notify_end(self, visit_id):
        """Deliver a bounded durable goodbye without keeping the local life suspended."""
        async with self._lock:
            visit = self._state["visits"].get(visit_id)
            notice = visit.get("end_pending") if visit else None
            if not notice or notice.get("next_at", 0) > self._now() or self._closed:
                return
            if (self._find_peer(visit["scope"], visit["remote"]["id"]) is None
                    or notice["attempts"] >= 3):
                visit.pop("end_pending", None)
                visit.pop("outbox", None)
                await self._save()
                return
            notice["attempts"] += 1
            notice["next_at"] = self._now() + min(600, 60 * 2 ** notice["attempts"])
            payload = {"op": "end", "visit": self._wire(visit)}
            await self._save()
        try:
            reply = await self._send(visit_id, payload)
            async with self._lock:
                visit = self._state["visits"].get(visit_id)
                if not visit or self._closed:
                    return
                remote = reply.get("visit")
                if visit["role"] == "guest" and isinstance(remote, dict) and remote.get("status") in TERMINAL:
                    prior = (copy.deepcopy(visit["log"]), visit["status"], visit.get("end_reason"))
                    self._synchronize(visit, remote, visit["host_id"], allow_terminal=True)
                    if (visit["log"], visit["status"], visit.get("end_reason")) != prior:
                        self._invalidate_memory(visit)
                visit.pop("end_pending", None)
                await self._save()
        except ValueError:
            async with self._lock:
                visit = self._state["visits"].get(visit_id)
                notice = visit.get("end_pending") if visit else None
                if notice and notice["attempts"] >= 3 and not self._closed:
                    visit.pop("end_pending", None)
                    visit.pop("outbox", None)
                    await self._save()
            return

    def _receive_step(self, visit, sender, payload):
        if visit["role"] != "host" or sender != visit["guest_id"] or visit["status"] != "active":
            raise ValueError("现在不能交付访客回合")
        if visit["next_actor"] != sender or payload.get("seq") != len(visit["log"]):
            raise ValueError("这个回合已过期")
        if not visit.get("manual") and not outing_enabled(visit) and visit["turns"][sender] >= visit["turn_limit"]:
            raise ValueError("访客发言已到上限")
        result = clean_decision(payload.get("decision"))
        self._record_turn(visit, sender, result)
        experience_seq = payload.get("experience_seq", 0)
        if type(experience_seq) is not int or experience_seq < 0:
            raise ValueError("外界互动回应编号无效")
        if experience_seq >= visit.get("remote_experience_seq", 0):
            visit.pop("remote_experience_pending", None)
        if result["leave"]:
            visit["status"], visit["ended_at"] = "completed", self._now()
            visit["end_reason"] = result["say"] if outing_enabled(visit) else "访客已告别归家"
            self._clear_decision_wait(visit)

    @staticmethod
    def _scene_superseded_step(visit, sender, payload):
        """Only a host result can supersede an unadopted legacy scene turn."""
        seq = payload.get("seq")
        if (outing_enabled(visit) or visit["role"] != "host" or sender != visit["guest_id"]
                or visit["status"] != "active" or visit["next_actor"] != sender
                or visit["event_stage"] != "resolved" or type(seq) is not int
                or not 0 <= seq < len(visit["log"])):
            return False
        return all(row["actor_id"] == visit["host_id"] and row["kind"] == "result"
                   for row in visit["log"][seq:])

    def _validate_superseded_step_reply(self, visit, reply, outgoing=None):
        """Validate a non-application proof before acknowledging the durable packet."""
        if "step_superseded" not in reply and "step_seq" not in reply:
            return False
        superseded = reply.get("step_superseded")
        if superseded is True:
            remote = validate_snapshot(visit, reply.get("visit"), now=self._now())
            cursor = visit.get("decision_cursor")
            cursor = cursor if isinstance(cursor, dict) else {}
            seq = reply.get("step_seq")
            sent = outgoing.get("payload") if isinstance(outgoing, dict) else None
            sent = sent if isinstance(sent, dict) else {}
            op = sent.get("op") if outgoing is not None else reply.get("delivered_op")
            expected_seq = sent.get("seq") if outgoing is not None else cursor.get("log_seq")
            if (op != "step" or type(expected_seq) is not int or seq != expected_seq
                    or outing_enabled(visit) or visit["role"] != "guest"
                    or not visit.get("unsent_turn") or type(seq) is not int
                    or not 0 <= seq <= len(visit["log"])
                    or cursor.get("phase") != "turn" or cursor.get("log_seq") != seq
                    or cursor.get("event_stage") != "offered"
                    or cursor.get("next_actor") != visit["guest_id"]
                    or remote.get("status") != "active" or remote.get("next_actor") != visit["guest_id"]
                    or remote.get("event_stage") != "resolved"
                    or cursor.get("turns") != visit["turns"] or remote.get("turns") != visit["turns"]
                    or len(remote.get("log", [])) <= seq
                    or any(row["actor_id"] != visit["host_id"] or row["kind"] != "result"
                           for row in remote["log"][seq:])):
                raise ValueError("旧回合缺少已推进且未被采用的场景证明")
            return True
        raise ValueError("旧回合未采用标记无效")

    def _synchronize_guest_reply(self, visit, reply):
        """A signed result can discard only the exact step it did not apply."""
        superseded = self._validate_superseded_step_reply(visit, reply)
        self._synchronize(visit, reply["visit"], visit["host_id"])
        if superseded:
            for key in ("decision", "decision_cursor", "model_retry", "unsent_turn"):
                visit.pop(key, None)
        elif reply.get("delivered_op") == "step":
            visit.pop("unsent_turn", None)

    @staticmethod
    def _shared_choice_executable(visit, result, now):
        """Check explicit references against public state before committing a model turn."""
        if not outing_enabled(visit):
            return True
        if not visit.get("outing"):
            return not result.get("action_control") and not result.get("scene_update")
        trial = copy.deepcopy(visit)
        actor = visit["host_id"] if visit["role"] == "host" else visit["guest_id"]
        advance_activity(trial, now)
        if result.get("action_control") and not apply_action_control(trial, actor, result["action_control"], now):
            return False
        if result.get("scene_update") and not apply_scene_update(trial, actor, result["scene_update"], now):
            return False
        new_plan = bool(result.get("outing") and not result.get("continue_activity")
                        or actor == visit["guest_id"] and visit.get("outing_offer"))
        step = result.get("action_step") or {}
        return not step.get("peer_action_id") or not new_plan and record_action(trial, actor, step, now)

    @staticmethod
    def _confirmed_step_receipt(visit, remote, outgoing):
        """A late exact step receipt may acknowledge work already confirmed by a push."""
        sent = outgoing.get("payload") or {}
        cursor = visit.get("decision_cursor") or {}
        seq = sent.get("seq")
        guest = visit["guest_id"]
        if (visit["role"] != "guest" or not outing_enabled(visit) or not visit.get("unsent_turn")
                or sent.get("op") != "step" or outgoing.get("sender") != guest
                or outgoing.get("target") != visit["host_id"] or type(seq) is not int or seq < 0
                or cursor.get("phase") != "turn" or cursor.get("log_seq") != seq
                or not isinstance(sent.get("decision"), dict) or not isinstance(visit.get("decision"), dict)
                or any(visit["decision"].get(key) != value for key, value in sent["decision"].items())
                or any(remote.get(key) != visit.get(key) for key in ("id", "host_id", "guest_id", "manual", "outing_version"))
                or remote.get("status") != "active" or visit.get("status") != "active"):
            return False
        turns = remote.get("turns") or {}
        prior_turns = cursor.get("turns") or {}
        if (turns.get(guest) != prior_turns.get(guest, -2) + 1
                or turns.get(guest) != visit["turns"][guest]
                or type(turns.get(visit["host_id"])) is not int
                or turns[visit["host_id"]] > visit["turns"][visit["host_id"]]):
            return False
        try:
            logs = read_page(visit["log"], remote, "log", LOG_PAGE_SIZE, advancing=False)
        except ValueError:
            return False
        if not seq <= len(logs) < len(visit["log"]) or logs != visit["log"][:len(logs)]:
            return False
        choice = sent["decision"]
        expected = [("say", choice.get("say", "")),
                    ("action_plan", choice.get("action") or (choice.get("action_step") or {}).get("description", ""))]
        expected = [(kind, value) for kind, value in expected if value]
        if not expected:
            control = choice.get("action_control") or {}
            operation = control.get("operation")
            owner = guest if operation == "cancel" else visit["host_id"]
            status = "cancelled" if operation == "cancel" else "declined"
            rows = (remote.get("shared_activity") or {}).get("action_steps", {}).get(owner, [])
            return operation in {"cancel", "decline"} and any(
                row.get("id") == control.get("action_id") and row.get("status") == status
                and row.get("controlled_by") == guest and row.get("control_operation") == operation
                and row.get("control_action_id") == control.get("action_id")
                and row.get("control_reason") == control.get("reason")
                and row.get("stage") == cursor.get("outing_stage")
                and type(row.get("ended_at")) in {int, float} and math.isfinite(row["ended_at"])
                and type(row.get("accepted_at")) in {int, float} and math.isfinite(row["accepted_at"])
                and row["accepted_at"] <= row["ended_at"]
                and outgoing["created_at"] <= row["ended_at"] <= max((item.get("at", 0) for item in visit["log"]), default=0)
                for row in rows)
        if len(logs) < seq + len(expected):
            return False
        return all(row.get("actor_id") == guest and row.get("kind") == kind and row.get("text") == value
                   for row, (kind, value) in zip(logs[seq:], expected))

    def _record_turn(self, visit, sender, result):
        self._append(visit, sender, "say", result["say"])
        self._append(visit, sender, "action_plan", result["action"] or (result.get("action_step") or {}).get("description", ""))
        visit["turns"][sender] += 1
        if (sender == self._local_id(visit)
                and visit.get("decision_experience_seq", 0) >= visit.get("local_experience_seq", 0)):
            visit.pop("local_experience_pending", None)
        if outing_enabled(visit):
            advance_activity(visit, self._now())
            if result.get("action_control"):
                apply_action_control(visit, sender, result["action_control"], self._now())
            if result.get("scene_update") and apply_scene_update(visit, sender, result["scene_update"], self._now()):
                self._append(visit, sender, "milestone", result["scene_update"]["summary"])
            new_plan = bool(result.get("outing") and not result.get("continue_activity")
                            or sender == visit["guest_id"] and visit.get("outing_offer"))
            if not new_plan and visit.get("outing"):
                record_action(visit, sender, result.get("action_step"), self._now())
            self._record_outing_choice(visit, sender, result)
            return
        if visit["event_stage"] == "offered":
            visit.setdefault("action_plans", {})[sender] = result["action"] or result["say"]
        visit["next_actor"] = visit["host_id"] if sender == visit["guest_id"] else visit["guest_id"]
        visit["next_at"] = self._now() + self.config.poll_seconds

    def _offer_activity(self, visit, decision):
        if decision.get("continue_activity") and (visit.get("outing") or {}).get("until", 0) > self._now():
            visit.pop("outing_offer", None)
            visit["outing_host_pause"] = decision.get("pause_minutes", 5)
            return False
        offer = proposed_activity(decision)
        if not offer:
            visit.pop("outing_offer", None)
            return False
        visit["outing_offer"] = offer
        visit["outing_host_action"] = decision.get("action", "")
        visit["outing_host_step"] = copy.deepcopy(decision.get("action_step") or {})
        visit["outing_host_farewell"] = decision.get("farewell", "")
        return True

    def _record_outing_choice(self, visit, sender, result):
        settled = bool(sender == visit["host_id"] and result.get("outcome")
                       and visit.get("event_stage") == "offered" and len(visit.get("action_plans", {})) == 2)
        if result.get("outcome") and not settled:
            self._append(visit, sender, "experience", result["outcome"])
        if settled:
            self._append(visit, sender, "result", result["outcome"])
            visit["event_stage"] = "resolved"
        if result.get("leave"):
            # A farewell is an independent decision to end this visit. It
            # cannot also accept a new plan or promise another shared pause.
            for key in ("outing_offer", "outing_host_action", "outing_host_step", "outing_host_farewell", "outing_host_pause"):
                visit.pop(key, None)
            return
        if sender == visit["host_id"]:
            request_turn = {actor: request["id"] for actor, request in pending_outing_requests(visit).items()}
            if request_turn:
                visit["outing_request_turn"] = request_turn
            else:
                visit.pop("outing_request_turn", None)
            self._offer_activity(visit, result)
            visit["next_actor"] = visit["guest_id"]
            visit["next_at"] = self._now() + self.config.poll_seconds
            return
        for actor, request_id in visit.pop("outing_request_turn", {}).items():
            if visit.get("outing_requests", {}).get(actor, {}).get("id") == request_id:
                visit.setdefault("outing_requests_handled", {})[actor] = request_id
        offer = visit.get("outing_offer")
        if offer and result.get("outing_accept") and not result.get("leave"):
            prior_route = (visit.get("outing") or {}).get("route", [])
            if prior_route:
                # Keep the original planned deadline and record the actual switch.
                prior_route[-1].setdefault("ended_at", self._now())
            begin_activity(visit, offer, self._now(), result, floor_seconds=self.config.poll_seconds)
            if offer.get("travel_seconds"):
                self._append(visit, visit["host_id"], "travel_plan", "前往" + offer["place"] + "，抵达后计划" + offer["activity"])
            else:
                self._append(visit, visit["host_id"], "activity", offer["activity"])
                visit["outing_logged_stage"] = visit["outing"]["stage"]
            record_action(visit, visit["host_id"], visit.pop("outing_host_step", {}), self._now())
            record_action(visit, visit["guest_id"], result.get("action_step"), self._now())
            visit["action_plans"] = {visit["host_id"]: visit.pop("outing_host_action", ""),
                                     visit["guest_id"]: result.get("action", "")}
            if offer.get("event"):
                self._append(visit, visit["host_id"], "event", offer["event"])
                visit["event_stage"] = "offered"
            else:
                visit["event_stage"] = "none"
            visit["next_at"] = self._outing_wait_at(visit)
            return
        if not offer and (visit.get("outing") or {}).get("until", 0) > self._now():
            pause_activity(visit, visit.pop("outing_host_pause", 5), result.get("pause_minutes", 5),
                           self._now(), floor_seconds=self.config.poll_seconds)
            visit["next_at"] = self._outing_wait_at(visit)
            return
        visit.pop("outing_offer", None)
        visit.pop("outing_host_action", None)
        visit.pop("outing_host_step", None)
        visit.pop("outing_host_farewell", None)
        visit["next_actor"] = visit["host_id"]
        # A declined plan gets breathing room; it is not an every-tick retry.
        visit["next_at"] = self._now() + 300

    async def tick(self) -> None:
        if not self.config.enabled or self._closed or self.error or self._tick_lock.locked():
            return
        await self._load()
        async with self._tick_lock:
            ids = list(self._state["visits"])
            for visit_id in ids:
                if self._closed or not self.config.enabled:
                    return
                visit = self._state["visits"].get(visit_id)
                if not visit:
                    continue
                if visit["status"] in TERMINAL:
                    await self._notify_end(visit_id)
                    await self._observe_memory(visit_id)
                    await self._observe_record_card(visit_id)
                    continue
                async with self._lock:
                    if self._advance_shared_clock(visit):
                        await self._save()
                    completed = await self._complete_outing_if_due(visit)
                if completed:
                    await self._notify_end(visit_id)
                    await self._observe_memory(visit_id)
                    await self._observe_record_card(visit_id)
                    continue
                if visit["expires_at"] <= self._now():
                    async with self._lock:
                        await self._finish(visit, "interrupted" if outing_enabled(visit) and visit.get("arrived_at") else "expired",
                                           "后续联络未续上，只保留已确认经过" if outing_enabled(visit) and visit.get("arrived_at") else "本次停留到期，保留已确认经过")
                    await self._notify_end(visit_id)
                    await self._observe_memory(visit_id)
                    await self._observe_record_card(visit_id)
                    continue
                if not self._life_enabled(visit["scope"]):
                    async with self._lock:
                        await self._finish(visit, "interrupted", "这个生活已关闭串门或归属发生变化")
                    continue
                try:
                    await self._refresh_outing_budget_hold(visit_id)
                except VisitStateError:
                    # A failed read cannot prove the previous wait obsolete.
                    continue
                if max(visit["next_at"], (visit.get("state_retry") or {}).get("next_at", 0),
                       self._touched.get(visit["scope"], 0)) > self._now():
                    continue
                try:
                    async with asyncio.timeout(self.config.timeout_seconds):
                        current = await self.actor_port.describe(visit["scope"])
                except asyncio.CancelledError:
                    raise
                except Exception:
                    async with self._lock:
                        if self._closed or not self.config.enabled or visit["status"] in TERMINAL:
                            continue
                        if not defer_state_read(visit, now=self._now(), checkpoint="tick"):
                            await self._finish(visit, "interrupted", "暂时无法确认当前生活状态，只保留已确认经过")
                        else:
                            await self._save()
                    continue
                if self._closed or not self.config.enabled or visit["status"] in TERMINAL:
                    continue
                async with self._lock:
                    if clear_state_read(visit, checkpoint="tick"):
                        await self._save()
                if current["persona_key"] != visit["local"]["persona_key"] or current["actor_id"] != visit["local"]["actor_id"]:
                    async with self._lock:
                        await self._finish(visit, "interrupted", "生活身份变化，本次联系结束")
                    continue
                try:
                    await self._advance(visit_id)
                except VisitStateError:
                    async with self._lock:
                        if visit["status"] in TERMINAL or self._closed:
                            continue
                        if defer_state_read(visit, now=self._now(), checkpoint="exchange"):
                            await self._save()
                        else:
                            await self._finish(visit, "interrupted", "暂时无法确认当前生活状态，只保留已确认经过")
                except ValueError as error:
                    async with self._lock:
                        if visit["status"] in TERMINAL or self._closed:
                            continue
                        if isinstance(error, VisitTransportError):
                            visit["contact_error"] = str(error)
                        else:
                            visit.pop("contact_error", None)
                        visit["network_failures"] = visit.get("network_failures", 0) + 1
                        visit["next_at"] = self._now() + min(600, 60 * 2 ** visit["network_failures"])
                        if visit["network_failures"] >= 3:
                            reason = "暂时联系不上，只保留已确认经过"
                            if isinstance(error, VisitTransportError):
                                reason += "：" + str(error)
                            await self._finish(visit, "interrupted", reason)
                        else:
                            await self._save()
            await self._drain_manual_invites()
            await self._automatic()

    async def _advance(self, visit_id):
        visit = self._state["visits"][visit_id]
        if visit.get("manual_restart_proposal"):
            await self._confirm_manual_continuation(visit_id)
            return
        if visit.get("manual_continuation"):
            await self._confirm_manual_continuation(visit_id)
            if visit.get("manual_continuation"):
                return
        if visit["status"] == "offered":
            result = await self._decide(visit_id, "receive")
            async with self._lock:
                if visit["status"] != "offered" or self._closed or not self.config.enabled:
                    return
                if not result:
                    return  # No completed choice is not a refusal.
                if visit.get("decision_cursor") != self._decision_cursor(visit, "receive"):
                    return
                if not result["accept"] or result["leave"]:
                    if visit["status"] not in TERMINAL:
                        self._append(visit, self._local_id(visit), result["contact_kind"], result["say"])
                        await self._finish(visit, "declined", result["say"] or "现在不方便，稍后再约")
                    return
                visit["intro"] = result["say"]
                if visit["role"] == "host":
                    visit["scene"] = clean_scene(result["scene"])
                    if outing_enabled(visit):
                        self._offer_activity(visit, result)
                self._activate(visit)
                if visit["role"] == "host":
                    self._append(visit, visit["host_id"], "say", result["say"])
                await self._save()
            return
        if visit["status"] == "inviting":
            if visit.get("proposal_stage") == "pending":
                if visit.get("model_pending"):
                    return
                if (visit.get("decision_cursor") == self._decision_cursor(visit, "propose")
                        and isinstance(visit.get("decision"), dict)) or valid_retry(visit, self._decision_cursor(visit, "propose")):
                    result = await self._decide(visit_id, "propose")
                    await self._submit_proposal(visit_id, result)
                return
            if visit.get("proposal_stage") == "prepared":
                await self._submit_proposal(visit_id, retry=True)
                return
            reply = await self._send(visit_id, {"op": "status"})
            async with self._lock:
                if self._closed or visit["status"] in TERMINAL:
                    return
                if reply.get("declined"):
                    await self._finish(visit, "declined", str(reply.get("reason", "稍后再约")), notify=False)
                elif "visit" in reply:
                    self._synchronize(visit, reply["visit"], visit["remote"]["id"])
                    await self._save()
            return
        if outing_enabled(visit):
            await self._advance_outing(visit_id)
            return
        if visit["role"] == "guest":
            reply = await self._send(visit_id, {"op": "sync", "visit": {**self._wire(visit), "intro": visit.get("intro", "")}})
            async with self._lock:
                if self._closed or visit["status"] in TERMINAL:
                    return
                self._synchronize_guest_reply(visit, reply)
                await self._save()
            if visit["status"] != "active" or visit["next_actor"] != visit["guest_id"] or visit["next_at"] > self._now():
                return
            if visit["event_stage"] == "offered" and len(visit.get("action_plans", {})) == 2:
                return  # The host settles both choices before a new guest turn.
            async with self._lock:
                if visit["status"] in TERMINAL or self._closed:
                    return
                if visit.get("unsent_turn") and (visit.get("decision_cursor") != self._decision_cursor(visit, "turn")
                                                 or not isinstance(visit.get("decision"), dict)):
                    await self._finish(visit, "interrupted", "待发回合已失去原阶段证明，只保留已确认经过")
                    return
            result = await self._decide(visit_id, "turn")
            if not result:
                return
            async with self._lock:
                if visit["status"] in TERMINAL or self._closed:
                    return
                if visit.get("decision_cursor") != self._decision_cursor(visit, "turn"):
                    return  # A confirmed scene or request advanced after inference.
                visit["unsent_turn"] = True
                await self._save()
                step = {"op": "step", "seq": len(visit["log"]),
                        "experience_seq": visit.get("decision_experience_seq", 0), "decision":
                        {k: result[k] for k in ("say", "action", "leave")}}
            reply = await self._send(visit_id, step)
            async with self._lock:
                if self._closed or visit["status"] in TERMINAL:
                    return
                self._synchronize_guest_reply(visit, reply)
                await self._save()
            return
        retry = visit.get("model_retry") or {}
        scene_cursor = self._decision_cursor(visit, "scene")
        saved_scene = (visit.get("decision_cursor") == scene_cursor and isinstance(visit.get("decision"), dict))
        if ((retry.get("phase") == "scene" and valid_retry(visit, scene_cursor)) or saved_scene) \
                and scene_cursor.get("scene_stage") in {"event_offer", "event_resolve"}:
            await self._scene(visit_id, scene_cursor["scene_stage"])
            return
        if visit["event_stage"] == "offered" and len(visit.get("action_plans", {})) == 2:
            await self._scene(visit_id, "event_resolve")
            return
        if visit["next_actor"] != visit["host_id"]:
            return
        if not visit.get("manual") and visit["turns"][visit["host_id"]] >= visit["turn_limit"]:
            async with self._lock:
                await self._finish(visit, "completed", "这次先聊到这里，告别归家")
            return
        result = await self._decide(visit_id, "turn")
        async with self._lock:
            if not result or visit["status"] in TERMINAL or self._closed:
                return
            if visit.get("decision_cursor") != self._decision_cursor(visit, "turn"):
                return
            self._record_turn(visit, visit["host_id"], result)
            if result["leave"] or not visit.get("manual") and visit["turns"][visit["host_id"]] >= visit["turn_limit"]:
                await self._finish(visit, "completed", "告别归家")
            else:
                await self._save()
        if visit["status"] == "active" and visit["event_stage"] == "none" and visit["turns"][visit["host_id"]] == 1:
            if self._random() < .2:
                await self._scene(visit_id, "event_offer")
            else:
                async with self._lock:
                    visit["event_stage"] = "skipped"
                    await self._save()

    async def _advance_outing(self, visit_id):
        visit = self._state["visits"][visit_id]
        if (visit["role"] == "host" and visit.get("outbox", {}).get("payload", {}).get("op") == "sync"):
            await self._send(visit_id, {"op": "sync", "visit": self._wire(visit)})
        if visit.get("budget_notice_pending"):
            await self._publish_outing_hold(visit_id)
            if visit.get("budget_notice_pending") or visit["status"] != "active":
                return
        if self._activity_waiting(visit):
            return  # The same confirmed shared clock runs on both servers.
        async with self._lock:
            if self._advance_shared_clock(visit):
                await self._save()
            if await self._complete_outing_if_due(visit):
                return
            holds = visit.get("budget_holds", {})
            if holds and not visit.get("manual"):
                resume_at = max(hold["reset_at"] for hold in holds.values())
                if resume_at > self._now():
                    visit["next_at"] = resume_at
                    await self._save()
                    return
                visit.pop("budget_holds", None)
                visit.pop("outing_budget_hold", None)
        if visit["role"] == "guest":
            sync = {"op": "sync", "visit": {**self._wire(visit), "intro": visit.get("intro", "")}}
            if visit.get("local_experience_pending"):
                sync["experience_wake"] = {"seq": visit.get("local_experience_seq", 1), "at": self._now()}
            reply = await self._send(visit_id, sync)
            async with self._lock:
                if self._closed or visit["status"] in TERMINAL:
                    return
                self._synchronize(visit, reply["visit"], visit["host_id"])
                if reply.get("delivered_op") == "step":
                    visit.pop("unsent_turn", None)
                    if visit.get("decision_experience_seq", 0) >= visit.get("local_experience_seq", 0):
                        visit.pop("local_experience_pending", None)
                await self._save()
            if (visit["status"] != "active" or self._activity_waiting(visit)
                    or visit["next_actor"] != visit["guest_id"] or visit["next_at"] > self._now()):
                return
            result = await self._decide(visit_id, "turn")
            if not result:
                await self._publish_outing_hold(visit_id)
                return
            async with self._lock:
                if self._closed or visit["status"] in TERMINAL:
                    return
                if visit.get("decision_cursor") != self._decision_cursor(visit, "turn"):
                    return  # A new signed request arrived after this decision finished.
                visit["unsent_turn"] = True
                await self._save()
                step = {"op": "step", "seq": len(visit["log"]),
                        "experience_seq": visit.get("decision_experience_seq", 0), "decision":
                        {key: result[key] for key in ("say", "action", "leave", "outing", "outing_accept", "outcome", "return_after", "farewell", "pause_minutes", "continue_activity", "action_step", "action_control", "scene_update")}}
            reply = await self._send(visit_id, step)
            async with self._lock:
                if self._closed or visit["status"] in TERMINAL:
                    return
                self._synchronize(visit, reply["visit"], visit["host_id"])
                visit.pop("unsent_turn", None)
                if visit.get("decision_experience_seq", 0) >= visit.get("local_experience_seq", 0):
                    visit.pop("local_experience_pending", None)
                await self._save()
            return
        if visit["next_actor"] != visit["host_id"]:
            return
        result = await self._decide(visit_id, "turn")
        if not result:
            await self._publish_outing_hold(visit_id)
            return
        async with self._lock:
            if not result or self._closed or visit["status"] in TERMINAL:
                return
            if visit.get("decision_cursor") != self._decision_cursor(visit, "turn"):
                return
            had_experience = visit.get("local_experience_pending") or visit.get("remote_experience_pending")
            self._record_turn(visit, visit["host_id"], result)
            shared = visit.get("shared_activity") or {}
            publish = bool(had_experience or shared.get("action_steps") or shared.get("milestones"))
            if result["leave"]:
                await self._finish(visit, "completed", result["say"])
            else:
                await self._save()
        if publish and visit["status"] == "active":
            await self._send(visit_id, {"op": "sync", "visit": self._wire(visit)})

    async def _scene(self, visit_id, stage):
        async with self._lock:
            visit = self._state["visits"][visit_id]
            visit["scene_stage"] = stage
            await self._save()
        result = await self._decide(visit_id, "scene")
        async with self._lock:
            if not result or visit["status"] in TERMINAL or self._closed:
                return
            if visit.get("decision_cursor") != self._decision_cursor(visit, "scene"):
                return
            if stage == "event_offer":
                event = result["scene"]["event"]
                visit["scene"]["event"] = event
                visit["event_stage"] = "offered" if event else "skipped"
                visit["action_plans"] = {}
                self._append(visit, visit["host_id"], "event", event)
            else:
                visit["event_stage"] = "resolved"
                self._append(visit, visit["host_id"], "result", result["outcome"])
            visit["next_at"] = self._now() + self.config.poll_seconds
            await self._save()
