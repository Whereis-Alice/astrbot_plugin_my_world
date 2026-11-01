"""Invitation-first state machine; the host owns scene ordering and settlement."""
from __future__ import annotations

import asyncio
import copy
from typing import Any

from .protocol import RESERVED, TERMINAL, clean_decision, clean_scene, new_id, public_actor
from .rhythm import VisitRhythm
from .snapshots import validate_snapshot
from .transport import VisitTransportError
from .decisions import VisitDecisionError, capture_decision_candidate
from .model_retry import clear_state_read, defer_decision, defer_state_read, valid_retry
from .ports import VisitStateError, describe_local


class VisitFlow(VisitRhythm):
    @staticmethod
    def _decision_cursor(visit, phase):
        cursor = {"phase": phase, "status": visit["status"], "log_seq": len(visit["log"]),
                  "turns": copy.deepcopy(visit["turns"]), "event_stage": visit["event_stage"],
                  "scene_stage": visit.get("scene_stage", ""), "next_actor": visit["next_actor"]}
        if phase == "propose":
            cursor["proposal_stage"] = visit.get("proposal_stage", "")
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
                                  candidate=visit["decision"]):
                    await self._save()
                else:
                    await self._finish(visit, "interrupted", str(VisitDecisionError("state")))
                return None
            if any(current[key] != visit["local"][key] for key in ("persona_key", "actor_id")):
                await self._finish(visit, "interrupted", "生活身份变化，本次联系结束")
                return None
            if phase in {"turn", "scene"}:
                if not current["ready"]:
                    if defer_decision(visit, cursor, category="life_busy", now=self._now(),
                                      candidate=visit["decision"]):
                        await self._save()
                    else:
                        await self._finish(visit, "interrupted", str(VisitDecisionError("life_busy")))
                    return None
                if visit.pop("model_retry", None) is not None:
                    await self._save()
                return copy.deepcopy(visit["decision"])
            declining = visit["decision"].get("accept") is False or visit["decision"].get("leave") is True
            reason = (current.get("ready_reason") or "现在不方便，稍后再约") if not declining and not current["ready"] else ""
            if not declining and not reason and not current.get("reception_open", True):
                reason = "现在不在接待时间，稍后再约"
            if not declining and not reason and current.get("chat_busy") and (phase == "receive" or visit["automatic"]):
                reason = "正在接眼前的交谈，稍后再约"
            if reason:
                decision = clean_decision({"accept": False, "say": reason, "leave": True})
                decision["contact_kind"] = "contact_status"
                visit["decision"] = decision
                await self._save()
            if visit.pop("model_retry", None) is not None:
                await self._save()
            return copy.deepcopy(visit["decision"])

    def _new_visit(self, scope, peer, local, visit_id, role, reason, automatic):
        own = self._state["instance_id"] + ":" + local["actor_id"]
        host, guest = (own, peer["id"]) if role == "host" else (peer["id"], own)
        return {"id": visit_id, "scope": scope, "role": role, "local": copy.deepcopy(local),
                "connection_id": peer.get("connection_id", peer["actor"]["instance_id"]),
                "remote": copy.deepcopy(peer["actor"]), "reason": str(reason)[:300],
                "status": "inviting", "automatic": automatic, "created_at": self._now(),
                "expires_at": self._now() + self.config.duration_minutes * 60,
                "host_id": host, "guest_id": guest, "scene": {}, "log": [], "private": {},
                "turns": {host: 0, guest: 0}, "calls": {host: 0, guest: 0},
                "max_calls": self.config.max_calls, "turn_limit": self.config.turns_per_actor,
                "next_actor": guest, "next_at": self._now(), "event_stage": "none"}

    async def _decide(self, visit_id, phase):
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
                if candidate is None and visit["calls"][own] >= self._allocation(visit, own):
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
                        elif not current["ready"]:
                            raise VisitDecisionError("life_busy", request_started=False, candidate=candidate)
                        elif phase in {"propose", "receive"} and not current.get("reception_open", True):
                            raw = {"accept": False, "leave": True, "say": "现在不在接待时间，稍后再约",
                                   "contact_kind": "contact_status"}
                        elif phase == "receive" and current.get("chat_busy"):
                            raw = {"accept": False, "leave": True, "say": "正在接眼前的交谈，稍后再约",
                                   "contact_kind": "contact_status"}
                        else:
                            raw = candidate
                    result = clean_decision(raw)
                    # Only trusted local prechecks can explicitly refund a successful status result.
                    request_started = not isinstance(raw, dict) or raw.get("_request_started") is not False
                    result["contact_kind"] = ("contact_status" if isinstance(raw, dict)
                                              and raw.get("contact_kind") == "contact_status" else "contact_reply")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            budget = None
            if isinstance(error, VisitDecisionError) and error.category == "budget" and snapshot["automatic"]:
                try:
                    budget = await self._automatic_budget_snapshot(snapshot["scope"])
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass  # The confirmed failure still needs its normal settlement.
            async with self._lock:
                visit = self._state["visits"][visit_id]
                if visit.get("model_pending") == token and not self._closed:
                    if visit["status"] in TERMINAL:
                        return None
                    visit.pop("model_pending", None)
                    if charged and isinstance(error, VisitDecisionError) and not error.request_started:
                        visit["calls"][own] = max(0, visit["calls"][own] - 1)
                    if self._decision_cursor(visit, phase) != cursor:
                        await self._save()
                        return None
                    saved_candidate = getattr(error, "candidate", None)
                    if saved_candidate is None:
                        saved_candidate = candidate if candidate is not None else generated.get("candidate")
                    category = (error.category if isinstance(error, VisitDecisionError) else
                                "state" if saved_candidate is not None else
                                "timeout" if isinstance(error, TimeoutError) else "provider")
                    if defer_decision(visit, cursor, category=category, now=self._now(),
                                      candidate=saved_candidate):
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
            visit = self._state["visits"][visit_id]
            if visit.get("model_pending") != token or visit["status"] in TERMINAL or self._closed:
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
            visit["private"] = {key: result[key] for key in ("thought", "reflection", "promise")}
            visit.pop("model_retry", None)
            # Durable output lets a request retry reuse this decision.
            visit["decision"] = result
            if resumable:
                visit["decision_cursor"] = cursor
            else:
                visit.pop("decision_cursor", None)
            await self._save()
        return result

    async def invite(self, scope: str, peer_id: str, reason: str = "", direction: str = "outgoing",
                     automatic: bool = False) -> dict[str, Any]:
        self._enabled()
        await self._load()
        if direction not in {"outgoing", "incoming"}:
            raise ValueError("请选择去做客或邀请来访")
        if automatic and self.config.auto_probability <= 0:
            raise ValueError("自主邀请概率为 0，今天先留在自己的世界")
        if automatic and not await self._automatic_budget_available(scope):
            raise VisitDecisionError("budget")
        peer_id = await self._prepare_peer(scope, peer_id)
        local = await self.actor_port.describe(scope)
        async with self._lock:
            self._enabled()
            peer = self._peer(scope, peer_id)
            if not peer.get("enabled", True):
                raise ValueError("这位友邻暂未开启联系")
            if self.active(scope):
                raise ValueError("她已经有邀请或串门，先等这次结束")
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
            visit["proposal_stage"] = "pending"
            self._state["visits"][visit_id] = visit
            await self._save()
        result = await self._decide(visit_id, "propose")
        return await self._submit_proposal(visit_id, result)

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
                self._append(visit, self._local_id(visit), "invitation", result["say"])
                visit["proposal_offer"] = {
                    "op": "offer", "role": "host" if visit["role"] == "guest" else "guest",
                    "reason": visit["reason"], "say": result["say"], "max_calls": visit["max_calls"],
                    "turn_limit": visit["turn_limit"], "duration": self.config.duration_minutes * 60,
                    "invitation": copy.deepcopy(visit["log"][0]) if visit["log"] else None}
                visit["proposal_stage"] = "prepared"
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
            self._append(visit, visit["host_id"], "arrival", "双方接受邀请，开始在东道主世界相处")
            scene = visit["scene"]
            self._append(visit, visit["host_id"], "activity", "开始相处：" +
                         "，".join(value for value in (scene.get("place"), scene.get("activity")) if value))
            self._append(visit, visit["guest_id"], "say", intro)
        visit["next_at"] = self._now() + self.config.poll_seconds

    def _synchronize(self, visit, remote, sender, *, allow_terminal=False):
        validate_snapshot(visit, remote, now=self._now())
        if visit["status"] in TERMINAL and not allow_terminal:
            self._clear_decision_wait(visit)
            return
        if allow_terminal and (visit["role"] != "guest" or sender != visit["host_id"]
                               or remote.get("status") not in TERMINAL):
            raise ValueError("结束后的确认只能来自东道主的最终经过")
        if remote.get("status") in {"offered", "inviting"}:
            return  # Their pending acceptance does not turn our proposal into a received invitation.
        if visit["role"] == "host":
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
        if not isinstance(logs, list) or len(logs) > 40:
            raise ValueError("旅程经过过长")
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
        if type(remote_calls) is not int or not 0 <= remote_calls <= self._allocation(visit, sender):
            raise ValueError("东道主调用额度无效")
        own_calls = visit["calls"][self._local_id(visit)]
        for key in ("status", "arrived_at", "scene", "log", "next_actor", "next_at", "turns", "event_stage", "ended_at", "end_reason"):
            if key in remote:
                visit[key] = copy.deepcopy(remote[key])
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
        if visit["turns"][sender] >= visit["turn_limit"]:
            raise ValueError("访客发言已到上限")
        result = clean_decision(payload.get("decision"))
        self._record_turn(visit, sender, result)
        if result["leave"]:
            visit["status"], visit["ended_at"] = "completed", self._now()
            visit["end_reason"] = "访客已告别归家"
            self._clear_decision_wait(visit)

    def _record_turn(self, visit, sender, result):
        self._append(visit, sender, "say", result["say"])
        self._append(visit, sender, "action_plan", result["action"])
        visit["turns"][sender] += 1
        if visit["event_stage"] == "offered":
            visit.setdefault("action_plans", {})[sender] = result["action"] or result["say"]
        visit["next_actor"] = visit["host_id"] if sender == visit["guest_id"] else visit["guest_id"]
        visit["next_at"] = self._now() + self.config.poll_seconds

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
                    continue
                if visit["expires_at"] <= self._now():
                    async with self._lock:
                        await self._finish(visit, "expired", "本次停留到期，保留已确认经过")
                    await self._notify_end(visit_id)
                    await self._observe_memory(visit_id)
                    continue
                if not self._life_enabled(visit["scope"]):
                    async with self._lock:
                        await self._finish(visit, "interrupted", "这个生活已关闭串门或归属发生变化")
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
            await self._automatic()

    async def _advance(self, visit_id):
        visit = self._state["visits"][visit_id]
        if visit["status"] == "offered":
            result = await self._decide(visit_id, "receive")
            async with self._lock:
                if visit["status"] != "offered" or self._closed or not self.config.enabled:
                    return
                if not result and visit.get("model_retry"):
                    return
                if not result or not result["accept"] or result["leave"]:
                    if visit["status"] not in TERMINAL:
                        if result:
                            self._append(visit, self._local_id(visit), result["contact_kind"], result["say"])
                        await self._finish(visit, "declined", result["say"] if result and result["say"] else "现在不方便，稍后再约")
                    return
                visit["intro"] = result["say"]
                if visit["role"] == "host":
                    visit["scene"] = clean_scene(result["scene"])
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
        if visit["role"] == "guest":
            reply = await self._send(visit_id, {"op": "sync", "visit": {**self._wire(visit), "intro": visit.get("intro", "")}})
            async with self._lock:
                if self._closed or visit["status"] in TERMINAL:
                    return
                self._synchronize(visit, reply["visit"], visit["host_id"])
                if reply.get("delivered_op") == "step":
                    visit.pop("unsent_turn", None)
                await self._save()
            if visit["status"] != "active" or visit["next_actor"] != visit["guest_id"] or visit["next_at"] > self._now():
                return
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
                visit["unsent_turn"] = True
                await self._save()
                step = {"op": "step", "seq": len(visit["log"]), "decision":
                        {k: result[k] for k in ("say", "action", "leave")}}
            reply = await self._send(visit_id, step)
            async with self._lock:
                if self._closed or visit["status"] in TERMINAL:
                    return
                self._synchronize(visit, reply["visit"], visit["host_id"])
                visit.pop("unsent_turn", None)
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
        if visit["turns"][visit["host_id"]] >= visit["turn_limit"]:
            async with self._lock:
                await self._finish(visit, "completed", "这次先聊到这里，告别归家")
            return
        result = await self._decide(visit_id, "turn")
        async with self._lock:
            if not result or visit["status"] in TERMINAL or self._closed:
                return
            self._record_turn(visit, visit["host_id"], result)
            if result["leave"] or visit["turns"][visit["host_id"]] >= visit["turn_limit"]:
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

    async def _scene(self, visit_id, stage):
        async with self._lock:
            visit = self._state["visits"][visit_id]
            visit["scene_stage"] = stage
            await self._save()
        result = await self._decide(visit_id, "scene")
        async with self._lock:
            if not result or visit["status"] in TERMINAL or self._closed:
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
