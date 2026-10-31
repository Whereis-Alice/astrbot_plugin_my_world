"""Invitation-first state machine; the host owns scene ordering and settlement."""
from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timezone
from typing import Any

from .protocol import RESERVED, TERMINAL, clean_decision, clean_scene, new_id, public_actor


class VisitFlow:
    def _new_visit(self, scope, peer, local, visit_id, role, reason, automatic):
        own = self._state["instance_id"] + ":" + local["actor_id"]
        host, guest = (own, peer["id"]) if role == "host" else (peer["id"], own)
        return {"id": visit_id, "scope": scope, "role": role, "local": copy.deepcopy(local),
                "remote": copy.deepcopy(peer["actor"]), "reason": str(reason)[:300],
                "status": "inviting", "automatic": automatic, "created_at": self._now(),
                "expires_at": self._now() + self.config.duration_minutes * 60,
                "host_id": host, "guest_id": guest, "scene": {}, "log": [], "private": {},
                "turns": {host: 0, guest: 0}, "calls": {host: 0, guest: 0},
                "max_calls": self.config.max_calls, "turn_limit": self.config.turns_per_actor,
                "next_actor": guest, "next_at": self._now(), "event_stage": "none"}

    async def _decide(self, visit_id, phase):
        async with self._lock:
            visit = self._state["visits"][visit_id]
            if visit["status"] in TERMINAL or self._closed:
                return None
            own = self._local_id(visit)
            if visit["calls"][own] >= self._allocation(visit, own):
                await self._finish(visit, "completed" if visit.get("arrived_at") else "declined", "本次相见的预算已用完")
                return None
            if visit.get("model_pending"):
                return None
            token = new_id()
            visit["model_pending"] = token
            visit["calls"][own] += 1
            snapshot = copy.deepcopy(visit)
            await self._save()  # Admission, including failure, is durable before inference.
        try:
            async with asyncio.timeout(self.config.timeout_seconds):
                result = clean_decision(await self.actor_port.decide(snapshot["scope"], phase, snapshot))
        except asyncio.CancelledError:
            raise
        except Exception:
            async with self._lock:
                visit = self._state["visits"][visit_id]
                if visit.get("model_pending") == token:
                    await self._finish(visit, "interrupted", "这次思考未完成，只保留已确认经过")
            return None
        async with self._lock:
            visit = self._state["visits"][visit_id]
            if visit.get("model_pending") != token or visit["status"] in TERMINAL or self._closed:
                return None
            if visit["expires_at"] <= self._now():
                await self._finish(visit, "expired", "思考期间本次停留到期，旧决定作废")
                return None
            visit.pop("model_pending", None)
            visit["private"] = {key: result[key] for key in ("thought", "reflection", "promise")}
            # Durable output lets a request retry reuse this decision.
            visit["decision"] = result
            await self._save()
        return result

    async def invite(self, scope: str, peer_id: str, reason: str = "", direction: str = "outgoing",
                     automatic: bool = False) -> dict[str, Any]:
        self._enabled()
        await self._load()
        if direction not in {"outgoing", "incoming"}:
            raise ValueError("请选择去做客或邀请来访")
        local = await self.actor_port.describe(scope)
        async with self._lock:
            peer = self._peer(scope, peer_id)
            if self.active(scope):
                raise ValueError("她已经有邀请或串门，先等这次结束")
            if local["persona_key"] != peer["local"]["persona_key"]:
                raise ValueError("人设已变化，请重新配对")
            visit_id = new_id()
            visit = self._new_visit(scope, peer, local, visit_id,
                                    "guest" if direction == "outgoing" else "host", reason, automatic)
            self._state["visits"][visit_id] = visit
            await self._save()
        result = await self._decide(visit_id, "propose")
        async with self._lock:
            visit = self._state["visits"][visit_id]
            if not result or not result["accept"] or result["leave"]:
                if visit["status"] not in TERMINAL:
                    await self._finish(visit, "declined", "现在不方便，稍后再约")
                return self._visible(visit)
            visit["scene"] = clean_scene(result["scene"]) if visit["role"] == "host" else {}
            self._append(visit, self._local_id(visit), "invitation", result["say"])
            await self._save()
            offer = {"op": "offer", "role": "host" if visit["role"] == "guest" else "guest",
                     "reason": visit["reason"], "say": result["say"], "max_calls": visit["max_calls"],
                     "turn_limit": visit["turn_limit"], "duration": self.config.duration_minutes * 60,
                     "invitation": copy.deepcopy(visit["log"][0]) if visit["log"] else None}
        try:
            reply = await self._send(visit_id, offer)
            async with self._lock:
                visit = self._state["visits"][visit_id]
                if reply.get("declined"):
                    await self._finish(visit, "declined", str(reply.get("reason") or "对方暂时不方便"))
                else:
                    offered = reply.get("visit") or {}
                    visit["max_calls"] = min(visit["max_calls"], offered.get("max_calls", visit["max_calls"]))
                    visit["turn_limit"] = min(visit["turn_limit"], offered.get("turn_limit", visit["turn_limit"]))
                    visit["expires_at"] = min(visit["expires_at"], offered.get("expires_at", visit["expires_at"]))
                    visit["next_at"] = self._now() + self.config.poll_seconds
                    await self._save()
        except ValueError:
            async with self._lock:
                self._state["visits"][visit_id]["next_at"] = self._now() + self.config.poll_seconds
                await self._save()
        async with self._lock:
            return self._visible(self._state["visits"][visit_id])

    def _activate(self, visit, intro=""):
        if visit["status"] not in {"inviting", "offered"}:
            return
        visit["status"] = "active"
        visit["arrived_at"] = self._now()
        if visit["role"] == "host":
            self._append(visit, visit["host_id"], "arrival", "双方接受邀请，开始在东道主世界相处")
            scene = visit["scene"]
            self._append(visit, visit["host_id"], "activity", "开始相处：" +
                         "，".join(value for value in (scene.get("place"), scene.get("activity")) if value))
            self._append(visit, visit["guest_id"], "say", intro)
        visit["next_at"] = self._now() + self.config.poll_seconds

    def _synchronize(self, visit, remote, sender):
        if not isinstance(remote, dict) or remote.get("id") != visit["id"]:
            raise ValueError("无效旅程快照")
        for key in ("host_id", "guest_id"):
            if remote.get(key) != visit[key]:
                raise ValueError("旅程参与者不一致")
        if visit["status"] in TERMINAL:
            return
        if remote.get("status") in {"offered", "inviting"}:
            return  # Their pending acceptance does not turn our proposal into a received invitation.
        if visit["role"] == "host":
            if remote.get("status") == "active":
                self._activate(visit, remote.get("intro", ""))
            elif remote.get("status") in TERMINAL:
                visit["status"] = "declined" if not visit.get("arrived_at") else "interrupted"
                visit["ended_at"] = self._now()
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
        own_calls = visit["calls"][self._local_id(visit)]
        for key in ("status", "arrived_at", "scene", "log", "next_actor", "next_at", "turns", "event_stage", "ended_at", "end_reason"):
            if key in remote:
                visit[key] = copy.deepcopy(remote[key])
        remote_calls = remote.get("calls", {}).get(sender, 0)
        if type(remote_calls) is not int or not 0 <= remote_calls <= self._allocation(visit, sender):
            raise ValueError("东道主调用额度无效")
        visit["calls"][sender] = max(visit["calls"][sender], remote_calls)
        visit["calls"][self._local_id(visit)] = own_calls

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
                visit = self._state["visits"].get(visit_id)
                if not visit:
                    continue
                if visit["status"] in TERMINAL:
                    await self._remember(visit_id)
                    continue
                if visit["expires_at"] <= self._now():
                    async with self._lock:
                        await self._finish(visit, "expired", "本次停留到期，保留已确认经过")
                    await self._remember(visit_id)
                    continue
                if max(visit["next_at"], self._touched.get(visit["scope"], 0)) > self._now():
                    continue
                current = await self.actor_port.describe(visit["scope"])
                if current["persona_key"] != visit["local"]["persona_key"] or current["actor_id"] != visit["local"]["actor_id"]:
                    async with self._lock:
                        await self._finish(visit, "interrupted", "生活身份变化，本次联系结束")
                    continue
                try:
                    await self._advance(visit_id)
                except ValueError:
                    async with self._lock:
                        visit["network_failures"] = visit.get("network_failures", 0) + 1
                        visit["next_at"] = self._now() + min(600, 60 * 2 ** visit["network_failures"])
                        if visit["network_failures"] >= 3:
                            await self._finish(visit, "interrupted", "暂时联系不上，只保留已确认经过")
                        else:
                            await self._save()
            await self._automatic()

    async def _advance(self, visit_id):
        visit = self._state["visits"][visit_id]
        if visit["status"] == "offered":
            result = await self._decide(visit_id, "receive")
            async with self._lock:
                if not result or not result["accept"] or result["leave"]:
                    if visit["status"] not in TERMINAL:
                        await self._finish(visit, "declined", result["say"] if result else "稍后再约")
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
            reply = await self._send(visit_id, {"op": "status"})
            async with self._lock:
                if reply.get("declined"):
                    await self._finish(visit, "declined", str(reply.get("reason", "稍后再约")))
                elif "visit" in reply:
                    self._synchronize(visit, reply["visit"], visit["remote"]["id"])
                    await self._save()
            return
        if visit["role"] == "guest":
            reply = await self._send(visit_id, {"op": "sync", "visit": {**self._wire(visit), "intro": visit.get("intro", "")}})
            async with self._lock:
                self._synchronize(visit, reply["visit"], visit["host_id"])
                if reply.get("delivered_op") == "step":
                    visit.pop("unsent_turn", None)
                await self._save()
            if visit["status"] != "active" or visit["next_actor"] != visit["guest_id"] or visit["next_at"] > self._now():
                return
            result = visit.get("decision") if visit.get("unsent_turn") else await self._decide(visit_id, "turn")
            if not result:
                return
            async with self._lock:
                visit["unsent_turn"] = True
                await self._save()
                step = {"op": "step", "seq": len(visit["log"]), "decision":
                        {k: result[k] for k in ("say", "action", "leave")}}
            reply = await self._send(visit_id, step)
            async with self._lock:
                self._synchronize(visit, reply["visit"], visit["host_id"])
                visit.pop("unsent_turn", None)
                await self._save()
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
            if not result:
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
            if not result:
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

    async def _automatic(self):
        if not self.config.auto_enabled or not self.config.weekly_limit:
            return
        for scope in {peer["scope"] for peer in self._state["peers"].values()}:
            if self.active(scope) or self._touched.get(scope, 0) > self._now():
                continue
            local = await self.actor_port.describe(scope)
            if not local["ready"] or local.get("chat_busy") or not local.get("reception_open", True):
                continue
            date = str(local.get("local_date") or datetime.fromtimestamp(self._now(), timezone.utc).date())
            week = datetime.fromisoformat(date).strftime("%G-%V")
            async with self._lock:
                record = self._state["draws"].setdefault(scope, {})
                if record.get("day") == date:
                    continue
                if record.get("week") != week:
                    record.update(week=week, count=0)
                record["day"] = date
                picked = record.get("count", 0) < self.config.weekly_limit and self._random() < self.config.auto_probability
                if picked:
                    record["count"] = record.get("count", 0) + 1
                await self._save()
                peers = [p for p in self._state["peers"].values() if p["scope"] == scope and p["url"]]
                peers.sort(key=lambda peer: max((v["created_at"] for v in self._state["visits"].values()
                    if v["scope"] == scope and v["remote"]["id"] == peer["id"]), default=0))
            if picked and peers:
                try:
                    await self.invite(scope, peers[0]["id"], automatic=True)
                except ValueError:
                    pass
