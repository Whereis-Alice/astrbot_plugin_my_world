"""Quiet daily life and bounded relief of needs, without model calls or messaging."""

from __future__ import annotations

from typing import Any

from .models import ActionDef, WorldConfig
from .state import WorldState


def life_role(action: ActionDef | None) -> str:
    if action is None:
        return "none"
    if action.life_role != "auto":
        return action.life_role
    if action.id in {"sleep", "nap", "rest"}:
        return "rest"
    if action.id in {"walk_to", "wander", "search_web"}:
        return "explore"
    if action.target_type == "user":
        return "social"
    if action.target_type != "none" or action.id in {"say", "share", "think", "stare"}:
        return "none"
    if action.category == "continuous" or action.idle:
        return "interest"
    return "none"


def safe_idle_action(action: ActionDef) -> bool:
    return (
        action.idle
        and action.category == "continuous"
        and action.llm_level == "template"
        and action.target_type == "none"
        and not action.tool_list()
        and not action.trigger_command
        and action.on_complete.trigger == "none"
    )


def idle_definition(state: WorldState, world: WorldConfig) -> ActionDef | None:
    if not world.idle_life.enabled or state.current_action or state.state != "idle":
        return None
    item = state.idle_activity or {}
    action = world.action_map().get(str(item.get("id") or ""))
    if (
        not action
        or not safe_idle_action(action)
        or not action.available_in(state.node_id)
        or item.get("node_id") != state.node_id
        or int(item.get("until_tick") or 0) <= state.world_time
    ):
        return None
    return action


def current_life_role(state: WorldState, world: WorldConfig | None) -> tuple[str, float]:
    if world is None:
        return "none", 0.0
    if state.is_sleeping:
        return "rest", 1.0
    if state.current_action:
        return life_role(world.action_map().get(str(state.current_action.get("type") or ""))), 1.0
    idle = idle_definition(state, world)
    return life_role(idle), world.idle_life.relief_scale if idle else 0.0


def activity_view(state: WorldState, world: WorldConfig) -> dict[str, Any]:
    current = state.current_action or {}
    if current:
        definition = world.action_map().get(str(current.get("type") or ""))
        label = str(
            current.get("desc") or (definition.name if definition else "") or current.get("type")
        )
        if current.get("type") == "walk_to":
            target = world.node_map().get(str(current.get("target_node") or ""))
            if target:
                label = f"正在前往{target.name or target.id}"
        return {
            "kind": "action",
            "label": label,
            "id": current.get("type", ""),
            "detail": definition.description if definition else "",
        }
    if state.is_sleeping:
        return {"kind": "rest", "label": "睡觉" if state.state == "sleeping" else "小睡"}
    idle = idle_definition(state, world)
    if idle:
        return {
            "kind": "idle",
            "label": idle.name or idle.id,
            "id": idle.id,
            "detail": idle.description,
            "until_tick": state.idle_activity.get("until_tick"),
        }
    if state.state != "idle":
        return {"kind": "state", "label": state.state}
    return {"kind": "idle", "label": "自由休息"}


def completion_relief(action: ActionDef, *, elapsed_minutes: float = 0) -> dict[str, float]:
    """Fallback only: explicit effects own their fields, and speech alone earns no relief."""
    role = life_role(action)
    effects = {
        "rest": {"boredom": -0.015, "loneliness": -0.006},
        "interest": {"boredom": -0.035, "loneliness": -0.012},
        "explore": {"boredom": -0.025, "loneliness": -0.008},
        "social": {"boredom": -0.02, "loneliness": -0.04},
    }.get(role, {})
    explicit = set(action.on_complete.effects) | set(action.on_complete.effects_per_minute)
    # Very short repeated actions cannot instantly empty the needs bars.
    scale = min(1.0, max(0.1, elapsed_minutes / 20.0))
    return {key: value * scale for key, value in effects.items() if key not in explicit}


def event_relief(tier: str, explicit: dict[str, Any]) -> dict[str, float]:
    # A setback can be absorbing too; it does not count as company.
    effects = {"boredom": -0.015}
    if tier in {"success", "great", "narrow", "good", "daily"}:
        effects["loneliness"] = -0.008
    return {key: value for key, value in effects.items() if key not in explicit}
