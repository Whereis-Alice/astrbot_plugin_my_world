"""One fair collector per life; all waiting stays outside the model worker pool."""
from __future__ import annotations

import asyncio
import time

from ..browse_reply import BrowseReply, RETRYABLE, available_rows, reply_status


def budget_policy(engine) -> list:
    limits = engine.world.limits
    return [limits.background_daily_calls, limits.background_daily_tokens,
            bool(limits.background_pacing), engine.world.timezone,
            getattr(engine, "_background_budget_revision", 0)]


def budget_delay(engine, state, target: str, *, source: str, browse_id: str = "") -> float:
    """Reuse a known release time without re-reading the budget while it is held."""
    observation = state.participation.get(target) or {}
    if (observation.get("decision") != "deferred" or observation.get("budget_source") != source
            or observation.get("budget_browse_id", "") != browse_id
            or observation.get("budget_policy") != budget_policy(engine)):
        return 0.
    return max(0., float(observation.get("budget_next_at") or 0) - engine._now())


def reading_attention_delay(state, ticket: BrowseReply, now: float) -> float:
    """Wait on this reading's cadence without consuming its one-shot marker."""
    thought = next((t for t in reversed(state.thoughts) if t.get("browse_id") == ticket.browse_id
        and t.get("browse_session") == ticket.target), {})
    attention = (thought.get("detail") or {}).get("browse_reply") or {}
    return (max(0., float(attention.get("next_at") or 0) - now)
            if attention.get("status") == "waiting" else 0.)


def registries(engine):
    for name in ("_participation_pending", "_participation_browses", "_participation_settlers", "_participation_wakeups"):
        if not hasattr(engine, name):
            setattr(engine, name, {})
    return engine._participation_pending, engine._participation_browses, engine._participation_settlers


def wake(engine, session: str) -> None:
    """A reply completion wakes existing work; it never invents a new opportunity."""
    event = getattr(engine, "_participation_wakeups", {}).get(engine.state_key(session))
    if event is not None:
        event.set()


def queue_message(engine, session: str) -> None:
    if not engine.is_enabled(session) or not engine.chat_is_group(session):
        return
    with engine.config_scope(session):
        if not engine.world.decider.message_driven or not engine.proactive_chat_allowed(session):
            return
    if getattr(engine, "_background_closed", False):
        return
    pending, _, _ = registries(engine)
    key = engine.state_key(session)
    pending.setdefault(key, {})[session] = time.monotonic()
    ensure_collector(engine, key)
    wake(engine, session)


def queue_browse(engine, ticket: BrowseReply) -> bool:
    if not ticket.messages or ticket.wait_seconds <= 0 or getattr(engine, "_background_closed", False):
        return False
    _, browses, _ = registries(engine)
    browses.setdefault(ticket.life, {})[ticket.target] = ticket.queued()
    ensure_collector(engine, ticket.life)
    wake(engine, ticket.target)
    return True


def ensure_collector(engine, key: str) -> None:
    pending, browses, settlers = registries(engine)
    if key in settlers and not settlers[key].done():
        return
    engine._participation_wakeups[key] = asyncio.Event()
    task = asyncio.create_task(settle(engine, key), name="my_world:listen:" + key)
    settlers[key] = task

    def finished(done):
        if settlers.get(key) is done:
            settlers.pop(key, None)
            pending.pop(key, None)
            browses.pop(key, None)
            engine._participation_wakeups.pop(key, None)
        if not done.cancelled() and done.exception():
            engine._log("debug", "接话聚合留待下一条消息")
    task.add_done_callback(finished)


async def wait_for_change(engine, key: str, delay: float, *, max_wait: float = 5) -> bool:
    event = engine._participation_wakeups[key]
    try:
        await asyncio.wait_for(event.wait(), max(.01, min(delay, max_wait)))
    except TimeoutError:
        return False
    event.clear()
    return True


async def settle(engine, key: str) -> None:
    pending, browses, _ = registries(engine)
    started, retry_at, retry_config = time.monotonic(), {}, {}
    while pending.get(key) or browses.get(key):
        arrivals = {sid: ticket.started for sid, ticket in browses.get(key, {}).items()}
        arrivals.update(pending.get(key, {}))
        target = min(arrivals, key=lambda sid: (retry_at.get(sid, 0), arrivals[sid]))
        arrived = pending.get(key, {}).get(target)
        with engine.config_scope(target):
            current_config = (id(engine.world), budget_policy(engine))
            if target in retry_config and current_config != retry_config[target]:
                retry_at.pop(target, None)
                retry_config.pop(target, None)
            if not engine.is_enabled(target) or not engine.proactive_chat_allowed(target):
                retry_at.pop(target, None)
            delay = (min(engine.world.decider.settle_seconds - (time.monotonic() - arrived),
                         12 - (time.monotonic() - started)) if arrived is not None else 0)
        delay = max(delay, retry_at.get(target, 0) - time.monotonic())
        ticket = browses.get(key, {}).get(target)
        if ticket and ticket.remaining(engine._now()) <= 0:
            await settle_browse(engine, ticket)
            if browses.get(key, {}).get(target) is ticket:
                browses[key].pop(target, None)
            if arrived is None:
                retry_at.pop(target, None)
                continue
            ticket = None
        if ticket:
            delay = min(delay, max(0, ticket.remaining(engine._now())))
        if delay > 0:
            if await wait_for_change(engine, key, delay, max_wait=30 if target in retry_config else 5):
                retry_at.pop(target, None)
                retry_config.pop(target, None)
            continue
        active = getattr(engine, "_background_jobs", {}).get("participation:" + key)
        if active and not active.done():
            # Another session's model call must not extend a reading's deadline.
            await asyncio.wait({active}, timeout=min(5, max(.01, ticket.remaining(engine._now()))) if ticket else 5)
            continue
        if arrived is not None:
            retry = await settle_message(engine, key, target, arrived)
        else:
            retry = await settle_browse(engine, ticket)
            if retry is None and browses.get(key, {}).get(target) is ticket:
                browses[key].pop(target, None)
        if retry is None:
            retry_at.pop(target, None)
            retry_config.pop(target, None)
            started = time.monotonic()
        else:
            retry_at[target] = time.monotonic() + retry
            if retry > 5:
                with engine.config_scope(target):
                    retry_config[target] = (id(engine.world), budget_policy(engine))


