"""Persistent projects in the character's world, with dated activity records."""

from __future__ import annotations

from hashlib import sha256
import copy
from datetime import date
import math
import re
import unicodedata
from typing import Any
from uuid import uuid4

CLOSED = {"completed", "abandoned", "expired"}
ON_HOLD = frozenset({"paused", "waiting", "blocked"})
RETIRED_COMPUTER_KEYS = frozenset({"executions", "artifacts", "execution_review_required", "capability_key", "failure_count"})


def execution_state(state: Any, item: dict) -> str:
    """Project lifecycle and saved effort do not prove a step is running now."""
    status = item.get("status")
    if status in CLOSED:
        return "此事项已结束，此刻未执行该事项的步骤"
    if status in ON_HOLD:
        return {"paused": "已暂停", "waiting": "等待中", "blocked": "目前受阻"}[status] + "，此刻未执行该事项的步骤"
    action = getattr(state, "current_action", None) or {}
    if (not action.get("type") or not item.get("id") or action.get("matter_id") != item["id"]
            or getattr(state, "state", "") in {"sleeping", "napping"}):
        return "事项未结束，但此刻未执行该事项的步骤"
    if action["type"] == "walk_to":
        return "正在前往步骤地点，步骤尚未开始"
    return "正在执行该事项的步骤"


def activity_basis(state, *, now: float, persona_key: str = "") -> str:
    """Only shared personal activity is motivation for autonomous life."""
    rows = []
    for item in sorted(practice_records(state, persona_key=persona_key).values(), key=lambda r: r.get("last_at", 0), reverse=True):
        if 0 <= now - float(item.get("last_at") or 0) <= 3 * 86400:
            rows.append(f"- 实际活动：{str(item.get('label') or '')[:80]}；累计投入{float(item.get('minutes') or 0):g}分钟。")
    for item in state.life_matters:
        if (item.get("who") or item.get("kind") == "promise"
                or (item.get("persona_key") and item["persona_key"] != persona_key)):
            continue
        for stage in current_stage_notes(item)[-1:]:
            if 0 <= now - float(stage.get("at") or 0) <= 3 * 86400:
                rows.append(f"- 世界内阶段：{item['text']}；当前事项状态：{item.get('status') or 'active'}；"
                            f"此刻执行状态：{execution_state(state, item)}；"
                            f"{stage_record_text(stage, status=item.get('status') or 'active')}")
    # A limit may omit a record, never cut off its qualifications or question.
    selected, used = [], 0
    for row in rows:
        if len(selected) < 8 and used + len(row) + bool(selected) <= 1600:
            selected.append(row)
            used += len(row) + (len(selected) > 1)
    return "\n".join(selected)


def current_stage_notes(item: dict) -> list[dict]:
    return [row for row in item.get("stage_notes", []) if row.get("goal", item["text"]) == item["text"]]


def stage_record_text(stage: dict, *, status: str = "active") -> str:
    """Keep the activity headline subordinate to its concrete qualifications."""
    if status in ON_HOLD:
        next_focus = f"保留的后续位置（目前未执行）：{stage.get('next_focus') or '尚未确定'}。"
    elif status in CLOSED:
        next_focus = "此事项已结束；阶段记录只保留当时所得。"
    else:
        next_focus = f"后续待做：{stage.get('next_focus') or '尚未确定'}。"
    return (f"活动概述（不是独立的结论依据）：{stage.get('summary') or ''}；"
            f"具体记录与限定（回答依据优先）：{stage.get('content') or '未记录具体结论'}；"
            f"{next_focus}"
            "正文里仍未确认的关系，不因标题写了完成或对照而变成已确认。")


def _minutes(value: Any) -> float:
    try:
        number = float(value)
        return max(0, number) if math.isfinite(number) else 0
    except (ValueError, TypeError, OverflowError):
        return 0


def current_bookmark(item: dict) -> dict:
    """Read the unfinished step without inventing content or mutating its clock."""
    if item.get("status") in CLOSED:
        return {}
    step = next((s for s in item.get("steps", []) if s.get("status") != "done"
                 and s.get("goal", item.get("text")) == item.get("text")), None)
    if not step or _minutes(step.get("worked_minutes")) <= 0:
        return {}
    return _step_bookmark(item, step)


