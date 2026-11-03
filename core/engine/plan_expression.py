"""A life plan must choose speech independently of its social trigger."""
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, TYPE_CHECKING

from ..json_actions import extract_json_object, strip_reasoning
from ..speech_decision import parse_decision
from ..speech_policy import private_recipient

if TYPE_CHECKING:
    from . import MyWorldEngine
    from .models import TickOutcome
    from ..state import WorldState


async def choose_autonomous_plan(
    engine: MyWorldEngine, state: WorldState, outcome: TickOutcome,
    plan: dict[str, Any] | None, raw: str, *, target: str,
    options: tuple[list[str], list[dict[str, Any]], dict[str, str]],
    candidates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    allowed, rows, targets = options
    payload = extract_json_object(strip_reasoning(str(raw or ""))) or {}
    steps = (plan or {}).get("steps", [])
    lines = [line.strip() for step in steps if step.get("action") in {"say", "share"}
             for line in step.get("messages", []) if isinstance(line, str) and line.strip()]
    decision = parse_decision(json.dumps({**payload, "intent": payload.get("intent") or "listen",
        "say": "\n".join(lines)}, ensure_ascii=False), targets,
        allowed=tuple(allowed), strict_reply=True)
    available = {str(item["user_id"]) for item in candidates}
    named = payload.get("contact_people", [])
    named = named if isinstance(named, list) and all(isinstance(uid, str) for uid in named) else []
    selected = {uid.strip() for uid in named if uid.strip()}
    if not selected:
        # An explicit user action already names its recipient. Do not infer a
        # broadcast recipient from the social trigger or unrelated chat history.
        selected = {str(step.get("target") or "") for step in steps
            if (definition := engine.world.action_map().get(step.get("action"))) is not None
            and definition.target_type == "user" and step.get("target")}
    if decision.intent == "outreach" and (not selected or not selected <= available):
        decision.error = "主动联系必须选择本轮可联系的具体对象"
    if decision.error:
        outcome.notes.append("自主计划的开口选择无效，保留安静生活：" + decision.error)
    chat = decision.intent in {"join_chat", "continue_chat"} and not decision.error
    outreach = decision.intent == "outreach" and not decision.error
    life_share = decision.intent == "life_share" and not decision.error
    accepted = []
    for step in steps:
        definition = engine.world.action_map().get(step.get("action"))
        if definition is None:
            continue
        personal = (definition.id not in {"say", "share", "poke"}
            and definition.target_type in {"none", "node"}
            and definition.llm_level not in {"tool", "command"}
            and not definition.tool_list() and not definition.trigger_command
            and definition.on_complete.trigger in {"none", "llm_followup"})
        if personal:
            accepted.append({**step, "kind": "life_share" if life_share or outreach else "quiet"})
        elif chat and definition.id in {"say", "share"}:
            accepted.append({**step, "kind": "continuation", "target": "", "send_to": target})
        elif outreach:
            destination = str(step.get("send_to") or (plan or {}).get("send_to") or target)
            resolved = engine.resolve_send_to(state, destination, fallback="")
            recipient = private_recipient(resolved)
            person = str(step.get("target") or recipient)
            if (not resolved or (recipient and recipient not in selected)
                    or definition.target_type == "user" and person not in selected):
                outcome.notes.append("联系落点或动作对象不属于本轮所选对象，未发送该步骤")
                continue
            accepted.append({**step, "kind": "outreach", **({"target": person}
                if definition.target_type == "user" else {})})
        elif life_share and definition.target_type != "user":
            accepted.append({**step, "kind": "life_share"})
    if plan is None:
        if decision.inner:
            await engine.remember_speech_decision(state, target, decision,
                trigger="plan", outcome=outcome, drafts=[])
        return None
    plan["steps"] = accepted
    plan["contact_people"] = sorted(selected) if outreach else []
    plan["speech_choice"] = {"intent": decision.intent if not decision.error else "listen",
        "reply_to": decision.reply_to if chat else "", "target": target}
    if chat:
        plan["send_to"] = target
        plan["speech_scene"] = {"target": target, "rows": deepcopy(rows), "window": 900,
            "reply_to": decision.reply_to}
        for step in accepted:
            if step.get("kind") == "continuation":
                step["speech_scene"] = deepcopy(plan["speech_scene"])
    if not accepted:
        if decision.inner:
            await engine.remember_speech_decision(state, target, decision,
                trigger="plan", outcome=outcome, drafts=[])
        return None
    return plan
