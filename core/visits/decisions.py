"""Local decision failures have fixed diagnostics, never provider response bodies."""
from __future__ import annotations

from typing import Literal
import copy


FAILURES = {
    "budget": "今日串门模型预算已用完，稍后再约",
    "busy": "后台模型通道正在忙，稍后再约",
    "backoff": "后台模型正在等待重试，稍后再约",
    "timeout": "等待串门模型回应超时，这次联系结束",
    "provider": "串门模型暂时不可用，请检查本地模型配置与调用记录",
    "format": "串门模型返回的选择不完整或格式无效，这次联系结束",
    "life_busy": "她暂时在处理眼前的事，稍后继续相处",
    "identity": "生活归属、角色或人设已变化，这次联系结束",
}

RETRYABLE = frozenset({"busy", "backoff", "timeout", "provider", "format", "life_busy"})
WAITING = {
    "busy": "模型通道暂时忙", "backoff": "模型正在冷却",
    "timeout": "这次模型回应超时", "provider": "模型暂时不可用",
    "format": "这次模型选择不完整", "life_busy": "她正在处理眼前的事",
}


class VisitDecisionError(ValueError):
    """Expose only a known failure category as the trip's ending reason."""

    def __init__(self, category: Literal["budget", "busy", "backoff", "timeout", "provider", "format", "life_busy", "identity"],
                 *, request_started: bool = True, candidate: dict | None = None):
        self.category = category if category in FAILURES else "provider"
        self.request_started = request_started
        self.candidate = copy.deepcopy(candidate) if isinstance(candidate, dict) else None
        super().__init__(FAILURES[self.category])

    @classmethod
    def unavailable(cls, error: object = "") -> VisitDecisionError:
        message = str(error or "")
        if message in {"今日后台预算已用完，生活规则继续运行", "今日串门模型预算已用完，稍后再约",
                       "今日串门预算已用完，先休息一下，明天再联系友邻"}:
            return cls("budget", request_started=False)
        if message == "后台通道忙，留待下一次":
            return cls("busy", request_started=False)
        if message.startswith("后台模型暂时退避，规则继续运行"):
            return cls("backoff", request_started=False)
        return cls("provider")
