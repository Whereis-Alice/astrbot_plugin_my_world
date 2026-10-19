"""低频的自主作息决策；不通过发言概率触发，不调用外部工具。"""

from __future__ import annotations

from ..model_calls import model_operation

import asyncio
import json
import math
from typing import Any

from ..json_actions import extract_json_object
from ..planner import active_plan, create_plan
from ..pathfinding import find_path
from ..state import STATE_DROWSY, STATE_IDLE, WorldState
from .models import TickOutcome


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


class SleepRhythmMixin:
    def _cancel_pending_sleep(self, state: WorldState, *, reason: str, source: str = "cancel",
            postpone: bool = True) -> bool:
        """取消尚未开始的睡眠；保留真实睡眠、清醒负担和历史。"""

        if state.is_sleeping or str((state.current_action or {}).get("type")) in {"sleep", "nap"}:
            return False
        rhythm = state.sleep_rhythm
        if rhythm.get("decision") != "sleep" and not state.drowsy_sleep_step and state.state != STATE_DROWSY:
            return False
        state.drowsy_sleep_step = {}
        state.drowsy_started_world_time = 0
        state.drowsy_started_at = 0.0
        if state.state == STATE_DROWSY:
            state.state = STATE_IDLE
        for key in ("retry_at", "event_notice_at", "duration_seconds"):
            rhythm.pop(key, None)
        minutes = self.world.sleep.evaluation_minutes
        rhythm.update(decision="continue", source=source, reason=reason,
            next_evaluation_at=self._now() + minutes * 60, planned_wake_at=0)
        # 普通活动规划也不能立刻把刚取消的睡眠排回来；过劳保护仍有效。
        if postpone:
            grace = max(1, int(math.ceil(minutes * 60 / self.tick_seconds)))
            state.no_sleep_until = max(state.no_sleep_until, state.world_time + grace)
        return True

    def _sync_sleep_rhythm_mode(self, state: WorldState) -> None:
        """读取生活状态时协调新模式，避免旧的待睡步骤在下一拍继续。"""

        rhythm = state.sleep_rhythm
        mode = self.world.sleep.mode
        previous = rhythm.get("mode")
        plan = state.current_plan or {}
        pending_source = state.drowsy_sleep_step.get("plan_source") or (
            "manual" if plan.get("manual") else plan.get("source"))
        autonomous_pending = rhythm.get("decision") == "sleep" or pending_source == "sleep_rhythm"
        # 升级前没有 mode 标记：自主待睡决定可识别，其余只补上当前模式。
        if previous is None:
            previous = "autonomous" if autonomous_pending else mode
        rhythm["mode"] = mode
        if previous == mode:
            return
        sleeping = state.is_sleeping or str((state.current_action or {}).get("type")) in {"sleep", "nap"}
        if sleeping:
            return  # 本段已经开始的睡眠按原时长完成，不虚构一次醒来。
        automatic = pending_source in {"sleep_rhythm", "forced", "schedule", "rule", "llm", "extreme"}
        has_sleep = any(step.get("action") == "sleep" and step.get("status") != "done"
            for step in plan.get("steps", []) if isinstance(step, dict))
        pending_automatic = automatic and (has_sleep or bool(state.drowsy_sleep_step))
        if pending_automatic:
            state.current_plan = None
            if state.current_action:
                state.current_action["from_plan"] = False
        if autonomous_pending or pending_automatic:
            self._cancel_pending_sleep(state, reason="作息模式已切换，取消原来的待睡安排",
                source="mode_change", postpone=False)
        # 新模式可以重新判断；切回自主不会复用过时的模型结论。
        rhythm.pop("retry_at", None)
        rhythm["next_evaluation_at"] = 0

    def _rhythm_duration_bounds(self) -> tuple[float, float]:
        config = self.world.sleep
        low, high = sorted((float(config.min_hours), float(config.max_hours)))
        return low * 3600, high * 3600

    def _rhythm_awake_hours(self, state: WorldState) -> float:
        now = self._now()
        start = _number(state.sleep_rhythm.get("awake_since"))
        if start <= 0 or start > now:
            # 旧存档没有清醒起点，从现有困意估计，不编造最近睡眠记录。
            start = now - min(18.0, max(0.0, float(state.sleep_pressure)) * 20) * 3600
            state.sleep_rhythm["awake_since"] = start
        return max(0.0, (now - start) / 3600)

    @model_operation("sleep", "生活时钟检查到睡眠决策时机")
    async def _tick_sleep_rhythm(self, state: WorldState, outcome: TickOutcome) -> None:
        if self.world.sleep.mode != "autonomous" or state.is_sleeping:
            return
        config = self.world.sleep
        awake = self._rhythm_awake_hours(state)
        rhythm = state.sleep_rhythm
        now = self._now()
        exhausted = awake >= config.max_awake_hours or (state.energy <= 0.08 and awake >= 8)
        if state.state == STATE_DROWSY:
            if exhausted:
                state.sleep_delay_until = 0
                await self._go_to_sleep(state, self.node(state.node_id), outcome, reason="过劳保护，临睡期不再继续拖延")
            return
        # 用户刚叫醒/要求再待一会儿依旧有效；连续清醒上限不被无限续期拖过。
        if not exhausted and (state.world_time < state.no_sleep_until or state.sleep_delayed):
            return
        if exhausted:
            if now < _number(rhythm.get("retry_at")):
                return
            if rhythm.get("decision") != "sleep" or rhythm.get("source") != "safety":
                await self._save_rhythm_decision(state, outcome, "sleep", 9, 15,
                    "连续清醒太久或严重疲劳，停止继续熬夜", "safety")
            state.sleep_delay_until = 0
            state.no_sleep_until = 0
            await self._run_rhythm_sleep(state, outcome, force=True)
            return
        if rhythm.get("decision") == "sleep":
            await self._run_rhythm_sleep(state, outcome)
            return
        if now < _number(rhythm.get("next_evaluation_at")):
            return
        if awake < 8 and state.sleep_pressure < 0.6:
            return  # 刚睡饱后的短时疲劳优先交给普通休息/小睡，不再睡一整晚。
        hour = self.local_now().hour
        evening = hour >= 20 or hour < 7
        # 白天精神好不逐小时问模型；到评估窗口后仍可自由选择继续活动。
        if awake < 12 and state.energy > 0.35 and state.sleep_pressure < 0.6:
            return
        if not evening and awake < 16 and state.energy > 0.25 and state.sleep_pressure < 0.8:
            return
        interval = int(config.evaluation_minutes)
        rhythm["next_evaluation_at"] = now + interval * 60
        low, high = self._rhythm_duration_bounds()
        hours = min(high / 3600, max(low / 3600, 8.0 + max(0, awake - 16) * 0.25))
        decision = "sleep" if awake >= 17 or state.energy <= 0.2 or state.sleep_pressure >= 0.85 else "continue"
        reason = "结合清醒时长与疲劳决定休息" if decision == "sleep" else "还可以继续活动，稍后再评估"
        source = "fallback"
        if self.llm is not None:
            schedules = []
            for item in getattr(self.schedules, "schedules", []):
                if item.enabled and self.schedule_in_scope(item, state.session_id) and not any(step.type == "sleep" for step in item.action_chain):
                    schedules.append({"id": item.id, "time": str(item.time),
                        "actions": [step.type for step in item.action_chain]})
            current = state.current_action or {}
            own_topics = list(getattr(state, "own_topics", []) or [])[-3:]
            context = {
                "当地时间": self.local_now().isoformat(timespec="minutes"),
                "清醒小时": round(awake, 2), "精力": round(state.energy, 3),
                "困意": round(state.sleep_pressure, 3), "心情": state.mood,
                "正在做": str(current.get("desc") or current.get("type") or "自由活动"),
                "最近睡眠": state.sleep_history[-4:], "当天的事": own_topics,
                "日程": schedules[:12], "上次决定": str(rhythm.get("reason") or ""),
                "最近经历": state.recent_events[-6:],
                "经历摘要": state.event_digest[-4:],
            }
            persona = await self._persona_text(state.session_id)
            prompt = (
                f"人设：\n{persona[:6000]}\n作息偏好：{config.rhythm_prompt[:1200]}\n"
                + json.dumps(context, ensure_ascii=False, default=str)
                + f"\n决定现在入睡还是继续活动。不是必须到固定钟点才睡；"
                f"根据真实疲劳、睡眠不足、当日事情与人设作决定，不为了每天不同随便掷时间。"
                f"小睡不足不能当完整的一晚；普通群聊热闹不代表必须一直陪聊。"
                f"睡眠时长在 {low/3600:g}~{high/3600:g} 小时，清醒上限 {config.max_awake_hours:g} 小时。"
                f"继续活动最多 {interval} 分钟后再看。只输出 JSON："
                '{"decision":"sleep 或 continue","hours":8,"review_minutes":60,"reason":"具体理由"}'
            )
            try:
                reply = await asyncio.wait_for(self._ask_llm(state.session_id,
                    "你正在为自己的生活决定作息。只判断与解释，不发消息、不调用工具。", prompt), timeout=45)
            except asyncio.TimeoutError:
                reply = None
            outcome.llm_calls += 1
            try:
                data = extract_json_object(reply or "")
                if isinstance(data, dict) and data.get("decision") in {"sleep", "continue"}:
                    decision = data["decision"]
                    hours = _number(data.get("hours"), hours)
                    interval = int(min(interval, max(15, _number(data.get("review_minutes"), interval))))
                    reason = str(data.get("reason") or reason).strip()[:240]
                    source = "llm"
            except (ValueError, TypeError):
                pass
        await self._save_rhythm_decision(state, outcome, decision, hours, interval, reason, source)
        if decision == "sleep":
            await self._run_rhythm_sleep(state, outcome)

    async def _save_rhythm_decision(self, state: WorldState, outcome: TickOutcome,
            decision: str, hours: float, interval: int, reason: str, source: str) -> None:
        low, high = self._rhythm_duration_bounds()
        duration = int(min(high, max(low, _number(hours, 8) * 3600)))
        state.sleep_rhythm.update(decision=decision, duration_seconds=duration,
            evaluated_at=self._now(), next_evaluation_at=self._now() + interval * 60,
            reason=reason, source=source)
        await self._log_event(state, "sleep_rhythm", {
            "note": f"{'准备入睡' if decision == 'sleep' else '继续活动'}：{reason}",
            "decision": decision, "hours": round(duration / 3600, 2), "source": source,
        }, outcome=outcome)

    async def _run_rhythm_sleep(self, state: WorldState, outcome: TickOutcome, *, force: bool = False) -> None:
        if state.is_sleeping or state.state == STATE_DROWSY:
            return
        if not force and (state.world_time < state.no_sleep_until or state.sleep_delayed):
            return
        if self._now() < _number(state.sleep_rhythm.get("retry_at")):
            return
        if await self._defer_rhythm_for_event(state, outcome):
            return
        definition = self.world.action_map().get("sleep")
        target = self.world.sleep_node_id()
        if definition is None or not definition.enabled or not target:
            # 配置未提供睡觉动作/地点，不能虚构动作或每 tick 刷错误。
            state.sleep_rhythm["decision"] = "continue"
            state.sleep_rhythm["next_evaluation_at"] = self._now() + 3600
            state.sleep_rhythm["retry_at"] = self._now() + 3600
            await self._log_event(state, "skip", {"action": "sleep", "note": "自主作息需要启用睡觉动作并配置可用的睡眠地点"})
            return
        if state.node_id != target:
            movement = self.world.action_map().get("walk_to")
            if movement is None or not movement.enabled or find_path(self.world.adjacent(), state.node_id, target) is None:
                state.sleep_rhythm["retry_at"] = self._now() + 900
                await self._log_event(state, "skip", {"action": "sleep", "note": "无法走到睡眠地点，请检查地图连线与移动动作；15 分钟后重试"})
                return
        current = state.current_action or {}
        if current or active_plan(state):
            # 普通睡意先收尾；过劳时可以终止持续动作，但先走完正在行走的这一段。
            plan = active_plan(state) or {}
            if plan.get("source") in {"sleep_rhythm", "forced"} and any(
                step.get("action") == "sleep" and step.get("status") != "done"
                for step in plan.get("steps", [])
            ):
                return
            if not force or current.get("type") == "walk_to":
                return
            self.pause_matter_action(state, current)
            state.current_action = None
            state.current_plan = None
            state.state = STATE_IDLE
            await self._log_event(state, "interrupt", {"action": current.get("type"), "note": "过劳保护，结束当前事情去休息"})
        steps = []
        if state.node_id != target:
            steps.append({"action": "walk_to", "target_node": target})
        steps.append({"action": "sleep", "duration": int(state.sleep_rhythm["duration_seconds"])})
        plan = create_plan(steps=steps, world_time=state.world_time, now=self._now(), tick_seconds=self.tick_seconds,
            valid_for=12 * 3600,
            reason=str(state.sleep_rhythm.get("reason") or "休息"), source="forced" if force else "sleep_rhythm")
        await self._apply_plan(state, self.node(state.node_id), outcome, plan)
        await self._tick_plan(state, self.node(state.node_id), outcome, depth=0)

    async def _defer_rhythm_for_event(self, state: WorldState, outcome: TickOutcome) -> bool:
        """入睡前再次检查关键事件；结束后立即让作息继续，日志最多每 15 分钟一条。"""

        if self.world.sleep.mode != "autonomous":
            return False
        thread = self._active_thread(state)
        if not thread or not self._thread_critical(thread):
            state.sleep_rhythm.pop("event_notice_at", None)
            return False
        if self._now() >= _number(state.sleep_rhythm.get("event_notice_at")):
            state.sleep_rhythm["event_notice_at"] = self._now() + 900
            await self._log_event(state, "skip", {
                "action": "sleep", "note": "危险事件还在关键阶段，先处理眼前事情再休息",
            }, outcome=outcome)
        return True

    def _start_sleep_rhythm(self, state: WorldState, kind: str, duration_ticks: int) -> None:
        state.sleep_rhythm.update(sleep_started_at=self._now(), sleeping_kind=kind,
            planned_wake_at=self._now() + duration_ticks * self.tick_seconds,
            awake_before_sleep=self._rhythm_awake_hours(state), decision="sleeping")

    def _finish_sleep_rhythm(self, state: WorldState, action: dict, *, interrupted: bool) -> None:
        if action.get("sleep_recorded"):
            return
        action["sleep_recorded"] = True
        seconds = max(0, int(action.get("elapsed_ticks") or 0)) * self.tick_seconds
        kind = str(action.get("type") or "sleep")
        rhythm = state.sleep_rhythm
        started = _number(rhythm.get("sleep_started_at")) or self._now() - seconds
        low, _ = self._rhythm_duration_bounds()
        state.sleep_history.append({"kind": kind, "started_at": started, "ended_at": self._now(),
            "hours": round(seconds / 3600, 2), "interrupted": interrupted,
            "disturbances": int(state.startled_count or 0), "rested": kind == "sleep" and seconds >= low,
            "reason": str(rhythm.get("reason") or "")[:240]})
        state.sleep_history = state.sleep_history[-7:]
        # 小睡只抵消部分清醒负担；短暂叫醒也不能洗掉整晚的睡眠不足。
        before = _number(rhythm.get("awake_before_sleep"), 12)
        rhythm.pop("retry_at", None)
        effective_awake = 0 if kind == "sleep" and seconds >= low else max(0, before - seconds / 3600 * 2)
        rhythm.update(awake_since=self._now() - effective_awake * 3600, decision="awake",
            next_evaluation_at=self._now() + self.world.sleep.evaluation_minutes * 60,
            last_wake_at=self._now(), planned_wake_at=0, sleep_started_at=0)
        state.sleep_started_at = 0
