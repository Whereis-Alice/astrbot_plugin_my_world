"""Bounded background work, deadlines, backoff and conservative offline reconciliation."""
from __future__ import annotations

from ..recovery import sleep_action_kind

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any

from ..llm_usage import collect_llm_usage
from ..model_calls import call_context
from ..model_call_runtime import safe_error
from .life_inference import LIFE_WORK, released_life_lock, validate_life_preparation

CRITICAL_TICK = ContextVar("world_critical_tick", default=False)


class BackgroundMixin:
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
        # weather, daily_schedule, profile_sync and calendar_diary.
        concurrency = self._background_concurrency()
        capacity = max(8, concurrency * 6, len(self.tick_session_ids()) * 7 + 4)
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
        now = self._now()
        failures = getattr(self, "_background_failures", {})
        self._background_failures = failures
        failure_key = (id(channel), purpose)
        failure = failures.get(failure_key, {})
        if failure.get("until", 0) > now:
            reason = str(failure.get("error") or "")
            return LLMReply(ok=False, error="后台模型暂时退避，规则继续运行" + (f"；上次失败：{reason}" if reason else ""))
        active = getattr(self, "_background_active", 0)
        if active >= self.world.limits.background_concurrency:
            return LLMReply(ok=False, error="后台通道忙，留待下一次")
        self._background_active = active + 1
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
        day = self._today_key(now)
        admitted = False
        usage = None
        try:
            budget = await self.db.call("reserve_background", day, calls=self.world.limits.background_daily_calls,
                                        tokens=self.world.limits.background_daily_tokens, reserve=reserve)
            if not budget["allowed"]:
                return LLMReply(ok=False, error=budget["reason"])
            admitted = True
            with collect_llm_usage(propagate=True) as usage:
                # Tick never spends a full provider timeout holding a character lock.
                timeout = min(2, self.world.limits.background_llm_timeout_seconds) if CRITICAL_TICK.get() and not LIFE_WORK.get() else self.world.limits.background_llm_timeout_seconds
                async with released_life_lock(self):
                    async with asyncio.timeout(timeout):
                        reply = await channel.generate(**kwargs)
            if reply is None or not getattr(reply, "ok", True):
                raise RuntimeError(str(getattr(reply, "error", "") or "后台模型未返回有效内容")[:500])
            failures.pop(failure_key, None)
            return reply
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            count = min(8, int(failure.get("count", 0)) + 1)
            failures[failure_key] = {"count": count, "until": self._now() + min(3600, 30 * 2 ** (count - 1)),
                                     "error": safe_error(exc) or type(exc).__name__}
            raise
        finally:
            self._background_active -= 1
            if admitted:
                measured = usage.total if usage and usage.responses and usage.reported_responses == usage.responses and not usage.incomplete else None
                await self.db.call("settle_background", day, reserve=reserve, measured=measured)

    def reconcile_life_time(self, state) -> float:
        """Reconcile elapsed needs/rest; never execute tools, messages or projects offline."""
        now = self._now()
        previous = float(state.life_updated_at or 0)
        state.life_updated_at = now
        if not previous:
            return self.tick_seconds  # Legacy saves have no reliable life cursor.
        elapsed = max(0, now - previous)
        if elapsed <= self.tick_seconds * 2.5:
            return self.tick_seconds
        gap = min(elapsed, 24 * 3600)
        missing_ticks = max(0, int(gap / self.tick_seconds) - 1)
        old_tick = state.world_time
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
        if can_recover and (sleeping or passive_recovery(definition)):
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
        elif current:
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
