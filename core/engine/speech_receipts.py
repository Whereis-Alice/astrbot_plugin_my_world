"""Persist actual speech at acceptance and project plan receipts by session."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

from ..speech_policy import PLAN_SPEECH
from .life_inference import STATE_LEASE


def image_receipt_ref(ref):
    """Keep receipt identity without persisting large generated image payloads."""
    from hashlib import sha256
    from ..state import _chat_image_refs
    refs = _chat_image_refs([ref])
    return refs[0] if refs and not ref.startswith("base64://") else "sha256:" + sha256(ref.encode()).hexdigest()


def bind_plan_speech(outcome, body, target, *, image=False):
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
            "intent": "outreach" if outcome.speech_kind == "outreach" else "life_share", "origin": outcome.speech_kind,
            "motivation": str(plan.get("reason") or ""), "status": "prepared",
            "at": plan.get("created_ts", 0), "drafts": [], "sent": []}
        outcome.speech_decisions.append(trace)
    trace.setdefault("draft_images" if image else "drafts", []).append(image_receipt_ref(body) if image else body)
    return trace["id"]


def delivery_status(trace):
    if not trace.get("sent") and not trace.get("sent_images"):
        return "not_sent"
    return "sent" if (len(trace.get("sent", [])) == len(trace.get("drafts", []))
        and len(trace.get("sent_images", [])) == len(trace.get("draft_images", []))) else "partial"


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
            if thought.get("browse_id") and thought["browse_id"] == trace.get("browse_id"):
                thought["browse_replied"] = bool(trace["sent"] or trace.get("sent_images"))


def accept_receipt(engine, outcome, target, key, **payload):
    first = key not in outcome.accepted_receipts
    record = outcome.accepted_receipts.setdefault(key, {"at": engine._now(), "target": target, **payload})
    outcome.accepted_speech_at.setdefault(record["target"], record["at"])
    return first, record


async def remember_accepted(engine, outcome, target, text, key, identity="", *, kind=None):
    if key in outcome.remembered_speech:
        return
    first, record = accept_receipt(engine, outcome, target, key, type="text", text=text,
                                  identity=identity, kind=kind or outcome.speech_kind)
    target, text, identity, kind = (record[name] for name in ("target", "text", "identity", "kind"))
    stamp = record["at"]
    trace = next((r for r in outcome.speech_decisions if r["id"] == identity
                  and r["target"] == target), None)
    if trace is not None:
        if first:
            outcome.delivered_decisions.setdefault(identity, []).append(text)
        trace.update(sent=list(outcome.delivered_decisions.get(identity, [])))
        trace["status"] = delivery_status(trace)
    if first:
        engine.note_peer_chat_receipt(outcome, target, kind or outcome.speech_kind)

    async def persist():
        async with receipt_state(engine, outcome.session_id) as state:
            recorded = state.note_chat(user_id="__self__",
                name=state.bot_current_nickname or state.bot_base_nickname or "你",
                text=text, now=stamp, keep=engine.chat_history_limit(),
                is_self=True, origin=target,
                event_id=f"speech:{outcome.speech_receipt_batch}:{key}")
            state.note_reply(text, session_id=target)
            if recorded is not None:
                recorded["speech_turn"] = outcome.speech_receipt_batch
                recorded["speech_kind"] = kind or outcome.speech_kind
            engine._tag_chat_origin(state, target, recorded)
            if trace is not None:
                update_thought(state, trace)

    await persist_accepted(persist)
    outcome.remembered_speech.add(key)


async def remember_accepted_images(engine, outcome, target, images, key, identities, kinds):
    """Remember only platform-accepted references; never invent a caption or speech."""
    if not images or key in outcome.remembered_speech:
        return
    first, record = accept_receipt(engine, outcome, target, key, type="images", images=list(images),
                                  identities=list(identities), kinds=list(kinds))
    target, images, identities, kinds = (record[name] for name in ("target", "images", "identities", "kinds"))
    stamp = record["at"]
    traces = []
    for identity in dict.fromkeys(identities):
        trace = next((r for r in outcome.speech_decisions if r["id"] == identity
                      and r["target"] == target), None)
        if trace is None:
            continue
        sent = list(outcome.delivered_image_decisions.get(identity, []))
        if first:
            sent.extend(image_receipt_ref(ref) for ref, owner in zip(images, identities) if owner == identity)
        trace.update(sent_images=sent)
        trace["status"] = delivery_status(trace)
        traces.append(trace)
        outcome.delivered_image_decisions[identity] = sent
    if first:
        for kind in set(kinds):
            engine.note_peer_chat_receipt(outcome, target, kind)

    async def persist():
        async with receipt_state(engine, outcome.session_id) as state:
            notes = list(dict.fromkeys(outcome.image_notes.get(target, {}).get(ref, "") for ref in images))
            note = "、".join(value for value in notes if value)[:120]
            caption = f"［我发了{'一' if len(images) == 1 else len(images)}张图片：{note}］" if note else ""
            recorded = state.note_chat(user_id="__self__",
                name=state.bot_current_nickname or state.bot_base_nickname or "你",
                text=caption, images=images, has_images=True, raw_text="", now=stamp, keep=engine.chat_history_limit(),
                is_self=True, origin=target, event_id=f"speech:{outcome.speech_receipt_batch}:{key}")
            if recorded is not None:
                recorded["speech_turn"] = outcome.speech_receipt_batch
                recorded["speech_kind"] = kinds[0]
                recorded["image_count"] = len(images)
            engine._tag_chat_origin(state, target, recorded)
            for trace in traces:
                update_thought(state, trace)

    await persist_accepted(persist)
    outcome.remembered_speech.add(key)


async def persist_accepted(persist):
    try:
        await persist()
    except Exception:
        # One persistence retry uses the same receipt identity and never sends again.
        await persist()


async def remember_pending_receipts(engine, outcome):
    """Retry local history at finalization; platform delivery is never repeated."""
    for key, record in list(outcome.accepted_receipts.items()):
        if key in outcome.remembered_speech:
            continue
        if record["type"] == "text":
            await remember_accepted(engine, outcome, record["target"], record["text"], key,
                                    record["identity"], kind=record["kind"])
        else:
            await remember_accepted_images(engine, outcome, record["target"], record["images"], key,
                                           record["identities"], record["kinds"])


def scoped_plan_speech(item, session):
    detail = dict(item.get("detail") or {})
    rows = [r for r in detail.pop("plan_speech", []) if r.get("target") == session]
    reply = str(item.get("reply") or "")
    if rows:
        sent = [line for row in rows for line in row.get("sent", [])]
        drafts = [line for row in rows for line in row.get("drafts", [])]
        images = [ref for row in rows for ref in row.get("sent_images", [])]
        draft_images = [ref for row in rows for ref in row.get("draft_images", [])]
        speech = {**rows[-1], "sent": sent, "drafts": drafts, "sent_images": images, "draft_images": draft_images}
        speech["status"] = delivery_status(speech) if sent or images else rows[-1]["status"]
        detail["speech"] = speech
        reply = "\n".join(sent)
    return detail, reply


def scoped_thought(item, session):
    detail, reply = scoped_plan_speech(item, session)
    return {**item, "detail": detail, "reply": reply}
