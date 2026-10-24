"""Optional platform group metadata; never substitute a member's nickname."""
from __future__ import annotations

import asyncio
import time
from typing import Any


class GroupNamesMixin:
    GROUP_NAMES_TIMEOUT = 3
    GROUP_NAMES_TTL = 300
    GROUP_NAMES_FAILURE_TTL = 60

    def _group_names_client(self, platform_id: str, sessions: list[str]) -> Any:
        getter = getattr(self.plugin.context, 'get_platform_inst', None)
        if callable(getter):
            platform = getter(platform_id)
            client = getattr(platform, 'get_client', None)
            if callable(client):
                return client()
        # Older hosts may only expose clients on events. Use an exact origin,
        # never last_event_any/nearby_event from a different adapter or bot.
        for sid in sessions:
            event = self.plugin.last_event(sid)
            if event is not None:
                return getattr(event, 'bot', None)
        return None

    async def fetch_group_names(self, session_ids: list[str]) -> dict[str, str]:
        grouped: dict[str, list[str]] = {}
        for sid in session_ids:
            parts = str(sid).split(':', 2)
            if len(parts) == 3 and parts[1] == 'GroupMessage':
                grouped.setdefault(parts[0], []).append(sid)
        # Each adapter gets one bounded read, shared by simultaneous page loads.
        results = await asyncio.gather(*(self._platform_group_names(pid, sids)
                                         for pid, sids in grouped.items()))
        return {sid: name for result in results for sid, name in result.items()}

    async def _platform_group_names(self, platform_id: str, sessions: list[str]) -> dict[str, str]:
        try:
            bot = self._group_names_client(platform_id, sessions)
        except Exception:
            return {}
        action = getattr(bot, 'call_action', None)
        if not callable(action):
            return {}
        if not hasattr(self, '_group_names_cache'):
            self._group_names_cache: dict[str, tuple[Any, float, dict[str, str]]] = {}
            self._group_names_locks: dict[str, asyncio.Lock] = {}
        lock = self._group_names_locks.setdefault(platform_id, asyncio.Lock())
        async with lock:
            cached = self._group_names_cache.get(platform_id)
            if cached is not None and cached[0] is bot and time.monotonic() < cached[1]:
                names = cached[2]
            else:
                names = {}
                ttl = self.GROUP_NAMES_TTL
                try:
                    rows = await asyncio.wait_for(action('get_group_list', no_cache=True),
                                                  timeout=self.GROUP_NAMES_TIMEOUT)
                    if not isinstance(rows, list):
                        raise ValueError('Invalid group metadata')
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        gid = str(row.get('group_id') or '').strip()
                        name = ' '.join(str(row.get('group_name') or '').split())[:40]
                        if gid and name:
                            names[gid] = name
                except Exception as exc:
                    ttl = self.GROUP_NAMES_FAILURE_TTL
                    self.plugin.logger.debug(f'[my_world] 读取群名称失败：{type(exc).__name__}')
                self._group_names_cache[platform_id] = (bot, time.monotonic() + ttl, names)
        return {sid: names[sid.split(':', 2)[2]] for sid in sessions
                if sid.split(':', 2)[2] in names}
