"""Address group speech without relying on an empty OneBot display name."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import time
from typing import Any


def _field(part: Any, name: str) -> Any:
    return part.get(name) if isinstance(part, dict) else getattr(part, name, None)


def _kind(part: Any) -> str:
    kind = _field(part, "type") or type(part).__name__
    return str(getattr(kind, "value", kind)).lower()


def has_body(chain: list[Any]) -> bool:
    """Addressing is not speech; images, voice and other payloads still are."""
    return any(_kind(part) not in {"at", "atall", "reply"}
               and (_kind(part) != "plain" or str(_field(part, "text") or "").strip())
               for part in chain)


def addressed_chain(chain: list[Any], people: tuple[str, ...], quote: str) -> list[Any]:
    from astrbot.api.message_components import At, Reply

    result, mentions, replies = [], set(), set()
    for part in chain:
        kind = _kind(part)
        if kind in {"at", "atall", "reply"}:
            key = str(_field(part, "id" if kind == "reply" else "qq") or "").strip()
            seen = replies if kind == "reply" else mentions
            if (kind != "reply" and not key) or (key and key in seen):
                continue
            if key:
                seen.add(key)
        result.append(part)
    result[:0] = [At(qq=uid) for uid in people if uid and uid not in mentions]
    if quote and quote not in replies:
        result.insert(0, Reply(id=quote))
    return result


def _name(value: Any) -> str:
    return " ".join(str(value or "").split()).lstrip("@＠")[:80]


class GroupMentions:
    """Bounded metadata reads, scoped to the actual adapter, group and person."""
    TIMEOUT = 1.0
    CACHE_SIZE = 256

    def __init__(self, plugin: Any):
        self.plugin = plugin
        self.cache: OrderedDict[tuple[str, str], tuple[Any, float, str]] = OrderedDict()

    def _onebot(self, target: str) -> tuple[bool, Any]:
        parts = target.split(":", 2)
        if len(parts) != 3 or parts[1] != "GroupMessage":
            return False, None
        getter = getattr(self.plugin.context, "get_platform_inst", None)
        try:
            platform = getter(parts[0]) if callable(getter) else None
        except Exception:
            platform = None
        if platform is not None:
            if platform.meta().name != "aiocqhttp":
                return False, None
            try:
                return True, platform.get_client()
            except Exception:
                return True, None
        event = self.plugin.last_event(target)
        if event is not None and str(event.unified_msg_origin) == target:
            getter = getattr(event, "get_platform_name", None)
            if callable(getter) and getter() == "aiocqhttp":
                return True, getattr(event, "bot", None)
        return False, None

    def _known_name(self, target: str, uid: str, part: Any) -> str:
        name = _name(_field(part, "name"))
        if name:
            return name
        event = self.plugin.last_event(target)
        if (event is not None and str(event.unified_msg_origin) == target
                and str(event.get_sender_id()) == uid):
            name = _name(event.get_sender_name())
            if name:
                return name
        try:
            row = self.plugin.engine.profiles.profile(target, uid)
        except Exception:
            row = None
        names = (row or {}).get("payload", {}).get("names") or []
        return _name(names[-1]) if names else uid

    async def _member_name(self, target: str, uid: str, bot: Any) -> str:
        key = (target, uid)
        cached = self.cache.get(key)
        if cached is not None and cached[0] is bot and time.monotonic() < cached[1]:
            self.cache.move_to_end(key)
            return cached[2]
        name = ""
        action = getattr(bot, "call_action", None)
        if callable(action):
            try:
                member = await asyncio.wait_for(action("get_group_member_info",
                    group_id=int(target.split(":", 2)[2]), user_id=int(uid), no_cache=False), self.TIMEOUT)
                if (isinstance(member, dict)
                        and (member.get("user_id") is None or str(member["user_id"]) == uid)):
                    name = _name(member.get("card")) or _name(member.get("nickname"))
            except Exception:
                pass  # Failed metadata is not a transport failure; never retry the send.
        self.cache[key] = (bot, time.monotonic() + (120 if name else 15), name)
        self.cache.move_to_end(key)
        while len(self.cache) > self.CACHE_SIZE:
            self.cache.popitem(last=False)
        return name

    async def prepare(self, target: str, chain: list[Any]) -> list[Any]:
        from astrbot.api.message_components import At, Plain

        if not any(_kind(part) == "at" for part in chain):
            return chain
        onebot, bot = self._onebot(target)
        if not onebot:
            return chain

        async def prepare(part):
            uid = str(_field(part, "qq") or "").strip()
            if _kind(part) != "at" or uid == "all":
                return part
            # OneBot numeric user IDs only; malformed targets cannot ping anyone.
            if not uid.isdigit() or int(uid) <= 0:
                return None
            name = await self._member_name(target, uid, bot)
            if name:
                return At(qq=uid, name=name)
            return Plain(text=f"@{self._known_name(target, uid, part)} ")

        return [part for part in await asyncio.gather(*(prepare(part) for part in chain)) if part is not None]