async def settle_message(engine, key: str, target: str, arrived: float) -> float | None:
    pending = engine._participation_pending
    with engine.config_scope(target):
        async with engine.session_state(target) as state:
            observation = state.participation.get(target) or {}
            rows = engine.live_chat(state, target)
            gate = engine.participation_gate(state, target)
            fresh = any(int(row.get("seq") or 0) > observation.get("checked_seq", 0) for row in rows)
            if not fresh or gate in {"paused", "disabled", "message_driven_disabled"}:
                if observation.get("decision") in {"waiting", "deferred"}:
                    observation.update(decision="idle", reason="现场已结束，等待新的消息" if not fresh
                                       else "当前自主接话已暂停")
                pending.get(key, {}).pop(target, None)
                return None
            if gate:
                engine.note_participation_waiting(state, target, gate)
                return 5
            held = budget_delay(engine, state, target, source="message")
            if held:
                expires = max(float(row.get("at") or 0) for row in rows) + engine.world.decider.live_window_seconds
                return max(.01, min(held, expires - engine._now() + .01))
    task = engine.queue_background("participation:" + key, lambda: engine.maybe_participate(target, sample=True))
    if task is None:
        return 5
    await asyncio.shield(task)
    with engine.config_scope(target):
        async with engine.session_state(target) as state:
            observation = state.participation.get(target) or {}
            choice = observation.get("opportunity") or {}
            if observation.get("decision") == "deferred":
                held = budget_delay(engine, state, target, source="message")
                if held:
                    rows = engine.live_chat(state, target)
                    if not rows:
                        pending.get(key, {}).pop(target, None)
                        observation.update(decision="idle", reason="现场已结束，等待新的消息")
                        return None
                    expires = max(float(row.get("at") or 0) for row in rows) + engine.world.decider.live_window_seconds
                    return max(.01, min(held, expires - engine._now() + .01))
                return max(1, min(30, float(choice.get("due_at") or 0) - engine._now()))
            if observation.get("decision") == "waiting":
                return 5
    if pending.get(key, {}).get(target) == arrived:
        pending[key].pop(target, None)
    return None


async def settle_browse(engine, ticket: BrowseReply) -> float | None:
    target = ticket.target
    with engine.config_scope(target):
        async with engine.session_state(target) as state:
            status, reason = ticket.error(engine, state)
            if not status:
                rows = ticket.eligible(available_rows(engine, state, target))
                if not rows:
                    status, reason = "no_scene", "原消息已回应或现场已结束，本次不再接话"
            gate = engine.participation_gate(state, target, source="browse") if not status else ""
            if status:
                if status != "already_considered":
                    engine.note_browse_reply_gate(state, target, ticket.browse_id, status, reason)
                return None
            if gate in RETRYABLE:
                return min(5, max(.01, ticket.remaining(engine._now())))
            attention = reading_attention_delay(state, ticket, engine._now())
            if attention:
                return min(attention, max(.01, ticket.remaining(engine._now())))
            held = budget_delay(engine, state, target, source="browse", browse_id=ticket.browse_id)
            if held and reply_status(state, ticket) == "waiting":
                return min(held, max(.01, ticket.remaining(engine._now())))
            if held and reply_status(state, ticket) == "budget":
                return None
    # Recheck inside the worker too: admission can race with a new native reply.
    task = engine.queue_background("participation:" + ticket.life,
        lambda: engine.maybe_participate(target, source="browse", browse_id=ticket.browse_id, browse_ticket=ticket))
    if task is None:
        return min(5, max(.01, ticket.remaining(engine._now())))
    remaining = max(.01, ticket.remaining(engine._now()))
    done, _ = await asyncio.wait({task}, timeout=remaining)
    if not done:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        with engine.config_scope(target):
            async with engine.session_state(target) as state:
                engine.note_browse_reply_gate(state, target, ticket.browse_id, "expired",
                    "短暂等待已到上限，本次不再接话")
        return None
    await task
    with engine.config_scope(target):
        async with engine.session_state(target) as state:
            held = budget_delay(engine, state, target, source="browse", browse_id=ticket.browse_id)
            if held and reply_status(state, ticket) == "waiting":
                return min(held, max(.01, ticket.remaining(engine._now())))
            attention = reading_attention_delay(state, ticket, engine._now())
            if attention:
                return min(attention, max(.01, ticket.remaining(engine._now())))
            if reply_status(state, ticket) in RETRYABLE:
                return min(5, max(.01, ticket.remaining(engine._now())))
    return None
