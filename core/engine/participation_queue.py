"""One fair collector per life; all waiting stays outside the model worker pool."""
from __future__ import annotations

import asyncio
import time

from ..browse_reply import BrowseReply, RETRYABLE, reply_status


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
    if ticket.wait_seconds <= 0 or getattr(engine, "_background_closed", False):
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


async def wait_for_change(engine, key: str, delay: float) -> bool:
    event = engine._participation_wakeups[key]
    try:
        await asyncio.wait_for(event.wait(), max(.01, min(delay, 5)))
    except TimeoutError:
        return False
    event.clear()
    return True


async def settle(engine, key: str) -> None:
    pending, browses, _ = registries(engine)
    started, retry_at = time.monotonic(), {}
    while pending.get(key) or browses.get(key):
        arrivals = {sid: ticket.started for sid, ticket in browses.get(key, {}).items()}
        arrivals.update(pending.get(key, {}))
        target = min(arrivals, key=lambda sid: (retry_at.get(sid, 0), arrivals[sid]))
        arrived = pending.get(key, {}).get(target)
        with engine.config_scope(target):
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
            if await wait_for_change(engine, key, delay):
                retry_at.pop(target, None)
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
            started = time.monotonic()
        else:
            retry_at[target] = time.monotonic() + retry


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
    task = engine.queue_background("participation:" + key, lambda: engine.maybe_participate(target, sample=True))
    if task is None:
        return 5
    await asyncio.shield(task)
    async with engine.session_state(target) as state:
        observation = state.participation.get(target) or {}
        choice = observation.get("opportunity") or {}
        if observation.get("decision") == "deferred":
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
                rows = ticket.eligible(engine.live_chat(state, target, window=engine._participation_window("browse")))
                checked = (state.participation.get(target) or {}).get("checked_seq", 0)
                if not any(int(row.get("seq") or 0) > checked for row in rows):
                    status, reason = "no_scene", "原消息已回应、已判断或现场已结束，本次不再接话"
            gate = engine.participation_gate(state, target, source="browse") if not status else ""
            if status:
                engine.note_browse_reply_gate(state, target, ticket.browse_id, status, reason)
                return None
            if gate in RETRYABLE:
                return min(5, max(.01, ticket.remaining(engine._now())))
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
            if reply_status(state, ticket) in RETRYABLE:
                return min(5, max(.01, ticket.remaining(engine._now())))
    return None
