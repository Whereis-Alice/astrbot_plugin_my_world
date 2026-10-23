"""Schedule a configured recovery activity without imposing human sleep."""
from __future__ import annotations

from ..planner import active_plan
from ..recovery import is_sleep_action, restores_energy, recovery_gain, recovery_needed, recovery_target


class RecoveryMixin:
    def activity_review_mode(self, state):
        """Sleep-free characters still review once a day during quiet time."""
        if not self.world.memory.sleep_review and not (self.world.profile.enabled and self.world.profile.consolidate_enabled):
            return None
        now, cycle = self._now(), self._sleep_review_cycle(state)
        review = state.sleep_rhythm.get("review")
        if not isinstance(review, dict) or review.get("cycle") != cycle:
            review = {"cycle": cycle, "mark": now, "passes": 0, "retry_at": 0}
            state.sleep_rhythm["review"] = review
        if review.get("passes") or now < review.get("retry_at", 0):
            return None
        if now - review["mark"] < max(1, self.world.profile.sleep_consolidate_minutes) * 60:
            return None
        if self.native_turn_busy(state.session_id):
            return None
        thread = self._active_thread(state)
        if thread and self._thread_critical(thread):
            return None
        current = self.world.action_map().get(str((state.current_action or {}).get("type") or ""))
        if current and not (current.idle or restores_energy(current, energy=state.energy)):
            return None
        return "full"

    def settle_recovery_progress(self, state, action) -> None:
        definition = self.world.action_map().get(str(action.get("type") or ""))
        if not definition or not (is_sleep_action(definition) or definition.recovery_kind == "restore"
                                   or restores_energy(definition, energy=state.energy)):
            return
        effects = {key: value for key, value in definition.on_complete.effects_per_minute.items()
                   if str(value).strip().startswith(("+", "-"))}
        elapsed = max(0, int(action.get("elapsed_ticks") or 0)) * self.tick_seconds / 60
        minutes = max(0, elapsed - float(action.get("effects_settled_minutes") or 0))
        if minutes:
            if effects:
                self.dynamics.apply_effects(state, effects, world=self.world, scale=minutes, now=self._now())
            action["effects_settled_minutes"] = elapsed

    def sync_activity_recovery(self, state) -> None:
        """Drop incompatible old sleep state without granting completion rewards."""
        state.sleep_pressure = 0
        state.drowsy_sleep_step = {}
        state.drowsy_started_world_time = 0
        state.drowsy_started_at = 0
        action = state.current_action or {}
        definition = self.world.action_map().get(str(action.get("type") or ""))
        if definition and is_sleep_action(definition):
            self.settle_recovery_progress(state, action)
            state.current_action = None
        if state.state in {"sleeping", "napping", "drowsy"}:
            state.state = "idle"
        plan = state.current_plan or {}
        definitions = self.world.action_map()
        if any(step.get("status") != "done" and step.get("action") in definitions
               and is_sleep_action(definitions[step["action"]]) for step in plan.get("steps", [])):
            state.current_plan = None
            if state.current_action:
                state.current_action["from_plan"] = False
        if state.sleep_rhythm.get("mode") != "activity":
            state.sleep_rhythm = {"mode": "activity", "decision": "continue"}
            state.grumpy_until = 0
            state.grumpy_note = ""

    async def tick_activity_recovery(self, state, outcome) -> None:
        config = self.world.sleep
        if state.energy >= recovery_target(config):
            state.sleep_rhythm.pop("recovering", None)
            return
        if recovery_needed(state.energy, config):
            state.sleep_rhythm["recovering"] = True
        if not state.sleep_rhythm.get("recovering"):
            return
        if self.native_turn_busy(state.session_id):
            return
        thread = self._active_thread(state)
        if thread and self._thread_critical(thread):
            return
        current = state.current_action or {}
        definition = self.world.action_map().get(str(current.get("type") or ""))
        remaining = max(0, int(current.get("duration_ticks", 0)) - int(current.get("elapsed_ticks", 0))) * self.tick_seconds
        if definition and recovery_gain(definition, state.energy, self.world, remaining, state) > 0:
            return
        if (active_plan(state) or {}).get("source") == "recovery" and (not current or current.get("type") == "walk_to"):
            return
        if self._now() < float(state.sleep_rhythm.get("retry_at") or 0):
            return
        plan = self.decider.recovery_plan(state)
        if not plan:
            state.sleep_rhythm["retry_at"] = self._now() + 900
            await self._log_event(state, "skip", {"note": "精力低，但恢复动作未启用、没有正向精力效果、次数用尽或地点不可达；请检查恢复动作配置"}, outcome=outcome, silent=True)
            return
        if current or active_plan(state):
            if state.energy > 0.08:
                return
            if not self.admit_life(state, "rest", force=True)["allow"]:
                return
            await self._log_event(state, "interrupt", {"action": current.get("type"),
                "note": "精力严重不足，保留当前进度后去恢复"}, outcome=outcome)
        await self._apply_plan(state, self.node(state.node_id), outcome, plan)
        await self._tick_plan(state, self.node(state.node_id), outcome, depth=0)
