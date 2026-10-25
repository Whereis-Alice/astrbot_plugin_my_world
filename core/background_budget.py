"""Cancellation-safe admission and settlement for autonomous model calls."""

from __future__ import annotations

import asyncio
from typing import Any

from .llm_usage import LLMUsage


async def _finish_write(task: asyncio.Task) -> Any:
    """Finish a short SQLite write even if shutdown cancels its awaiter again."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError()
    return result


class BackgroundReservation:
    """Refund only work that never entered the model channel; retain unknown usage."""

    def __init__(self, db: Any, day: str, *, calls: int, tokens: int, reserve: int) -> None:
        self.db, self.day, self.reserve = db, day, reserve
        self.calls, self.tokens = calls, tokens
        self.allowed = False
        self.reason = ''
        self.started = False
        self.usage: LLMUsage | None = None

    async def __aenter__(self) -> BackgroundReservation:
        reservation = asyncio.create_task(self.db.call('reserve_background', self.day,
            calls=self.calls, tokens=self.tokens, reserve=self.reserve))
        try:
            budget = await asyncio.shield(reservation)
        except asyncio.CancelledError:
            # Cancelling to_thread cannot stop a write already running. Wait for
            # its actual result and refund exactly this unstarted reservation.
            async def withdraw() -> None:
                budget = await reservation
                if budget['allowed']:
                    await self.db.call('release_background', self.day, reserve=self.reserve)

            await _finish_write(asyncio.create_task(withdraw()))
            raise
        self.allowed, self.reason = budget['allowed'], budget.get('reason', '')
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if not self.allowed:
            return
        if not self.started:
            write = self.db.call('release_background', self.day, reserve=self.reserve)
        else:
            usage = self.usage
            measured = (usage.total if usage and usage.responses
                and usage.reported_responses == usage.responses and not usage.incomplete else None)
            write = self.db.call('settle_background', self.day, reserve=self.reserve, measured=measured)
        await _finish_write(asyncio.create_task(write))
