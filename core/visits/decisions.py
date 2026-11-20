"""Local decision failures have fixed diagnostics, never provider response bodies."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Literal
import copy


_CANDIDATE: ContextVar[dict | None] = ContextVar("world_visit_decision_candidate", default=None)


@contextmanager
def capture_decision_candidate() -> Iterator[dict]:
    """Keep a parsed result available when the outer deadline cancels verification."""
    captured = {}
    token = _CANDIDATE.set(captured)
    try:
        yield captured
    finally:
        _CANDIDATE.reset(token)


def record_decision_candidate(candidate: dict) -> None:
    captured = _CANDIDATE.get()
    if captured is not None:
        captured["candidate"] = copy.deepcopy(candidate)


FAILURES = {
    "budget": "今日串门模型预算已用完，稍后再约",
    "busy": "后台模型通道正在忙，稍后再约",
    "backoff": "后台模型正在等待重试，稍后再约",
    "timeout": "等待串门模型回应超时，这次联系结束",
    "provider": "串门模型暂时不可用，请检查本地模型配置与调用记录",
    "format": "串门模型返回的选择不完整或格式无效，这次联系结束",
    "life_busy": "她暂时在处理眼前的事，稍后继续相处",
    "state": "暂时无法确认接待状态，这次联系结束",
    "identity": "生活归属、角色或人设已变化，这次联系结束",
}

RETRYABLE = frozenset({"busy", "backoff", "timeout", "provider", "format", "life_busy", "state"})
WAITING = {
    "busy": "模型通道暂时忙", "backoff": "模型正在冷却",
    "timeout": "这次模型回应超时", "provider": "模型暂时不可用",
    "format": "这次模型选择不完整", "life_busy": "她正在处理眼前的事",
    "state": "暂时无法确认接待状态",
}

# Only these structural labels may survive a failed model choice. Never save
# response bodies, arbitrary keys, provider errors or private thoughts here.
FORMAT_ISSUES = {
    "departure_fields": "收尾只提交本人一句自然告别：仅含say的JSON对象，say须为非空字符串且最多400字；不提交新动作、活动、朋友台词或任何回顾字段",
    "json_object": "没有得到完整的JSON对象",
    "invitation_accept": "邀请缺少真正的布尔值accept",
    "text_fields": "文字字段没有使用字符串",
    "leave_boolean": "leave没有使用布尔值",
    "scene_fields": "scene不是包含文字字段的对象",
    "choice_boolean": "活动选择没有使用布尔值",
    "pause_number": "停留时间没有使用有限数字",
    "empty_choice": "没有留下本阶段可执行的选择",
    "action_step_fields": "action_step的字段不成立：description须为1至400字的具体本人动作，seconds须为0.05至14400的有限数字，waiting和requires_peer须为布尔值。peer_action_id只在明确加入现有协作时使用，并须为当前准确ID。没有新动作时省略action_step或用空对象{}，不要把非空的坏动作当成没有动作",
    "action_control_fields": "action_control的字段不成立：action_id须为当前准确动作ID，operation须为cancel或decline字符串，可选reason为不超过160字的字符串。没有取消或拒绝时省略action_control或用空对象{}，不能替朋友取消独立动作",
    "scene_update_fields": "scene_update的字段不成立：summary须为1至500字字符串，action_ids须为1至12个不重复的当前准确ID；objects和pending若提供，均须为最多8项的列表，每项为1至160字字符串，done须为布尔值。不结算时省略scene_update或用空对象{}；不改变的objects或pending可省略沿用原记录",
    "scene_update_capacity": "scene_update的objects或pending列表超过8项。每份列表最多8项，每项最多160字；依已确认事实自主归并有明确关联的条目，保留仍相关的事实，或省略不改变的列表。不能靠裁剪动作ID、补造结果或宣称旧物件消失来修复容量",
    "cooperation_flags": "加入已有协作时，peer_action_id须引用朋友的awaiting_peer动作，同时requires_peer必须为true；这表示你本人愿意配合该共同动作，不是替朋友同意，也不是另发新请求",
    "action_references": "这次动作引用或执行条件不成立。普通动作须等自己的同类动作结束，或先用action_control取消；新动作时长须能在双方已约定的归家时间前完成。协作加入、取消和结算须引用当前可用的准确动作ID",
    "action_time": "这一步的固有耗时超过双方已约定归家前剩余的可用时间，协作还须容得下所引用朋友动作的完整耗时。保留未完打算、自然结束或另选真实可行的短步骤；不要压缩实际耗时、延长归家或宣称动作已经完成",
    "action_capacity": "你在当前活动已有6个尚未确认结果或放弃的动作，没有位置再开始新动作。elapsed只表示时间已到，仍占未决位置。东道主可依据ready_action_ids自主用scene_update确认有依据的结果；本人也可明确用action_control取消不再继续的具体动作。可以先回应朋友或安静继续，不能自动宣称旧动作成功或丢弃旧选择",
}


class VisitDecisionError(ValueError):
    """Expose only a known failure category as the trip's ending reason."""

    def __init__(self, category: Literal["budget", "busy", "backoff", "timeout", "provider", "format", "life_busy", "state", "identity"],
                 *, request_started: bool = True, candidate: dict | None = None, format_issue: str = "",
                 repair_text: str = ""):
        self.category = category if category in FAILURES else "provider"
        self.request_started = request_started
        self.candidate = copy.deepcopy(candidate) if isinstance(candidate, dict) else None
        self.format_issue = (format_issue if self.category == "format" and isinstance(format_issue, str)
                             and format_issue in FORMAT_ISSUES else "")
        # Private correction input is neither an executable candidate nor a trip fact.
        self.repair_text = repair_text[:8000] if self.category == "format" and isinstance(repair_text, str) else ""
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
