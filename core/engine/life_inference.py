"""Optimistic background inference: persist, release, infer, validate, resume.

Discard is a cancellation, so model fallbacks cannot accidentally apply a stale
decision. Only explicitly marked life entrypoints opt in; chat is unaffected.
"""
from __future__ import annotations

import asyncio
import copy
import math
from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import wraps
from ..visit_presence import active_visit, presence_epoch

LIFE_WORK = ContextVar("my_world_life_work", default=False)
STATE_LEASE = ContextVar("my_world_state_lease", default=None)
TIME_FIELDS = frozenset({"updated_at", "world_time", "life_updated_at", "energy", "loneliness",
                         "curiosity", "boredom", "affect", "valence", "valence_offset",
                         "affect_synced_at", "mood", "desire", "desire_slept", "sleep_pressure",
                         "low_valence_since", "storm_since", "schedule_cursor", "event_last_roll_at"})
# A no-op schedule scan advances its cursor every minute. Preserve that cursor
# on rejoin, but do not mistake it for a changed appointment or discard every
# model decision that takes longer than one heartbeat. Actual schedule changes
# (plans, actions, delays, fired markers and definitions) remain guarded.


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
    if isinstance(result.get("life_decision"), dict):
        # The read-only life overview refreshes the same choice's observation
        # time. Only a changed choice, reason or matter invalidates inference.
        result["life_decision"].pop("at", None)
    if result.get("current_action"):
        for name in ACTION_TIME_FIELDS:
            result["current_action"].pop(name, None)
    if result.get("idle_activity"):
        for name in IDLE_TIME_FIELDS:
            result["idle_activity"].pop(name, None)
        try:
            until = int(payload["idle_activity"].get("until_tick") or 0)
            result["idle_completed"] = bool(until and int(payload.get("world_time") or 0) >= until)
        except (TypeError, ValueError, OverflowError):
            result["idle_completed"] = None
    # Rule-only numerical evolution can continue; crossing meaningful boundaries
    # invalidates the decision rather than applying an old plan to an exhausted life.
    for name, source, default, scale in (("energy_band", "energy", .5, 5),
                                         ("valence_band", "valence", .5, 4),
                                         ("sleep_band", "sleep_pressure", 0, 4)):
        try:
            result[name] = int(float(payload.get(source, default)) * scale)
        except (TypeError, ValueError, OverflowError):
            result[name] = None
    return result


ACTION_TIME_FIELDS = ("elapsed_ticks", "effects_settled_minutes", "progress_at", "progress_ticks", "home_elapsed_remainder")
IDLE_TIME_FIELDS = ("progress_at", "progress_ticks")
IDLE_PRACTICE_FIELDS = ("type", "elapsed_ticks", "practice_id", "practice_accounted_ticks")


def _natural_heart_decay(engine, before, after) -> bool:
    old, new = before.get("heart_knots", []), after.get("heart_knots", [])
    if len(old) != len(new):
        return False
    rate = max(0., float(engine.world.state_dynamics.heart_knot_decay_per_hour or 0))
    for first, last in zip(old, new):
        if not isinstance(first, dict) or not isinstance(last, dict):
            return False
        if {k: v for k, v in first.items() if k not in {"at", "strength"}} != {
                k: v for k, v in last.items() if k not in {"at", "strength"}}:
            return False
        initial = float(first.get("at") or first.get("since") or engine._now())
        updated = float(last.get("at") or last.get("since") or engine._now())
        strength = float(first.get("strength") or 0)
        expected = strength - rate * max(0., updated - initial) / 3600
        actual = float(last.get("strength") or 0)
        if (not all(math.isfinite(value) for value in (initial, updated, expected, actual))
                or not initial <= updated <= engine._now() or actual <= .02
                or int(strength * 4) != int(actual * 4)
                or not math.isclose(actual, expected, abs_tol=1e-9, rel_tol=1e-9)):
            return False
    return True


def _miss_frontier(engine, payload, scores):
    from ..state import WorldState
    state = WorldState.from_payload(copy.deepcopy(payload), payload["session_id"])
    eager = engine.decider.reach_out_eagerness(state)
    if float(state.loneliness or 0) > .7:
        eager *= 1.4
    profile = engine.world.profile
    thresholds = (float(profile.miss_threshold or .6),
                  max(.25, float(profile.miss_push_threshold or .7) / max(.2, min(4., float(eager or 1)))))
    # The actual contact selector keeps insertion order when scores tie.
    return tuple(tuple(uid for uid, value in sorted(scores.items(), key=lambda pair: -pair[1])
                       if value >= threshold) for threshold in thresholds)