def _step_bookmark(item: dict, step: dict) -> dict:
    saved = step.get("bookmark") if isinstance(step.get("bookmark"), dict) else {}
    if saved.get("goal", item.get("text")) != item.get("text"):
        saved = {}
    worked = _minutes(step.get("worked_minutes"))
    return {**saved, "matter_id": item["id"], "step_id": step["id"], "goal": item["text"],
            "step": step.get("text", ""), "worked_minutes": worked,
            "remaining_minutes": max(0, _minutes(step.get("minutes")) - worked),
            "focus": str(step.get("focus") or step.get("text") or ""),
            "open_question": str(saved.get("open_question", step.get("open_question")) or ""),
            "next_focus": str(saved.get("next_focus") or step.get("focus") or step.get("text") or "")}


def pending_review_bookmarks(item: dict) -> list[dict]:
    """Handoff old thoughts until review, without reopening a completed timer."""
    if not item.get("needs_review") or item.get("status") in CLOSED:
        return []
    return [_step_bookmark(item, step) for step in item.get("steps", [])
            if step.get("status") == "done" and not step.get("stage_noted")
            and step.get("goal", item.get("text")) == item.get("text")
            and _minutes(step.get("worked_minutes")) > 0]


def _thought_key(text: str) -> str:
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text).casefold())


def capture_bookmark(item: dict, step: dict, *, now: float, reason: str = "") -> None:
    if (step.get("status") == "done" or _minutes(step.get("worked_minutes")) <= 0
            or step.get("goal", item.get("text")) != item.get("text")):
        return
    saved = step.get("bookmark") if isinstance(step.get("bookmark"), dict) else {}
    if saved.get("goal", item.get("text")) != item.get("text"):
        saved = {}
    step["bookmark"] = {**saved, "goal": item["text"], "at": now,
                        "worked_minutes": step["worked_minutes"],
                        "reason": str(reason or saved.get("reason") or "在线活动仍在推进")[:240]}


def record_bookmark_note(item: dict, payload: Any, basis: dict, *, now: float) -> bool:
    """A note is a bounded in-world thought, never a completion receipt."""
    if (not isinstance(payload, dict) or item.get("status") in CLOSED | {"paused"}
            or item.get("revision") != basis.get("revision") or item.get("text") != basis.get("goal")
            or payload.get("matter_id") != item.get("id") or payload.get("step_id") != basis.get("step_id")):
        return False
    current = current_bookmark(item)
    note = payload.get("note")
    if (not current or current["step_id"] != basis.get("step_id")
            or _minutes(basis.get("worked_minutes")) <= 0
            or current["worked_minutes"] < _minutes(basis.get("worked_minutes"))):
        return False
    if any(isinstance(payload.get(key), str) and len(payload[key].strip()) > limit
           for key, limit in (("note", 600), ("next_focus", 240), ("open_question", 240))):
        return False  # Reject together: a long note may qualify a cleared question.
    # Repeating a planned instruction is not a new observation. Preserve the
    # existing thought if a rhythm call merely copies the step description.
    changes = {}
    instructions = {_thought_key(str(step.get(key) or "")) for step in item.get("steps", [])
                    if step.get("id") == current["step_id"] for key in ("text", "focus")}
    if (isinstance(note, str) and note.strip()
            and _thought_key(note) not in instructions | {_thought_key(item["text"])}):
        changes["note"] = note.strip()
    for key in ("next_focus", "open_question"):
        value = payload.get(key)
        if isinstance(value, str):
            changes[key] = value.strip()
    if all(current.get(key, "") == value for key, value in changes.items()):
        return False
    step = next(s for s in item["steps"] if s["id"] == current["step_id"])
    capture_bookmark(item, step, now=now)
    step["bookmark"].update(changes, note_basis_minutes=basis["worked_minutes"], noted_at=now, scope="simulation")
    history(item, "bookmark_note", changes.get("note") or changes.get("next_focus") or changes.get("open_question")
            or "更新中途书签，清除旧的接续点或疑问",
            now, step_id=step["id"], scope="simulation")
    return True


