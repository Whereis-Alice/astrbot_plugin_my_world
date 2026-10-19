"""Observe individual plugin requests and native final responses without altering them."""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any
from uuid import uuid4

from .llm_usage import normalized_usage, record_llm_usage
from .model_calls import call_context


def safe_error(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"(?i)(authorization[\"'\s]*[:=][\"'\s]*)(?:(?:bearer|basic)\s+)?[^\s,;\"']+", r"\1[已隐藏]", text)
    text = re.sub(r"(?i)(bearer\s+|(?:api[_-]?key|token)[\"'\s]*[:=][\"'\s]*)([^\s,;\"']+)", r"\1[已隐藏]", text)
    text = re.sub(r"\b(?:sk-|abk_)[A-Za-z0-9_-]{8,}", "[已隐藏]", text)
    text = re.sub(r"https?://[^\s<>\"']+", "[请求地址已隐藏]", text)
    return text[:600]


def model_name(plugin, provider_id: str, response=None) -> str:
    raw = getattr(response, "raw_completion", None)
    model = raw.get("model") if isinstance(raw, dict) else getattr(raw, "model", None)
    if model:
        return str(model)[:160]
    try:
        return str(plugin.context.get_provider_by_id(provider_id).get_model() or "")[:160]
    except Exception:
        return ""


async def save_call(plugin, *, response=None, error="", status="success", started_at: float,
                    duration_ms: int, session_id="", provider_id="", purpose="", reason="",
                    role="", trace_id="", attempt=1, retry_reason="", coverage="direct",
                    attribution=None) -> None:
    context = attribution if isinstance(attribution, dict) else call_context()
    usage = normalized_usage(getattr(response, "usage", None))
    row = {"started_at": started_at, "duration_ms": duration_ms,
           "session_id": session_id or context.get("session_id", ""),
           "provider_id": str(provider_id)[:160], "model": model_name(plugin, provider_id, response),
           "purpose": purpose or context.get("purpose") or role or "other",
           "reason": reason or context.get("reason") or "插件发起模型请求",
           "chain": context.get("chain", []), "role": role,
           "trace_id": trace_id or context.get("trace_id") or uuid4().hex,
           "attempt": attempt, "retry_reason": retry_reason, "status": status,
           "coverage": coverage, "usage": usage, "error": safe_error(error),
           "response_id": str(getattr(response, "id", "") or "")[:160]}
    try:
        await plugin.db.call("append_model_call", row)
    except Exception as exc:
        # Observability must never turn a successful conversation into a failure.
        logger = getattr(plugin, "logger", None)
        if logger:
            logger.debug(f"[my_world] 模型调用记录未能保存：{type(exc).__name__}")


async def tracked_generate(plugin, *, session_id="", purpose="", reason="", role="",
                           trace_id="", attempt=1, retry_reason="", coverage="direct",
                           invoke=None, **kwargs):
    started = time.time()
    clock = time.monotonic()
    response = None
    status, error = "success", ""
    try:
        response = await (invoke or plugin.context.llm_generate)(**kwargs)
        if getattr(response, "role", "") == "err":
            status, error = "error", "Provider 返回错误响应"
        record_llm_usage(getattr(response, "usage", None))
        return response
    except asyncio.CancelledError:
        status, error = "cancelled", "请求被取消或等待超时"
        raise
    except Exception as exc:
        status, error = "error", exc
        raise
    finally:
        await save_call(plugin, response=response, error=error, status=status,
            started_at=started, duration_ms=max(0, round((time.monotonic() - clock) * 1000)),
            session_id=session_id, provider_id=kwargs.get("chat_provider_id", ""),
            purpose=purpose, reason=reason, role=role, trace_id=trace_id,
            attempt=attempt, retry_reason=retry_reason, coverage=coverage)


async def observe_native_response(plugin, event, response, turn) -> None:
    """Public host hooks expose the final response, not intermediate Agent calls."""
    if (not isinstance(turn, dict) or response is None or getattr(response, "is_chunk", False)
            or turn.get("model_call_recorded") or plugin.in_self_initiated_call()
            or bool(getattr(event, "_profileweaver_auto_extract_running", False))):
        return
    turn["model_call_recorded"] = True
    provider = plugin.turn_provider_override(event.unified_msg_origin)
    if not provider:
        try:
            provider = await plugin.context.get_current_chat_provider_id(event.unified_msg_origin)
        except Exception:
            provider = ""
    started = float(turn.get("started_at") or time.time())
    await save_call(plugin, response=response, started_at=started,
        duration_ms=max(0, round((time.time() - started) * 1000)),
        session_id=event.unified_msg_origin, provider_id=provider, purpose="chat",
        reason="收到用户消息，由 AstrBot 原生 Agent 生成最终回复", role="main",
        attribution=turn.get("model_attribution"), coverage="native_final",
        status="error" if getattr(response, "role", "") == "err" else "success")
