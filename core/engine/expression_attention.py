"""Reuse chat attention for action expressions that have no independent speech permission."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from ..background_budget import capture_background_admission, current_background_admission

if TYPE_CHECKING:
    from ..state import WorldState
    from . import MyWorldEngine
    from .models import TickOutcome


@asynccontextmanager
async def chat_expression_attention(engine: MyWorldEngine, state: WorldState, target: str,
                                    outcome: TickOutcome | None, rows: list[dict], *,
                                    required: bool = True, reuse: bool = True,
                                    completed: bool = False) -> AsyncIterator[bool]:
    """Charge an actual chat judgement once; life sharing and native replies opt out.

    An unstarted request restores only its own observation. Nested inference
    preserves the outer admission's budget result without borrowing its started
    flag for this expression.
    """
    if not required or outcome is None:
        yield True
        return
    now = engine._now()
    identity = (state.session_id, target)
    admitted = getattr(outcome, "_expression_attention", {})
    observation = state.participation.get(target) or {}
    accepted = outcome.accepted_speech_at.get(target)
    owner = outcome.receipt_owner
    if reuse and (identity in admitted and 0 <= now - admitted[identity] <= 120
            or accepted is not None and 0 <= now - accepted <= 120
            and (owner is None or owner.state_key == state.session_id)
            or observation.get("request_id") == outcome.speech_receipt_batch
            and observation.get("decision") == "considering"):
        yield True
        return
    rows = [row for row in rows if engine._chat_origin(row, state) == target
            and not row.get("is_self") and not row.get("internal")]
    with engine.config_scope(target):
        ready = bool(rows) and engine.participation_sample(state, target, rows, await engine._persona_text(target))
    if not ready:
        reason = (((state.participation.get(target) or {}).get("opportunity") or {}).get("reason")
                  if rows else "")
        if not reason:
            reason = "没有仍可回应的聊天现场，活动照常完成"
        outcome.notes.append(reason)
        yield False
        return
    observation = state.participation.setdefault(target, {})
    previous = {name: observation[name] for name in
                ("checked_at", "evaluated_at", "checked_seq", "attempts") if name in observation}
    observation.update(checked_at=now, evaluated_at=now,
                       checked_seq=max(int(row.get("seq") or 0) for row in rows),
                       request_id=outcome.speech_receipt_batch, decision="considering",
                       reason="正在考虑是否接话")
    observation["attempts"] = [at for at in observation.get("attempts", []) if 0 <= now - at < 3600][-119:] + [now]
    outer = current_background_admission()
    status, reason = "considered", "本轮接话判断已结束"
    with capture_background_admission() as admission:
        try:
            yield True
        except asyncio.CancelledError:
            status, reason = "cancelled", "接话推理已取消，等待最新现场"
            raise
        except Exception as exc:
            status, reason = "failed", f"接话判断未完成（{type(exc).__name__}）"
            raise
        finally:
            if outer is not None:
                outer.started |= admission.started
                outer.deferred |= admission.deferred
                if admission.deferred:
                    outer.reason, outer.next_at = admission.reason, admission.next_at
            if admission.started or completed:
                admitted = dict(getattr(outcome, "_expression_attention", {}))
                admitted[identity] = now
                outcome._expression_attention = admitted
            else:
                engine.restore_unstarted_participation(state, target, outcome.speech_receipt_batch,
                    previous, source="completion", browse_id="", started_at=now)
                if admission.deferred:
                    status, reason = "deferred", admission.reason or "模型尚未调用，留待新的现场"
            engine.finish_participation_observation(state, target, outcome.speech_receipt_batch, status, reason)


async def _attention_proof(engine, state, target, outcome, *, generation="") -> dict:
    from hashlib import sha256
    return {"target": target, "state_key": state.session_id, "at": engine._now(),
            "request_id": outcome.speech_receipt_batch, "generation": generation,
            "model_generation": (getattr(outcome, "_speech_generations", {}).get(target) or {}).get("id", ""),
            "self_seq": max([int(row.get("seq") or 0) for row in state.recent_chat
                if row.get("is_self") and engine._chat_origin(row, state) == target] or [0]),
            "persona": sha256((await engine._persona_text(target)).encode("utf-8")).hexdigest()}


async def _proof_current(engine, state, target, proof, *, allow_turn="", max_age=120) -> bool:
    from hashlib import sha256
    observation = state.participation.get(target) or {}
    if not isinstance(proof, dict):
        return False
    try:
        at = float(proof.get("at") or 0)
        self_seq = int(proof.get("self_seq") or 0)
    except (TypeError, ValueError, OverflowError):
        return False
    if (proof.get("target") != target
            or proof.get("state_key") != state.session_id or not proof.get("generation")
            or proof.get("request_id") != observation.get("request_id")
            or engine._now() < at or max_age is not None and engine._now() - at > max_age):
        return False
    persona = sha256((await engine._persona_text(target)).encode("utf-8")).hexdigest()
    if proof.get("persona") != persona:
        return False
    spoken = [row for row in state.recent_chat if row.get("is_self") and engine._chat_origin(row, state) == target]
    sent = max([float(observation.get("sent_at") or 0)] + [float(row.get("at") or 0) for row in spoken])
    newer = [row for row in spoken if float(row.get("at") or 0) > at or int(row.get("seq") or 0) > self_seq]
    if sent <= at and not newer:
        return True
    return bool(allow_turn and newer and max(float(row.get("at") or 0) for row in newer) >= sent
                and all(row.get("speech_turn") == allow_turn for row in newer))


async def admit_generated_chat(engine, state, target, outcome, rows, *, raw=None,
                               source="plan_choice") -> tuple[bool, dict, str]:
    """A returned chat choice spends attention, even when its trigger could share life."""
    from uuid import uuid4
    generation = getattr(outcome, "_speech_generations", {}).get(target) or {}
    matches = (raw is not None and generation.get("raw") is raw
               and generation.get("state_key") == state.session_id)
    if matches and generation.get("charged"):
        if await _proof_current(engine, state, target, generation.get("proof"), max_age=None):
            generation["proof"] = {**generation["proof"], "at": engine._now()}
            return True, generation["proof"], ""
        reason = "这次已生成台词的接话准入或真实现场已变化，取消发送"
        outcome.notes.append(reason)
        return False, {}, reason
    # This is a local check of existing output. It does not mark another model
    # call as started or borrow a different call's same-outcome/accepted tail.
    async with chat_expression_attention(engine, state, target, outcome, rows,
                                         reuse=False, completed=True) as allowed:
        pass
    observation = state.participation.get(target) or {}
    if not allowed:
        reason = (observation.get("opportunity") or {}).get("reason") or "没有仍可回应的现场"
        engine.decision_trace(target, "participation", "deferred", reason,
                              source=source, local_check=True)
        return False, {}, reason
    proof = await _attention_proof(engine, state, target, outcome,
        generation=generation.get("id") if matches else uuid4().hex)
    observation.update(attention_source=source, reason="已有台词通过本地接话节奏检查")
    if matches:
        generation.update(charged=True, proof=proof)
    return True, proof, ""


async def planned_chat_step_ready(engine, state, target, *, from_plan: bool) -> bool:
    """Old plans and later ticks cannot keep a previous fast continuation admission."""
    from ..speech_policy import PLAN_SPEECH, SPEECH_OUTCOME
    from .proactive_contact import CHAT_KINDS
    outcome, context = SPEECH_OUTCOME.get(), PLAN_SPEECH.get()
    if (not from_plan or outcome is None or outcome.speech_kind not in CHAT_KINDS
            or not context or context[0] is not outcome):
        return True
    plan = context[1]
    steps = plan.get("steps") or []
    index = int(plan.get("current_step") or 0)
    step = steps[index] if 0 <= index < len(steps) else {}
    scene = step.get("speech_scene") or plan.get("speech_scene") or {}
    checked = getattr(outcome, "_plan_attention_steps", {})
    identity = (state.session_id, target, plan.get("id"))
    previous = checked.get(identity) or {}
    current_generation = (getattr(outcome, "_speech_generations", {}).get(target) or {}).get("id", "")
    if (previous.get("model_generation") == current_generation and await _proof_current(
            engine, state, target, previous, allow_turn=outcome.speech_receipt_batch)):
        return True  # Adjacent segments of this actual delivery turn count once.
    proof = scene.get("attention")
    if not await _proof_current(engine, state, target, proof):
        allowed, proof, reason = await admit_generated_chat(engine, state, target, outcome,
            scene.get("rows") or [], source="stored_plan")
        if not allowed:
            await remember_blocked_plan_chat(engine, state, outcome, plan, target,
                reason=reason, drafts=step.get("messages") or [], source="stored_plan",
                reply_to=scene.get("reply_to", ""))
            return False
        scene["attention"] = proof
    proof = {**proof, "model_generation": current_generation}
    outcome._plan_attention_steps = {**checked, identity: proof}
    return True


async def remember_blocked_plan_chat(engine, state, outcome, plan, target, *, reason,
                                     drafts=(), source="plan_choice", reply_to="") -> None:
    """Attach rejected real wording to the plan's existing thought, without another inner."""
    from uuid import uuid4
    from .speech_receipts import update_thought
    trace = {"id": uuid4().hex, "thought_id": plan.get("thought_id") or plan.get("id"),
             "plan_id": plan.get("id"), "target": target, "trigger": "plan",
             "intent": (plan.get("speech_choice") or {}).get("intent") or "continue_chat",
             "origin": "continuation", "reply_to": reply_to, "status": "not_sent",
             "note": reason, "drafts": list(drafts), "sent": [], "at": engine._now(),
             "local_check": True, "attention_source": source}
    update_thought(state, trace)
    outcome.speech_decisions.append(trace)
    await engine._log_event(state, "speech_decision", trace, outcome=outcome, silent=True)