def bookmark_basis(state, *, persona_key: str) -> list[dict]:
    """Only personal unfinished work can enter shared autonomous rhythm calls."""
    rows = []
    for item in state.life_matters:
        if (item.get("who") or item.get("kind") == "promise" or item.get("status") in CLOSED | {"paused"}
                or (item.get("persona_key") and item["persona_key"] != persona_key)):
            continue
        bookmark = current_bookmark(item)
        if bookmark:
            rows.append({**bookmark, "revision": item.get("revision"),
                         "previous_stage": current_stage_notes(item)[-1:]})
    return sorted(rows, key=lambda row: float(row.get("at") or 0), reverse=True)[:2]


def record_stage(item: dict, payload: Any, *, now: float) -> dict | None:
    """Attach bounded simulated content only to newly completed, timed steps."""
    if not isinstance(payload, dict):
        return None
    summary, content = payload.get("summary"), payload.get("content")
    next_focus = payload.get("next_focus") or ""
    ids = payload.get("basis_step_ids")
    if not isinstance(summary, str) or not summary.strip() or not isinstance(content, str) or not content.strip():
        return None
    if (len(summary.strip()) > 240 or len(content.strip()) > 1200
            or not isinstance(next_focus, str) or len(next_focus.strip()) > 240):
        return None  # Do not truncate away a qualification or unresolved question.
    if not isinstance(ids, list) or not ids or len(ids) > 6 or any(not isinstance(x, str) for x in ids):
        return None
    known = {s["id"]: s for s in unnoted_steps(item)}
    used = {sid for row in item.get("stage_notes", []) for sid in row.get("basis_step_ids", [])}
    if not set(ids) <= known.keys() or set(ids) & used:
        return None
    stage = {"at": now, "goal": item.get("text", ""), "summary": summary.strip(), "content": content.strip(),
             "next_focus": next_focus.strip(),
             "basis_step_ids": list(dict.fromkeys(ids)), "scope": "simulation"}
    item["stage_notes"] = [*item.get("stage_notes", []), stage][-8:]
    for sid in stage["basis_step_ids"]:
        known[sid]["stage_noted"] = True
    return stage


def unnoted_steps(item: dict) -> list[dict]:
    """Completed effort that may support new content, independent of plan timing."""
    return [s for s in item.get("steps", []) if s.get("status") == "done"
            and not s.get("stage_noted")
            and s.get("goal", item.get("text")) == item.get("text")
            and isinstance(s.get("result"), dict) and s["result"].get("ok")
            and s["result"].get("kind") in {"life_simulation", "in_world_action"}
            and _minutes(s["result"].get("minutes")) > 0]


PRACTICE_FIELDS = ("label", "sessions", "minutes", "days", "last_at", "episodes")


def _merge_practice(records: list[dict]) -> dict:
    rows = sorted(records, key=lambda row: _minutes(row.get("last_at")))
    if not rows:
        return {}
    return {"label": rows[-1].get("label", ""),
            "sessions": sum(int(_minutes(row.get("sessions"))) for row in rows),
            "minutes": sum(_minutes(row.get("minutes")) for row in rows),
            "days": sorted({day for row in rows for day in row.get("days", [])})[-30:],
            "last_at": max(_minutes(row.get("last_at")) for row in rows),
            "episodes": sorted([copy.deepcopy(episode) for row in rows for episode in row.get("episodes", [])],
                               key=lambda row: _minutes(row.get("at")))[-30:]}


def practice_records(state, *, persona_key: str | None = None) -> dict[str, dict]:
    """Select owned activity; unmarked historical effort remains compatible."""
    result = {}
    for action, record in state.life_interests.items():
        if not isinstance(record, dict):
            continue
        owners = record.get("personas")
        if not isinstance(owners, dict):
            result[action] = copy.deepcopy(record)
            continue
        selected = [owners[key] for key in dict.fromkeys(("", persona_key or ""))
                    if isinstance(owners.get(key), dict)]
        if selected:
            result[action] = _merge_practice(selected)
    return result


def practice_owner(record: dict, persona_key: str) -> dict:
    """Keep the pre-upgrade totals unmarked, then account each character separately."""
    owners = record.get("personas")
    if not isinstance(owners, dict):
        owners = record["personas"] = {}
        if record.get("sessions") or record.get("minutes") or record.get("days"):
            owners[""] = {key: copy.deepcopy(record[key]) for key in PRACTICE_FIELDS if key in record}
    owner = owners.setdefault(persona_key, {"label": record.get("label", ""), "sessions": 0, "minutes": 0, "days": []})
    return owner