def _natural_miss_growth(engine, before, after) -> bool:
    from ..state import WorldState
    old, new = before.get("miss", {}), after.get("miss", {})
    beats = int(after.get("world_time", 0)) - int(before.get("world_time", 0))
    profile = engine.world.profile
    rate = float(profile.miss_growth_per_min or 0)
    if not profile.enabled or rate <= 0 or beats <= 0:
        return False
    state = WorldState.from_payload(copy.deepcopy(before), before["session_id"])
    maximum = rate * max(.1, engine.tick_seconds / 60) * beats * (1 + max(0., float(profile.miss_loneliness_weight or 0)))
    for uid in old.keys() | new.keys():
        first, last = float(old.get(uid, 0)), float(new.get(uid, 0))
        if first == last:
            continue
        view = engine.profiles.view(engine.profile_session(state, uid), uid)
        if (view is None or engine._miss_ignored(view) or uid not in state.user_presence
                or not 0 < float(state.miss_ready_at.get(uid) or 0) <= engine._now()):
            return False
        factor = .5 + max(0., min(1., float(view.affinity) / 100)) + .4 * max(0, len(view.affinities) - 1)
        if not math.isfinite(last) or not first <= last <= min(1., first + maximum * factor) + 1e-9:
            return False
    return _miss_frontier(engine, before, old) == _miss_frontier(engine, after, new)


def _replace_practice_id(value, generated, actual):
    if isinstance(value, dict):
        return {k: _replace_practice_id(v, generated, actual) for k, v in value.items()}
    if isinstance(value, list):
        return [_replace_practice_id(v, generated, actual) for v in value]
    return actual if value == generated else value


def _practice_record_matches(engine, predicted, old, actual, practice_id, identity) -> bool:
    """Keep real period segments: irregular online beats are not a continuous interval."""
    if predicted.keys() != actual.keys():
        return False
    variable = {"minutes", "episodes", "days", "personas"}
    if any(predicted[k] != actual[k] for k in predicted.keys() - variable):
        return False
    if not math.isclose(float(predicted.get("minutes", 0)), float(actual.get("minutes", 0)), abs_tol=1e-7, rel_tol=1e-9):
        return False
    if "episodes" in predicted:
        prior, rows = old.get("episodes", []), actual["episodes"]
        if not isinstance(rows, list) or len(rows) > 30:
            return False
        other_old = [row for row in prior if row.get("practice_id") != practice_id]
        other_new = [row for row in rows if row.get("practice_id") != practice_id]
        if other_new != other_old and not (len(rows) == 30 and other_new == other_old[-len(other_new):]):
            return False
        def key(row):
            return row.get("day"), row.get("period"), row.get("node")
        previous = {key(row): row for row in prior if row.get("practice_id") == practice_id}
        current = [row for row in rows if row.get("practice_id") == practice_id]
        if len({key(row) for row in current}) != len(current) or not previous.keys() <= {key(row) for row in current}:
            return False
        for row in current:
            first = previous.get(key(row))
            fixed = {k: v for k, v in row.items() if k not in {"minutes", "at"}}
            expected = ({k: v for k, v in first.items() if k not in {"minutes", "at"}} if first else
                        {"practice_id": practice_id, "day": row.get("day"), "period": row.get("period"), **identity})
            amount, at = float(row.get("minutes", 0)), float(row.get("at", 0))
            if (fixed != expected or not math.isfinite(amount) or not math.isfinite(at)
                    or amount <= 0 or amount < float((first or {}).get("minutes", 0))
                    or not float((first or {}).get("at", 0)) <= at <= float(actual.get("last_at", 0))):
                return False
            if first and amount == first.get("minutes") and row != first:
                return False
            local = engine.local_at(at - .001)
            period = "早晨" if 5 <= local.hour < 11 else "白天" if 11 <= local.hour < 18 else "晚间" if 18 <= local.hour < 23 else "夜间"
            if row["day"] != engine._today_key(at - .001) or row["period"] != period:
                return False
        increment = sum(float(row["minutes"]) for row in current) - sum(float(row["minutes"]) for row in previous.values())
        expected_increment = float(predicted.get("minutes", 0)) - float(old.get("minutes", 0))
        if not math.isclose(increment, expected_increment, abs_tol=1e-7, rel_tol=1e-9):
            return False
        days = sorted(set(old.get("days", [])) | {row["day"] for row in current})[-30:]
        if actual.get("days") != days:
            return False
    elif predicted.get("days") != actual.get("days"):
        return False
    if "personas" in predicted:
        from ..life_matters import PRACTICE_FIELDS
        owners = predicted["personas"]
        if owners.keys() != actual["personas"].keys():
            return False
        old_owners = old.get("personas")
        if not isinstance(old_owners, dict):
            old_owners = {"": {k: old[k] for k in PRACTICE_FIELDS if k in old}} if (
                old.get("sessions") or old.get("minutes") or old.get("days")) else {}
        for owner, expected in owners.items():
            if not _practice_record_matches(engine, expected, old_owners.get(owner, {}),
                                            actual["personas"][owner], practice_id, identity):
                return False
    return True


