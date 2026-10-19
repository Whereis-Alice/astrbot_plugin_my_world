"""Bound only My World's disposable sandboxes; QQ computer sessions are untouched."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import importlib
import inspect
import time


PREFIX = "my_world_life:"


@dataclass
class _Lease:
    key: str
    booter: object = None
    active: bool = False
    last_used: float = 0
    idle_seconds: int = 300
    timer: object = None


class LifeSandboxPool:
    def __init__(self, *, client=None, logger=None):
        self._client = client
        self._logger = logger
        self._entries: dict[str, _Lease] = {}
        self._lock = asyncio.Lock()
        self._cleanup_tasks: set[asyncio.Task] = set()
        self._closed = False
        self._adopted = False

    def _native(self):
        if self._client is None:
            self._client = importlib.import_module("astrbot.core.computer.computer_client")
        return self._client

    def _spawn(self, coroutine):
        task = asyncio.create_task(coroutine, name="my_world:sandbox_cleanup")
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    def _schedule(self, entry, seconds):
        if entry.timer:
            entry.timer.cancel()
        entry.timer = asyncio.get_running_loop().call_later(
            seconds, lambda: self._spawn(self._expire(entry)))

    async def _retire(self, entry):
        if entry.timer:
            entry.timer.cancel()
            entry.timer = None
        client = self._native()
        try:
            # Neo's default shutdown closes only its HTTP client. Request disposal
            # of OUR project, while leaving the shared Bay service running.
            shutdown = entry.booter.shutdown
            kwargs = {"delete_sandbox": True} if "delete_sandbox" in inspect.signature(shutdown).parameters else {}
            async with asyncio.timeout(15):
                await shutdown(**kwargs)
        except asyncio.CancelledError:
            if not self._closed:
                self._schedule(entry, 30)
            raise
        except Exception as exc:
            if self._logger:
                self._logger.warning(f"[my_world] 生活沙盒回收待重试：{type(exc).__name__}")
            if not self._closed:
                self._schedule(entry, 30)
            return False
        if client.session_booter.get(entry.key) is entry.booter:
            clear = getattr(client, "_clear_cua_idle_state", None)
            if callable(clear):
                clear(entry.key)
            client.session_booter.pop(entry.key, None)
        if self._entries.get(entry.key) is entry:
            self._entries.pop(entry.key, None)
        return True

    async def _expire(self, entry):
        async with self._lock:
            if self._entries.get(entry.key) is entry and not entry.active:
                await self._retire(entry)

    async def _cancelled_boot(self, entry, task):
        # get_booter() does not clean up CancelledError in every supported SDK.
        # Let native startup finish and dispose of it; retain its slot meanwhile.
        try:
            booter = await task
        except BaseException:
            async with self._lock:
                if self._entries.get(entry.key) is entry:
                    self._entries.pop(entry.key, None)
            return
        async with self._lock:
            entry.booter, entry.active = booter, False
            await self._retire(entry)

    @asynccontextmanager
    async def lease(self, key, context, *, maximum=1, idle_seconds=300):
        if not key.startswith(PREFIX):
            raise ValueError("生活沙盒只能管理自己的独立会话")
        async with self._lock:
            if self._closed:
                raise RuntimeError("生活沙盒管理器已停止")
            client = self._native()
            if not self._adopted:
                self._adopted = True
                # Recover prior My World sessions after a hot reload. Never adopt
                # an ordinary QQ session or change native/global computer config.
                for old_key, booter in list(client.session_booter.items()):
                    if old_key.startswith(PREFIX):
                        old = _Lease(old_key, booter, last_used=time.monotonic(), idle_seconds=idle_seconds)
                        self._entries[old_key] = old
                        if idle_seconds:
                            self._schedule(old, idle_seconds)
            entry = self._entries.get(key)
            if entry and entry.active:
                raise RuntimeError("这个生活沙盒仍在执行，留待下一次")
            maximum = max(1, maximum)
            # A lowered setting takes effect without interrupting an active task.
            target = maximum if entry else maximum - 1
            for old in sorted(list(self._entries.values()), key=lambda row: row.last_used):
                if len(self._entries) <= target:
                    break
                if old is not entry and not old.active:
                    await self._retire(old)
            if not entry and len(self._entries) >= maximum:
                raise RuntimeError("生活沙盒名额正在使用或等待回收，留待下一次")
            if sum(row.active for row in self._entries.values()) >= maximum:
                raise RuntimeError("生活沙盒运行名额已满，留待下一次")
            if entry is None:
                entry = _Lease(key)
                self._entries[key] = entry
            if entry.timer:
                entry.timer.cancel()
                entry.timer = None
            entry.active, entry.idle_seconds = True, idle_seconds
            task = asyncio.create_task(client.get_booter(context, key), name="my_world:sandbox_boot")
            try:
                entry.booter = await asyncio.shield(task)
            except asyncio.CancelledError:
                self._spawn(self._cancelled_boot(entry, task))
                raise
            except BaseException:
                self._entries.pop(key, None)
                raise
        try:
            yield entry.booter
        finally:
            async with self._lock:
                # Native tools may renew an expired sandbox under the same UMO.
                # Dispose of that replacement rather than a stale client.
                current = self._native().session_booter.get(key)
                if current is not None:
                    entry.booter = current
                entry.active, entry.last_used = False, time.monotonic()
                if self._closed:
                    await self._retire(entry)
                elif idle_seconds:
                    self._schedule(entry, idle_seconds)

    async def close(self):
        async with self._lock:
            self._closed = True
            for entry in list(self._entries.values()):
                if entry.timer:
                    entry.timer.cancel()
                    entry.timer = None
                if not entry.active:
                    await self._retire(entry)
        # A native startup still in flight keeps its slot and cleanup callback.
        # Do not cancel it and orphan an unregistered VM during plugin unload.
        pending = list(self._cleanup_tasks)
        if pending:
            done, _ = await asyncio.wait(pending, timeout=15)
            for task in done:
                task.result()
