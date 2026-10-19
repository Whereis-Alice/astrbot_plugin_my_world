"""Optimistic background inference: persist, release, infer, validate, resume.

Discard is a cancellation, so model fallbacks cannot accidentally apply a stale
decision. Only explicitly marked life entrypoints opt in; chat is unaffected.
"""
from __future__ import annotations

import asyncio
import copy
from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import wraps

LIFE_WORK = ContextVar("my_world_life_work", default=False)
STATE_LEASE = ContextVar("my_world_state_lease", default=None)
TIME_FIELDS = frozenset({"updated_at", "world_time", "life_updated_at", "energy", "loneliness",
                         "curiosity", "boredom", "affect", "valence", "valence_offset",
                         "affect_synced_at", "mood", "desire", "desire_slept", "sleep_pressure",
                         "low_valence_since", "storm_since"})


class LifeWorkDiscarded(asyncio.CancelledError):
    """The world changed while this life decision was thinking."""


def life_work(default=None):
    def decorate(fn):
        @wraps(fn)
        async def run(self, *args, **kwargs):
            token = LIFE_WORK.set(True)
            try:
                return await fn(self, *args, **kwargs)
            except LifeWorkDiscarded:
                return copy.deepcopy(default)
            finally:
                LIFE_WORK.reset(token)
        return run
    return decorate


def locked_life_work(fn):
    """Keep execution with external effects serialized; only decisions release locks."""
    @wraps(fn)
    async def run(*args, **kwargs):
        token = LIFE_WORK.set(False)
        try:
            return await fn(*args, **kwargs)
        finally:
            LIFE_WORK.reset(token)
    return run


def decision_state(payload: dict) -> dict:
    result = {k: copy.deepcopy(v) for k, v in payload.items() if k not in TIME_FIELDS}
    if result.get("current_action"):
        result["current_action"].pop("elapsed_ticks", None)
    # Rule-only numerical evolution can continue; crossing meaningful boundaries
    # invalidates the decision rather than applying an old plan to an exhausted life.
    result["energy_band"] = int(float(payload.get("energy", .5)) * 5)
    result["valence_band"] = int(float(payload.get("valence", .5)) * 4)
    result["sleep_band"] = int(float(payload.get("sleep_pressure", 0)) * 4)
    return result


@asynccontextmanager
async def released_life_lock(engine):
    lease = STATE_LEASE.get()
    if not LIFE_WORK.get() or not lease or lease["owner"] is not asyncio.current_task():
        yield
        return
    state, lock, origin = lease["state"], lease["lock"], lease["origin"]
    world, key = engine.world, engine.state_key(origin)
    persona = await engine._persona_text(origin)
    schedules = engine.schedules.model_dump() if engine.schedules else None
    day = engine.local_now().date()
    await engine.save_state(state)
    before = state.to_payload()
    lock.release()
    lease["released"] = True
    lease["save"] = False
    try:
        yield
    finally:
        async def rejoin():
            await lock.acquire()
            lease["released"] = False
            from ..state import WorldState
            latest = WorldState.from_payload(await engine.db.call("get_state", key), key)
            after = latest.to_payload()
            stale = (engine.world is not world or engine.state_key(origin) != key
                     or not engine.is_enabled(origin) or engine.local_now().date() != day
                     or (engine.schedules.model_dump() if engine.schedules else None) != schedules
                     or await engine._persona_text(origin) != persona or decision_state(before) != decision_state(after))
            if stale:
                state.__dict__.clear()
                state.__dict__.update(latest.__dict__)
                lease["save"] = True
                engine.decision_trace(origin, "life_inference", "discarded", "思考期间生活或人工设置发生变化，旧决定作废")
                raise LifeWorkDiscarded()
            for name in TIME_FIELDS:
                if name in after:
                    setattr(state, name, copy.deepcopy(after[name]))
            if state.current_action and latest.current_action:
                state.current_action["elapsed_ticks"] = latest.current_action.get("elapsed_ticks", 0)
            lease["save"] = True
        # Even cancellation while another writer owns the lock must rejoin before
        # session_state saves and releases it. Shield only this short commit phase.
        commit = asyncio.create_task(rejoin())
        cancelled = False
        while not commit.done():
            try:
                await asyncio.shield(commit)
            except asyncio.CancelledError:
                if commit.done():
                    break
                cancelled = True
        commit.result()
        if cancelled:
            raise asyncio.CancelledError()
