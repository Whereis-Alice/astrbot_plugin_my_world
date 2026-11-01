"""Cancellation-safe admission and settlement for autonomous model calls."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from collections.abc import Callable, Iterator
from typing import Any

from .llm_usage import LLMUsage


@dataclass
class BackgroundAdmission:
    started: bool = False
    deferred: bool = False
    reason: str = ""
    next_at: float = 0.


_ADMISSION: ContextVar[BackgroundAdmission | None] = ContextVar("world_background_admission", default=None)


def current_background_admission() -> BackgroundAdmission | None:
    return _ADMISSION.get()


@contextmanager
def capture_background_admission() -> Iterator[BackgroundAdmission]:
    """Keep unsent budget deferral distinct from an actual model decision."""
    admission = BackgroundAdmission()
    token = _ADMISSION.set(admission)
    try:
        yield admission
    finally:
        _ADMISSION.reset(token)


def record_background_admission(*, started: bool = False, deferred: bool = False,
                                reason: str = "", next_at: float = 0.) -> None:
    admission = _ADMISSION.get()
    if admission is not None:
        admission.started |= started
        admission.deferred |= deferred
        if deferred:
            admission.reason, admission.next_at = reason, next_at


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

    def __init__(self, db: Any, day: str, *, calls: int, tokens: int, reserve: int,
                 window: dict[str, Any] | None = None, on_change: Callable[[], None] | None = None) -> None:
        self.db, self.day, self.reserve = db, day, reserve
        self.calls, self.tokens = calls, tokens
        self.window = window
        self.on_change = on_change
        self.allowed = False
        self.reason = ''
        self.next_at = 0.
        self.started = False
        self.usage: LLMUsage | None = None

    def _changed(self) -> None:
        if self.on_change is not None:
            self.on_change()

    async def __aenter__(self) -> BackgroundReservation:
        reservation = asyncio.create_task(self.db.call('reserve_background', self.day,
            calls=self.calls, tokens=self.tokens, reserve=self.reserve, window=self.window))
        try:
            budget = await asyncio.shield(reservation)
        except asyncio.CancelledError:
            # Cancelling to_thread cannot stop a write already running. Wait for
            # its actual result and refund exactly this unstarted reservation.
            async def withdraw() -> None:
                budget = await reservation
                if budget['allowed']:
                    await self.db.call('release_background', self.day, reserve=self.reserve)
                    self._changed()

            await _finish_write(asyncio.create_task(withdraw()))
            raise
        self.allowed, self.reason = budget['allowed'], budget.get('reason', '')
        self.next_at = float(budget.get('next_at') or 0)
        if self.allowed:
            self._changed()
        else:
            record_background_admission(deferred=True, reason=self.reason, next_at=self.next_at)
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
        async def settle() -> None:
            await write
            self._changed()

        await _finish_write(asyncio.create_task(settle()))
