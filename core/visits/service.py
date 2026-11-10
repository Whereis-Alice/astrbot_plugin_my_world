"""Pairing and authenticated inbox. Character inference stays outside HTTP handlers."""
from __future__ import annotations

import copy
import asyncio
import random
import time
from typing import Any, Callable

from .config import VisitConfig, normalize_url
from .ports import VisitActorPort, describe_local
from .flow import VisitFlow
from .persistence import VisitJournal
from .protocol import (RESERVED, TERMINAL, code_id, derive_key, digest, envelope,
                       new_id, original_envelope, public_actor, renew_delivery, text,
                       validate_actor, validate_outing_request, verify)
from .transport import VisitTransport, VisitTransportError
from .snapshots import validate_snapshot
from .model_retry import clear_state_read


class VisitService(VisitFlow, VisitJournal):
    def __init__(self, db: Any, config: VisitConfig, actor_port: VisitActorPort,
                 clock: Callable[[], float] | None = None, rng: Callable[[], float] | None = None) -> None:
        super().__init__(db, config, actor_port, clock or time.time, rng or random.random)
        self.transport = VisitTransport(self.receive, config)
        self._tick_lock = asyncio.Lock()
        self.error = config.error

    async def start(self) -> None:
        await self._load()
        if not self.config.enabled or self._closed or self.error:
            return
        try:
            await self.transport.start()
        except (OSError, ValueError):
            self.error = "串门端口未能启动，请检查监听地址和端口占用后重载"

    async def close(self) -> None:
        self._closed = True
        try:
            try:
                await self._close_record_card_tasks()
            finally:
                await self._close_memory_tasks()
        finally:
            await self.transport.close()

    def _enabled(self):
        if not self.config.enabled or self._closed or self.error:
            raise ValueError(self.error or "串门尚未开启")

    async def create_pair_code(self, scope: str = "") -> dict[str, Any]:
        if not scope:
            return await self._global_pair_code()
        self._enabled()
        await self._load()
        actor = await self._exchange_description(scope)
        async with self._lock:
            self._enabled()
            code = new_id() + new_id()
            self._state["codes"] = {key: value for key, value in self._state["codes"].items()
                                    if value.get("scope") != scope}
            self._state["codes"][code_id(code)] = {"code": code, "scope": scope, "expires_at": self._now() + 3600}
            await self._save()
            return {"code": code, "expires_at": self._now() + 3600,
                    "url": self.config.public_url, "local": public_actor(actor, self._state["instance_id"])}

    async def pair(self, scope: str, url: str, code: str, alias: str = "") -> dict[str, Any]:
        self._enabled()
        await self._load()
        url = normalize_url(url)
        if not isinstance(code, str) or len(code) != 64 or not all(c in "0123456789abcdef" for c in code):
            raise ValueError("配对码格式无效，请重新生成")
        local = await self._exchange_description(scope)
        self._enabled()
        actor = public_actor(local, self._state["instance_id"])
        request = envelope(code, actor["id"], "pair:" + code_id(code),
                           {"op": "pair", "actor": actor, "url": self.config.public_url}, self._now())
        response = await self.transport.request(url, request)
        payload = verify(response, code, self._now())
        if response["target"] != actor["id"] or payload.get("reply_to") != request["message_id"]:
            raise ValueError("配对答复不属于本次请求")
        remote = validate_actor(payload.get("actor"))
        if remote["id"] == actor["id"] or response["sender"] != remote["id"]:
            raise ValueError("不能把自己配对为友邻")
        async with self._lock:
            self._enabled()
            peer = self._add_peer(scope, local, remote, url, derive_key(code, actor["id"], remote["id"]), alias)
            await self._save()
            return self._safe_peer(peer)

    def _add_peer(self, scope, local, remote, url, secret, alias="", *, register=True):
        peer = {"id": remote["id"], "scope": scope, "local": copy.deepcopy(local),
                "actor": copy.deepcopy(remote), "url": url, "secret": secret, "alias": text(alias, 80),
                "name": remote["name"], "world": remote["world"], "enabled": True,
                "life_generation": self._state.get("life_generations", {}).get(scope, 0)}
        if register:
            self._register_connection(peer, enable_scope=True)
        # Journey identities stay on the wire as actor IDs. Only the local
        # binding index needs a scope suffix when several lives know one actor.
        key = next((key for key, value in self._state["peers"].items()
                    if value["id"] == remote["id"] and value["scope"] == scope), None)
        if key is None:
            key = remote["id"] if remote["id"] not in self._state["peers"] else remote["id"] + "#" + digest(scope)
        self._state["peers"][key] = peer
        return peer

    async def remove_peer(self, scope: str, peer_id: str) -> dict[str, Any]:
        await self._load()
        async with self._lock:
            self._peer(scope, peer_id)
            for visit in list(self._state["visits"].values()):
                if visit["scope"] == scope and visit["remote"]["id"] == peer_id and visit["status"] in RESERVED:
                    await self._finish(visit, "interrupted", "已解除配对")
            key = next(key for key, peer in self._state["peers"].items()
                       if peer["id"] == peer_id and peer["scope"] == scope)
            self._state["peers"].pop(key)
            await self._save()
        return {"ok": True}

    async def receive(self, body: dict[str, Any]) -> dict[str, Any]:
        self._enabled()
        await self._load()
        if not isinstance(body, dict):
            raise ValueError("无效协议消息")
        receipt_key = str(body.get("sender")) + "/" + str(body.get("message_id"))
        async with self._lock:
            self._enabled()
            cached = self._state["receipts"].get(receipt_key)
            if cached:
                return await self._cached_response(body, cached)
        if str(body.get("target", "")).startswith("pair:"):
            if isinstance(body.get("payload"), dict) and body["payload"].get("op") == "pair_world":
                return await self._receive_world_pair(body, receipt_key)
            return await self._receive_pair(body, receipt_key)
        if isinstance(body.get("payload"), dict) and body["payload"].get("op") in {"world_catalog", "world_bind", "world_availability"}:
            return await self._receive_control(body, receipt_key)
        peer = self._wire_peer(body.get("sender"), body.get("target"))
        if not peer or not self._peer_trusted(peer):
            raise ValueError("尚未配对的世界")
        peer = copy.deepcopy(peer)
        payload = verify(body, peer["secret"], self._now())
        if not self._exchange_allowed(peer, payload):
            raise ValueError("这个生活已关闭串门或绑定已变化")
        local = await self._exchange_description(peer["scope"])
        actor = public_actor(local, self._state["instance_id"])
        if body["target"] != actor["id"] or local["persona_key"] != peer["local"]["persona_key"]:
            raise ValueError("生活身份已变化，请稍后重新联系")
        async with self._lock:
            # A concurrent duplicate may have entered while describe was awaiting.
            self._enabled()
            if receipt_key in self._state["receipts"]:
                return await self._cached_response(body, self._state["receipts"][receipt_key])
            current_peer = self._find_peer(peer["scope"], body["sender"])
            if (not current_peer or current_peer["scope"] != peer["scope"]
                    or current_peer["secret"] != peer["secret"] or current_peer["local"] != peer["local"]
                    or not self._peer_trusted(current_peer) or not self._exchange_allowed(current_peer, payload)):
                raise ValueError("友邻连接已变化，请稍后重新联系")
            current_local = await self._exchange_description(peer["scope"])
            self._enabled()
            if current_local["actor_id"] != local["actor_id"] or current_local["persona_key"] != local["persona_key"]:
                raise ValueError("生活身份已变化，旧请求作废")
            result = await self._inbox(peer, current_local, payload)
            return await self._receipt(body, result, peer["secret"], actor["id"], receipt_key)

    async def _cached_response(self, body, cached):
        verify(body, cached["key"], self._now(), fresh=False)
        if (digest(body["payload"]) != cached["digest"]
                or (cached.get("request_digest") and digest(original_envelope(body)) != cached["request_digest"])):
            raise ValueError("重复编号的消息内容发生变化")
        context = await self._receipt_context(body, cached["key"], cached["response"])
        if cached.get("context") and context != cached["context"]:
            raise ValueError("生活身份或配对已变化，旧请求作废")
        self._enabled()
        return copy.deepcopy(cached["response"])

    def _settlement_exchange(self, peer, payload):
        visit = self._state["visits"].get(payload.get("id"))
        return bool(visit and visit["status"] in TERMINAL and self._visit_belongs(visit, peer)
                    and payload.get("op") in {"end", "status", "sync"})

    def _exchange_allowed(self, peer, payload):
        return (self._canonical_life(peer["scope"]) == peer["scope"]
                and ((self._life_enabled(peer["scope"]) and self._binding_current(peer)
                      # A legacy pairing establishes trust only. Actual offers
                      # must upgrade that trust to a verified conversation bind.
                      and (payload.get("op") == "pair" or self._routing_existing_exchange(peer, payload)
                           or self._routing_binding_current(peer)))
                     or self._settlement_exchange(peer, payload)))

    async def _receipt_context(self, request, key, response, *, validate_identity=True):
        """A receipt remains reusable only while its authenticated identity holds."""
        op = request["payload"].get("op")
        peer = None
        catalog = False
        if op == "pair_world":
            conn = self._state["connections"].get(request["sender"])
            expected = derive_key(key, "world:" + self._state["instance_id"], "world:" + request["sender"])
            if not conn or not conn.get("enabled", True) or not any(a["secret"] == expected for a in conn["anchors"]):
                raise ValueError("全局配对已变化，旧配对请求作废")
            context = [conn["id"], expected]
            catalog = True
        elif op in {"world_catalog", "world_bind", "world_availability"}:
            conn, anchor = self._anchor_for(request)
            if anchor["secret"] != key:
                raise ValueError("全局配对已变化，旧管理请求作废")
            context = [conn["id"], digest(anchor)]
            catalog = op == "world_catalog"
            if op == "world_bind":
                peer = self._wire_peer(request["payload"]["actor"]["id"], response["payload"]["actor"]["id"])
                if not peer or peer.get("anchor_digest") != digest(anchor):
                    raise ValueError("角色绑定已变化，旧请求作废")
            elif op == "world_availability":
                target = request["payload"].get("target_actor")
                selected = None
                for scope in self._participating():
                    local = await self._exchange_description(scope)
                    if public_actor(local, self._state["instance_id"])["id"] == target:
                        selected = (scope, local)
                        break
                if selected is None:
                    raise ValueError("这份友邻生活已停止接待，旧额度答复作废")
                scope, local = selected
                status = (await self._local_automatic_availability(scope, local) if validate_identity
                          else response["payload"]["availability"])
                context.append([target, scope, local["persona_key"],
                                self._state.get("life_generations", {}).get(scope, 0),
                                {field: status.get(field) for field in ("allowed", "day", "reset_at", "used", "limit")}])
        else:
            target = response["sender"] if op == "pair" else request["target"]
            peer = self._wire_peer(request["sender"], target)
            expected = derive_key(key, request["sender"], target) if op == "pair" else key
            if not peer or peer["secret"] != expected:
                raise ValueError("角色配对已变化，旧请求作废")
            context = []
        if peer is not None:
            if not self._peer_trusted(peer) or not self._exchange_allowed(peer, request["payload"]):
                raise ValueError("生活已关闭串门或配对已变化，旧请求作废")
            local = await self._exchange_description(peer["scope"]) if validate_identity else peer["local"]
            if local["actor_id"] != peer["local"]["actor_id"] or local["persona_key"] != peer["local"]["persona_key"]:
                raise ValueError("生活身份已变化，旧请求作废")
            context.append([peer["scope"], local["actor_id"], local["persona_key"], peer["secret"],
                            peer.get("life_generation", 0)])
        if catalog:
            context.append((await self._catalog_snapshot())[1])
        return digest(context)

    async def _receive_pair(self, body, receipt_key):
        async with self._lock:
            item = self._state["codes"].get(body["target"][5:])
            if not item or item.get("kind") == "world" or not item.get("scope") or item["expires_at"] <= self._now():
                raise ValueError("配对码已过期或已经使用")
            code, scope = item["code"], item["scope"]
        payload = verify(body, code, self._now())
        remote = validate_actor(payload.get("actor"))
        if payload.get("op") != "pair" or remote["id"] != body["sender"]:
            raise ValueError("配对身份不一致")
        url = normalize_url(payload["url"]) if payload.get("url") else ""
        local = await self._exchange_description(scope)
        actor = public_actor(local, self._state["instance_id"])
        if actor["id"] == remote["id"]:
            raise ValueError("不能配对自己")
        async with self._lock:
            self._enabled()
            if receipt_key in self._state["receipts"]:
                return await self._cached_response(body, self._state["receipts"][receipt_key])
            if self._state["codes"].get(body["target"][5:]) != item:
                raise ValueError("配对码已经使用")
            self._add_peer(scope, local, remote, url, derive_key(code, actor["id"], remote["id"]))
            self._state["codes"].pop(body["target"][5:])
            return await self._receipt(body, {"actor": actor}, code, actor["id"], receipt_key)

    async def _receipt(self, request, payload, key, sender, receipt_key):
        result = envelope(key, sender, request["sender"],
                          {**payload, "reply_to": request["message_id"]}, self._now())
        self._state["receipts"][receipt_key] = {"key": key, "at": self._now(),
            "digest": digest(request["payload"]), "request_digest": digest(original_envelope(request)),
            "context": await self._receipt_context(request, key, result, validate_identity=False), "response": result}
        await self._save()
        return result

    async def _inbox(self, peer, local, payload):
        op, visit_id = payload.get("op"), payload.get("id")
        if not isinstance(op, str) or op not in {"offer", "end", "step", "sync", "status"}:
            raise ValueError("未知旅程消息")
        if not isinstance(visit_id, str) or len(visit_id) != 32:
            raise ValueError("无效旅程编号")
        if op == "offer":
            visit = self._state["visits"].get(visit_id)
            if visit:
                if not self._visit_belongs(visit, peer):
                    raise ValueError("旅程身份冲突")
                return {"visit": self._wire(visit)}
            role = payload.get("role")
            if role not in {"host", "guest"}:
                raise ValueError("无效到访方向")
            manual = payload.get("manual", False)
            if type(manual) is not bool:
                raise ValueError("人工邀请标记无效")
            requested_version = payload.get("outing_version", 0)
            if type(requested_version) is not int or requested_version not in {0, 1}:
                raise ValueError("共同游玩版本无效")
            outing_version = 1 if getattr(self.config, "outings_enabled", False) and requested_version == 1 else 0
            if self._settlement_backlog_full(peer["scope"]):
                reason = "我想先把前面的几件事收好，稍后再约"
                return {"declined": True, "reason": reason,
                        "contact": {"kind": "contact_status", "text": reason}}
            if not manual and not local["ready"] and not local.get("readiness_wait"):
                reason = local.get("ready_reason") or "现在不方便，稍后再约"
                return {"declined": True, "reason": reason,
                        "contact": {"kind": "contact_status", "text": reason}}
            if not manual and getattr(self.config, "outings_enabled", False):
                availability = await self._local_automatic_availability(peer["scope"], local)
                if not availability["allowed"]:
                    reason = "接待方今日自主串门预算已用完"
                    return {"declined": True, "reason": reason,
                            "budget": {"side": "receiver", "actor_id": self._state["instance_id"] + ":" + local["actor_id"],
                                       **availability},
                            "contact": {"kind": "contact_status", "text": reason}}
            visit = self._new_visit(peer["scope"], peer, local, visit_id, role, payload.get("reason", ""), False)
            visit["manual"], visit["outing_version"] = manual, outing_version
            for name, upper in (("max_calls", self.config.max_calls), ("turn_limit", self.config.turns_per_actor)):
                value = payload.get(name)
                if type(value) is not int or not (2 if name == "max_calls" else 1) <= value <= 20:
                    raise ValueError("旅程预算不兼容")
                visit[name] = min(value, upper)
            calls = payload.get("calls", 1)  # Older offers carried one successful proposal implicitly.
            if type(calls) is not int or calls < 0 or not (manual or outing_version) and calls > 20:
                raise ValueError("邀请调用额度无效")
            if not (manual or outing_version) and calls > self._allocation(visit, peer["id"]):
                reason = "这次邀请已用的模型额度超过双方可用预算，稍后再约"
                return {"declined": True, "reason": reason,
                        "contact": {"kind": "contact_status", "text": reason}}
            duration = payload.get("duration")
            if type(duration) is not int or not 60 <= duration <= (14400 if outing_version else 3600):
                raise ValueError("无效停留期限")
            limit = self.config.outing_duration_minutes if outing_version else self.config.duration_minutes
            visit["expires_at"] = self._now() + min(duration, limit * 60)
            visit["status"] = "offered"
            visit["contacted"] = True
            visit["calls"][peer["id"]] = calls
            invitation = payload.get("invitation")
            if invitation is not None:
                if (not isinstance(invitation, dict) or type(invitation.get("seq")) is not int or invitation["seq"] != 1
                        or invitation.get("kind") != "invitation" or invitation.get("actor_id") != peer["id"]
                        or not isinstance(invitation.get("text"), str) or len(invitation["text"]) > 500):
                    raise ValueError("邀请来源无效")
                visit["log"] = [copy.deepcopy(invitation)]
            current = self.active(peer["scope"])
            if current:
                # Crossing proposals must settle on the same invitation on both
                # servers. Random IDs make the choice fair without a third arbiter.
                crossing = (current["status"] == "inviting" and not current.get("arrived_at")
                            and current["remote"]["id"] == peer["id"])
                if not crossing:
                    reason = ("我现在正和友邻相处，稍后再约" if current.get("arrived_at") else
                              "我还有一通联系在等回应，稍后再约")
                    return {"declined": True, "reason": reason,
                            "contact": {"kind": "contact_status", "text": reason}}
                if visit_id >= current["id"]:
                    return {"declined": True, "reason": "双方同时提出邀请，接续另一条邀请"}
                await self._finish(self._state["visits"][current["id"]], "declined",
                                   "双方同时提出邀请，接续对方的邀请", notify=False)
            if self._routing_required():
                # Descriptions, allowance reads and crossing-proposal cleanup
                # can yield after receive's first verification. A new offer
                # must still belong to this conversation when it is created.
                current_peer = self._find_peer(peer["scope"], peer["id"])
                if (not current_peer or current_peer["secret"] != peer["secret"]
                        or current_peer["local"] != peer["local"]
                        or not self._peer_trusted(current_peer)
                        or not self._exchange_allowed(current_peer, payload)
                        or current_peer["local"]["actor_id"] != local["actor_id"]
                        or current_peer["local"]["persona_key"] != local["persona_key"]):
                    raise ValueError("接待期间会话归属或绑定已变化，旧邀请作废")
            self._state["visits"][visit_id] = visit
            return {"visit": self._wire(visit)}
        visit = self._state["visits"].get(visit_id)
        if not visit or not self._visit_belongs(visit, peer):
            raise ValueError("找不到本友邻的旅程")
        if op == "end":
            previous = (copy.deepcopy(visit["log"]), visit["status"], visit.get("end_reason"))
            candidate = copy.deepcopy(visit)
            manual_continuation = payload.get("manual_continuation", False)
            if type(manual_continuation) is not bool:
                raise ValueError("人工延续标记无效")
            if manual_continuation:
                self._resume_manual_activity(candidate)
            remote = payload.get("visit")
            if remote is not None:
                validate_snapshot(candidate, remote, now=self._now())
            if candidate["role"] == "guest" and remote is not None:
                if remote.get("status") not in TERMINAL:
                    raise ValueError("对方尚未确认结束本次相见")
                self._synchronize(candidate, remote, peer["id"], allow_terminal=True)
            elif remote is not None and not candidate.get("arrived_at") and remote.get("status") == "declined":
                self._synchronize(candidate, remote, peer["id"])
            visit.clear()
            visit.update(candidate)
            if visit["status"] not in TERMINAL:
                status = "completed" if (visit.get("arrived_at") and isinstance(remote, dict)
                                          and remote.get("status") == "completed") else "interrupted"
                reason = (remote.get("end_reason") if isinstance(remote, dict)
                          and remote.get("status") in TERMINAL else "")
                natural = status == "completed" and visit.get("outing_version") == 1
                await self._finish(visit, status, reason or ("" if natural else "对方已告别或结束本次联系"), notify=False)
            if (visit["log"], visit["status"], visit.get("end_reason")) != previous:
                self._invalidate_memory(visit)
            visit.pop("end_pending", None)
            self._clear_decision_wait(visit)
            visit.pop("outbox", None)
            visit.pop("unsent_turn", None)
            self._record_remote_budget_hold(peer, visit)
            return {"visit": self._wire(visit)}
        if visit["status"] in TERMINAL:
            return {"visit": self._wire(visit)}
        if await self._complete_outing_if_due(visit):
            return {"visit": self._wire(visit)}
        if visit["expires_at"] <= self._now():
            outing = visit.get("outing_version") == 1 and visit.get("arrived_at")
            await self._finish(visit, "interrupted" if outing else "expired",
                               "这次相处的联系暂未续上，保留已确认经过" if outing else "本次停留已经到期")
            return {"visit": self._wire(visit)}
        manual_continuation = payload.get("manual_continuation", False)
        if type(manual_continuation) is not bool:
            raise ValueError("人工延续标记无效")
        calls = payload.get("calls", 0)
        if (type(calls) is not int or calls < 0 or not (
                visit.get("manual") or visit.get("outing_version") or manual_continuation)
                and calls > self._allocation(visit, peer["id"])):
            raise ValueError("旅程调用额度无效")
        candidate = copy.deepcopy(visit)
        if manual_continuation:
            local_request_pending = (candidate.get("manual_continuation")
                                     and self._local_id(candidate) in candidate.get("outing_requests", {}))
            self._resume_manual_activity(candidate)
            if local_request_pending:
                candidate["manual_continuation"] = True
        self._accept_outing_request(candidate, peer["id"], payload)
        superseded = False
        if op == "step":
            superseded = self._scene_superseded_step(candidate, peer["id"], payload)
            if not superseded:
                self._receive_step(candidate, peer["id"], payload)
        elif op == "sync":
            self._synchronize(candidate, payload.get("visit"), peer["id"])
        elif op != "status":
            raise ValueError("未知旅程消息")
        candidate["calls"][peer["id"]] = max(candidate["calls"][peer["id"]], calls)
        self._record_remote_budget_hold(peer, candidate)
        visit.clear()
        visit.update(candidate)
        return {"visit": self._wire(visit),
                **({"step_superseded": True, "step_seq": payload["seq"]} if superseded else {})}

    def _accept_outing_request(self, visit, sender, payload):
        request = payload.get("human_request")
        if request is None:
            return
        if not (visit.get("manual") and visit.get("outing_version") == 1 and visit.get("arrived_at")):
            raise ValueError("真人游玩请求缺少已到访的人工相处证明")
        request = validate_outing_request(request, now=self._now())
        requests = visit.setdefault("outing_requests", {})
        prior = requests.get(sender)
        if prior and request["seq"] < prior["seq"]:
            return  # A delayed signed status cannot replace the sender's newer request.
        if prior and request["seq"] == prior["seq"]:
            if prior != request:
                raise ValueError("已经确认的真人游玩请求不能被改写")
            return
        requests[sender] = request
        visit["next_at"] = self._outing_wait_at(visit)

    def _record_remote_budget_hold(self, peer, snapshot):
        """A received checkpoint protects every local life from redundant proposals."""
        hold = snapshot.get("budget_holds", {}).get(peer["id"])
        if not hold or hold.get("reset_at", 0) <= self._now():
            return
        connection = self._state["connections"].get(peer.get("connection_id"))
        if not connection:
            return
        previous = connection.get("availability", {}).get(peer["id"], {})
        availability = self._validate_automatic_availability({
            **{key: hold.get(key, previous.get(key, "" if key == "day" else 0)) for key in ("day", "used", "limit")},
            "allowed": False, "reset_at": hold["reset_at"], "reason": hold.get("reason", "")})
        connection.setdefault("availability", {})[peer["id"]] = availability

    def _resume_manual_activity(self, visit):
        """An authenticated human continuation releases this trip's inference hold."""
        visit["manual"] = True
        for key in ("manual_continuation", "budget_holds", "outing_budget_hold", "budget_notice_pending"):
            visit.pop(key, None)
        visit["next_at"] = self._outing_wait_at(visit)

    def _visit_belongs(self, visit, peer):
        return (visit["scope"] == peer["scope"] and visit["remote"]["id"] == peer["id"]
                and visit["local"]["actor_id"] == peer["local"]["actor_id"]
                and visit["local"]["persona_key"] == peer["local"]["persona_key"])

    async def _exchange_description(self, scope):
        return await describe_local(self.actor_port, scope, timeout_seconds=self.config.timeout_seconds)

    async def _send(self, visit_id, payload):
        async with self._lock:
            self._enabled()
            visit = self._state["visits"][visit_id]
            if visit["status"] in TERMINAL and payload.get("op") != "end":
                raise ValueError("本次旅程已结束，旧动作不再发送")
            peer = self._find_peer(visit["scope"], visit["remote"]["id"])
            if not peer or not self._peer_trusted(peer) or not self._visit_belongs(visit, peer) \
                    or not self._exchange_allowed(peer, {**payload, "id": visit_id}):
                raise ValueError("生活或配对已变化，旧联系作废")
            local = await self._exchange_description(visit["scope"])
            self._enabled()  # Closing can finish while the local read yields.
            if local["actor_id"] != visit["local"]["actor_id"] or local["persona_key"] != visit["local"]["persona_key"]:
                raise ValueError("生活身份已变化，旧联系作废")
            if payload.get("op") != "end" and await self._complete_outing_if_due(visit):
                raise ValueError("双方已按约定告别归家，旧动作不再发送")
            if payload.get("op") != "end" and visit["expires_at"] <= self._now():
                outing = visit.get("outing_version") == 1 and visit.get("arrived_at")
                await self._finish(visit, "interrupted" if outing else "expired",
                                   "这次相处的联系暂未续上，保留已确认经过" if outing else "本次停留已经到期，旧联系作废")
                raise ValueError("本次停留已经到期，旧动作不再发送")
            if not peer["url"]:
                raise ValueError("对方未提供可访问的串门地址，请补填对外地址后重新配对")
            if visit.get("manual_continuation") is True:
                payload = {**payload, "manual_continuation": True}
                if isinstance(payload.get("visit"), dict):
                    payload["visit"] = {**payload["visit"], "manual": True}
            local_request = visit.get("outing_requests", {}).get(self._local_id(visit))
            if local_request and payload.get("op") != "end":
                payload = {**payload, "human_request": copy.deepcopy(local_request)}
            payload = {**payload, "id": visit_id, "calls": visit["calls"][self._local_id(visit)]}
            outgoing = visit.get("outbox")
            if not outgoing:
                outgoing = envelope(peer["secret"], self._local_id(visit), peer["id"], payload, self._now())
                visit["outbox"] = outgoing
                visit["contacted"] = True
                await self._save()
            self._enabled()  # Keep a durable pre-close wire, but do not send it now.
            if not self._exchange_allowed(peer, outgoing["payload"]):
                raise ValueError("发送期间会话归属已变化，旧联系作废")
            peer = copy.deepcopy(peer)
        try:
            response = await self.transport.request(peer["url"], outgoing)
        except VisitTransportError as error:
            if error.http_status != 400 or self._now() - outgoing["created_at"] <= 300:
                raise
            # First replay the exact durable packet: an old receiver may already
            # have its receipt. Only a stale rejection needs fresh delivery proof.
            # Keep the original ID, contents and signature for every retry, so a
            # lost reply or a restart cannot execute a turn twice.
            async with self._lock:
                self._enabled()
                visit = self._state["visits"].get(visit_id)
                current_peer = self._find_peer(visit["scope"], peer["id"]) if visit else None
                if (not visit or not current_peer or current_peer["secret"] != peer["secret"]
                        or not self._peer_trusted(current_peer) or not self._visit_belongs(visit, current_peer)
                        or not self._exchange_allowed(current_peer, outgoing["payload"])
                        or (visit.get("outbox") or {}).get("message_id") != outgoing["message_id"]
                        or visit["status"] in TERMINAL and outgoing["payload"]["op"] != "end"):
                    raise ValueError("生活或旅程已变化，旧联系不能续证") from error
                local = await self._exchange_description(visit["scope"])
                self._enabled()
                if local["actor_id"] != visit["local"]["actor_id"] or local["persona_key"] != visit["local"]["persona_key"]:
                    raise ValueError("生活身份已变化，旧联系不能续证") from error
                if outgoing["payload"]["op"] != "end":
                    if await self._complete_outing_if_due(visit) or visit["expires_at"] <= self._now():
                        raise ValueError("本次停留已经结束，旧联系不能续证") from error
                if not self._exchange_allowed(current_peer, outgoing["payload"]):
                    raise ValueError("续证期间会话归属已变化，旧联系作废") from error
                renewed = renew_delivery(outgoing, peer["secret"], self._now())
            response = await self.transport.request(peer["url"], renewed)
        result = verify(response, peer["secret"], self._now(), fresh=False)
        if response["sender"] != peer["id"] or response["target"] != outgoing["sender"] or result.get("reply_to") != outgoing["message_id"]:
            raise ValueError("串门答复不属于本次联系")
        async with self._lock:
            visit = self._state["visits"].get(visit_id)
            current_peer = self._find_peer(visit["scope"], peer["id"]) if visit else None
            if (self._closed or not visit or not current_peer or current_peer["secret"] != peer["secret"]
                    or not self._peer_trusted(current_peer) or not self._exchange_allowed(current_peer, outgoing["payload"])
                    or not self._visit_belongs(visit, current_peer)
                    or (visit["status"] in TERMINAL and outgoing["payload"]["op"] != "end")):
                raise ValueError("世界或旅程已结束，旧答复作废")
            local = await self._exchange_description(visit["scope"])
            self._enabled()  # A received reply cannot mutate a closed instance.
            if local["actor_id"] != visit["local"]["actor_id"] or local["persona_key"] != visit["local"]["persona_key"]:
                raise ValueError("生活身份已变化，旧答复作废")
            if outgoing["payload"]["op"] != "end" and await self._complete_outing_if_due(visit):
                raise ValueError("双方已按约定告别归家，旧答复不再使用")
            if outgoing["payload"]["op"] != "end" and visit["expires_at"] <= self._now():
                outing = visit.get("outing_version") == 1 and visit.get("arrived_at")
                await self._finish(visit, "interrupted" if outing else "expired",
                                   "这次相处的联系暂未续上，保留已确认经过" if outing else "本次停留已经到期，旧答复作废")
                raise ValueError("本次停留已经到期，旧答复作废")
            if (visit.get("outbox") or {}).get("message_id") != outgoing["message_id"]:
                raise ValueError("本次联系已被更新，旧答复作废")
            self._enabled()
            if not self._exchange_allowed(current_peer, outgoing["payload"]):
                raise ValueError("答复期间会话归属已变化，旧答复作废")
            candidate = copy.deepcopy(visit)
            remote = result.get("visit")
            if remote is None and not (outgoing["payload"]["op"] == "offer"
                                       and result.get("declined") is True):
                raise ValueError("对方未提供本次旅程答复")
            if remote is not None and not isinstance(remote, dict):
                raise ValueError("对方返回无效旅程快照")
            if (candidate.get("manual_continuation") is True
                    and outgoing["payload"].get("manual_continuation") is not True
                    and (remote or {}).get("manual") is True and not candidate.get("manual")):
                # Both sides can receive a human continuation while an older
                # packet is in flight. Renew its signed status before new work.
                visit.pop("outbox", None)
                await self._save()
                raise ValueError("正在重新确认双方的人工接续，请稍后继续")
            if outgoing["payload"].get("manual_continuation") is True:
                self._resume_manual_activity(candidate)
                current_request = visit.get("outing_requests", {}).get(self._local_id(visit))
                remote_requests = (remote or {}).get("outing_requests")
                received_request = (remote_requests.get(self._local_id(visit))
                                    if isinstance(remote_requests, dict) else None)
                if (visit.get("manual_continuation") and current_request
                        and received_request != current_request):
                    candidate["manual_continuation"] = True
            for name in ("max_calls", "turn_limit"):
                value = (remote or {}).get(name)
                if value is not None:
                    if type(value) is not int or not (2 if name == "max_calls" else 1) <= value <= visit[name]:
                        raise ValueError("对方返回不兼容的旅程预算")
                    candidate[name] = value
            if not (candidate.get("manual") or candidate.get("outing_version")) and any(
                    count > self._allocation(candidate, actor) for actor, count in candidate["calls"].items()):
                raise ValueError("对方的旅程预算不足以容纳已发出的调用")
            if remote is not None:
                validate_snapshot(candidate, remote, now=self._now())
                self._validate_superseded_step_reply(candidate, result, outgoing)
                self._record_remote_budget_hold(peer, remote)
            if result.get("declined") is True and outgoing["payload"]["op"] == "offer":
                if result.get("budget") is not None:
                    remote_budget = result["budget"]
                    if not isinstance(remote_budget, dict) or remote_budget.get("actor_id") != peer["id"]:
                        raise ValueError("对方的自动联系额度不属于这份生活")
                    availability = self._validate_automatic_availability(remote_budget)
                    candidate["budget_side"] = "remote"
                    candidate["automatic_budget"] = availability
                    conn = self._state["connections"].get(peer.get("connection_id"))
                    if conn:
                        conn.setdefault("availability", {})[peer["id"]] = availability
                contact = result.get("contact")
                if contact is None:  # Older neighbours return only their availability reason.
                    contact = {"kind": "contact_status", "text": result.get("reason") or "对方暂时不方便"}
                if (not isinstance(contact, dict) or contact.get("kind") not in {"contact_reply", "contact_status"}
                        or not isinstance(contact.get("text"), str) or len(contact["text"]) > 500):
                    raise ValueError("对方返回无效联系答复")
                self._append(candidate, peer["id"], contact["kind"], contact["text"])
            expires = (remote or {}).get("expires_at")
            if isinstance(expires, (int, float)):
                candidate["expires_at"] = (expires if candidate.get("outing_version") == 1 and candidate.get("arrived_at")
                                           else min(visit["expires_at"], expires))
            candidate.pop("outbox", None)
            clear_state_read(candidate, checkpoint="exchange")
            candidate["network_failures"] = 0
            candidate.pop("contact_error", None)
            visit.clear()
            visit.update(candidate)
            await self._save()
        result["delivered_op"] = outgoing["payload"]["op"]
        sent_request = outgoing["payload"].get("human_request")
        received_request = (result.get("visit") or {}).get("outing_requests", {}).get(outgoing["sender"])
        result["delivered_request"] = sent_request["id"] if sent_request and received_request == sent_request else None
        return result

    async def return_home(self, scope: str) -> dict[str, Any]:
        await self._load()
        async with self._lock:
            visit = self.active(scope)
            if not visit:
                return {"ok": True, "status": "completed"}
            current = self._state["visits"][visit["id"]]
            await self._finish(current, "interrupted", "真人要求告别归家")
        await self._notify_end(visit["id"])
        await self._observe_memory(visit["id"])
        await self._observe_record_card(visit["id"])
        return {"ok": True, "status": "interrupted"}
