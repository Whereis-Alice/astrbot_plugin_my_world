"""Match paired lives by opaque shared-conversation proofs, never private scopes."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import re

from .ports import VisitStateError, describe_local
from .protocol import TERMINAL, digest, public_actor

TOKEN = re.compile(r"^[0-9a-f]{64}$")
MAX_CONTEXTS = 256
ROUTING_ERROR = "尚未确认友邻在同一会话中的生活，请更新双方插件并刷新角色目录"


class ConversationRouting:
    def _routing_required(self):
        return callable(getattr(self.actor_port, "routing_contexts", None))

    def _routing_contexts(self, scope):
        if not self._routing_required():
            return []
        try:
            values = self.actor_port.routing_contexts(scope)
        except Exception as error:
            raise VisitStateError() from error
        if (not isinstance(values, list) or len(values) > MAX_CONTEXTS
                or any(not isinstance(value, str) or not value or len(value) > 500 for value in values)):
            raise VisitStateError()
        return sorted(set(values))

    def _routing_tokens(self, scope, anchor):
        return sorted(hmac.new(anchor["secret"].encode(), ("mw-conversation-v1|" + value).encode(),
                               hashlib.sha256).hexdigest() for value in self._routing_contexts(scope))

    @staticmethod
    def _validate_route_tokens(value):
        if (not isinstance(value, list) or len(value) > MAX_CONTEXTS
                or any(not isinstance(item, str) or not TOKEN.fullmatch(item) for item in value)
                or len(set(value)) != len(value)):
            raise ValueError("友邻会话归属证明无效")
        return sorted(value)

    def _routing_proof(self, scope, anchor):
        return {"version": 1, "tokens": self._routing_tokens(scope, anchor)}

    def _read_routing_proof(self, value):
        if not isinstance(value, dict) or set(value) != {"version", "tokens"} or type(value["version"]) is not int or value["version"] != 1:
            raise ValueError(ROUTING_ERROR)
        return self._validate_route_tokens(value["tokens"])

    async def _routing_catalog_snapshot(self, anchor=None):
        """Capture cards and private membership together; publish only salted tags."""
        try:
            async with asyncio.timeout(self.config.timeout_seconds):
                cards, identities, routes = [], [], {}
                scopes = self._participating()[:32]
                for scope in scopes:
                    local = await describe_local(self.actor_port, scope, timeout_seconds=self.config.timeout_seconds)
                    card = public_actor(local, self._state["instance_id"])
                    contexts = self._routing_contexts(scope)
                    identities.append([scope, card, local.get("persona_key"),
                                       self._state.get("life_generations", {}).get(scope, 0), contexts])
                    if self._life_enabled(scope) and local.get("persona_key"):
                        cards.append(card)
                        if anchor is not None and self._routing_required():
                            routes[card["id"]] = self._routing_tokens(scope, anchor)
                if self._participating()[:32] != scopes:
                    raise ValueError("生活参与状态已变化，请刷新角色目录")
                # Re-read synchronous membership after all yielding descriptions.
                if any(self._routing_contexts(row[0]) != row[-1] for row in identities):
                    raise ValueError("会话归属已变化，请刷新角色目录")
                metadata = {"version": 1, "actors": routes} if anchor is not None and self._routing_required() else None
                return cards, digest(identities), metadata
        except TimeoutError as error:
            raise VisitStateError() from error

    def _read_routing_catalog(self, value, cards):
        if value is None:
            return None
        if (not isinstance(value, dict) or set(value) != {"version", "actors"}
                or type(value["version"]) is not int or value["version"] != 1
                or not isinstance(value["actors"], dict) or set(value["actors"]) != {card["id"] for card in cards}):
            raise ValueError("友邻会话目录无效")
        return {actor: self._validate_route_tokens(tokens) for actor, tokens in value["actors"].items()}

    def _routing_catalog_current(self, connection):
        anchors = connection.get("anchors", [])
        return bool(anchors and isinstance(connection.get("routing"), dict)
                    and connection.get("routing_anchor") == digest(anchors[0]))

    async def _reserve_routing_catalog_refresh(self, cid):
        async with self._lock:
            connection = self._state["connections"].get(cid)
            if (self._closed or not self.config.enabled or not connection or not connection.get("enabled", True)
                    or not connection.get("url") or not connection.get("anchors")):
                return False
            stamp = digest(connection["anchors"][0])
            checked = stamp in {connection.get("routing_anchor"), connection.get("routing_probe_anchor")}
            if checked and connection.get("next_catalog_at", 0) > self._now():
                return False
            # Old versions may have cached a populated catalog and a long retry
            # deadline. One membership discovery must still run after upgrade.
            connection["routing_probe_anchor"] = stamp
            connection["next_catalog_at"] = self._now() + max(60, self.config.auto_interval_minutes * 60)
            await self._save()
            return True

    def _routed_actors(self, scope, connection):
        if not self._routing_required():
            return connection.get("actors", [])
        if not self._routing_catalog_current(connection):
            return []
        own = set(self._routing_tokens(scope, connection["anchors"][0]))
        if not own:
            return []
        matched = [actor for actor in connection.get("actors", [])
                   if own.intersection(connection["routing"].get(actor["id"], []))]
        return matched if len(matched) == 1 else []

    def _check_routing_proof(self, scope, anchor, proof):
        if not self._routing_required():
            return None
        remote = self._read_routing_proof(proof)
        matches = [candidate for candidate in self._participating()
                   if set(remote).intersection(self._routing_tokens(candidate, anchor))]
        if matches != [scope]:
            raise ValueError("双方生活不属于同一会话，未建立串门联系")
        return {"anchor": digest(anchor), "local": self._routing_tokens(scope, anchor), "remote": remote}

    def _routing_binding_current(self, peer):
        if not self._routing_required():
            return True
        proof = peer.get("routing")
        connection = self._state.get("connections", {}).get(peer.get("connection_id"), {})
        anchor = next((item for item in connection.get("anchors", [])
                       if isinstance(proof, dict) and digest(item) == proof.get("anchor")), None)
        if anchor is None or proof.get("local") != self._routing_tokens(peer["scope"], anchor):
            return False
        try:
            return self._check_routing_proof(peer["scope"], anchor,
                {"version": 1, "tokens": proof.get("remote")}) == proof
        except ValueError:
            return False

    def _routing_existing_exchange(self, peer, payload):
        """Already-created visits retain their original key and historical owner."""
        visit = self._state.get("visits", {}).get(payload.get("id"))
        return bool(visit and self._visit_belongs(visit, peer)
                    and (visit.get("arrived_at") or visit.get("status") in TERMINAL))

    async def _discover_routing_catalogs(self):
        """A missing match must not suppress the directory refresh that can find it."""
        if not self._routing_required():
            return
        for cid in list(self._state.get("connections", {})):
            if not await self._reserve_routing_catalog_refresh(cid):
                continue
            try:
                await self.refresh_connection(cid)
            except (ValueError, ConnectionError, TimeoutError, OSError):
                pass  # Existing catalog diagnostics retain pairing and retry time.
