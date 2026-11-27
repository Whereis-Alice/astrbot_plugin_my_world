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
                   recorded_inner: str = "", generation_error: str = "", strict_reply: bool = False,
                   require_say: bool = False) -> SpeechDecision:
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
    inner = " ".join(inner.split())
    say, reply_to = parse_participation(raw, targets)
    intent = payload.get("intent") or (default_intent if say else "listen")
    # Normalize only inside a protocol that permits a grounded continuation.
    if intent == "join_chat" and "join_chat" not in allowed and "continue_chat" in allowed:
        intent = "continue_chat"
    if intent not in allowed:
        return SpeechDecision(inner=inner, error="发言意图不在本轮允许范围")
    if intent in {"life_share", "outreach"} and payload.get("reply_to"):
        return SpeechDecision(inner=inner, error="接消息需要选择续聊意图，不能借生活分享或找人追答")
    if strict_reply and intent != "listen" and payload.get("reply_to") and str(payload["reply_to"]) not in targets:
        return SpeechDecision(inner=inner, error="引用不在本次可回应的消息范围")
    if intent == "continue_chat" and not reply_to:
        return SpeechDecision(inner=inner, error="续聊需要引用本轮仍可回应的消息")
    if require_say and intent != "listen" and not say:
        return SpeechDecision(inner=inner, error="选择了接话却没有有效正文")
    if intent == "listen":
        say, reply_to = "", ""
    motivation = payload.get("motivation") or reasoning.get("intent") or ""
    motivation = " ".join(motivation.split())[:240] if isinstance(motivation, str) else ""
    return SpeechDecision(inner, say, reply_to, intent,
                          who=str(reasoning.get("who") or "")[:100],
                          motivation=motivation)


def decision_output(allowed: tuple[str, ...]) -> str:
    instructions = {
        "join_chat": "join_chat 顺着仍在进行的话题接一句，顺接全场时可以不引用。",
        "continue_chat": "continue_chat 接一条具体消息，必须把它的 can_reply=true 编号填入 reply_to。",
        "listen": "listen 保留想法，say 和 reply_to 都留空。",
    }
    return (
        '\n# 想法与发言\n只输出一个 JSON 对象：{"inner":"","intent":"","say":"","motivation":"","reply_to":""}。'
        "intent 只能选 " + "、".join(allowed) + "。" + "".join(instructions[intent] for intent in allowed)
        + "接较早的一句或需要明确对象时引用；reply_to 仅选 can_reply=true 的编号。"
        "inner 写此刻自己心里真实闪过的一句话，别写分析步骤、系统规则或台词复述。"
        "motivation 简短说明是哪一点让你想接或继续听。"
        "说出口的话只写在 say 里，直接写会发出去的正文；内心、旁白和动作说明留在正文之外。\n"
    )
