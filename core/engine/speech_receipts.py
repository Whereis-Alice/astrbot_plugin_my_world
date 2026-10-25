"""Persist actual speech at acceptance and project plan receipts by session."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

from ..speech_policy import PLAN_SPEECH
from .life_inference import STATE_LEASE


def bind_plan_speech(outcome, body, target):
    context = PLAN_SPEECH.get()
    if not context or context[0] is not outcome:
        return ""
    plan = context[1]
    if not plan.get("thought_id"):
        return ""
    trace = next((row for row in outcome.speech_decisions
        if row.get("thought_id") == plan["thought_id"] and row["target"] == target), None)
    if trace is None:
        trace = {"id": uuid4().hex, "thought_id": plan["thought_id"],
            "plan_id": plan["id"], "target": target, "trigger": "plan",
            "intent": "life_share", "origin": outcome.speech_kind,
            "motivation": str(plan.get("reason") or ""), "status": "prepared",
            "at": plan.get("created_ts", 0), "drafts": [], "sent": []}
        outcome.speech_decisions.append(trace)
    trace["drafts"].append(body)
    return trace["id"]


@asynccontextmanager
async def receipt_state(engine, session):
    lease = STATE_LEASE.get() or {}
    if (lease.get("owner") is asyncio.current_task() and not lease.get("released")
            and engine.state_key(lease["origin"]) == engine.state_key(session)):
        # Instant speech runs inside the life lock. Re-entering that lock would
        # deadlock, and loading a second state would overwrite the active plan.
        yield lease["state"]
        await engine.save_state(lease["state"])
    else:
        async with engine.session_state(session) as state:
            yield state


def update_thought(state, trace):
    for thought in state.thoughts:
        if trace.get("thought_id"):
            if thought.get("decision_id") != trace["thought_id"]:
                continue
            rows = thought.setdefault("detail", {}).setdefault("plan_speech", [])
            rows[:] = [r for r in rows if r.get("id") != trace["id"]]
            rows.append(dict(trace))
        else:
            speech = (thought.get("detail") or {}).get("speech") or {}
            if speech.get("id") != trace["id"]:
                continue
            thought["detail"]["speech"] = dict(trace)
            if trace["sent"]:
                thought["reply"] = "\n".join(trace["sent"])


async def remember_accepted(engine, outcome, target, text, key, identity=""):
    if key in outcome.remembered_speech:
        return
    async with receipt_state(engine, outcome.session_id) as state:
        recorded = state.note_chat(user_id="__self__",
            name=state.bot_current_nickname or state.bot_base_nickname or "你",
            text=text, now=engine._now(), keep=engine.chat_history_limit(),
            is_self=True, origin=target,
            event_id=f"speech:{outcome.speech_receipt_batch}:{key}")
        state.note_reply(text, session_id=target)
        if recorded is not None:
            recorded["speech_turn"] = outcome.speech_receipt_batch
        engine._tag_chat_origin(state, target, recorded)
        trace = next((r for r in outcome.speech_decisions if r["id"] == identity
                      and r["target"] == target), None)
        if trace is not None:
            sent = [*outcome.delivered_decisions.get(identity, []), text]
            trace.update(sent=sent, status="sent" if len(sent) == len(trace["drafts"]) else "partial")
            update_thought(state, trace)
    if trace is not None:
        outcome.delivered_decisions[identity] = sent
    outcome.remembered_speech.add(key)


def scoped_plan_speech(item, session):
    detail = dict(item.get("detail") or {})
    rows = [r for r in detail.pop("plan_speech", []) if r.get("target") == session]
    reply = str(item.get("reply") or "")
    if rows:
        sent = [line for row in rows for line in row.get("sent", [])]
        drafts = [line for row in rows for line in row.get("drafts", [])]
        status = ("sent" if len(sent) == len(drafts) else "partial") if sent else rows[-1]["status"]
        detail["speech"] = {**rows[-1], "sent": sent, "drafts": drafts, "status": status}
        reply = "\n".join(sent)
    return detail, reply


def scoped_thought(item, session):
    detail, reply = scoped_plan_speech(item, session)
    return {**item, "detail": detail, "reply": reply}