def _natural_idle_practice(engine, before, after) -> bool:
    from ..state import WorldState
    old, new = before.get("idle_activity") or {}, after.get("idle_activity") or {}
    if not old or not new:
        return False
    omitted = set(IDLE_TIME_FIELDS + IDLE_PRACTICE_FIELDS)
    if {k: v for k, v in old.items() if k not in omitted} != {k: v for k, v in new.items() if k not in omitted}:
        return False
    identifier = str(old.get("id") or "")
    if new.get("type", identifier) != identifier or old.get("practice_id") and old["practice_id"] != new.get("practice_id"):
        return False
    start, until = int(old.get("start_tick") or 0), int(old.get("until_tick") or 0)
    if not start <= int(after.get("world_time") or 0) < until:
        return False
    accounted, previous = int(new.get("practice_accounted_ticks") or 0), int(old.get("practice_accounted_ticks") or 0)
    elapsed = max(0, int(after.get("world_time") or 0) - start)
    if (not previous <= accounted <= elapsed or new.get("elapsed_ticks", elapsed) != elapsed
            or new.get("practice_id") and accounted <= 0):
        return False
    if before.get("life_interests") == after.get("life_interests"):
        return accounted == previous and new.get("practice_id") == old.get("practice_id")
    if accounted <= previous:
        return False
    expected = WorldState.from_payload(copy.deepcopy(before), before["session_id"])
    action = expected.idle_activity
    at = float((after.get("life_interests", {}).get(identifier) or {}).get("last_at") or 0)
    if not 0 < at <= engine._now() or at > float(new.get("progress_at") or engine._now()):
        return False
    action.update(type=identifier, elapsed_ticks=accounted, progress_ticks=accounted, progress_at=at)
    lease = STATE_LEASE.get()
    if not lease:
        return False
    token = STATE_LEASE.set({**lease, "state": expected})
    try:
        # This operation mutates only the detached state's practice journal.
        engine.settle_life_practice(expected, action, at=at)
    finally:
        STATE_LEASE.reset(token)
    generated, actual = action.get("practice_id"), new.get("practice_id")
    if not generated or not actual:
        return False
    predicted = _replace_practice_id(expected.life_interests, generated, actual)
    observed = after.get("life_interests", {})
    if predicted.keys() != observed.keys() or any(predicted[k] != observed[k] for k in predicted if k != identifier):
        return False
    identity = {"node": str(old.get("node_id") or before.get("node_id")), "action": identifier,
                "label": str(old.get("action_name") or engine.world.action_map()[identifier].name or identifier)}
    return _practice_record_matches(engine, predicted[identifier], before.get("life_interests", {}).get(identifier, {}),
                                    observed[identifier], actual, identity)


def _canonical_matters(payload) -> bool:
    items = payload.get("life_matters")
    return bool(payload.get("life_migrated") is True and isinstance(items, list)
                and all(isinstance(item, dict) and item.get("id") and isinstance(item.get("text"), str)
                        and isinstance(item.get("status"), str) for item in items))


def comparable_life_states(engine, before, after):
    """Validate natural changes before omitting them; preserve latest receipts on rejoin."""
    first, last = decision_state(before), decision_state(after)
    retained = set()
    for name, validator in (("heart_knots", _natural_heart_decay), ("miss", _natural_miss_growth)):
        if first.get(name) != last.get(name) and _natural_change(validator, engine, before, after):
            first[name] = copy.deepcopy(last[name])
            retained.add(name)
    if _natural_change(_natural_idle_practice, engine, before, after):
        for row in (first["idle_activity"], last["idle_activity"]):
            for name in IDLE_PRACTICE_FIELDS:
                row.pop(name, None)
        first["life_interests"] = copy.deepcopy(last["life_interests"])
        retained.add("life_interests")
    if _canonical_matters(before) and _canonical_matters(after):
        first.pop("own_topics", None)
        last.pop("own_topics", None)
        retained.add("own_topics")
    return first, last, retained


