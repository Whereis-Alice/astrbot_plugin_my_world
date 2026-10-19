"""Normalize Provider token usage and collect totals for Web generation tasks."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator


def _number(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return max(0, number)


def normalized_usage(usage: Any) -> dict[str, Any]:
    other = _field(usage, "input_other")
    cached = _field(usage, "input_cached", "cached_tokens")
    if cached is None:
        details = (usage.get("prompt_tokens_details") or usage.get("input_tokens_details")) if isinstance(usage, Mapping) else (getattr(usage, "prompt_tokens_details", None) or getattr(usage, "input_tokens_details", None))
        cached = _field(details, "cached_tokens")
    input_total = _field(usage, "input", "prompt_tokens", "input_tokens")
    output = _field(usage, "output", "completion_tokens", "output_tokens")
    total = _field(usage, "total", "total_tokens")
    if other is None and input_total is not None:
        other = max(0, input_total - (cached or 0))
    if input_total is None and (other is not None or cached is not None):
        input_total = (other or 0) + (cached or 0)
    if input_total is None and total is not None and output is not None:
        input_total = max(0, total - output)
        other = max(0, input_total - (cached or 0))
    if total is None and (input_total is not None or output is not None):
        total = (input_total or 0) + (output or 0)
    return {"available": total is not None, "partial": total is not None and (input_total is None or output is None),
            "input": input_total, "input_other": other, "input_cached": cached,
            "output": output, "total": total}


def _field(value: Any, *names: str) -> int | None:
    for name in names:
        raw = value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)
        number = _number(raw)
        if number is not None:
            return number
    return None


@dataclass
class LLMUsage:
    """当前生成任务累计的 token 用量。"""

    responses: int = 0
    reported_responses: int = 0
    input_other: int = 0
    input_cached: int = 0
    output: int = 0
    tokens_total: int = 0
    incomplete: bool = False

    @property
    def input(self) -> int:
        return self.input_other + self.input_cached

    @property
    def total(self) -> int:
        return self.tokens_total

    def add(self, usage: Any) -> None:
        """记录一次模型响应；有些 Provider 不返回 usage，也要计入响应次数。"""

        self.responses += 1
        parsed = normalized_usage(usage)
        if not parsed["available"]:
            return
        self.reported_responses += 1
        self.input_other += parsed["input_other"] or 0
        self.input_cached += parsed["input_cached"] or 0
        self.output += parsed["output"] or 0
        self.tokens_total += parsed["total"] or 0
        self.incomplete = self.incomplete or parsed["partial"]

    def payload(self) -> dict[str, Any]:
        available = self.reported_responses > 0
        return {
            "available": available,
            "partial": available and (self.incomplete or self.reported_responses < self.responses),
            "responses": self.responses,
            "reported_responses": self.reported_responses,
            "input": self.input,
            "input_other": self.input_other,
            "input_cached": self.input_cached,
            "output": self.output,
            "total": self.total,
        }


_CURRENT_USAGE: ContextVar[LLMUsage | None] = ContextVar(
    "my_world_llm_usage",
    default=None,
)


@contextmanager
def collect_llm_usage(*, propagate: bool = False) -> Iterator[LLMUsage]:
    """收集当前异步任务里所有 LLM 响应的 usage。"""

    previous = _CURRENT_USAGE.get()
    usage = LLMUsage()
    token = _CURRENT_USAGE.set(usage)
    try:
        yield usage
    finally:
        _CURRENT_USAGE.reset(token)
        if propagate and previous is not None:
            for key in ("responses", "reported_responses", "input_other", "input_cached", "output", "tokens_total"):
                setattr(previous, key, getattr(previous, key) + getattr(usage, key))
            previous.incomplete = previous.incomplete or usage.incomplete


def record_llm_usage(value: Any) -> None:
    """Adapter 每拿到一份模型响应就调用；不在生成接口里时是空操作。"""

    current = _CURRENT_USAGE.get()
    if current is not None:
        current.add(value)
