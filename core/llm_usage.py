"""LLM token 用量统计：只在需要汇总的一次 Web 生成请求里生效。"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator


def _number(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, number)


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

    @property
    def input(self) -> int:
        return self.input_other + self.input_cached

    @property
    def total(self) -> int:
        return self.input + self.output

    def add(self, usage: Any) -> None:
        """记录一次模型响应；有些 Provider 不返回 usage，也要计入响应次数。"""

        self.responses += 1
        if usage is None:
            return
        input_other = _field(usage, "input_other", "prompt_tokens")
        input_cached = _field(usage, "input_cached", "cached_tokens")
        output = _field(usage, "output", "completion_tokens", "output_tokens")
        total = _field(usage, "total", "total_tokens")
        if input_other is None and input_cached is None:
            prompt = _field(usage, "input", "prompt_tokens")
            if prompt is not None:
                input_other = prompt
        if input_other is None and total is not None:
            input_other = max(
                0,
                total - (output or 0) - (input_cached or 0),
            )
        if input_other is None and input_cached is None and output is None:
            return
        self.reported_responses += 1
        self.input_other += input_other or 0
        self.input_cached += input_cached or 0
        self.output += output or 0

    def payload(self) -> dict[str, Any]:
        available = self.reported_responses > 0
        return {
            "available": available,
            "partial": available and self.reported_responses < self.responses,
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
def collect_llm_usage() -> Iterator[LLMUsage]:
    """收集当前异步任务里所有 LLM 响应的 usage。"""

    usage = LLMUsage()
    token = _CURRENT_USAGE.set(usage)
    try:
        yield usage
    finally:
        _CURRENT_USAGE.reset(token)


def record_llm_usage(value: Any) -> None:
    """Adapter 每拿到一份模型响应就调用；不在生成接口里时是空操作。"""

    current = _CURRENT_USAGE.get()
    if current is not None:
        current.add(value)
