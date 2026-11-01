"""Bounded background work, deadlines, backoff and conservative offline reconciliation."""
from __future__ import annotations

from ..recovery import sleep_action_kind

import asyncio
import time
from hashlib import sha256
from collections import deque
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any

from ..llm_usage import collect_llm_usage
from ..background_budget import BackgroundReservation, record_background_admission
from ..background_rhythm import background_admission, background_window
from ..model_calls import call_context
from ..model_call_runtime import safe_error
from .life_inference import LIFE_WORK, LifeWorkDiscarded, released_life_lock, validate_life_preparation
from ..visit_presence import active_visit, pause_intervals

CRITICAL_TICK = ContextVar("world_critical_tick", default=False)
VISIT_EXHAUSTED = "今日串门预算已用完，先休息一下，明天再联系友邻"
VISIT_UNOWNED = "串门缺少可用的生活归属，留待重新准备"


class BackgroundMixin:
    def _background_budget_changed(self) -> None:
        self._background_budget_revision = getattr(self, "_background_budget_revision", 0) + 1
        for event in getattr(self, "_participation_wakeups", {}).values():
            event.set()

    def _background_budget_window(self, purpose: str = "") -> dict[str, Any]:
        local = self.local_at(self._now())
        if local.tzinfo is None:
            local = local.replace(tzinfo=self._resolve_tz())
        return background_window(local,
            calls=self.world.limits.background_daily_calls,
            tokens=self.world.limits.background_daily_tokens,
            enabled=self.world.limits.background_pacing and purpose != "world_visit")

    def _visit_budget_scope(self, session_id: str) -> str:
        """Only an explicit, enabled life can own a visit reservation."""
        if not str(session_id or "").strip():
            return ""
        scope = self.state_key(self.scope_session(str(session_id)))
        with self.config_scope(scope):
            return scope if self.is_enabled(scope) else ""

    def _background_budget_pool(self, *, purpose: str = "", session_id: str = "") -> dict[str, Any] | None:
        """Routine work shares the instance day; visits share their actual life."""
        if purpose != "world_visit":
            day = self._today_key(self._now())
            return {"day": day, "ledger": day, "scope": "",
                    "call_limit": self.world.limits.background_daily_calls,
                    "token_limit": self.world.limits.background_daily_tokens,
                    "window": self._background_budget_window(purpose)}
        scope = self._visit_budget_scope(session_id)
        if not scope:
            return None
        config = getattr(getattr(self, "visits", None), "config", None)
        calls = max(0, int(getattr(config, "daily_calls", 24)))
        tokens = max(0, int(getattr(config, "daily_tokens", 0)))
        with self.config_scope(scope):
            local = self.local_at(self._now())
            if local.tzinfo is None:
                local = local.replace(tzinfo=self._resolve_tz())
            day = local.date().isoformat()
            window = background_window(local, calls=calls, tokens=tokens, enabled=False)
        # The store's existing atomic reserve/settle/refund methods all receive
        # this same suffix. Normal daily keys remain exactly YYYY-MM-DD.
        ledger = "world_visit:" + day + ":" + sha256(scope.encode()).hexdigest()
        return {"day": day, "ledger": ledger, "scope": scope,
                "call_limit": calls, "token_limit": tokens, "window": window}

    async def _background_budget_status(self, *, purpose: str = "", session_id: str = "") -> dict[str, Any]:
        """Read the selected allowance and its owner without spending a call."""
        pool = self._background_budget_pool(purpose=purpose, session_id=session_id)
        if pool is None:
            return {"day": "", "ledger": "", "scope": "", "call_limit": 0, "token_limit": 0,
                    "calls": 0, "tokens": 0, "reserved": 0, "unknown": 0,
                    "allowed": False, "reason": VISIT_UNOWNED, "paced": False, "next_at": 0.}
        item = await self.db.call("background_status", pool["ledger"])
        # A midnight or life ownership change during the read cannot apply an
        # earlier ledger to the newly selected life.
        latest = self._background_budget_pool(purpose=purpose, session_id=session_id)
        if latest is None:
            return {**pool, **item, "allowed": False, "reason": VISIT_UNOWNED,
                    "paced": False, "next_at": 0.}
        if latest["ledger"] != pool["ledger"]:
            item = await self.db.call("background_status", latest["ledger"])
        pool = latest
        admission = background_admission(item, calls=pool["call_limit"], tokens=pool["token_limit"],
                                         window=pool["window"])
        if purpose == "world_visit" and not admission["allowed"]:
            admission["reason"] = VISIT_EXHAUSTED
        return {**pool, **admission}

    def _background_concurrency(self) -> int:
        # The shared worker pool belongs to global settings; each scoped model
        # channel still applies its own inference limit in _budgeted_generate.
        world = getattr(self, "_base_runtime", {}).get("world") or self.world
        return max(1, world.limits.background_concurrency)

    def queue_background(self, key: str, work: Callable[[], Awaitable[Any]]) -> asyncio.Task | None:
        jobs = getattr(self, "_background_jobs", None)
        if jobs is None:
            jobs = self._background_jobs = {}
        if getattr(self, "_background_closed", False):
            return None
        current = jobs.get(key)
        if current and not current.done():
            return current
        # Reserve room for every per-character family and each global task. Keep
        # this aligned with the queue sites in main.py and engine mixins: decision,
        # participation, chat_browse, memory, life_seed, matter, social; plus
        # weather, daily_schedule, profile_sync, calendar_diary and world_visits.
        concurrency = self._background_concurrency()
        capacity = max(8, concurrency * 6, len(self.tick_session_ids()) * 7 + 5)
        live = sum(not task.done() for task in jobs.values())
        if live >= capacity:
            return None
        if not hasattr(self, "_background_condition"):
            self._background_condition = asyncio.Condition()
            self._background_waiters = deque()
            self._background_running_jobs = 0
        condition = self._background_condition
        waiters = self._background_waiters

        async def run():
            ticket, admitted = object(), False
            token = CRITICAL_TICK.set(False)
            try:
                async with condition:
                    waiters.append(ticket)
                    await condition.wait_for(lambda: waiters[0] is ticket and
                        self._background_running_jobs < self._background_concurrency())
                    waiters.popleft()
                    admitted = True
                    self._background_running_jobs += 1
                    condition.notify_all()
                return await work()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log("debug", f"后台工作 {key} 留待下次：{type(exc).__name__}")
            finally:
                async with condition:
                    if admitted:
                        self._background_running_jobs -= 1
                    elif ticket in waiters:
                        waiters.remove(ticket)
                    condition.notify_all()
                CRITICAL_TICK.reset(token)
        task = asyncio.create_task(run(), name="my_world:" + key)
        jobs[key] = task
        def forget(completed):
            if jobs.get(key) is completed:
                jobs.pop(key, None)
        # A task cancelled before its first instruction never enters run/finally.
        task.add_done_callback(forget)
        return task

    async def close_background(self) -> None:
        self._background_closed = True
        tasks = list(getattr(self, "_background_jobs", {}).values())
        tasks += list(getattr(self, "_participation_settlers", {}).values())
        tasks += list(getattr(self, "_life_clock_jobs", {}).values()) + list(getattr(self, "_life_delivery_jobs", set()))
        if getattr(self, "_schedule_delivery_job", None):
            tasks.append(self._schedule_delivery_job)
        if getattr(self, "_schedule_model_job", None):
            tasks.append(self._schedule_model_job)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _budgeted_generate(self, channel, **kwargs: Any):
        await validate_life_preparation(self)
        from ..ports import LLMReply
        context = call_context()
        purpose = str(context.get("purpose") or "other")
        request_session = str(context.get("session_id") or "")
        # Calls caused by a person are not part of the autonomous background
        # allowance. In particular, post-chat reflection is what keeps the
        # "inner thoughts" page updating; a busy conversation must not let other
        # lives starve it by exhausting the autonomous planning budget.
        interactive = purpose in {
            "reply", "chat", "reflection", "creation", "persona", "evaluation",
            "action", "tool_params", "followup",
        } and not CRITICAL_TICK.get() and not LIFE_WORK.get()
        if interactive:
            async with asyncio.timeout(self.world.limits.background_llm_timeout_seconds):
                return await channel.generate(**kwargs)
        session = str(kwargs.get("session_id") or request_session or "")
        visit_scope = ""
        if purpose == "world_visit":
            visit_scope = self._visit_budget_scope(session)
            if (not visit_scope or request_session
                    and self._visit_budget_scope(request_session) != visit_scope):
                record_background_admission(deferred=True, reason=VISIT_UNOWNED)
                return LLMReply(ok=False, error=VISIT_UNOWNED)

        def visit_owned():
            return (purpose != "world_visit" or
                    self._visit_budget_scope(session) == visit_scope and
                    (not request_session or self._visit_budget_scope(request_session) == visit_scope))

        # Preparation must not occupy a slot or spend a reservation. A malformed
        # option, unavailable tool context or failed routing lookup stays local.
        reserve = (len(str(kwargs.get("system_prompt", ""))) + len(str(kwargs.get("prompt", "")))
                   + len(str(kwargs.get("contexts") or "")) + len(str(kwargs.get("func_tool") or ""))
                   + int(kwargs.get("max_tokens") or 2048))
        from ..speech_runtime import SPEECH_REQUEST
        speech = SPEECH_REQUEST.get()
        available = self.allowed_tool_names(speech["state"].node_id) if speech else set()
        if self.world.decider.proactive_tool_names:
            available &= set(self.world.decider.proactive_tool_names)
        context = getattr(getattr(channel, "plugin", None), "context", None)
        if (speech and speech.get("allow_tools") and available and self.world.decider.proactive_tools
                and getattr(channel, "role", "") == "main" and callable(getattr(context, "tool_loop_agent", None))):
            reserve *= self.world.decider.proactive_tool_steps + 1
        provider_id = str(getattr(channel, "provider_id", "") or "")
        generate, generation_kwargs = channel.generate, kwargs
        admitted_generate = None
        timeout = min(2, self.world.limits.background_llm_timeout_seconds) if CRITICAL_TICK.get() and not LIFE_WORK.get() else self.world.limits.background_llm_timeout_seconds
        resolver = getattr(channel, "resolve_provider_id", None)
        if callable(resolver):
            async with asyncio.timeout(timeout):
                provider_id = await resolver(session)
            if not isinstance(provider_id, str):
                raise ValueError("后台模型 Provider 解析结果无效")
            resolved_generate = getattr(channel, "generate_resolved", None)
            if callable(resolved_generate):
                generate = resolved_generate
                generation_kwargs = {**kwargs, "resolved_provider_id": provider_id}
            admitted_generate = getattr(channel, "generate_admitted", None)
            await validate_life_preparation(self)
        now = self._now()
        if not visit_owned():
            record_background_admission(deferred=True, reason=VISIT_UNOWNED)
            return LLMReply(ok=False, error=VISIT_UNOWNED)
        pool = self._background_budget_pool(purpose=purpose, session_id=session)
        if pool is None:
            record_background_admission(deferred=True, reason=VISIT_UNOWNED)
            return LLMReply(ok=False, error=VISIT_UNOWNED)
        ledger, window = pool["ledger"], pool["window"]
        failures = getattr(self, "_background_failures", {})
        self._background_failures = failures
        failure_key = (id(channel), purpose, session, provider_id)
        failure = failures.get(failure_key, {})
        if failure.get("until", 0) > now:
            reason = str(failure.get("error") or "")
            return LLMReply(ok=False, error="后台模型暂时退避，规则继续运行" + (f"；上次失败：{reason}" if reason else ""))
        # No await may separate this shared slot check from its admission.
        active = getattr(self, "_background_active", 0)
        if active >= self.world.limits.background_concurrency:
            return LLMReply(ok=False, error="后台通道忙，留待下一次")
        self._background_active = active + 1
        budget = None
        try:
            async with BackgroundReservation(self.db, ledger,
                    calls=pool["call_limit"], tokens=pool["token_limit"], reserve=reserve, window=window,
                    on_change=self._background_budget_changed,
                    exhausted_reason=VISIT_EXHAUSTED if purpose == "world_visit" else "") as budget:
                if not budget.allowed:
                    return LLMReply(ok=False, error=budget.reason)
                with collect_llm_usage(propagate=True) as usage:
                    budget.usage = usage
                    # Tick never spends a full provider timeout holding a character lock.
                    async with released_life_lock(self):
                        async with asyncio.timeout(timeout):
                            await validate_life_preparation(self)
                            def current_ledger():
                                latest = self._background_budget_pool(purpose=purpose, session_id=session)
                                return visit_owned() and latest and latest["ledger"] == ledger

                            changed_reason = ("世界日期或生活归属已变化，本次后台准备取消，稍后重新考虑"
                                              if purpose == "world_visit" else
                                              "世界日期已变化，本次后台准备取消，稍后重新考虑")
                            if not current_ledger():
                                reason = changed_reason
                                record_background_admission(deferred=True, reason=reason, next_at=self._now())
                                return LLMReply(ok=False, error=reason)
                            async def begin_request():
                                await validate_life_preparation(self)
                                if not current_ledger():
                                    reason = changed_reason
                                    record_background_admission(deferred=True, reason=reason, next_at=self._now())
                                    raise RuntimeError(reason)
                                budget.started = True
                                record_background_admission(started=True)

                            if callable(admitted_generate):
                                reply = await admitted_generate(resolved_provider_id=provider_id,
                                    before_request=begin_request, **kwargs)
                            else:
                                # Third-party ports without a dispatch boundary
                                # retain conservative accounting on channel entry.
                                await begin_request()
                                reply = await generate(**generation_kwargs)
                            if not budget.started:
                                reason = safe_error(getattr(reply, "error", "")) or "后台请求尚未发出，留待重新准备"
                                record_background_admission(deferred=True, reason=reason,
                                                            next_at=self._now() + self.tick_seconds)
                                return reply if reply is not None and not getattr(reply, "ok", True) else LLMReply(ok=False, error=reason)
            if reply is None or not getattr(reply, "ok", True):
                raise RuntimeError(str(getattr(reply, "error", "") or "后台模型未返回有效内容")[:500])
            failures.pop(failure_key, None)
            return reply
        except asyncio.CancelledError:
            raise
        except LifeWorkDiscarded:
            raise
        except Exception as exc:
            if budget is not None and not budget.started:
                reason = safe_error(exc) or type(exc).__name__
                record_background_admission(deferred=True, reason=reason,
                                            next_at=self._now() + self.tick_seconds)
                return LLMReply(ok=False, error=reason)
            count = min(8, int(failure.get("count", 0)) + 1)
            failures[failure_key] = {"count": count, "until": self._now() + min(3600, 30 * 2 ** (count - 1)),
                                     "error": safe_error(exc) or type(exc).__name__}
            raise
        finally:
            self._background_active -= 1

    def reconcile_life_time(self, state, *, stay_up_costs: bool = True) -> float:
        """Reconcile elapsed needs/rest; never execute tools, messages or projects offline."""
        now = self._now()
        previous = float(state.life_updated_at or 0)
        state.life_updated_at = now
        if not previous:
            return self.tick_seconds  # Legacy saves have no reliable life cursor.
        elapsed = max(0, now - previous)
        visit = active_visit(self, state.session_id)
        pauses = pause_intervals(self, state.session_id, previous, now,
                                persona_key=self._matter_persona_key(state) or "")
        if pauses:
            return self._reconcile_visit_time(state, previous, now, pauses, visiting=bool(visit),
                                              stay_up_costs=stay_up_costs)
        if elapsed <= self.tick_seconds * 2.5:
            return self.tick_seconds
        gap = min(elapsed, 24 * 3600)
        missing_ticks = max(0, int(gap / self.tick_seconds) - 1)
        old_tick = state.world_time
        if visit:
            # An accepted visit pauses the home action, including home recovery.
            # A slow clock does not prove that any of that work happened. Only
            # the bounded elapsed needs/visit clock is handed to the caller.
            state.world_time += missing_ticks
            return min(gap, 6 * 3600)
        self._settle_idle_practice(state, at=previous)
        state.idle_activity = {}
        state.world_time += missing_ticks
        # Translate relative counters by the elapsed life clock.
        current = state.current_action or {}
        remaining = min(gap, 6 * 3600)
        from ..recovery import passive_recovery
        definition = self.world.action_map().get(str(current.get("type") or ""))
        sleeping = sleep_action_kind(definition)
        can_recover = definition and definition.enabled and (definition.id == "sleep" or definition.available_in(state.node_id))
        completed = self._home_completion_pending(current)
        if can_recover and (sleeping or passive_recovery(definition)) and not completed:
            left = max(0, int(current.get("duration_ticks", 0)) - int(current.get("elapsed_ticks", 0)))
            slept = min(left, int(elapsed / self.tick_seconds))
            current["elapsed_ticks"] = int(current.get("elapsed_ticks", 0)) + slept
            sleep_seconds = slept * self.tick_seconds
            # The settlement cap bounds need evolution, not the historical clock.
            # A multi-day outage must still wake at the end of the original rest.
            wake_at = previous + sleep_seconds
            # Sleep has its own energy, curiosity, desire and pressure dynamics.
            # Settle that interval before the completion path turns her idle.
            if sleep_seconds:
                self.dynamics.tick(state, node=self.node(state.node_id), elapsed_seconds=sleep_seconds,
                                   world=self.world, now=wake_at)
            definition = self.world.action_map().get(str(current.get("type")))
            if definition and slept:
                self.settle_recovery_progress(state, current)
            if slept >= left:
                if sleeping:
                    current["offline_wake"] = True
                    current["offline_wake_at"] = wake_at
                    state.goodmorning_day = self._today_key(now)
                else:
                    current["offline_recovery_complete"] = True
                remaining = min(max(0, elapsed - sleep_seconds), 6 * 3600)
            else:
                remaining = max(0, gap - sleep_seconds)
            current["offline_tick_settled"] = True
        elif current and not completed:
            # Preserve project identity; a missing runtime cannot prove work actually happened.
            self.pause_matter_action(state, current, at=previous)
            state.current_action = None
            state.current_plan = None
            state.state = "idle"
        if state.current_plan and state.current_plan.get("expires_at", now + 1) <= now:
            state.current_plan = None
        state.idle_activity = {}
        # An unanswered historical greeting must not become a burst of catch-up speech.
        state.awaiting_reply = False
        state.life_recovery = {"at": now, "elapsed_seconds": elapsed, "settled_seconds": gap,
                               "from_tick": old_tick, "note": "离线期间仅结算时间与休息；没有补发消息或虚构事项进展"}
        self._note_event(state, "offline_recovery", dict(state.life_recovery))
        return remaining

    @staticmethod
    def _home_completion_pending(action) -> bool:
        return any((action or {}).get(flag) for flag in
                   ("offline_wake", "offline_recovery_complete", "offline_home_complete"))

    def _reconcile_visit_time(self, state, previous: float, now: float,
                              pauses: list[tuple[float, float]], *, visiting: bool,
                              stay_up_costs: bool = True) -> float:
        """Only retained visit windows pause home recovery; deadlines keep real time."""
        elapsed = max(0, now - previous)
        paused = sum(last - first for first, last in pauses)
        home = max(0, elapsed - paused)
        gap, old_tick = min(elapsed, 24 * 3600), state.world_time
        if elapsed > self.tick_seconds * 2.5:
            state.world_time += max(0, int(gap / self.tick_seconds) - 1)
        self._settle_idle_practice(state, at=previous)
        state.idle_activity = {}
        current = state.current_action or {}
        segments = self._visit_segments(previous, now, pauses)
        definition = self.world.action_map().get(str(current.get("type") or ""))
        from ..recovery import passive_recovery
        sleeping = sleep_action_kind(definition)
        recover = (definition and definition.enabled
                   and (definition.id == "sleep" or definition.available_in(state.node_id))
                   and (sleeping or passive_recovery(definition)))
        if self._home_completion_pending(current):
            # A home rest/work already finished before arrival. Preserve its
            # actual finish marker until returning, without sleeping again.
            pending = self._bound_visit_segments(segments, 6 * 3600)
            current["offline_tick_settled"] = True
        elif recover:
            left = max(0, int(current.get("duration_ticks", 0)) - int(current.get("elapsed_ticks", 0)))
            advanced = min(left, int(home / self.tick_seconds))
            state._visit_rest_progress = {"action": current, "initial_ticks": int(current.get("elapsed_ticks", 0)),
                                          "ticks": advanced, "seconds": 0.0}
            if advanced >= left:
                before, after, wake_at = self._split_visit_rest(segments, advanced * self.tick_seconds, previous)
                # Rest keeps its established recovery clock. Awake needs before
                # and after waking share one six-hour settlement allowance.
                before = self._bound_visit_segments(before, float("inf"), away_limit=6 * 3600)
                self._evolve_visit_segments(state, self.node(state.node_id), before, stay_up_costs=stay_up_costs)
                del state._visit_rest_progress
                away_used = sum(last - first for first, last, away in before if away)
                pending = self._bound_visit_segments(after, max(0, 6 * 3600 - away_used))
                if sleeping:
                    current["offline_wake"] = True
                    current["offline_wake_at"] = wake_at
                    state.goodmorning_day = self._today_key(now)
                else:
                    current["offline_recovery_complete"] = True
            else:
                pending = self._bound_visit_segments(segments, float("inf"), away_limit=6 * 3600)
            current["offline_tick_settled"] = True
        elif current and home <= self.tick_seconds * 2.5:
            # A just-returned life still owns its quiet work. The last fixed
            # tick must not claim home activity when no home minute elapsed.
            pending = self._bound_visit_segments(segments, 6 * 3600, home_limit=min(home, self.tick_seconds))
            if visiting and home >= self.tick_seconds and definition and definition.category == "continuous":
                self.settle_life_practice(state, current, at=previous)
                self.settle_matter_work(state, current, at=previous)
                current["elapsed_ticks"] = min(int(current.get("duration_ticks") or 0),
                                                int(current.get("elapsed_ticks") or 0) + 1)
                _, _, progressed_at = self._split_visit_rest(segments, self.tick_seconds, previous)
                self.settle_life_practice(state, current, at=progressed_at)
                self.settle_matter_work(state, current, at=progressed_at)
                if int(current.get("elapsed_ticks") or 0) >= int(current.get("duration_ticks") or 0):
                    current["offline_home_complete"] = True
            if home < self.tick_seconds:
                current["offline_tick_settled"] = True
            else:
                current.pop("offline_tick_settled", None)
        else:
            if current:
                self.pause_matter_action(state, current, at=previous)
                state.current_action, state.current_plan, state.state = None, None, "idle"
            pending = self._bound_visit_segments(segments, 6 * 3600)
        if state.current_plan and state.current_plan.get("expires_at", now + 1) <= now:
            state.current_plan = None
        state.awaiting_reply = False
        if elapsed > self.tick_seconds * 2.5:
            state.life_recovery = {"at": now, "elapsed_seconds": elapsed, "settled_seconds": gap,
                                   "paused_visit_seconds": paused, "home_seconds": home,
                                   "from_tick": old_tick,
                                   "note": "在外期间只结算基本需要，家中活动与休息暂停；未补发消息或虚构进展"}
            self._note_event(state, "offline_recovery", dict(state.life_recovery))
        # This cursor belongs only to the loaded object. It is consumed after
        # the normal wake/completion path, and is never part of its saved payload.
        state._visit_need_segments = pending
        return sum(last - first for first, last, _ in pending)

    @staticmethod
    def _visit_segments(previous: float, now: float, pauses: list) -> list:
        segments, cursor = [], previous
        for first, last in pauses:
            if first > cursor:
                segments.append((cursor, first, False))
            segments.append((first, last, True))
            cursor = last
        if cursor < now:
            segments.append((cursor, now, False))
        return segments

    @staticmethod
    def _split_visit_rest(segments: list, seconds: float, previous: float) -> tuple[list, list, float]:
        before, after, wake_at = [], [], previous
        for first, last, away in segments:
            if seconds <= 0:
                after.append((first, last, away))
                continue
            if away:
                before.append((first, last, True))
                continue
            rest = min(last - first, seconds)
            before.append((first, first + rest, False))
            seconds -= rest
            wake_at = first + rest
            if wake_at < last:
                after.append((wake_at, last, False))
        return before, after, wake_at

    @staticmethod
    def _bound_visit_segments(segments: list, budget: float, *, home_limit: float = float("inf"),
                              away_limit: float = float("inf")) -> list:
        selected = []
        limits = {False: home_limit, True: away_limit}
        for first, last, away in segments:
            seconds = min(last - first, budget, limits[away])
            if seconds > 0:
                selected.append((first, first + seconds, away))
                budget -= seconds
                limits[away] -= seconds
        return selected

    def _evolve_visit_segments(self, state, node, segments: list, *, stay_up_costs: bool = True) -> bool:
        reset = False
        periods = []
        for first, last, away in segments:
            clocks = (self._night_segments(first, last) if away or not state.is_sleeping else [(first, last)])
            periods.extend((start, end, away) for start, end in clocks)
        for first, last, away in periods:
            current, home_state = state.current_action, state.state
            try:
                if away:
                    state.current_action, state.state = None, "idle"
                phase = (first if self.world.is_night_time(self.local_at(first).hour)
                         != self.world.is_night_time(self.local_at(last).hour) else None)
                kwargs = {"phase_at": phase} if phase is not None else {}
                reset = self.dynamics.tick(state, node=None if away else node, elapsed_seconds=last - first,
                                           world=self.world, now=last, **kwargs) or reset
                # The ordinary visitor tick returns before home stay-up costs.
                # Home costs keep their online order before recovery effects.
                if not away and stay_up_costs:
                    penalty = self._stay_up_penalty(state, at=first)
                    if penalty > 1.0:
                        extra = (float(self.world.state_dynamics.energy_decay_per_min)
                                 * (penalty - 1.0) * ((last - first) / 60.0))
                        state.energy = max(0.0, float(state.energy) - max(0.0, extra))
                progress = getattr(state, "_visit_rest_progress", None)
                if not away and progress and current is progress["action"]:
                    progress["seconds"] += last - first
                    ticks = min(progress["ticks"], int(progress["seconds"] / self.tick_seconds))
                    current["elapsed_ticks"] = progress["initial_ticks"] + ticks
                    self.settle_recovery_progress(state, current, at=last)
            finally:
                state.current_action, state.state = current, home_state
        return reset

    def _settle_elapsed_needs(self, state, *, node, elapsed_seconds: float, now: float,
                              stay_up_costs: bool = True, away: bool = False) -> bool:
        segments = getattr(state, "_visit_need_segments", None)
        if segments is None:
            return self._evolve_visit_segments(state, node, [(now - elapsed_seconds, now, away)],
                                               stay_up_costs=stay_up_costs)
        del state._visit_need_segments
        try:
            return self._evolve_visit_segments(state, node, segments, stay_up_costs=stay_up_costs)
        finally:
            if hasattr(state, "_visit_rest_progress"):
                del state._visit_rest_progress
