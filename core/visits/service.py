"""Pairing and authenticated inbox. Character inference stays outside HTTP handlers."""
from __future__ import annotations

import copy
import asyncio
import random
import time
from typing import Any, Callable

from .config import VisitConfig, normalize_url
from .ports import VisitActorPort
from .flow import VisitFlow
from .persistence import VisitJournal
from .protocol import (RESERVED, TERMINAL, code_id, derive_key, digest, envelope,
                       new_id, public_actor, text, validate_actor, verify)
from .transport import VisitTransport
from .snapshots import validate_snapshot


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
        await self.transport.close()

    def _enabled(self):
        if not self.config.enabled or self._closed or self.error:
            raise ValueError(self.error or "串门尚未开启")

    async def create_pair_code(self, scope: str) -> dict[str, Any]:
        self._enabled()
        await self._load()
        actor = await self.actor_port.describe(scope)
        async with self._lock:
            self._enabled()
            code = new_id() + new_id()
            self._state["codes"] = {key: value for key, value in self._state["codes"].items()
                                    if value["scope"] != scope}
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
        local = await self.actor_port.describe(scope)
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

    def _add_peer(self, scope, local, remote, url, secret, alias=""):
        if len(self._state["peers"]) >= self.config.max_peers and remote["id"] not in self._state["peers"]:
            raise ValueError("友邻数量已到上限")
        existing = self._state["peers"].get(remote["id"])
        if existing and existing["scope"] != scope:
            raise ValueError("这个友邻已配对本机另一位角色，请先解除原配对")
        peer = {"id": remote["id"], "scope": scope, "local": copy.deepcopy(local),
                "actor": remote, "url": url, "secret": secret, "alias": text(alias, 80),
                "name": remote["name"], "world": remote["world"], "enabled": True}
        self._state["peers"][peer["id"]] = peer
        return peer

    async def remove_peer(self, scope: str, peer_id: str) -> dict[str, Any]:
        await self._load()
        async with self._lock:
            self._peer(scope, peer_id)
            for visit in list(self._state["visits"].values()):
                if visit["scope"] == scope and visit["remote"]["id"] == peer_id and visit["status"] in RESERVED:
                    await self._finish(visit, "interrupted", "已解除配对")
            self._state["peers"].pop(peer_id)
            await self._save()
        return {"ok": True}

    async def receive(self, body: dict[str, Any]) -> dict[str, Any]:
        self._enabled()
        await self._load()
        if not isinstance(body, dict):
            raise ValueError("无效协议消息")
        receipt_key = str(body.get("sender")) + "/" + str(body.get("message_id"))
        async with self._lock:
            cached = self._state["receipts"].get(receipt_key)
            if cached:
                return self._cached_response(body, cached)
        if str(body.get("target", "")).startswith("pair:"):
            return await self._receive_pair(body, receipt_key)
        peer = self._state["peers"].get(body.get("sender"))
        if not peer:
            raise ValueError("尚未配对的世界")
        payload = verify(body, peer["secret"], self._now())
        local = await self.actor_port.describe(peer["scope"])
        actor = public_actor(local, self._state["instance_id"])
        if body["target"] != actor["id"] or local["persona_key"] != peer["local"]["persona_key"]:
            raise ValueError("生活身份已变化，请重新配对")
        async with self._lock:
            # A concurrent duplicate may have entered while describe was awaiting.
            self._enabled()
            if receipt_key in self._state["receipts"]:
                return self._cached_response(body, self._state["receipts"][receipt_key])
            current_peer = self._state["peers"].get(body["sender"])
            if (not current_peer or current_peer["scope"] != peer["scope"]
                    or current_peer["secret"] != peer["secret"]):
                raise ValueError("友邻配对已变化，请重新联系")
            result = await self._inbox(peer, local, payload)
            return await self._receipt(body, result, peer["secret"], actor["id"], receipt_key)

    def _cached_response(self, body, cached):
        verify(body, cached["key"], self._now(), fresh=False)
        if digest(body["payload"]) != cached["digest"]:
            raise ValueError("重复编号的消息内容发生变化")
        return copy.deepcopy(cached["response"])

    async def _receive_pair(self, body, receipt_key):
        async with self._lock:
            item = self._state["codes"].get(body["target"][5:])
            if not item or item["expires_at"] <= self._now():
                raise ValueError("配对码已过期或已经使用")
            code, scope = item["code"], item["scope"]
        payload = verify(body, code, self._now())
        remote = validate_actor(payload.get("actor"))
        if payload.get("op") != "pair" or remote["id"] != body["sender"]:
            raise ValueError("配对身份不一致")
        url = normalize_url(payload["url"]) if payload.get("url") else ""
        local = await self.actor_port.describe(scope)
        actor = public_actor(local, self._state["instance_id"])
        if actor["id"] == remote["id"]:
            raise ValueError("不能配对自己")
        async with self._lock:
            self._enabled()
            if receipt_key in self._state["receipts"]:
                return self._cached_response(body, self._state["receipts"][receipt_key])
            if body["target"][5:] not in self._state["codes"]:
                raise ValueError("配对码已经使用")
            self._add_peer(scope, local, remote, url, derive_key(code, actor["id"], remote["id"]))
            self._state["codes"].pop(body["target"][5:])
            return await self._receipt(body, {"actor": actor}, code, actor["id"], receipt_key)

    async def _receipt(self, request, payload, key, sender, receipt_key):
        result = envelope(key, sender, request["sender"],
                          {**payload, "reply_to": request["message_id"]}, self._now())
        self._state["receipts"][receipt_key] = {"key": key, "at": self._now(),
            "digest": digest(request["payload"]), "response": result}
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
                if visit["remote"]["id"] != peer["id"]:
                    raise ValueError("旅程身份冲突")
                return {"visit": self._wire(visit)}
            role = payload.get("role")
            if role not in {"host", "guest"}:
                raise ValueError("无效到访方向")
            if self._settlement_backlog_full(peer["scope"]):
                return {"declined": True, "reason": "我想先把前面的几件事收好，稍后再约"}
            if not local["ready"] or local.get("chat_busy") or not local.get("reception_open", True):
                reason = "现在不在接待时间，稍后再约" if not local.get("reception_open", True) else local.get("ready_reason") or "眼前还有交谈，稍后再约"
                return {"declined": True, "reason": reason}
            visit = self._new_visit(peer["scope"], peer, local, visit_id, role, payload.get("reason", ""), False)
            for name, upper in (("max_calls", self.config.max_calls), ("turn_limit", self.config.turns_per_actor)):
                value = payload.get(name)
                if type(value) is not int or not (2 if name == "max_calls" else 1) <= value <= 20:
                    raise ValueError("旅程预算不兼容")
                visit[name] = min(value, upper)
            duration = payload.get("duration")
            if type(duration) is not int or not 60 <= duration <= 3600:
                raise ValueError("无效停留期限")
            visit["expires_at"] = self._now() + min(duration, self.config.duration_minutes * 60)
            visit["status"] = "offered"
            visit["contacted"] = True
            visit["calls"][peer["id"]] = 1
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
                    raise ValueError("她现在已有邀请或串门，稍后再约")
                if visit_id >= current["id"]:
                    return {"declined": True, "reason": "双方同时提出邀请，接续另一条邀请"}
                await self._finish(self._state["visits"][current["id"]], "declined",
                                   "双方同时提出邀请，接续对方的邀请", notify=False)
            self._state["visits"][visit_id] = visit
            return {"visit": self._wire(visit)}
        visit = self._state["visits"].get(visit_id)
        if not visit or visit["remote"]["id"] != peer["id"]:
            raise ValueError("找不到本友邻的旅程")
        if op == "end":
            previous = (copy.deepcopy(visit["log"]), visit["status"], visit.get("end_reason"))
            remote = payload.get("visit")
            if remote is not None:
                validate_snapshot(visit, remote)
            if visit["role"] == "guest" and remote is not None:
                if remote.get("status") not in TERMINAL:
                    raise ValueError("对方尚未确认结束本次相见")
                self._synchronize(visit, remote, peer["id"], allow_terminal=True)
            if visit["status"] not in TERMINAL:
                status = "completed" if (visit.get("arrived_at") and isinstance(remote, dict)
                                          and remote.get("status") == "completed") else "interrupted"
                await self._finish(visit, status, "对方已告别或结束本次联系", notify=False)
            if (visit["log"], visit["status"], visit.get("end_reason")) != previous:
                self._invalidate_memory(visit)
            visit.pop("end_pending", None)
            visit.pop("model_pending", None)
            visit.pop("outbox", None)
            visit.pop("unsent_turn", None)
            return {"visit": self._wire(visit)}
        if visit["status"] in TERMINAL:
            return {"visit": self._wire(visit)}
        if visit["expires_at"] <= self._now():
            await self._finish(visit, "expired", "本次停留已经到期")
            return {"visit": self._wire(visit)}
        calls = payload.get("calls", 0)
        if type(calls) is not int or not 0 <= calls <= self._allocation(visit, peer["id"]):
            raise ValueError("旅程调用额度无效")
        candidate = copy.deepcopy(visit)
        if op == "step":
            self._receive_step(candidate, peer["id"], payload)
        elif op == "sync":
            self._synchronize(candidate, payload.get("visit"), peer["id"])
        elif op != "status":
            raise ValueError("未知旅程消息")
        candidate["calls"][peer["id"]] = max(candidate["calls"][peer["id"]], calls)
        visit.clear()
        visit.update(candidate)
        return {"visit": self._wire(visit)}

    async def _send(self, visit_id, payload):
        async with self._lock:
            self._enabled()
            visit = self._state["visits"][visit_id]
            peer = self._peer(visit["scope"], visit["remote"]["id"])
            if not peer["url"]:
                raise ValueError("对方未提供可访问的串门地址，请补填对外地址后重新配对")
            payload = {**payload, "id": visit_id, "calls": visit["calls"][self._local_id(visit)]}
            outgoing = visit.get("outbox")
            if not outgoing:
                outgoing = envelope(peer["secret"], self._local_id(visit), peer["id"], payload, self._now())
                visit["outbox"] = outgoing
                visit["contacted"] = True
                await self._save()
            peer = copy.deepcopy(peer)
        response = await self.transport.request(peer["url"], outgoing)
        result = verify(response, peer["secret"], self._now(), fresh=False)
        if response["sender"] != peer["id"] or response["target"] != outgoing["sender"] or result.get("reply_to") != outgoing["message_id"]:
            raise ValueError("串门答复不属于本次联系")
        async with self._lock:
            visit = self._state["visits"].get(visit_id)
            current_peer = self._state["peers"].get(peer["id"])
            if (self._closed or not visit or not current_peer or current_peer["secret"] != peer["secret"]
                    or (visit["status"] in TERMINAL and outgoing["payload"]["op"] != "end")):
                raise ValueError("世界或旅程已结束，旧答复作废")
            if (visit.get("outbox") or {}).get("message_id") != outgoing["message_id"]:
                raise ValueError("本次联系已被更新，旧答复作废")
            self._enabled()
            candidate = copy.deepcopy(visit)
            remote = result.get("visit")
            if remote is None and not (outgoing["payload"]["op"] == "offer"
                                       and result.get("declined") is True):
                raise ValueError("对方未提供本次旅程答复")
            if remote is not None and not isinstance(remote, dict):
                raise ValueError("对方返回无效旅程快照")
            for name in ("max_calls", "turn_limit"):
                value = (remote or {}).get(name)
                if value is not None:
                    if type(value) is not int or not (2 if name == "max_calls" else 1) <= value <= visit[name]:
                        raise ValueError("对方返回不兼容的旅程预算")
                    candidate[name] = value
            if remote is not None:
                validate_snapshot(candidate, remote)
            expires = (remote or {}).get("expires_at")
            if isinstance(expires, (int, float)):
                candidate["expires_at"] = min(visit["expires_at"], expires)
            candidate.pop("outbox", None)
            candidate["network_failures"] = 0
            visit.clear()
            visit.update(candidate)
            await self._save()
        result["delivered_op"] = outgoing["payload"]["op"]
        return result

    async def return_home(self, scope: str) -> dict[str, Any]:
        await self._load()
        visit = self.active(scope)
        if not visit:
            return {"ok": True, "status": "completed"}
        async with self._lock:
            current = self._state["visits"][visit["id"]]
            await self._finish(current, "interrupted", "真人要求告别归家")
        await self._notify_end(visit["id"])
        await self._remember(visit["id"])
        return {"ok": True, "status": "interrupted"}