def trim_practice_owners(record: dict) -> None:
    owners = record.get("personas", {})
    tagged = sorted(((key, row) for key, row in owners.items() if key),
                    key=lambda pair: _minutes(pair[1].get("last_at")))[-31:]
    record["personas"] = {**({"": owners[""]} if "" in owners else {}), **dict(tagged)}


def forget_practice(state, action: str, *, persona_key: str | None) -> dict | None:
    removed = practice_records(state, persona_key=persona_key).get(action)
    if removed is None:
        return None
    record = state.life_interests[action]
    owners = record.get("personas")
    if isinstance(owners, dict):
        for key in dict.fromkeys(("", persona_key or "")):
            owners.pop(key, None)
        if owners:
            state.life_interests[action] = {**_merge_practice(list(owners.values())), "personas": owners}
            return removed
    state.life_interests.pop(action, None)
    return removed


def stable_interests(state, *, now: float = 0, persona_key: str | None = None) -> list[dict]:
    """Several actual sessions across three days, rather than a sampled personality change."""
    return [{"action": key, **value} for key, value in practice_records(state, persona_key=persona_key).items()
            if len(value.get("days", [])) >= 3 and value.get("sessions", 0) >= 3
            and value.get("minutes", 0) >= 45
            and (not now or not value.get("last_at") or now - value["last_at"] <= 30 * 86400)][:8]


def routine_hints(state, *, now: float = 0, persona_key: str | None = None) -> list[dict]:
    """Repeated, dated circumstances are context, never a new identity or taste."""
    hints = []
    for item in stable_interests(state, now=now, persona_key=persona_key):
        periods, weekdays, recent = {}, {}, []
        for episode in item.get("episodes", []):
            if not isinstance(episode, dict) or not episode.get("day") or not episode.get("period"):
                continue
            if now and episode.get("at") and now - episode["at"] > 30 * 86400:
                continue
            periods.setdefault(episode["period"], set()).add(episode["day"])
            try:
                weekday = date.fromisoformat(episode["day"]).weekday()
            except (TypeError, ValueError):
                continue
            weekdays.setdefault(weekday, set()).add(episode["day"])
            recent.append(episode)
        if periods:
            weights = {}
            for row in recent:
                age = max(0, now - float(row.get("at") or now)) / 86400 if now else 0
                key = (row["period"], row["day"])
                weights[key] = max(weights.get(key, 0), 2 ** (-age / 7))
            qualified = [(period, days) for period, days in periods.items() if len(days) >= 3]
            if not qualified:
                continue
            period, days = max(qualified, key=lambda pair: sum(weights.get((pair[0], day), 0) for day in pair[1]))
            if len(days) >= 3:
                # Each date counts once; repeated ticks cannot manufacture a habit.
                score = round(sum(weights.get((period, day), 0) for day in days), 3)
                weekly = [{"weekday": key, "days": sorted(values)} for key, values in weekdays.items() if len(values) >= 3]
                last = max((float(row.get("at") or 0) for row in recent), default=0)
                hints.append({"action": item["action"], "label": item["label"], "period": period,
                              "days": sorted(days), "weekdays": weekly, "weight": score, "last_at": last,
                              "evidence": [{k: row.get(k) for k in ("day", "period", "node", "minutes")} for row in recent[-6:]],
                              "explanation": f"{len(days)}个不同日期曾在{period}{item['label']}；近期日期权重更高，约每周减半；只是可选的生活节奏"})
    return sorted(hints, key=lambda row: (-row["weight"], -row["last_at"], row["action"]))[:4]


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
         motivation: str = "", kind: str = "project", next_step: str = "",
         persona_key: str = "") -> dict:
    return {"id": "lm_" + uuid4().hex[:16], "text": text[:160], "who": who,
            "who_name": who_name, "session": session, "at": now, "updated_at": now,
            "kind": kind if kind in {"project", "promise", "idea"} else "project",
            "motivation": motivation[:300], "status": "active", "progress": 0,
            "next_step": next_step[:300], "obstacle": "", "resume_at": 0,
            "steps": [], "history": [], "revision": 1, "result": "",
            "work_day": "", "worked_minutes": 0, "last_attempt_at": 0,
            "plan_attempts": 0,
            **({"persona_key": persona_key} if persona_key else {})}


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


