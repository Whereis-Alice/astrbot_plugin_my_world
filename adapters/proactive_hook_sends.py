"""Stage public-hook sends on invocation-local handles before speech approval."""
from __future__ import annotations

import asyncio
import copy
from contextlib import contextmanager
from contextvars import ContextVar

from .group_mentions import _field


HOOK_SENDS = ContextVar("my_world_public_hook_sends", default=None)


class SendCapture:
    def __init__(self, plugin, event, target):
        self.plugin, self.event, self.target = plugin, event, target
        self.chains = []
        self.active, self.failed = True, False
        self.owner = asyncio.current_task()

    def append(self, target, chain):
        # Explicit proxies remain bound to this draft even in an inherited
        # child task. Expiry must reject it, never fall through to real sending.
        if not self.active or asyncio.current_task() is not self.owner or str(target) != self.target:
            if self.active:
                self.failed = True
            self.plugin.logger.warning("[my_world] 已拒绝过期或跨会话的主动装饰钩子发送")
            raise RuntimeError("主动装饰钩子的发送范围已关闭或目标不符")
        if len(self.chains) >= 64:
            self.failed = True
            raise RuntimeError("主动装饰钩子分段过多")
        components = getattr(chain, "chain", chain)
        self.chains.append(copy.deepcopy(list(components or [])))
        return True

    async def send(self, chain, *args, **kwargs):
        return self.append(self.target, chain)


class SendingContext:
    def __init__(self, context, capture):
        self.context, self.capture = context, capture

    def __getattr__(self, name):
        return getattr(self.context, name)

    async def send_message(self, session, chain, *args, **kwargs):
        return self.capture.append(str(session), chain)


class OneBotImagePart:
    """Retain the exact prepared OneBot image-summary payload, including summary."""
    def __init__(self, payload):
        self.payload = copy.deepcopy(payload)
        self.type = payload["type"]

    def __getattr__(self, name):
        data = self.__dict__.get("payload", {}).get("data", {})
        if name in data:
            return data[name]
        raise AttributeError(name)

    def toDict(self):
        return copy.deepcopy(self.payload)

    async def to_dict(self):
        return self.toDict()


class ImageTransport:
    """Stage only OutputPro's exact OneBot image-summary shape; reject other raw sends."""
    def __init__(self, transport, capture):
        self.transport, self.capture = transport, capture

    def __getattr__(self, name):
        return getattr(self.transport, name)

    async def send(self, *args, **kwargs):
        payload = kwargs.get("message") if "message" in kwargs else args[1] if len(args) > 1 else None
        raw = args[0] if args else kwargs.get("event")
        parts = self.capture.target.split(":", 2)
        room = str(_field(raw, "group_id") or "")
        person = str(_field(raw, "user_id") or "")
        addressed = len(parts) == 3 and (
            parts[1] == "GroupMessage" and room == parts[2] or
            parts[1] in {"FriendMessage", "PrivateMessage"} and not room and person == parts[2])
        valid = (isinstance(payload, list) and payload and all(isinstance(part, dict)
            and isinstance(part.get("data"), dict) and part.get("type") in {"image", "reply", "at", "text"}
            and (part["type"] != "text" or not str(part["data"].get("text", "")).strip()) for part in payload)
            and any(part["type"] == "image" for part in payload))
        if not valid or not addressed:
            self.capture.failed = True
            raise RuntimeError("主动装饰钩子不能直接发送未知的原生载荷")
        return self.capture.append(self.capture.target, [OneBotImagePart(part) for part in payload])


@contextmanager
def capture_sends(plugin, event, target):
    capture = SendCapture(plugin, event, target)
    token = HOOK_SENDS.set(capture)
    previous_send = getattr(event, "send", None)
    transport = getattr(event, "bot", None)
    while isinstance(transport, ImageTransport):
        transport = transport.transport
    event.send = capture.send
    if transport is not None and callable(getattr(transport, "send", None)):
        event.bot = ImageTransport(transport, capture)
    try:
        yield capture
    except BaseException:
        capture.failed = True
        raise
    finally:
        capture.active = False
        event.send = previous_send
        # Keep the detached event's closed proxy: a delayed child task must
        # not regain the real adapter by looking up event.bot after this scope.
        HOOK_SENDS.reset(token)


def fail_hook_sends():
    capture = HOOK_SENDS.get()
    if capture is not None:
        capture.failed = True
    return capture is not None


def scoped_handler(handler):
    """Rebind a handler's sends without changing its shared plugin or host."""
    capture = HOOK_SENDS.get()
    owner = getattr(handler, "__self__", None)
    if capture is None or owner is None:
        return handler
    local = copy.copy(owner)
    context = getattr(owner, "context", None)
    if context is not None:
        object.__setattr__(local, "context", SendingContext(context, capture))
    if "astrbot_plugin_outputpro_split" in str(getattr(handler, "__module__", "")):
        # Only OutputPro's documented invocation dependencies carry another
        # Context handle. Keep locks, host services and unrelated state live.
        memo = {id(owner): local}

        def rebind(value):
            if value is context or value is capture.plugin.context:
                return SendingContext(value, capture)
            if id(value) in memo:
                return memo[id(value)]
            if isinstance(value, list):
                return [rebind(item) for item in value]
            if "astrbot_plugin_outputpro_split" not in type(value).__module__:
                return value
            cloned = copy.copy(value)
            memo[id(value)] = cloned
            for name in ("context", "pipeline", "plugin_config", "cfg", "config", "_steps", "_official_support"):
                if name in getattr(value, "__dict__", {}):
                    object.__setattr__(cloned, name, rebind(value.__dict__[name]))
            return cloned

        for name in ("cfg", "pipeline"):
            if name in getattr(owner, "__dict__", {}):
                object.__setattr__(local, name, rebind(owner.__dict__[name]))
    return handler.__func__.__get__(local, type(local))
