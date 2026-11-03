"""Render execution order without promoting speech or plans into present facts."""
from __future__ import annotations

from typing import Any


_SPEECH_ACTIONS = {"say", "share", "think", "poke"}


def action_facts(state: Any, world: Any, *, persona_key: str = "", now: float = 0) -> str:
    """Keep completed, active and pending personal activities distinct and bounded."""
    definitions = world.action_map()

    def own(record: dict) -> bool:
        return not record.get("persona_key") or record["persona_key"] == persona_key

    def label(record: dict) -> str:
        identity = str(record.get("type") or record.get("action") or "")
        definition = definitions.get(identity)
        # Tools have separate result evidence; completing their clock is not success.
        if identity in _SPEECH_ACTIONS or definition is None or definition.llm_level in {"tool", "command"}:
            return ""
        text = str(record.get("action_name") or definition.name or identity)
        if identity == "life_simulation":
            text = str(record.get("intent") or record.get("desc") or text)
        return " ".join(text.split())[:120]

    completed = []
    for event in state.recent_events or []:
        if not isinstance(event, dict) or not own(event) or event.get("kind") not in {"action", "action_done"}:
            continue
        detail = event.get("detail") or {}
        if not isinstance(detail, dict) or not (name := label(detail)):
            continue
        at = float(event.get("at") or 0)
        # Old records still establish order; they must not masquerade as "just now".
        age = f"约 {max(0, int((now - at) / 60))} 分钟前" if at and now else "较早的执行记录"
        completed.append(f"{age}完成：{name}")

    action = state.current_action if isinstance(state.current_action, dict) else {}
    action = action if own(action) else {}
    active = label(action) if action else ""
    plan = state.current_plan if isinstance(state.current_plan, dict) else {}
    plan = plan if own(plan) else {}
    current = int(plan.get("current_step") or 0)
    pending = []
    for index, step in enumerate((plan.get("steps") or [])[current:], current):
        if not isinstance(step, dict) or step.get("status") in {"done", "skipped"}:
            continue
        if action and index == current and step.get("action") == action.get("type"):
            continue
        if name := label(step):
            pending.append(name)

    lines = [*completed[-3:]]
    if active:
        lines.append("正在进行，尚未结束：" + active)
    if pending:
        lines.append("尚未开始的安排：" + " → ".join(pending[:3]))
    if not lines:
        return ""
    return ("# 自己行动的执行事实（以此核对当前自述）\n" + "\n".join(lines)
            + "\n旧活动结束不证明其附带状态已切换；设备开关、连接和身体姿态以明确执行或当前状态记录为准。"
            "尚未开始的事只能作为打算。场景陈设、旧台词和心里话都不证明此刻正在做什么。")