def belongs_to(item: dict, persona_key: str | None) -> bool:
    """Keep untagged legacy work; tagged work requires the same character."""
    return not item.get("persona_key") or item["persona_key"] == persona_key


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


def context_block(state, *, session_id: str, user_id: str = "", now: float = 0,
                  persona_key: str = "") -> str:
    items = ensure(state, now)
    rows = []
    shown_stages = set()
    for item in items:
        if item.get("status") in CLOSED or not visible(item, session_id):
            continue
        if item.get("persona_key") and item["persona_key"] != persona_key:
            continue
        if item.get("who") and item["who"] != user_id:
            continue
        next_label = "保留的接续位置（目前未执行）" if item.get("status") in ON_HOLD else "下一步"
        rows.append(f"- [{item['id']}] {item['text']}；动机：{item.get('motivation') or '原先想做的事'}；"
                    f"状态：{item['status']}；此刻执行状态：{execution_state(state, item)}；{progress_label(item)}；"
                    f"{next_label}：{item.get('next_step') or '尚待安排'}；阻碍：{item.get('obstacle') or '无'}")
        for stage in current_stage_notes(item)[-1:]:
            rows[-1] += (f"\n  世界内阶段所得（模拟内容）："
                         f"{stage_record_text(stage, status=item['status'])}")
            if len(rows) <= 6:
                shown_stages.add((item["id"], stage.get("summary"), stage.get("content")))
        for bookmark in pending_review_bookmarks(item):
            rows[-1] += (f"\n  计时结束前的思路（尚待复核）：{bookmark.get('note') or '未记录新发现'}；"
                         f"未解问题：{bookmark['open_question'] or '未记录'}")
    block = ("# 你的生活事项与保留进度\n这些是角色世界内的活动；标为生活模拟的进展属于AI模拟经历，不能当作现实操作依据。"
            "只依据已记录的活动回忆进展，未开始的打算、梦和口头声称都不算完成。"
            "可以自然提起，不要逐条念清单。涉及他人的事情只能在来源会话提及。\n" + "\n".join(rows[:6])) if rows else ""
    interests = stable_interests(state, now=now, persona_key=persona_key)
    if interests:
        block += "\n# 从日子里积累的活动经验\n这些只证明实际多日投入，不证明喜欢，更不能覆盖人设的喜恶、身份和说话方式。"
        block += "符合原有人设时才可温和延伸为日常偏好；可改变或放下，不必每次重复。\n" + "\n".join(
            f"- {item['label']}：{len(item['days'])} 天、{item['sessions']} 次、累计 {item['minutes']:g} 分钟" for item in interests)
    routines = routine_hints(state, now=now, persona_key=persona_key)
    if routines:
        block += "\n# 可参考的生活节奏\n以下是实际重复发生的时段，只在符合人设、困意和日程时作为轻微倾向；不能推断她喜欢或讨厌什么。\n" + "\n".join(
            f"- {item['explanation']}。可以沿用，也可以改变，不必排满时间。" for item in routines)
    known = {item['id']: item for item in items}
    artifacts = [(key, artifact) for key, artifact in state.life_artifacts.items()
                 if artifact.get("session") == session_id and key in known
                 and (key, artifact.get("result"), artifact.get("content")) not in shown_stages
                 and visible(known[key], session_id)
                 and (not known[key].get("who") or known[key]["who"] == user_id)
                 and (not known[key].get("persona_key") or known[key]["persona_key"] == persona_key)
                 and artifact.get("label") == known[key].get("text")][-3:]
    if artifacts:
        block += "\n# 已留下的生活阶段记录\n这些活动和模拟成果只属于角色世界，不代表操作了现实文件、完成真实测试、寄送或向聊天发送成功。\n" + "\n".join(
            f"- [{item.get('scope', 'in_world')}] {item['label']}；事项状态：{known[key]['status']}："
            f"{stage_record_text({'summary': item['result'], 'content': item.get('content')}, status=known[key]['status'])}"
            for key, item in artifacts)
    return block
