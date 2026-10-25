"""A character's short thought and speech choice, separate from its trigger."""
from __future__ import annotations

from dataclasses import dataclass

from .json_actions import extract_json_object, strip_reasoning
from .participation import parse_participation


@dataclass
class SpeechDecision:
    inner: str = ""
    say: str = ""
    reply_to: str = ""
    intent: str = ""
    error: str = ""
    who: str = ""
    motivation: str = ""


def parse_decision(raw: str, targets: dict[str, str], *, default_intent: str = "join_chat",
                   allowed: tuple[str, ...] = ("join_chat", "listen"),
                   recorded_inner: str = "", generation_error: str = "") -> SpeechDecision:
    """Reuse a validated reading thought; a speech choice still requires valid JSON."""
    if generation_error:
        from .model_call_runtime import safe_error
        return SpeechDecision(error="发言判断未完成：" + safe_error(generation_error))
    if not str(raw or "").strip():
        return SpeechDecision(error="模型返回空内容，未取得发言决定")
    payload = extract_json_object(strip_reasoning(str(raw or "")))
    if not isinstance(payload, dict):
        return SpeechDecision(error="没有返回完整的想法与发言决定")
    reasoning = payload.get("reasoning")
    reasoning = reasoning if isinstance(reasoning, dict) else {}
    inner = recorded_inner or payload.get("inner", reasoning.get("inner", ""))
    if not isinstance(inner, str) or not inner.strip() or len(inner.strip()) > 240:
        return SpeechDecision(error="缺少有效的简短内心活动")
    say, reply_to = parse_participation(raw, targets)
    intent = payload.get("intent") or (default_intent if say else "listen")
    # Normalize only inside a protocol that permits a grounded continuation.
    if intent == "join_chat" and "join_chat" not in allowed and "continue_chat" in allowed:
        intent = "continue_chat"
    if intent not in allowed:
        return SpeechDecision(inner=" ".join(inner.split()), error="发言意图不在本轮允许范围")
    if intent == "continue_chat" and not reply_to:
        return SpeechDecision(inner=" ".join(inner.split()), error="续聊需要引用本轮仍可回应的消息")
    if intent == "listen":
        say, reply_to = "", ""
    motivation = payload.get("motivation") or reasoning.get("intent") or ""
    motivation = " ".join(motivation.split())[:240] if isinstance(motivation, str) else ""
    return SpeechDecision(" ".join(inner.split()), say, reply_to, intent,
                          who=str(reasoning.get("who") or "")[:100],
                          motivation=motivation)


DECISION_OUTPUT = (
    '\n# 想法与发言\n只输出 JSON：{"inner":"此刻具体的一句心里话",'
    '"intent":"join_chat 或 listen","say":"准备说的一句短话，没有就留空",'
    '"motivation":"为什么想接话或继续旁听，具体而简短的一句",'
    '"reply_to":"可引用编号或空字符串"}。'
    'inner 必须有内容，是符合人设的短暂感受，不是分析步骤、系统规则或台词复述；'
    '即使没有开口，也保留这次看过消息的真实想法。不要编共同经历或未看到的画面。'
    'inner 不会发给聊天对象，say 才是消息正文。\n'
)
