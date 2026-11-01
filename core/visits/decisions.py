"""Local decision failures have fixed diagnostics, never provider response bodies."""
from __future__ import annotations

from typing import Literal


FAILURES = {
    "budget": "今日后台模型预算已用完，今天先留在自己的世界",
    "busy": "后台模型通道正在忙，稍后再约",
    "backoff": "后台模型正在等待重试，稍后再约",
    "timeout": "等待串门模型回应超时，这次联系结束",
    "provider": "串门模型暂时不可用，请检查本地模型配置与调用记录",
    "format": "串门模型返回的选择不完整或格式无效，这次联系结束",
}


class VisitDecisionError(ValueError):
    """Expose only a known failure category as the trip's ending reason."""

    def __init__(self, category: Literal["budget", "busy", "backoff", "timeout", "provider", "format"]):
        self.category = category if category in FAILURES else "provider"
        super().__init__(FAILURES[self.category])

    @classmethod
    def unavailable(cls, error: object = "") -> VisitDecisionError:
        message = str(error or "")
        if message == "今日后台预算已用完，生活规则继续运行":
            return cls("budget")
        if message == "后台通道忙，留待下一次":
            return cls("busy")
        if message.startswith("后台模型暂时退避，规则继续运行"):
            return cls("backoff")
        return cls("provider")
