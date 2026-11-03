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
                                    required: bool = True) -> AsyncIterator[bool]:
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
    if (identity in admitted and 0 <= now - admitted[identity] <= 120
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
            if admission.started:
                admitted = dict(getattr(outcome, "_expression_attention", {}))
                admitted[identity] = now
                outcome._expression_attention = admitted
            else:
                engine.restore_unstarted_participation(state, target, outcome.speech_receipt_batch,
                    previous, source="completion", browse_id="", started_at=now)
                if admission.deferred:
                    status, reason = "deferred", admission.reason or "模型尚未调用，留待新的现场"
            engine.finish_participation_observation(state, target, outcome.speech_receipt_batch, status, reason)
