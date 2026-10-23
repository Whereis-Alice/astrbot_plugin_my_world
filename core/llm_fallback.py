"""Decide whether a failed role may use a distinct fallback provider."""
from __future__ import annotations

from typing import Any


async def fallback_block_reason(primary: Any, fallback: Any, session_id: str, error: Any = "") -> str:
    """Return a reason to stop, without issuing a model request or changing input."""
    message = str(error or "").lower()
    if any(marker in message for marker in (
        "prohibited_content", "prompt_blocked", "content_filter", "content_policy_violation",
    )):
        return "模型已明确拒绝本次内容"
    if fallback is None:
        return "没有可用的回退模型"
    if primary is fallback:
        return "主选和回退是同一模型通道"
    resolve_primary = getattr(primary, "resolve_provider_id", None)
    resolve_fallback = getattr(fallback, "resolve_provider_id", None)
    if callable(resolve_primary) and callable(resolve_fallback):
        try:
            first = await resolve_primary(session_id)
            second = await resolve_fallback(session_id)
            if first and first == second:
                return "主选和回退指向同一个 Provider"
        except Exception:
            pass  # Custom/unavailable resolution must not invent an identity.
    return ""
