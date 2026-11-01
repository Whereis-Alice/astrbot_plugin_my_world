"""One server trust relationship, with explicit local lives and actor bindings.

Old actor credentials remain valid trust anchors. New lives use authenticated
directory/bind requests and derived keys; private scope names never cross the wire.
"""
from __future__ import annotations

import copy
from typing import Any

from .config import normalize_url
from .protocol import ID, RESERVED, code_id, derive_key, digest, envelope, new_id, public_actor, text, validate_actor, verify

MAX_LIVES = 32
CONTROL_OPS = {"world_catalog", "world_bind"}


class WorldConnections:
    def _canonical_life(self, scope):
        canonical = getattr(self.actor_port, "canonical_scope", None)
        return canonical(scope) if callable(canonical) else str(scope or "")

    def _participating(self):
        return list(dict.fromkeys(value for scope in self._state.get("enabled_scopes", [])
                                  if (value := self._canonical_life(scope))))

    def _life_enabled(self, scope):
        return bool(scope and self._canonical_life(scope) == scope and scope in self._participating())

    def _available_lives(self):
        getter = getattr(self.actor_port, "lives", None)
        lives = getter() if callable(getter) else [{"scope": scope, "label": scope} for scope in self._participating()]
        enabled = set(self._participating())
        return [{"scope": row["scope"], "label": text(row.get("label") or row["scope"], 160),
                 "enabled": row["scope"] in enabled} for row in lives]

    def _register_connection(self, peer, *, enable_scope=False, migration=False):
        actor = validate_actor(peer["actor"])
        cid = actor["instance_id"]
        own = public_actor(peer["local"], self._state["instance_id"])["id"]
        connections = self._state.setdefault("connections", {})
        if not migration and cid not in connections and len(connections) >= self.config.max_peers:
            raise ValueError("友邻世界数量已到上限")
        # Migration must retain earlier pairings even if the configured limit
        # has since decreased. Admission of new worlds enforces that limit.
        conn = connections.setdefault(cid, {"id": cid, "name": actor["world"], "alias": peer.get("alias", ""),
            "url": peer.get("url", ""), "enabled": peer.get("enabled", True), "anchors": [],
            "actors": [], "default_actor": actor["id"], "migrated": True})
        if not conn.get("url") or (peer.get("enabled", True) and not conn.get("enabled", True)):
            conn["url"] = peer.get("url") or conn.get("url", "")
        conn["enabled"] = conn.get("enabled", True) or peer.get("enabled", True)
        if not conn.get("alias"):
            conn["alias"] = peer.get("alias", "")
        anchor = {"local_id": own, "remote_id": actor["id"], "secret": peer["secret"]}
        if anchor not in conn["anchors"]:
            conn["anchors"].append(anchor)
        if not any(row["id"] == actor["id"] for row in conn["actors"]):
            conn["actors"].append(actor)
        peer["connection_id"] = cid
        if enable_scope and peer["scope"] not in self._state.setdefault("enabled_scopes", []):
            self._state["enabled_scopes"].append(peer["scope"])

    def _migrate_connections(self):
        if self._state.get("world_pairing_version") == 1:
            return
        self._state.setdefault("connections", {})
        infer_scopes = "enabled_scopes" not in self._state
        self._state.setdefault("enabled_scopes", [])
        for peer in self._state["peers"].values():
            self._register_connection(peer, enable_scope=infer_scopes and peer.get("enabled", True), migration=True)
        for visit in self._state["visits"].values():
            if visit.get("remote", {}).get("instance_id"):
                visit["connection_id"] = visit["remote"]["instance_id"]
        self._state["world_pairing_version"] = 1

    @staticmethod
    def _safe_connection(conn):
        return {key: copy.deepcopy(conn.get(key)) for key in
                ("id", "name", "alias", "url", "enabled", "actors", "default_actor", "migrated", "catalog_error")}

    def _scoped_candidates(self, scope):
        if not self._life_enabled(scope):
            return []
        result = []
        for conn in self._state.get("connections", {}).values():
            if not conn.get("enabled", True) or not conn.get("url"):
                continue
            actors = conn.get("actors", [])
            for actor in actors:
                binding = self._find_peer(scope, actor["id"])
                if binding and not binding.get("enabled", True):
                    continue
                result.append({"id": actor["id"], "scope": scope, "connection_id": conn["id"],
                    "actor": copy.deepcopy(actor), "name": actor["name"], "world": actor["world"],
                    "alias": conn.get("alias", "") if len(actors) == 1 else actor["name"],
                    "url": conn["url"], "enabled": True, "virtual": True})
        return result

    def _automatic_peers(self, scope):
        # One candidate per neighbouring server; different remote lives share
        # this local life's daily success allowance for that neighbour.
        selected = []
        for conn in self._state.get("connections", {}).values():
            rows = [p for p in self._scoped_candidates(scope) if p["connection_id"] == conn["id"]]
            if rows:
                selected.append(next((p for p in rows if p["id"] == conn.get("default_actor")), rows[0]))
            elif not conn.get("actors") and self._life_enabled(scope) and conn.get("enabled", True) and conn.get("url") \
                    and conn.get("next_catalog_at", 0) <= self._now():
                selected.append({"id": conn["id"], "scope": scope, "connection_id": conn["id"],
                                 "url": conn["url"], "enabled": True, "virtual": True})
        return selected

    async def global_overview(self, scope=""):
        return await self.overview(scope)

    async def set_enabled_scopes(self, scopes):
        self._enabled()
        await self._load()
        if not isinstance(scopes, list) or len(scopes) > MAX_LIVES or any(not isinstance(s, str) or not s for s in scopes):
            raise ValueError("请选择有效生活，最多开启32份生活")
        values = list(dict.fromkeys(self._canonical_life(s) for s in scopes))
        if "" in values:
            raise ValueError("选择的生活已停用，请刷新后再保存")
        async with self._lock:
            self._enabled()
            if any(self._canonical_life(s) != s for s in values):
                raise ValueError("生活归属已变化，请刷新后重新选择")
            for visit in list(self._state["visits"].values()):
                if visit["scope"] not in values and visit["status"] in RESERVED:
                    await self._finish(visit, "interrupted", "这个生活已关闭串门")
            generations = self._state.setdefault("life_generations", {})
            for scope in set(self._participating()) ^ set(values):
                generations[scope] = generations.get(scope, 0) + 1
            self._state["enabled_scopes"] = values
            await self._save()
        return {"enabled_scopes": values}

    async def _catalog(self):
        cards, _ = await self._catalog_snapshot()
        return cards

    async def _catalog_snapshot(self):
        cards = []
        identities = []
        scopes = self._participating()[:MAX_LIVES]
        for scope in scopes:
            local = await self.actor_port.describe(scope)
            card = public_actor(local, self._state["instance_id"])
            identities.append([scope, card, local.get("persona_key"),
                               self._state.get("life_generations", {}).get(scope, 0)])
            if self._life_enabled(scope) and local.get("persona_key"):
                cards.append(card)
        if self._participating()[:MAX_LIVES] != scopes:
            raise ValueError("生活参与状态已变化，请刷新角色目录")
        return cards, digest(identities)

    @staticmethod
    def _validate_catalog(value, instance_id):
        if not isinstance(value, list) or len(value) > MAX_LIVES:
            raise ValueError("对方角色目录无效")
        cards = [validate_actor(row) for row in value]
        if any(row["instance_id"] != instance_id for row in cards) or len({r["id"] for r in cards}) != len(cards):
            raise ValueError("对方角色目录身份冲突")
        return cards

    async def _global_pair_code(self):
        self._enabled()
        await self._load()
        async with self._lock:
            self._enabled()
            code = new_id() + new_id()
            self._state["codes"] = {k: v for k, v in self._state["codes"].items() if v.get("kind") != "world"}
            self._state["codes"][code_id(code)] = {"code": code, "kind": "world", "expires_at": self._now() + 3600}
            await self._save()
        return {"code": code, "expires_at": self._now() + 3600, "url": self.config.public_url}

    async def pair_world(self, url, code, alias=""):
        self._enabled()
        await self._load()
        url = normalize_url(url)
        if not isinstance(code, str) or len(code) != 64 or any(c not in "0123456789abcdef" for c in code):
            raise ValueError("配对码格式无效，请重新生成")
        own = self._state["instance_id"]
        request = envelope(code, own, "pair:" + code_id(code), {"op": "pair_world", "instance_id": own,
            "url": self.config.public_url, "actors": await self._catalog()}, self._now(), message_id=code_id(code))
        reply = await self.transport.request(url, request)
        payload = verify(reply, code, self._now())
        remote = payload.get("instance_id")
        if (not isinstance(remote, str) or not ID.fullmatch(remote) or remote == own or reply["sender"] != remote
                or reply["target"] != own or payload.get("reply_to") != request["message_id"]):
            raise ValueError("全局配对答复不属于本次请求")
        cards = self._validate_catalog(payload.get("actors"), remote)
        async with self._lock:
            self._enabled()
            conn = await self._store_world(remote, url, derive_key(code, "world:" + own, "world:" + remote), cards, alias)
            await self._save()
        return self._safe_connection(conn)

    async def _store_world(self, remote, url, key, cards, alias=""):
        own = self._state["instance_id"]
        if remote not in self._state["connections"] and len(self._state["connections"]) >= self.config.max_peers:
            raise ValueError("友邻世界数量已到上限")
        if remote in self._state["connections"]:
            await self._drop_connection_bindings(remote, "全局配对已更新")
        conn = {"id": remote, "name": cards[0]["world"] if cards else "友邻世界", "alias": text(alias, 80),
                "url": url, "enabled": True, "actors": cards, "default_actor": cards[0]["id"] if cards else "",
                "anchors": [{"local_id": own, "remote_id": remote, "secret": key}], "migrated": False}
        self._state["connections"][remote] = conn
        return conn

    async def _receive_world_pair(self, body, receipt_key):
        async with self._lock:
            item = self._state["codes"].get(body["target"][5:])
            if not item or item.get("kind") != "world" or item["expires_at"] <= self._now():
                raise ValueError("配对码已过期或已使用，请使用新版全局配对码")
            code = item["code"]
        payload = verify(body, code, self._now())
        remote = payload.get("instance_id")
        own = self._state["instance_id"]
        if not isinstance(remote, str) or not ID.fullmatch(remote) or remote != body["sender"] or remote == own:
            raise ValueError("全局配对身份不一致")
        cards = self._validate_catalog(payload.get("actors"), remote)
        local_cards, catalog_stamp = await self._catalog_snapshot()
        url = normalize_url(payload["url"]) if payload.get("url") else ""
        async with self._lock:
            self._enabled()
            if receipt_key in self._state["receipts"]:
                return await self._cached_response(body, self._state["receipts"][receipt_key])
            if self._state["codes"].get(body["target"][5:]) != item:
                raise ValueError("配对码已变化或已经使用")
            if (await self._catalog_snapshot())[1] != catalog_stamp:
                raise ValueError("角色目录已变化，旧配对请求作废，请稍后重试")
            await self._store_world(remote, url, derive_key(code, "world:" + own, "world:" + remote), cards)
            self._state["codes"].pop(body["target"][5:])
            return await self._receipt(body, {"instance_id": own, "actors": local_cards}, code, own, receipt_key)

    def _anchor_for(self, body):
        for conn in self._state["connections"].values():
            if conn.get("enabled", True):
                for anchor in conn["anchors"]:
                    if anchor["remote_id"] == body.get("sender") and anchor["local_id"] == body.get("target"):
                        return conn, anchor
        raise ValueError("尚未配对或已停用的友邻世界")

    async def _receive_control(self, body, receipt_key):
        conn, anchor = self._anchor_for(body)
        payload = verify(body, anchor["secret"], self._now())
        conn, anchor = copy.deepcopy(conn), copy.deepcopy(anchor)
        op = payload.get("op")
        local = None
        scope = ""
        if op == "world_catalog":
            cards, catalog_stamp = await self._catalog_snapshot()
            result = {"actors": cards}
        elif op == "world_bind":
            remote = validate_actor(payload.get("actor"))
            remote_token = payload.get("actor_token")
            if not isinstance(remote_token, str) or len(remote_token) != 64 or any(c not in "0123456789abcdef" for c in remote_token):
                raise ValueError("角色绑定身份无效")
            if remote["instance_id"] != conn["id"]:
                raise ValueError("角色不属于已经配对的世界")
            for candidate in self._participating():
                card = await self.actor_port.describe(candidate)
                if public_actor(card, self._state["instance_id"])["id"] == payload.get("target_actor"):
                    scope, local = candidate, card
                    break
            if not local:
                raise ValueError("对方选择的生活没有开启串门，请刷新角色目录")
            result = {"actor": public_actor(local, self._state["instance_id"]),
                      "actor_token": self._binding_token(scope, local, anchor)}
        else:
            raise ValueError("未知世界管理消息")
        async with self._lock:
            self._enabled()
            current = self._state["connections"].get(conn["id"])
            if not current or not current.get("enabled", True) or anchor not in current["anchors"]:
                raise ValueError("全局配对已变化，旧请求作废")
            if receipt_key in self._state["receipts"]:
                return await self._cached_response(body, self._state["receipts"][receipt_key])
            if op == "world_catalog" and (await self._catalog_snapshot())[1] != catalog_stamp:
                raise ValueError("角色目录已变化，旧请求作废，请稍后重试")
            if local:
                current_local = await self.actor_port.describe(scope)
                if (not self._life_enabled(scope) or current_local["actor_id"] != local["actor_id"]
                        or current_local["persona_key"] != local["persona_key"]
                        or self._binding_token(scope, current_local, anchor) != result["actor_token"]):
                    raise ValueError("这个生活已关闭串门或身份已变化，请刷新角色目录")
                existing = self._find_peer(scope, remote["id"])
                if existing and not existing.get("enabled", True):
                    raise ValueError("这位友邻暂未开启联系")
                bound_key = self._bound_key(anchor, remote["id"], remote_token, result["actor"]["id"], result["actor_token"])
                active = self.active(scope)
                if active and (not existing or not self._visit_belongs(active, existing) or existing["secret"] != bound_key
                                          or existing["local"]["persona_key"] != local["persona_key"]):
                    raise ValueError("这份生活正在串门，稍后再建立新的角色联系")
                peer = self._add_peer(scope, local, remote, current["url"],
                                     bound_key, register=False)
                peer.update(connection_id=conn["id"], anchor_digest=digest(anchor))
            return await self._receipt(body, result, anchor["secret"], anchor["local_id"], receipt_key)

    async def _control_request(self, cid, payload):
        self._enabled()
        await self._load()
        conn = copy.deepcopy(self._state["connections"].get(cid))
        if not conn or not conn.get("enabled", True) or not conn.get("url"):
            raise ValueError("这个友邻世界没有开启联系")
        anchor = conn["anchors"][0]
        request = envelope(anchor["secret"], anchor["local_id"], anchor["remote_id"], payload, self._now())
        response = await self.transport.request(conn["url"], request)
        result = verify(response, anchor["secret"], self._now())
        if (response["sender"] != anchor["remote_id"] or response["target"] != anchor["local_id"]
                or result.get("reply_to") != request["message_id"]):
            raise ValueError("友邻世界答复不属于本次请求")
        current = self._state["connections"].get(cid)
        if self._closed or not current or not current.get("enabled", True) or anchor not in current["anchors"]:
            raise ValueError("全局配对已变化，旧答复作废")
        return result, anchor

    async def refresh_connection(self, cid):
        try:
            result, anchor = await self._control_request(cid, {"op": "world_catalog"})
            cards = self._validate_catalog(result.get("actors"), cid)
        except (ValueError, ConnectionError, TimeoutError, OSError):
            async with self._lock:
                conn = self._state["connections"].get(cid)
                if conn and not self._closed:
                    conn.update(catalog_error="暂未取得最新角色目录；原配对保留，对方旧版需升级以开放其他生活",
                                next_catalog_at=self._now() + max(60, self.config.auto_interval_minutes * 60))
                    await self._save()
            raise
        async with self._lock:
            self._enabled()
            conn = self._state["connections"].get(cid)
            if not conn or anchor not in conn["anchors"]:
                raise ValueError("全局配对已变化")
            conn.update(actors=cards, catalog_error="", catalog_ready=True,
                        next_catalog_at=self._now() + max(60, self.config.auto_interval_minutes * 60))
            if cards and conn.get("default_actor") not in {c["id"] for c in cards}:
                conn["default_actor"] = cards[0]["id"]
            await self._save()
        return self._safe_connection(conn)

    async def _prepare_peer(self, scope, peer_id):
        if not self._life_enabled(scope):
            raise ValueError("这个生活还没有开启串门，请在友邻页勾选参与的生活")
        local = await self.actor_port.describe(scope)
        existing = self._find_peer(scope, peer_id)
        if existing and not existing.get("enabled", True):
            raise ValueError("这位友邻暂未开启联系")
        existing_conn = self._state["connections"].get(existing.get("connection_id")) if existing else None
        if (existing and self._peer_trusted(existing) and existing["local"]["persona_key"] == local["persona_key"]
                and existing["local"]["actor_id"] == local["actor_id"] and self._binding_current(existing)
                and not existing.get("anchor_digest") and not existing_conn.get("catalog_ready")):
            # Discover an upgraded neighbour only when preparing a real contact.
            # Old servers keep their original keys and a bounded retry interval.
            if self.active(scope) or existing_conn.get("next_catalog_at", 0) > self._now():
                return peer_id
            try:
                await self.refresh_connection(existing_conn["id"])
            except (ValueError, ConnectionError, TimeoutError, OSError):
                return peer_id
        if self.active(scope):
            raise ValueError("这份生活已经有邀请或串门，稍后再联系")
        conn = next((c for c in self._state["connections"].values() if c["id"] == peer_id
                     or any(a["id"] == peer_id for a in c.get("actors", []))), None)
        if not conn:
            raise ValueError("请先选择已配对的友邻世界")
        if not conn.get("actors"):
            await self.refresh_connection(conn["id"])
        actor = next((a for a in conn["actors"] if a["id"] == peer_id), None)
        if actor is None and peer_id == conn["id"]:
            actor = next((a for a in conn["actors"] if a["id"] == conn.get("default_actor")), None)
        if not actor:
            raise ValueError("友邻暂未开放接待的生活，请稍后或刷新目录")
        binding = self._find_peer(scope, actor["id"])
        if binding and not binding.get("enabled", True):
            raise ValueError("这位友邻暂未开启联系")
        own = public_actor(local, self._state["instance_id"])
        anchor = conn["anchors"][0]
        own_token = self._binding_token(scope, local, anchor)
        result, anchor = await self._control_request(conn["id"], {"op": "world_bind", "actor": own,
            "actor_token": own_token, "target_actor": actor["id"]})
        remote = validate_actor(result.get("actor"))
        remote_token = result.get("actor_token")
        if (remote["id"] != actor["id"] or remote["instance_id"] != conn["id"]
                or not isinstance(remote_token, str) or len(remote_token) != 64
                or any(c not in "0123456789abcdef" for c in remote_token)):
            raise ValueError("友邻绑定返回了其他角色")
        current = await self.actor_port.describe(scope)
        async with self._lock:
            self._enabled()
            live_conn = self._state["connections"].get(conn["id"])
            if (not self._life_enabled(scope) or self.active(scope) or not live_conn or anchor not in live_conn["anchors"]
                    or current["actor_id"] != local["actor_id"] or current["persona_key"] != local["persona_key"]):
                raise ValueError("建立联系期间生活或配对已变化，旧绑定作废")
            if self._binding_token(scope, current, anchor) != own_token:
                raise ValueError("建立联系期间生活参与状态已变化，旧绑定作废")
            peer = self._add_peer(scope, local, remote, live_conn["url"],
                                 self._bound_key(anchor, own["id"], own_token, remote["id"], remote_token), live_conn.get("alias", ""), register=False)
            peer.update(connection_id=conn["id"], anchor_digest=digest(anchor))
            await self._save()
        return peer["id"]

    def _binding_token(self, scope, local, anchor):
        # The trust anchor salts private identity data; neither persona nor local
        # scope names are published, and concurrent binds derive the same key.
        identity = digest([scope, local["actor_id"], local["persona_key"],
                           self._state.get("life_generations", {}).get(scope, 0)])
        return derive_key(anchor["secret"], "life", identity)

    @staticmethod
    def _bound_key(anchor, first, first_token, second, second_token):
        return derive_key(anchor["secret"], first + "@" + first_token, second + "@" + second_token)

    def _binding_current(self, peer):
        return peer.get("life_generation", 0) == self._state.get("life_generations", {}).get(peer["scope"], 0)

    def _find_peer(self, scope, peer_id):
        return next((p for p in self._state["peers"].values() if p["id"] == peer_id and p["scope"] == scope), None)

    def _wire_peer(self, sender, target):
        return next((p for p in self._state["peers"].values() if p["id"] == sender
                     and self._canonical_life(p["scope"]) == p["scope"]
                     and public_actor(p["local"], self._state["instance_id"])["id"] == target), None)

    def _peer_trusted(self, peer):
        conn = self._state["connections"].get(peer.get("connection_id"))
        if not peer.get("enabled", True) or not conn or not conn.get("enabled", True):
            return False
        if peer.get("anchor_digest"):
            return any(digest(a) == peer["anchor_digest"] for a in conn["anchors"])
        return any(a["secret"] == peer["secret"] for a in conn["anchors"])

    async def _drop_connection_bindings(self, cid, reason):
        for visit in list(self._state["visits"].values()):
            if visit.get("connection_id") == cid and visit["status"] in RESERVED:
                await self._finish(visit, "interrupted", reason, notify=False)
        for key, peer in list(self._state["peers"].items()):
            if peer.get("connection_id") == cid:
                self._state["peers"].pop(key)

    async def remove_connection(self, cid):
        await self._load()
        async with self._lock:
            if cid not in self._state["connections"]:
                raise ValueError("找不到这个友邻世界")
            await self._drop_connection_bindings(cid, "已解除全局配对")
            self._state["connections"].pop(cid)
            await self._save()
        return {"ok": True}
