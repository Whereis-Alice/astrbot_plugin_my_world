"""Explicit physiological constraints, independent of a character's name or species."""
from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import SleepConfig


def recovery_target(config: SleepConfig) -> float:
    """A recovery cannot end below the threshold that would start it again."""
    return max(config.recovery_threshold, config.recovery_target)


def recovery_needed(energy: float, config: SleepConfig, *, recovering: bool = False) -> bool:
    """Start below the threshold; an existing intent continues to its target.

    This only reads state. The recovery executor owns setting/clearing intent,
    and callers retain their own activity/sleep mode and priority checks.
    """
    return energy < config.recovery_threshold or (recovering and energy < recovery_target(config))


def sleep_action_kind(action) -> str:
    if action is None:
        return ""
    kind = getattr(action, "recovery_kind", "auto")
    if kind != "auto":
        return kind if kind in {"sleep", "nap"} else ""
    if action.id in {"sleep", "nap"}:
        return action.id
    return {"sleeping": "sleep", "napping": "nap"}.get(getattr(action.during, "state", ""), "")


def is_sleep_action(action) -> bool:
    return bool(sleep_action_kind(action))


def recovery_mode_allows(action, world) -> bool:
    return world.sleep.mode != "activity" or not is_sleep_action(action)


def restores_energy(action, *, energy: float = 0.0) -> bool:
    from .state_dynamics import StateDynamics
    for effects in (action.on_complete.effects, action.on_complete.effects_per_minute):
        value = str(effects.get("energy", "")).strip()
        if StateDynamics._apply_operator(value, energy, 1) > energy:
            return True
    return False


def passive_recovery(action) -> bool:
    """Only simulated rest can progress while the runtime is offline."""
    return bool(action and action.category == "continuous" and not action.idle
                and (is_sleep_action(action) or action.recovery_kind == "restore")
                and action.llm_level == "template" and not action.tool_list()
                and not action.trigger_command and action.target_type == "none"
                and action.on_complete.trigger == "none")


def recovery_cost(action, world, state=None) -> float:
    from .mood import day_mood_rate
    config = world.state_dynamics
    rate = day_mood_rate(getattr(state, "day_mood", ""), "energy", strength=config.daily_mood_strength) if config.daily_mood_enabled else 1.0
    return max(0, config.energy_decay_per_min * rate) * (1.2 if action.during.state == "walking" else 1)


def recovery_seconds(action, energy: float, world, state=None) -> int:
    """Estimate needed time within the action's limits; fixed durations remain fixed."""
    if action.duration_mode == "fixed":
        return max(1, action.duration)
    low = max(60, action.duration_min or 60)
    high = max(low, action.duration_max or low)
    per_minute = str(action.on_complete.effects_per_minute.get("energy", ""))
    try:
        rate = float(per_minute) if per_minute.startswith("+") else 0
        completion = str(action.on_complete.effects.get("energy", ""))
        bonus = float(completion) if completion.startswith("+") else 0
    except ValueError:
        rate, bonus = 0, 0
    # Non-sleeping recovery still pays normal energy use. Ignore calming-area
    # bonuses when estimating so the plan does not depend on invented recovery.
    rate -= recovery_cost(action, world, state)
    wanted = recovery_target(world.sleep) - energy - bonus
    return min(high, max(low, math.ceil(max(0, wanted) / rate * 60))) if rate > 0 else low


def recovery_gain(action, energy: float, world, seconds: int, state=None) -> float:
    """Conservative net benefit; don't repeatedly choose an exhausting 'rest'."""
    from .state_dynamics import StateDynamics
    minutes = seconds / 60
    value = energy - recovery_cost(action, world, state) * minutes
    per_minute = action.on_complete.effects_per_minute
    incremental = str(per_minute.get("energy", "")).strip().startswith(("+", "-"))
    # Combine elapsed consumption and incremental recovery before clamping.
    # Clamping the entire cost first invents a positive yield near empty energy.
    if incremental:
        value = StateDynamics._apply_operator(str(per_minute["energy"]), value, minutes)
    value = max(0, min(1, value))
    ordered = ((action.on_complete.effects, 1),) if incremental else (
        (action.on_complete.effects, 1), (per_minute, 1))
    for effects, scale in ordered:
        if "energy" in effects:
            value = StateDynamics._apply_operator(str(effects["energy"]), value, scale)
            value = max(0, min(1, value))
    return value - energy


def recovery_note(world) -> str:
    if world.sleep.mode != "activity":
        return ""
    action = world.action_map().get(world.sleep.recovery_action_id)
    name = (action.name or action.id) if action else "已配置的精力恢复活动"
    return (f"生理设定：你不需要睡眠，通过{name}恢复精力。精力低代表需要恢复，"
            "不代表犯困；不安排睡觉、小睡、熬夜补觉或起床气。"
            "恢复活动的地点、时长、效果与能否中断以动作配置为准。")
