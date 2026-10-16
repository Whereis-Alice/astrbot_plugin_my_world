"""Global defaults and explicit per-session configuration blocks."""

from __future__ import annotations

import contextlib
import contextvars
import copy
import functools
import inspect
from typing import Any

MAP_KEYS = frozenset({"zones", "zone_edges", "nodes", "edges"})
ACTION_KEYS = frozenset({"actions", "purged_actions"})
GLOBAL_KEYS = frozenset(
    {
        "admin_ids",
        "proactive_chat_paused",
        "wizard_done",
        "schema_version",
        "default_actions_disabled",
    }
)
BLOCK_NAMES = ("map", "actions", "schedules", "settings")


def world_block(world: dict[str, Any], block: str) -> dict[str, Any]:
    if block == "map":
        return {key: copy.deepcopy(value) for key, value in world.items() if key in MAP_KEYS}
    if block == "actions":
        return {key: copy.deepcopy(value) for key, value in world.items() if key in ACTION_KEYS}
    if block == "settings":
        excluded = MAP_KEYS | ACTION_KEYS | GLOBAL_KEYS
        return {key: copy.deepcopy(value) for key, value in world.items() if key not in excluded}
    raise ValueError(f"Unknown configuration block: {block}")


def effective_config(world, schedules, overrides):
    resolved = copy.deepcopy(world)
    for block in ("map", "actions", "settings"):
        if isinstance(overrides.get(block), dict):
            resolved.update(world_block(overrides[block], block))
    return resolved, copy.deepcopy(overrides.get("schedules", schedules))


class ScopedAttribute:
    """Resolve a configuration-dependent service in the current asyncio context."""

    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, instance, owner=None):
        if instance is None:
            return self
        return instance._scoped_runtime()[self.name]

    def __set__(self, instance, value):
        instance._scoped_runtime()[self.name] = value


class ScopedConfigMixin:
    world = ScopedAttribute()
    schedules = ScopedAttribute()
    dynamics = ScopedAttribute()
    memory = ScopedAttribute()
    profiles = ScopedAttribute()
    consolidator = ScopedAttribute()
    decider = ScopedAttribute()
    engagement = ScopedAttribute()
    prompts = ScopedAttribute()

    def _init_config_scopes(self):
        self._config_scope = contextvars.ContextVar("my_world_config_scope", default="")
        self._base_runtime = {}
        self._scope_runtimes = {}

    @property
    def config_scope_id(self) -> str:
        return self._config_scope.get()

    @contextlib.contextmanager
    def config_scope(self, session_id: str = ""):
        session_id = str(session_id or "")
        # Shared lives keep one stable configuration owner, even if it is disabled.
        if session_id and getattr(self, "_group_of", None):
            session_id = self.main_session(self.scope_session(session_id))
        token = self._config_scope.set(session_id)
        try:
            yield
        finally:
            self._config_scope.reset(token)

    def _scoped_runtime(self):
        session_id = self.config_scope_id
        if not session_id or not self._base_runtime.get("world"):
            return self._base_runtime
        session = self.session_config(session_id)
        overrides = getattr(session, "config_overrides", None) or {}
        if not overrides:
            return self._base_runtime
        if session_id not in self._scope_runtimes:
            from .models import parse_schedules, parse_world

            raw_world, raw_schedules = effective_config(
                self._base_runtime["world"].model_dump(mode="json", by_alias=True),
                self._base_runtime["schedules"].model_dump(mode="json"),
                overrides,
            )
            world, _ = parse_world(raw_world)
            schedules, _ = parse_schedules(raw_schedules)
            self._apply_extension_actions(world)
            self._scope_runtimes[session_id] = self._make_runtime(world, schedules)
        return self._scope_runtimes[session_id]

    def proactive_chat_allowed(self, session_id: str) -> bool:
        if bool(self._base_runtime["world"].proactive_chat_paused):
            return False
        with self.config_scope(session_id):
            return bool(self.world.proactive_chat_enabled)

    def speech_allowed(self, session_id: str, kind: str = "") -> bool:
        """这一条消息现在允许发出去吗。

        ``reply`` 永远放行（别人叫她，她当然能回）；``schedule`` / ``event``
        各有自己的开关（默认开，因为那是她生活的一部分）；其余（自主搭话、
        接话、早晚问候这些）看顶栏那个「自主搭话」总闸。
        """

        if kind == "reply":
            return True
        if kind == "schedule":
            return bool(
                getattr(self._base_runtime["world"], "schedule_speech_enabled", True)
            )
        if kind == "event":
            return bool(getattr(self._base_runtime["world"], "event_speech_enabled", True))
        return self.proactive_chat_allowed(session_id)

    def scoped_schedules(self):
        session = self.session_config(self.config_scope_id)
        overrides = getattr(session, "config_overrides", None) or {}
        if "schedules" in overrides:
            return copy.deepcopy(overrides["schedules"])
        return self.store.raw_schedules()

    def save_scoped_schedules(self, data, *, reason):
        session = self.session_config(self.config_scope_id)
        overrides = getattr(session, "config_overrides", None) or {}
        if "schedules" in overrides:
            self.store.save_scope_block(self.config_scope_id, "schedules", data, reason=reason)
        else:
            self.store.save_schedules(data, reason=reason)

    def editable_world(self):
        if self.config_scope_id:
            return self.world.model_dump(mode="json", by_alias=True)
        return self.store.raw_world()

    def save_scoped_settings(self, world, *, reason):
        if not self.config_scope_id:
            return self.store.save_world(world, reason=reason)
        session = self.session_config(self.config_scope_id)
        overrides = getattr(session, "config_overrides", None) or {}
        if "settings" not in overrides:
            raise ValueError("请先为此会话启用并保存独立设置")
        return self.store.save_scope_block(self.config_scope_id, "settings", world, reason=reason)


def session_scoped(function):
    """Bind entrypoints to their explicit session; restore context on every exit."""

    signature = inspect.signature(function)

    def scope(owner, args, kwargs):
        engine = getattr(owner, "engine", owner)
        if not hasattr(engine, "config_scope"):
            return contextlib.nullcontext()
        bound = signature.bind_partial(owner, *args, **kwargs).arguments
        session_id = bound.get("session_id")
        if session_id is None:
            for key in ("ctx", "event", "outcome"):
                value = bound.get(key)
                session_id = getattr(value, "unified_msg_origin", None) or getattr(value, "session_id", None)
                if session_id:
                    break
        current = engine.config_scope_id
        if current and session_id and engine.state_key(current) == engine.state_key(session_id):
            session_id = current
        return engine.config_scope(session_id or current)

    if inspect.isasyncgenfunction(function):
        @functools.wraps(function)
        async def stream(owner, *args, **kwargs):
            with scope(owner, args, kwargs):
                async with contextlib.aclosing(function(owner, *args, **kwargs)) as iterator:
                    async for item in iterator:
                        yield item
        return stream

    @functools.wraps(function)
    async def invoke(owner, *args, **kwargs):
        with scope(owner, args, kwargs):
            return await function(owner, *args, **kwargs)
    return invoke


def global_config(function):
    @functools.wraps(function)
    def invoke(self, *args, **kwargs):
        with self.config_scope():
            return function(self, *args, **kwargs)
    return invoke
