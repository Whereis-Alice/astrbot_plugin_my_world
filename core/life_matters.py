"""Persistent projects in the character's world, with dated activity records."""

from __future__ import annotations

from hashlib import sha256
from typing import Any
from uuid import uuid4

CLOSED = {"completed", "abandoned", "expired"}
RETIRED_COMPUTER_KEYS = frozenset({"executions", "artifacts", "execution_review_required", "capability_key", "failure_count"})


def stable_interests(state, *, now: float = 0) -> list[dict]:
    """Several actual sessions across three days, rather than a sampled personality change."""
    return [{"action": key, **value} for key, value in state.life_interests.items()
            if len(value.get("days", [])) >= 3 and value.get("sessions", 0) >= 3
            and value.get("minutes", 0) >= 45
            and (not now or not value.get("last_at") or now - value["last_at"] <= 30 * 86400)][:8]


def routine_hints(state, *, now: float = 0) -> list[dict]:
    """Repeated, dated circumstances are context, never a new identity or taste."""
    hints = []
    for item in stable_interests(state, now=now):
        periods = {}
        for episode in item.get("episodes", []):
            if not isinstance(episode, dict) or not episode.get("day") or not episode.get("period"):
                continue
            if now and episode.get("at") and now - episode["at"] > 30 * 86400:
                continue
            periods.setdefault(episode["period"], set()).add(episode["day"])
        if periods:
            period, days = max(periods.items(), key=lambda pair: len(pair[1]))
            if len(days) >= 3:
                hints.append({"action": item["action"], "label": item["label"], "period": period, "days": sorted(days)})
    return hints[:4]


def history(item: dict, kind: str, text: str, now: float, **evidence: Any) -> None:
    item["history"] = [*item.get("history", []), {
        "at": now, "kind": kind, "text": str(text)[:500], **evidence,
    }][-60:]
    item["updated_at"] = now
    item["revision"] = int(item.get("revision", 0)) + 1


def ensure(state, now: float) -> list[dict]:
    if not isinstance(state.life_matters, list):
        state.life_matters = []
    state.life_matters = [item for item in state.life_matters if isinstance(item, dict) and item.get("id")]
    if not state.life_migrated:
        for old in state.own_topics or []:
            if not isinstance(old, dict) or not old.get("text"):
                continue
            item = make(str(old["text"]), now=float(old.get("at") or now),
                        session=str(old.get("session") or state.session_id),
                        who=str(old.get("who") or ""), who_name=str(old.get("who_name") or ""))
            # Stable even if a legacy save is reloaded before the first write.
            item["id"] = "lm_" + sha256(f"{state.session_id}:{old}".encode()).hexdigest()[:16]
            item["legacy"] = True
            state.life_matters.append(item)
        state.life_migrated = True
    for item in state.life_matters:
        _retire_computer_work(item, now)
        if (item.get("kind") == "promise" and item.get("status") == "blocked"
                and item.get("steps") and not item.get("needs_replan")
                and all(step.get("status") == "done" for step in item["steps"])):
            item["blocker"] = "evidence"
    for key, value in list(state.life_artifacts.items()):
        if value.get("scope") == "sandbox":
            del state.life_artifacts[key]
        else:
            value.pop("artifacts", None)
    sync(state)
    return state.life_matters


def _retire_computer_work(item: dict, now: float) -> None:
    """One-way save cleanup: keep the goal, retire tool state, replan in-world."""
    steps = item.get("steps", [])
    legacy = any(key in item for key in RETIRED_COMPUTER_KEYS) or any(s.get("action") == "native_agent" for s in steps)
    legacy = legacy or bool(item.get("remaining_condition")) or item.get("blocker") in {"environment", "capability", "materials", "uncertain"}
    for key in RETIRED_COMPUTER_KEYS | {"remaining_condition", "previous_condition"}:
        item.pop(key, None)
    if not legacy:
        return
    item.pop("recovery_condition", None)
    item["steps"] = [s for s in steps if s.get("action") != "native_agent"]
    item["history"] = [event for event in item.get("history", [])
                       if not str(event.get("kind", "")).startswith(("agent_", "execution_", "environment_"))
                       and event.get("receipt", {}).get("kind") != "native_agent"]
    if item.get("status") not in CLOSED:
        if item.get("status") != "paused":
            item["status"] = "active"
        item.update(needs_replan=True, needs_review=False, obstacle="", blocker="", resume_at=0,
                    last_attempt_at=0, plan_attempts=0, next_step="有空时在自己的世界里继续这件事")
        item["progress"] = 0
    else:
        item.pop("blocker", None)
    history(item, "simulation_mode", "这件事改为世界内模拟，保留原有目标与生活经过", now)


def progress_label(item: dict) -> str:
    steps = item.get("steps", [])
    return f"已进行 {sum(s.get('status') == 'done' for s in steps)}/{len(steps)} 步" if steps else "尚待安排"


def make(text: str, *, now: float, session: str, who: str = "", who_name: str = "",
         motivation: str = "", kind: str = "project", next_step: str = "") -> dict:
    return {"id": "lm_" + uuid4().hex[:16], "text": text[:160], "who": who,
            "who_name": who_name, "session": session, "at": now, "updated_at": now,
            "kind": kind if kind in {"project", "promise", "idea"} else "project",
            "motivation": motivation[:300], "status": "active", "progress": 0,
            "next_step": next_step[:300], "obstacle": "", "resume_at": 0,
            "steps": [], "history": [], "revision": 1, "result": "",
            "work_day": "", "worked_minutes": 0, "last_attempt_at": 0,
            "plan_attempts": 0}