def _natural_change(validator, engine, before, after) -> bool:
    try:
        return validator(engine, before, after)
    except (TypeError, ValueError, OverflowError):
        # Persisted nested timestamps are not all normalized by WorldState.
        # A bad receipt stays a changed field, so rejoin restores latest state.
        return False


def owned_life_lease() -> dict | None:
    lease = STATE_LEASE.get()
    return lease if LIFE_WORK.get() and lease and lease["owner"] is asyncio.current_task() else None


async def validate_life_preparation(engine) -> None:
    """Keep a prepared request's original world and character before admission."""
    lease = owned_life_lease()
    if not lease:
        return
    persona = await engine._persona_text(lease["origin"])
    if (active_visit(engine, lease["origin"])
                  or presence_epoch(engine, lease["origin"]) != lease["presence_epoch"]
                  or engine.world is not lease["world"] or engine.schedules is not lease["schedules"]
                  or engine.sessions is not lease["sessions"] or not engine.is_enabled(lease["origin"])
                  or engine.state_key(lease["origin"]) != lease["state"].session_id
                  or engine.local_now().date() != lease["day"]
                  or persona != lease["persona"]):
        engine.decision_trace(lease["origin"], "life_inference", "discarded",
                              "准备请求时人设、世界或串门经历已变化，取消旧决定", phase="before_request")
        raise LifeWorkDiscarded()


@asynccontextmanager
async def released_life_lock(engine):
    lease = owned_life_lease()
    if not lease or lease["released"]:
        # A receipt may call nested plugin hooks while its outer operation has
        # already released this lease. Only that outer scope may rejoin it.
        yield
        return
    state, lock, origin = lease["state"], lease["lock"], lease["origin"]
    world, key = lease["world"], state.session_id
    # Prompt, local materials and eventual commit all belong to the same lease.
    # A fresh read here could silently rebind an already prepared old prompt.
    persona = lease["persona"]
    world_config = world.model_dump(mode="json")
    sessions_config = lease["sessions"].model_dump(mode="json") if lease["sessions"] else None
    schedules = lease["schedules"].model_dump() if lease["schedules"] else None
    day = lease["day"]
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
            current_persona = await engine._persona_text(origin)
            after = latest.to_payload()
            before_decision, after_decision, retained = comparable_life_states(engine, before, after)
            changed_fields = sorted(k for k in before_decision.keys() | after_decision.keys()
                                    if before_decision.get(k) != after_decision.get(k))
            stale = (active_visit(engine, origin)
                     or presence_epoch(engine, origin) != lease["presence_epoch"]
                     or engine.world is not world or engine.state_key(origin) != key
                     or not engine.is_enabled(origin) or engine.local_now().date() != day
                     or engine.world.model_dump(mode="json") != world_config
                     or (engine.sessions.model_dump(mode="json") if engine.sessions else None) != sessions_config
                     or (engine.schedules.model_dump() if engine.schedules else None) != schedules
                     or current_persona != persona or changed_fields)
            if stale:
                state.__dict__.clear()
                state.__dict__.update(latest.__dict__)
                lease["save"] = True
                engine.decision_trace(origin, "life_inference", "discarded",
                                      "思考期间生活或人工设置发生变化，旧决定作废",
                                      changed_fields=changed_fields)
                raise LifeWorkDiscarded()
            for name in TIME_FIELDS | retained:
                if name in after:
                    setattr(state, name, copy.deepcopy(after[name]))
            if isinstance(after.get("life_decision"), dict):
                state.life_decision = copy.deepcopy(after["life_decision"])
            if state.current_action and latest.current_action:
                for name in ACTION_TIME_FIELDS:
                    if name in latest.current_action:
                        state.current_action[name] = copy.deepcopy(latest.current_action[name])
                    else:
                        state.current_action.pop(name, None)
            if state.idle_activity and latest.idle_activity:
                for name in IDLE_TIME_FIELDS + (IDLE_PRACTICE_FIELDS if "life_interests" in retained else ()):
                    if name in latest.idle_activity:
                        state.idle_activity[name] = copy.deepcopy(latest.idle_activity[name])
                    else:
                        state.idle_activity.pop(name, None)
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
        if cancelled or asyncio.current_task().cancelling():
            # A stale-result cancellation is handled as a discarded decision by
            # life_work; an actual unload/shutdown must propagate to its caller.
            # Observe the commit exception before propagating shutdown.
            if not commit.cancelled():
                commit.exception()
            raise asyncio.CancelledError()
        commit.result()
