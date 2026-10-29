"""Local activity windows shared by planning and execution; no sleep assumptions."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .models import ActionDef, WorldConfig


def window_allows(window: str, world: WorldConfig, hour: int) -> bool:
    if window == "any" or world.night.start_hour % 24 == world.night.end_hour % 24:
        return True
    night = world.is_night_time(hour)
    return night if window == "night" else not night


def matter_start_at(step: dict, action: ActionDef | None, world: WorldConfig, now: float,
                    local_at: Callable[[float], datetime], *, travel: ActionDef | None = None) -> float | None:
    """Intersect the saved earliest date, project window and action window.

    Old steps inherit the quiet-life preference without rewriting saved effort.
    Explicit nocturnal work is allowed; conflicting explicit windows need replan.
    """
    action_window = action.preconditions.time_window if action else "any"
    window = step.get("time_window", "auto")
    if window == "auto":
        window = action_window if action_window != "any" else "daytime" if world.night.quiet_life else "any"
    base = max(now, float(step.get("not_before") or 0))
    windows = (window, action_window)
    start = _next_window_at(base, windows, world, local_at)
    if start is None or travel is None:
        return start
    travel_window = travel.preconditions.time_window
    combined = _next_window_at(base, (*windows, travel_window), world, local_at)
    # Disjoint windows can still form a valid journey: travel in the daytime,
    # then wait at the destination for a nocturnal task. Arrival checks again.
    return combined if combined is not None else _next_window_at(base, (travel_window,), world, local_at)


def _next_window_at(base: float, windows: tuple[str, ...], world: WorldConfig,
                    local_at: Callable[[float], datetime]) -> float | None:
    local = local_at(base)
    if all(window_allows(w, world, local.hour) for w in windows):
        return base
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    for offset in range(1, 49):
        candidate = midnight + timedelta(hours=local.hour + offset)
        if candidate.tzinfo is not None:
            candidate = datetime.fromtimestamp(candidate.timestamp(), candidate.tzinfo)
        if all(window_allows(w, world, candidate.hour) for w in windows):
            return base + candidate.timestamp() - local.timestamp()
    return None


def life_time_context(world: WorldConfig, local: datetime) -> dict:
    return {"date_time": local.isoformat(timespec="minutes"), "timezone": world.timezone,
            "hour": local.hour, "night": world.is_night_time(local.hour),
            "night_start_hour": world.night.start_hour, "night_end_hour": world.night.end_hour,
            "quiet_life": world.night.quiet_life, "rhythm_preference": world.sleep.rhythm_prompt,
            "sleep_mode": world.sleep.mode}


def quiet_life_note(world: WorldConfig, local: datetime | None) -> str:
    if not local or not world.night.quiet_life or not world.is_night_time(local.hour):
        return ""
    return ("现在是当地夜间，普通生活优先安静休息、室内整理或符合人设的恢复活动；"
            "外出办事、逛店、运动等先考虑当前时段是否合适，不凭空认定商店营业或他人仍在工作。"
            "明确的夜间任务、夜行习惯、约定或紧急情况可以另作安排。"
            "无需睡眠的角色仍有昼夜生活节奏，可充电或安静活动，不因此安排睡觉。"
            + (f"作息偏好：{world.sleep.rhythm_prompt}" if world.sleep.rhythm_prompt else ""))