def sync(state) -> None:
    state.own_topics = [{**item} for item in state.life_matters if item.get("status") not in CLOSED]
    active = [item for item in state.life_matters if item.get("status") not in CLOSED]
    closed = [item for item in state.life_matters if item.get("status") in CLOSED]
    state.life_matters = [*active, *closed[-60:]]


def find(state, matter_id: str) -> dict | None:
    return next((item for item in state.life_matters if item.get("id") == matter_id), None)


def visible(item: dict, session_id: str, *, autonomy: bool = False) -> bool:
    # Sharing a physical world never grants permission to disclose a private promise.
    return not item.get("who") or str(item.get("session") or "") == session_id


def complete_step(state, matter_id: str, step_id: str, *, now: float,
                  receipt: dict) -> bool:
    item = find(state, matter_id)
    if item is None or item.get("status") in CLOSED | {"paused"}:
        return False
    step = next((s for s in item.get("steps", []) if s.get("id") == step_id), None)
    if step is None or step.get("status") == "done" or not receipt.get("ok"):
        return False
    step.update(status="done", result=receipt, finished_at=now)
    item["obstacle"] = ""
    remaining = [s for s in item["steps"] if s.get("status") != "done"]
    item["progress"] = round(100 * (len(item["steps"]) - len(remaining)) / len(item["steps"]))
    item["next_step"] = remaining[0]["text"] if remaining else ""
    history(item, "step_done", step["text"], now, receipt=receipt, step_id=step_id)
    if not remaining:
        if item.get("kind") == "promise":
            item.update(status="blocked", progress=99, next_step="等待真实结果或管理员确认",
                        obstacle="准备步骤已做完，尚未取得约定已兑现的依据", blocker="evidence",
                        recovery_condition="在来源聊天中实际兑现，或由管理员确认结果")
            history(item, "awaiting_evidence", item["obstacle"], now)
            sync(state)
            return True
        item.update(status="active", progress=99, needs_review=True, next_step="这一阶段已做完，核对结果后再安排")
        history(item, "stage_done", "本阶段的世界内活动已进行完，尚未确认整个目标完成", now, receipt=receipt)
        # An in-world work product, explicitly labelled; never an external file/operation.
        state.life_artifacts[item["id"]] = {"label": item["text"], "result": str(receipt.get("result") or "已执行本阶段步骤"),
                                            "at": now, "scope": "simulation" if any(s.get("result", {}).get("kind") == "life_simulation" for s in item["steps"]) else "in_world",
                                            "session": item["session"]}
        state.life_artifacts = dict(list(state.life_artifacts.items())[-40:])
    else:
        item["resume_at"] = float(remaining[0].get("not_before") or 0)
        item["status"] = "waiting" if item["resume_at"] > now else "active"
    sync(state)
    return True


def context_block(state, *, session_id: str, user_id: str = "", now: float = 0) -> str:
    items = ensure(state, now)
    rows = []
    for item in items:
        if item.get("status") in CLOSED or not visible(item, session_id):
            continue
        if user_id and item.get("who") and item["who"] != user_id:
            continue
        rows.append(f"- [{item['id']}] {item['text']}；动机：{item.get('motivation') or '原先想做的事'}；"
                    f"状态：{item['status']}；{progress_label(item)}；"
                    f"下一步：{item.get('next_step') or '尚待安排'}；阻碍：{item.get('obstacle') or '无'}")
    block = ("# 你一直在推进的生活事项\n这些是角色世界内的活动；标为生活模拟的进展属于AI模拟经历，不能当作现实操作依据。"
            "只依据已记录的活动回忆进展，未开始的打算、梦和口头声称都不算完成。"
            "可以自然提起，不要逐条念清单。涉及他人的事情只能在来源会话提及。\n" + "\n".join(rows[:6])) if rows else ""
    interests = stable_interests(state, now=now)
    if interests:
        block += "\n# 从日子里积累的活动经验\n这些只证明实际多日投入，不证明喜欢，更不能覆盖人设的喜恶、身份和说话方式。"
        block += "符合原有人设时才可温和延伸为日常偏好；可改变或放下，不必每次重复。\n" + "\n".join(
            f"- {item['label']}：{len(item['days'])} 天、{item['sessions']} 次、累计 {item['minutes']:g} 分钟" for item in interests)
    routines = routine_hints(state, now=now)
    if routines:
        block += "\n# 可参考的生活节奏\n以下是实际重复发生的时段，只在符合人设、困意和日程时作为轻微倾向；不能推断她喜欢或讨厌什么。\n" + "\n".join(
            f"- {item['label']}曾在{len(item['days'])}个日期的{item['period']}进行，可以考虑沿用，也可以换一种安排。" for item in routines)
    artifacts = [item for item in state.life_artifacts.values() if item.get("session") == session_id][-3:]
    if artifacts:
        block += "\n# 已留下的生活阶段记录\n这些活动和模拟成果只属于角色世界，不代表操作了现实文件、完成真实测试、寄送或向聊天发送成功。\n" + "\n".join(
            f"- [{item.get('scope', 'in_world')}] {item['label']}：{item['result']}" for item in artifacts)
    return block
