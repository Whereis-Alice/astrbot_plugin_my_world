"""计划队列（设计文档 4.6 节）。

计划是「一段有效期内的动作序列」，例如 [walk_to bedroom, sleep]。
Tick 每推进一次就调用一次 ``tick``，由引擎执行返回的步骤。
"""

from __future__ import annotations

from typing import Any
import math
from uuid import uuid4


def _read_pages(value: Any) -> int:
    """她写的"读几篇"：没写或写坏都当 -1（按动作配置来），0 也是有效值。"""

    if value is None or value == "":
        return -1
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return -1
    return number if number >= 0 else -1


def create_plan(
    *,
    steps: list[dict[str, Any]],
    world_time: int,
    valid_for: int = 1800,
    reason: str = "",
    source: str = "rule",
    send_to: str = "",
    now: float | None = None,
    tick_seconds: float = 60.0,
) -> dict[str, Any] | None:
    """构造计划对象。steps 为空时返回 None。"""

    normalized: list[dict[str, Any]] = []
    for step in steps or []:
        action = str(step.get("action") or step.get("type") or "").strip()
        if not action:
            continue
        normalized.append(
            {
                "action": action,
                # 这一步的来路（``reply`` = 排它的时候正有人在跟她说话）：
                # 轮到她做的时候要靠它判断"这算回话还是她主动开口"
                "kind": str(step.get("kind", "") or ""),
                "target_node": str(step.get("target_node", "") or ""),
                "target": str(step.get("target", "") or ""),
                "duration": int(step.get("duration", 0) or 0),
                "content": str(step.get("content", "") or ""),
                # 工具型动作靠 intent 说明"想做什么"，丢了它参数就补不出来
                "intent": str(step.get("intent", "") or ""),
                # 检索型动作自己写的查询词同理：丢了会退化成按主题兜一条
                "queries": [str(item) for item in (step.get("queries") or []) if str(item)],
                "search_depth": str(step.get("search_depth", "") or ""),
                "read_pages": _read_pages(step.get("read_pages")),
                "messages": list(step.get("messages") or []),
                "params": dict(step.get("params") or {}),
                "interject": bool(step.get("interject", False)),
                # 这一步说给哪个会话听（空 = 跟着计划 / 这一轮的落点）
                "send_to": str(step.get("send_to", "") or ""),
                "status": "pending",
            }
        )
    if not normalized:
        return None
    return {
        "id": uuid4().hex,
        "steps": normalized,
        "current_step": 0,
        "created_at": world_time,
        "valid_until": world_time + max(1, math.ceil(max(60, int(valid_for)) / max(1, tick_seconds))),
        "clock": "seconds_v2",
        "created_ts": float(now or 0),
        "expires_at": float(now) + max(60, int(valid_for)) if now is not None else 0,
        "reason": reason,
        "source": source,
        # 她想把这些话说给谁（会话组里的哪个群 / 私聊）；空 = 落点默认
        "send_to": str(send_to or ""),
    }


def same_plan(action: dict, plan: dict) -> bool:
    """New plans use an immutable identity; timestamps are legacy-only fallback."""
    if action.get("plan_id") or plan.get("id"):
        return bool(action.get("plan_id")) and action["plan_id"] == plan.get("id")
    recorded = action.get("plan_created_at")
    if recorded is None:
        return True  # Legacy action and legacy plan, neither has an identity.
    try:
        return int(recorded) == int(plan.get("created_at") or 0)
    except (TypeError, ValueError):
        return False


def active_plan(state, *, world_time: int | None = None) -> dict[str, Any] | None:
    """返回当前有效计划；过期则丢弃并返回 None。"""

    plan = state.current_plan
    if not isinstance(plan, dict) or not plan.get("steps"):
        return None
    now = state.world_time if world_time is None else world_time
    deadline = float(plan.get("expires_at") or 0)
    real_now = float(getattr(state, "plan_now", 0) or 0)
    if deadline and real_now >= deadline:
        state.current_plan = None
        return None
    valid_until = int(plan.get("valid_until", 0) or 0)
    if not deadline and valid_until and now >= valid_until:
        state.current_plan = None
        return None
    return plan


def migrate_plan(state, *, now: float, tick_seconds: float) -> None:
    """旧字段的差值是秒，创建位置是 tick；按已经过去的 tick 还原剩余有效期。"""
    state.plan_now = now
    plan = state.current_plan
    if not isinstance(plan, dict) or plan.get("clock") == "seconds_v2":
        return
    ttl = max(60, int(plan.get("valid_until", 0)) - int(plan.get("created_at", 0)))
    elapsed = max(0, state.world_time - int(plan.get("created_at", 0))) * tick_seconds
    plan.update(clock="seconds_v2", created_ts=now - elapsed, expires_at=now + ttl - elapsed)


def peek_step(state) -> dict[str, Any] | None:
    """查看当前待执行的步骤，不改变状态。"""

    plan = active_plan(state)
    if plan is None:
        return None
    index = int(plan.get("current_step", 0))
    steps = plan.get("steps") or []
    if index >= len(steps):
        state.current_plan = None
        return None
    step = steps[index]
    if step.get("status") in {"done", "skipped", "deferred"}:
        return advance(state)
    return step


def advance(state, *, status: str = "done") -> dict[str, Any] | None:
    """Record the execution outcome and move forward; skipping is not completion."""

    plan = state.current_plan
    if not isinstance(plan, dict):
        return None
    steps = plan.get("steps") or []
    index = int(plan.get("current_step", 0))
    if index < len(steps):
        if steps[index].get("status") not in {"done", "skipped", "deferred"}:
            steps[index]["status"] = status
    plan["current_step"] = index + 1
    if plan["current_step"] >= len(steps):
        state.current_plan = None
        return None
    return steps[plan["current_step"]]


def wait_for(state, step: dict[str, Any]) -> None:
    """把当前步骤标记为「进行中/等待中」。"""

    plan = state.current_plan
    if not isinstance(plan, dict):
        return
    index = int(plan.get("current_step", 0))
    steps = plan.get("steps") or []
    if 0 <= index < len(steps):
        steps[index].update(step)


def clear(state) -> None:
    state.current_plan = None


def describe(state) -> str:
    """给调试命令用的一行描述。"""

    plan = state.current_plan
    if not isinstance(plan, dict):
        return "（没有计划）"
    steps = plan.get("steps") or []
    index = int(plan.get("current_step", 0))
    done = [s.get("action") for s in steps[:index] if s.get("status") == "done"]
    skipped = [s.get("action") for s in steps[:index] if s.get("status") in {"skipped", "deferred"}]
    todo = [s.get("action") for s in steps[index:]]
    return (
        f"来源={plan.get('source')} 原因={plan.get('reason')} "
        f"已完成={'/'.join(str(d) for d in done) or '无'} "
        f"跳过或转交={'/'.join(str(d) for d in skipped) or '无'} "
        f"待执行={'/'.join(str(t) for t in todo) or '无'}"
    )
