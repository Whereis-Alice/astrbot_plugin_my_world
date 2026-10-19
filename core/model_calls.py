"""Task-local model call attribution. Prompts and responses are never stored here."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Any, Iterator
from uuid import uuid4


_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar("mw_model_call", default=None)


def call_context() -> dict[str, Any]:
    return dict(_CONTEXT.get() or {})


@contextmanager
def model_context(purpose: str, reason: str, session_id: str = "") -> Iterator[dict[str, Any]]:
    previous = call_context()
    chain = [*previous.get("chain", [])]
    if reason and (not chain or chain[-1] != reason):
        chain.append(reason)
    current = {"trace_id": previous.get("trace_id") or uuid4().hex,
               "purpose": purpose, "reason": reason, "chain": chain[-8:],
               "session_id": session_id or previous.get("session_id", "")}
    token = _CONTEXT.set(current)
    try:
        yield current
    finally:
        _CONTEXT.reset(token)


def model_operation(purpose: str, reason: str):
    """Attribute all nested calls without changing engine/port signatures."""
    def decorate(function):
        @wraps(function)
        async def run(*args, **kwargs):
            session = str(kwargs.get("session_id") or "")
            for value in [*args[1:2], kwargs.get("state"), kwargs.get("ctx"), kwargs.get("event")]:
                if value is None:
                    continue
                candidate = value if isinstance(value, str) else (
                    getattr(value, "session_id", "") or getattr(value, "unified_msg_origin", ""))
                if candidate:
                    session = session or str(candidate)
                    break
            with model_context(purpose, reason, session):
                return await function(*args, **kwargs)
        return run
    return decorate
