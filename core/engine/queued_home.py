"""Transient proof that queued home speech still belongs to its original life.

Proofs describe generated speech, never a saved plan. A new lease after returning
home can speak normally; returning cannot revive an older queued statement.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from ..visit_presence import active_visit, presence_epoch
from .life_inference import STATE_LEASE

HOME_KINDS = frozenset({"", "life_share", "outreach", "schedule", "event"})
PERSONA_CHECK_SECONDS = 2.0


@dataclass
class HomeSpeechProof:
    origin: str
    state_key: str
    epoch: int
    world: Any = field(repr=False)
    sessions: Any = field(repr=False)
    persona: str = field(repr=False)
    world_config: dict = field(repr=False)
    sessions_config: dict | None = field(repr=False)
    cancelled: str = ""


def _home_world_config(world) -> dict:
    # This policy describes opportunities to answer chat media. A native home
    # share does not acquire that policy merely because configuration reloaded.
    return world.model_dump(mode="json", exclude={"decider": {"media_only_participation"}})


def _sessions_config(sessions) -> dict | None:
    if sessions is None:
        return None
    config = sessions.model_dump(mode="json")
    for session in config.get("sessions", []):
        overrides = session.get("config_overrides") or {}
        settings = overrides.get("settings")
        if not isinstance(settings, dict):
            continue
        decider = settings.get("decider")
        if isinstance(decider, dict):
            decider.pop("media_only_participation", None)
            if not decider:
                settings.pop("decider", None)
        if not settings:
            overrides.pop("settings", None)
    return config


def capture_home_proof(kind: str) -> HomeSpeechProof | None:
    """Reuse the current owned lease; no database, persona or model read."""
    if kind not in HOME_KINDS:
        return None
    lease = STATE_LEASE.get()
    try:
        owner = asyncio.current_task()
    except RuntimeError:
        return None
    if not lease or lease.get("owner") is not owner:
        return None
    if "home_speech_proof" not in lease:
        lease["home_speech_proof"] = HomeSpeechProof(
            str(lease["origin"]), str(lease["state"].session_id),
            int(lease["presence_epoch"]), lease["world"], lease["sessions"],
            str(lease["persona"]), _home_world_config(lease["world"]),
            _sessions_config(lease["sessions"]))
    return lease["home_speech_proof"]


def _deny(outcome, proof, reason: str) -> bool:
    if proof is not None:
        proof.cancelled = proof.cancelled or reason
        reason = proof.cancelled
    if reason not in outcome.notes:
        outcome.notes.append(reason)
    return False


def home_speech_allowed(engine, outcome, kind: str, proof: HomeSpeechProof | None) -> bool:
    if kind not in HOME_KINDS:
        return True  # Replies and current visit dialogue keep their own authorization.
    if proof is not None and proof.cancelled:
        return _deny(outcome, proof, proof.cancelled)
    origin = proof.origin if proof is not None else outcome.generation_origin
    with engine.config_scope(origin):
        if proof is not None:
            if engine.state_key(origin) != proof.state_key:
                return _deny(outcome, proof, "旧家中发言的生活归属已变化，取消未发送部分")
            if not engine.is_enabled(origin):
                return _deny(outcome, proof, "旧家中发言的世界或会话设置已变化，取消未发送部分")
            # Reload also replaces unchanged models. Compare effective meaning
            # only on replacement, retaining the normal identity fast path.
            if engine.world is not proof.world:
                if _home_world_config(engine.world) != proof.world_config:
                    return _deny(outcome, proof, "旧家中发言的世界或会话设置已变化，取消未发送部分")
                proof.world = engine.world
            if engine.sessions is not proof.sessions:
                if _sessions_config(engine.sessions) != proof.sessions_config:
                    return _deny(outcome, proof, "旧家中发言的世界或会话设置已变化，取消未发送部分")
                proof.sessions = engine.sessions
        epoch = presence_epoch(engine, origin)
        if active_visit(engine, origin) or (proof is not None and epoch != proof.epoch):
            return _deny(outcome, proof, "串门经历已变化，取消旧家中发言的未发送部分")
        # Legacy/custom buffers did not preserve when their text was generated.
        # Before any retained arrival they remain compatible. After a visit we
        # cannot claim that unproven text was freshly written on returning.
        if proof is None and epoch:
            return _deny(outcome, proof, "旧家中发言没有生成时的串门记录，取消未发送部分")
    return True


async def home_persona_allowed(engine, outcome, kind: str, proof: HomeSpeechProof | None) -> bool:
    """Real adapters opt in to one bounded source-persona read per segment."""
    if not home_speech_allowed(engine, outcome, kind, proof):
        return False
    if kind not in HOME_KINDS or proof is None:
        return True
    port = engine.persona
    reader = getattr(port, "get_persona_text_for_send", None)
    if not callable(reader):
        reader = getattr(port, "get_persona_text", None)
    try:
        if not callable(reader):
            raise LookupError("persona port unavailable")
        with engine.config_scope(proof.origin):
            async with asyncio.timeout(PERSONA_CHECK_SECONDS):
                current = await reader(proof.origin)
        if not isinstance(current, str):
            raise LookupError("persona text unavailable")
    except Exception as exc:
        return _deny(outcome, proof, f"发送前人设读取不可用（{type(exc).__name__}），取消未发送部分")
    if not home_speech_allowed(engine, outcome, kind, proof):
        return False
    if current != proof.persona:
        return _deny(outcome, proof, "生成家中发言的角色人设已变化，取消未发送部分")
    return True
