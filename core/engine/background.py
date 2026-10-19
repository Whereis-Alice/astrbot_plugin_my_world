"""Bounded background work, deadlines, backoff and conservative offline reconciliation."""
from __future__ import annotations

import asyncio
import time
from contextvars import ContextVar
from typing import Any

from ..llm_usage import collect_llm_usage
from ..model_calls import call_context

CRITICAL_TICK = ContextVar("world_critical_tick", default=False)


class BackgroundMixin:
    def queue_background(self, key: str, work) -> asyncio.Task | None:
        jobs = getattr(self, "_background_jobs", None)
        if jobs is None:
            jobs = self._background_jobs = {}
        if getattr(self, "_background_closed", False):
            return None
        current = jobs.get(key)
        if current and not current.done():
            return current
        # Admission is bounded, including queued jobs. Low-priority work retries next beat.
        if sum(not task.done() for task in jobs.values()) >= max(8, self.world.limits.background_concurrency * 4):
            return None

        async def run():
            try:
                return await work()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log("debug", f"后台工作 {key} 留待下次：{type(exc).__name__}")
            finally:
                jobs.pop(key, None)
        task = asyncio.create_task(run(), name="my_world:" + key)
        jobs[key] = task
        return task

    async def close_background(self) -> None:
        self._background_closed = True
        tasks = list(getattr(self, "_background_jobs", {}).values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _budgeted_generate(self, channel, **kwargs: Any):
        from ..ports import LLMReply
        context = call_context()
        purpose = str(context.get("purpose") or "other")
        interactive = purpose in {"reply", "chat", "action", "tool_params", "followup"} and not CRITICAL_TICK.get()
        if interactive:
            async with asyncio.timeout(self.world.limits.background_llm_timeout_seconds):
                return await channel.generate(**kwargs)
        now = self._now()
        failures = getattr(self, "_background_failures", {})
        self._background_failures = failures
        failure_key = (id(channel), purpose)
        failure = failures.get(failure_key, {})
        if failure.get("until", 0) > now:
            return LLMReply(ok=False, error="后台模型暂时退避，规则继续运行")
        active = getattr(self, "_background_active", 0)
        if active >= self.world.limits.background_concurrency:
            return LLMReply(ok=False, error="后台通道忙，留待下一次")
        self._background_active = active + 1
        reserve = len(str(kwargs.get("system_prompt", ""))) + len(str(kwargs.get("prompt", ""))) + int(kwargs.get("max_tokens") or 2048)
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
                timeout = min(2, self.world.limits.background_llm_timeout_seconds) if CRITICAL_TICK.get() else self.world.limits.background_llm_timeout_seconds
                async with asyncio.timeout(timeout):
                    reply = await channel.generate(**kwargs)
            if reply is None or not getattr(reply, "ok", True):
                raise RuntimeError("后台模型未返回有效内容")
            failures.pop(failure_key, None)
            return reply
        except asyncio.CancelledError:
            raise
        except Exception:
            count = min(8, int(failure.get("count", 0)) + 1)
            failures[failure_key] = {"count": count, "until": now + min(3600, 30 * 2 ** (count - 1))}
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
        state.world_time += missing_ticks
        # Translate relative counters by the elapsed life clock.
        current = state.current_action or {}
        if current.get("type") in {"sleep", "nap"}:
            left = max(0, int(current.get("duration_ticks", 0)) - int(current.get("elapsed_ticks", 0)))
            slept = min(left, missing_ticks)
            current["elapsed_ticks"] = int(current.get("elapsed_ticks", 0)) + slept
            definition = self.world.action_map().get(str(current.get("type")))
            if definition and slept:
                self.dynamics.apply_effects(state, definition.on_complete.effects_per_minute,
                    world=self.world, scale=slept * self.tick_seconds / 60, now=now)
                current["effects_settled_minutes"] = float(current.get("effects_settled_minutes", 0)) + slept * self.tick_seconds / 60
            if slept >= left:
                current["offline_wake"] = True
                state.goodmorning_day = self._today_key(now)
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
        state.add_event("offline_recovery", dict(state.life_recovery))
        return min(gap, 6 * 3600) if not state.is_sleeping else self.tick_seconds
