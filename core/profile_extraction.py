"""Bounded background profile work, isolated from the native reply event."""

from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace
from typing import Any


class ProfileExtractionQueue:
    def __init__(self, service: Any, *, timeout: float = 60, max_pending: int = 16) -> None:
        self.service = service
        self.timeout = timeout
        self.max_pending = max_pending
        self.tasks: set[asyncio.Task] = set()
        self._slots = asyncio.Semaphore(2)
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._users: dict[tuple[str, str], int] = {}
        self._closed = False

    def schedule(self, event: Any, response: Any) -> bool:
        if (self._closed or not self.service.proactive_extraction_enabled
                or getattr(event, '_my_world_profile_extraction_queued', False)
                or getattr(event, '_profileweaver_llm_tool_used', False)
                or getattr(event, '_profileweaver_auto_extract_running', False)):
            return False
        text = str(getattr(event, 'message_str', '') or '').strip()
        if not self.service.is_profile_candidate_message(event, text):
            return False
        if len(self.tasks) >= self.max_pending:
            self.service.debug('自动画像队列已满，本条不另发模型请求')
            return False

        snapshot = copy.copy(event)
        snapshot.message_str = text
        for name in ('_extras', '_extra'):
            extras = getattr(event, name, None)
            if isinstance(extras, dict):
                setattr(snapshot, name, dict(extras))
        message = getattr(event, 'message_obj', None)
        if message is not None:
            snapshot.message_obj = copy.copy(message)
            sender = getattr(message, 'sender', None)
            if sender is not None:
                snapshot.message_obj.sender = copy.copy(sender)
        names = getattr(response, 'tools_call_name', []) or []
        observed = SimpleNamespace(tools_call_name=[names] if isinstance(names, str) else list(names))
        key = (self.service.event_session(snapshot) if self.service.session_based else '',
               str(snapshot.get_sender_id()))
        lock = self._locks.setdefault(key, asyncio.Lock())
        self._users[key] = self._users.get(key, 0) + 1

        async def run() -> None:
            try:
                async with asyncio.timeout(self.timeout):
                    async with lock:
                        async with self._slots:
                            if not self._closed:
                                await self.service.maybe_extract(snapshot, observed)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                self.service.debug('自动画像提取达到整轮时限，已结束等待')
            except Exception as exc:
                self.service.debug(f'后台画像提取失败：{type(exc).__name__}')

        task = asyncio.create_task(run(), name='my-world-profile-extraction')
        self.tasks.add(task)
        setattr(event, '_my_world_profile_extraction_queued', True)

        def finished(done: asyncio.Task) -> None:
            self.tasks.discard(done)
            self._users[key] -= 1
            if not self._users[key]:
                self._users.pop(key)
                self._locks.pop(key, None)

        task.add_done_callback(finished)
        return True

    async def close(self) -> None:
        self._closed = True
        current = asyncio.current_task()
        pending = [task for task in self.tasks if task is not current and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
