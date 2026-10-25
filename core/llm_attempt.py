"""Capture one text generation's failure without borrowing a shared last error."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from collections.abc import Iterator

from .model_call_runtime import safe_error


@dataclass
class GenerationAttempt:
    session_id: str
    error: str = ""


_ATTEMPT: ContextVar[GenerationAttempt | None] = ContextVar("world_generation_attempt", default=None)


@contextmanager
def capture_attempt(session_id: str) -> Iterator[GenerationAttempt]:
    attempt = GenerationAttempt(session_id)
    token = _ATTEMPT.set(attempt)
    try:
        yield attempt
    finally:
        _ATTEMPT.reset(token)


def record_attempt(session_id: str, error: object = "") -> str:
    """Use the same redaction for the local capture, diagnostics and logs."""
    message = safe_error(error)
    attempt = _ATTEMPT.get()
    if attempt is not None and attempt.session_id == session_id:
        attempt.error = message
    return message
