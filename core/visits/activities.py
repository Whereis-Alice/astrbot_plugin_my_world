"""Small public activity state: personal choices, real clocks and explicit results.

No model, tool or I/O runs here. An elapsed clock is evidence that an agreed
duration passed, never evidence of success, a discovery or another person's choice.
"""
from __future__ import annotations

import copy
import math
from typing import Any

from .protocol import clean_action_control, clean_action_step, clean_scene_update, text

SHARED_ACTIVITY_VERSION = 1
MAX_ACTIONS_PER_ACTOR = 6
MAX_MILESTONES = 8
_CLOSED = {"cancelled", "declined", "interrupted"}
_STATUSES = {"awaiting_peer", "running", "elapsed"} | _CLOSED


def _number(value: Any) -> bool:
    return type(value) in {int, float} and math.isfinite(value)


def begin_shared_activity(visit: dict) -> None:
    """Start the accepted stop; retain public milestones and explicit unfinished work."""
    outing = visit.get("outing") or {}
    previous = visit.get("shared_activity") or {}
    pending = list(previous.get("pending", []))
    for records in previous.get("action_steps", {}).values():
        for record in records:
            if not record.get("settled_revision") and record["status"] not in _CLOSED:
                pending.append("尚未确认结果：" + record["description"])
    event = (visit.get("scene") or {}).get("event")
    if event:
        pending.append(text(event, 160))
    visit["shared_activity"] = {
        "version": SHARED_ACTIVITY_VERSION, "stage": outing.get("stage", 0),
        "revision": int(previous.get("revision", -1)) + 1,
        "goal": text(outing.get("activity"), 120), "summary": "", "objects": [],
        "pending": list(dict.fromkeys(text(item, 160) for item in pending))[-8:], "done": False,
        "action_steps": {}, "milestones": copy.deepcopy(previous.get("milestones", []))[-MAX_MILESTONES:],
        "next_action": int(previous.get("next_action", 1)),
    }


def _current(visit: dict) -> dict:
    outing = visit.get("outing") or {}
    if not outing or visit.get("outing_version") != 1:
        return {}
    shared = visit.get("shared_activity")
    if not isinstance(shared, dict) or shared.get("stage") != outing.get("stage"):
        begin_shared_activity(visit)
    return visit["shared_activity"]


def record_action(visit: dict, actor: str, step: Any, now: float) -> bool:
    """Commit only this actor's choice. One foreground action and one wait may coexist."""
    choice = clean_action_step(step)
    outing = visit.get("outing") or {}
    actors = {visit.get("host_id"), visit.get("guest_id")}
    if (not choice or actor not in actors or visit.get("status") != "active"
            or not _number(now) or not outing):
        return False
    start = max(now, outing["started_at"])
    if outing.get("return_agreed") and start + choice["seconds"] > outing["until"]:
        return False  # Do not truncate the action or silently cancel the agreed return.
    shared = _current(visit)
    if not shared:
        return False
    advance_activity(visit, now)
    records = shared["action_steps"].get(actor, [])
    if any(row["status"] in {"running", "awaiting_peer"} and row["waiting"] == choice["waiting"]
           for row in records):
        return False
    retained = [row for row in records if not row.get("settled_revision") and row["status"] not in _CLOSED]
    if len(retained) >= MAX_ACTIONS_PER_ACTOR:
        return False  # Unresolved choices cannot be silently dropped to make room.
    partner = None
    if choice.get("peer_action_id"):
        peer = next((other for other in actors if other != actor), None)
        partner = next((item for item in shared["action_steps"].get(peer, [])
                        if item["id"] == choice["peer_action_id"] and item["status"] == "awaiting_peer"
                        and item["stage"] == shared["stage"] and item["requires_peer"]), None)
        if not partner or outing.get("return_agreed") and start + partner["seconds"] > outing["until"]:
            return False
    number = shared["next_action"]
    shared["next_action"] += 1
    shared["action_steps"][actor] = records = retained
    row = {"id": f"s{shared['stage']}-a{number}", "stage": shared["stage"], **choice,
           "accepted_at": now, "status": "awaiting_peer" if choice["requires_peer"] else "running"}
    if not choice["requires_peer"]:
        row.update(started_at=start, until=start + choice["seconds"])
    records.append(row)
    if choice["requires_peer"]:
        if partner:
            joint_id = ":".join(sorted((row["id"], partner["id"])))
            for item in (row, partner):
                item.update(status="running", started_at=start, until=start + item["seconds"], joint_id=joint_id)
    shared["revision"] += 1
    return True


def apply_action_control(visit: dict, actor: str, control: Any, now: float) -> bool:
    """Free a specific choice without inventing its result or the peer's next choice."""
    choice = clean_action_control(control)
    if (not choice or actor not in {visit.get("host_id"), visit.get("guest_id")}
            or visit.get("status") != "active" or not _number(now)):
        return False
    shared = _current(visit)
    if not shared:
        return False
    selected = next(((owner, row) for owner, rows in shared["action_steps"].items() for row in rows
                     if row["id"] == choice["action_id"] and row["stage"] == shared["stage"]), None)
    if not selected:
        return False
    owner, row = selected
    if row["status"] in _CLOSED or row.get("settled_revision") or now < row["accepted_at"]:
        return False
    if choice["operation"] == "cancel":
        if owner != actor:
            return False
        affected = [(row, "cancelled")]
        if row.get("joint_id"):
            affected.extend((other, "interrupted") for other_owner, rows in shared["action_steps"].items()
                            for other in rows if other_owner != actor and other.get("joint_id") == row["joint_id"]
                            and other["status"] not in _CLOSED and not other.get("settled_revision"))
    else:
        if owner == actor or row["status"] != "awaiting_peer" or not row["requires_peer"]:
            return False
        affected = [(row, "declined")]
    advance_activity(visit, now)
    for target, status in affected:
        target.update(status=status, ended_at=now, controlled_by=actor,
                      control_operation=choice["operation"], control_action_id=row["id"])
        if "reason" in choice:
            target["control_reason"] = choice["reason"]
    shared["revision"] += 1
    return True


def advance_activity(visit: dict, now: float) -> bool:
    """Record elapsed timers at their original deadline, without fabricating outcomes."""
    shared = visit.get("shared_activity")
    outing = visit.get("outing") or {}
    if (not isinstance(shared, dict) or shared.get("stage") != outing.get("stage") or not _number(now)):
        return False
    changed = False
    for records in shared.get("action_steps", {}).values():
        for row in records:
            if row["status"] == "running" and row["until"] <= now:
                row["status"], row["elapsed_at"] = "elapsed", row["until"]
                changed = True
    if changed:
        shared["revision"] += 1
    return changed


def _ready_actions(shared: dict, actors: set[str]) -> list[tuple[str, dict]]:
    available = [(actor, row) for actor, records in shared.get("action_steps", {}).items()
                 for row in records if row["status"] == "elapsed" and not row.get("settled_revision")]
    ready = [(actor, row) for actor, row in available if not row["requires_peer"]]
    for actor, row in available:
        if not row["requires_peer"]:
            continue
        joint = row.get("joint_id")
        partners = [(owner, item) for owner, item in available if item.get("joint_id") == joint]
        if joint and {owner for owner, _ in partners} == actors:
            ready.append((actor, row))
    return ready


def apply_scene_update(visit: dict, actor: str, update: Any, now: float) -> bool:
    """Only the host publishes an explicit result backed by unconsumed timed choices.

Independent actions may settle alone. A cooperative action must have both actors'
independent choices and both clocks elapsed. The caller guards the model revision.
"""
    result = clean_scene_update(update)
    if actor != visit.get("host_id") or visit.get("status") != "active" or not result or not _number(now):
        return False
    shared = _current(visit)
    if not shared or now < (visit.get("outing") or {}).get("started_at", math.inf):
        return False
    advance_activity(visit, now)
    available = _ready_actions(shared, {visit["host_id"], visit["guest_id"]})
    by_id = {row["id"]: (owner, row) for owner, row in available}
    references = result.get("action_ids")
    if references is None:
        # A saved legacy choice can be applied only when its scope is unambiguous.
        references = list(by_id) if len(by_id) == 1 else []
    if not references or any(reference not in by_id for reference in references):
        return False
    ready = [by_id[reference] for reference in references]
    for _, row in ready:
        if row["requires_peer"] and any(other["id"] not in references for _, other in available
                                        if other.get("joint_id") == row["joint_id"]):
            return False
    if result.get("done") and any(row["id"] not in references and not row.get("settled_revision")
                                  and row["status"] not in _CLOSED
                                  for rows in shared["action_steps"].values() for row in rows):
        return False
    shared["revision"] += 1
    revision = shared["revision"]
    shared["summary"] = result["summary"]
    for key in ("objects", "pending", "done"):
        if key in result:
            shared[key] = copy.deepcopy(result[key])
    evidence: dict[str, list[str]] = {}
    for owner, row in ready:
        row["settled_revision"] = revision
        evidence.setdefault(owner, []).append(row["id"])
    milestone = {"id": revision, "stage": shared["stage"], "at": now, "goal": shared["goal"],
                 "summary": result["summary"], "actions": evidence,
                 "objects": copy.deepcopy(shared["objects"]), "pending": copy.deepcopy(shared["pending"]),
                 "done": shared["done"]}
    shared["milestones"].append(milestone)
    shared["milestones"][:] = shared["milestones"][-MAX_MILESTONES:]
    return True


def activity_context(visit: dict, now: float) -> dict:
    """Return a read-only public view. Due clocks are shown without mutating the journal."""
    outing = visit.get("outing") or {}
    shared = visit.get("shared_activity")
    if not isinstance(shared, dict) or shared.get("stage") != outing.get("stage"):
        return {}
    result = {key: copy.deepcopy(shared[key]) for key in
              ("version", "stage", "revision", "goal", "summary", "objects", "pending", "done", "milestones")}
    actions = []
    for actor, records in shared.get("action_steps", {}).items():
        for stored in records:
            row = {"actor_id": actor, **copy.deepcopy(stored)}
            if row["status"] == "running":
                row["remaining_seconds"] = round(max(0, row["until"] - now), 3)
                if row["started_at"] > now:
                    row["status"] = "scheduled"
                    row["starts_in_seconds"] = round(row["started_at"] - now, 3)
                    row["remaining_seconds"] = row["seconds"]
                elif row["until"] <= now:
                    row["status"], row["elapsed_at"] = "elapsed", row["until"]
            actions.append(row)
    result["actions"] = actions
    result["ready_action_ids"] = [row["id"] for _, row in _ready_actions(
        {"action_steps": {actor: [row for row in actions if row["actor_id"] == actor]
                          for actor in (visit.get("host_id"), visit.get("guest_id"))}},
        {visit.get("host_id"), visit.get("guest_id")})]
    arrival = outing.get("started_at", 0)
    result["traveling"] = bool(outing.get("travel_seconds", 0) and arrival > now)
    if result["traveling"]:
        result.update(origin_place=outing.get("origin_place", ""), destination=outing.get("place", ""),
                      travel_remaining_seconds=round(max(0, arrival - now), 3), travel_until=arrival)
    return result


def next_activity_due(visit: dict, now: float) -> float | None:
    """Earliest future local clock; ticking it does not itself require model inference."""
    outing = visit.get("outing") or {}
    due = [outing["started_at"]] if outing.get("travel_seconds", 0) and outing.get("started_at", 0) > now else []
    shared = visit.get("shared_activity") or {}
    if shared.get("stage") == outing.get("stage"):
        due.extend(row["until"] for records in shared.get("action_steps", {}).values() for row in records
                   if row["status"] == "running" and row["until"] > now)
    return min(due) if due else None


def validate_shared_activity(value: Any, actors: set[str], *, outing: dict | None = None,
                             now: float | None = None) -> dict:
    """Validate bounded persisted/signed state. Old journeys may omit this extension."""
    if value is None or value == {}:
        return {}
    fields = {"version", "stage", "revision", "goal", "summary", "objects", "pending", "done",
              "action_steps", "milestones", "next_action"}
    if (not isinstance(value, dict) or set(value) != fields or type(value["version"]) is not int
            or value["version"] != SHARED_ACTIVITY_VERSION
            or any(type(value[key]) is not int or value[key] < 0 for key in ("stage", "revision"))
            or type(value["next_action"]) is not int or value["next_action"] < 1
            or type(value["done"]) is not bool
            or not isinstance(value["goal"], str) or len(value["goal"]) > 120
            or not isinstance(value["summary"], str) or len(value["summary"]) > 500
            or not isinstance(value["action_steps"], dict) or not set(value["action_steps"]) <= actors
            or not isinstance(value["milestones"], list) or len(value["milestones"]) > MAX_MILESTONES
            or outing is not None and value["stage"] != outing.get("stage")):
        raise ValueError("共同活动状态无效")
    for name in ("objects", "pending"):
        items = value[name]
        if (not isinstance(items, list) or len(items) > 8
                or any(not isinstance(item, str) or not item.strip() or len(item) > 160 for item in items)):
            raise ValueError("共同活动公开事项无效")
    seen = set()
    for records in value["action_steps"].values():
        if not isinstance(records, list) or len(records) > MAX_ACTIONS_PER_ACTOR:
            raise ValueError("共同活动动作数量无效")
        running = set()
        for row in records:
            allowed = {"id", "stage", "description", "seconds", "waiting", "requires_peer", "accepted_at",
                       "started_at", "until", "status", "joint_id", "elapsed_at", "settled_revision",
                       "peer_action_id", "ended_at", "controlled_by", "control_operation", "control_action_id", "control_reason"}
            required = {"id", "stage", "description", "seconds", "waiting", "requires_peer", "accepted_at", "status"}
            if (not isinstance(row, dict) or not required <= set(row) <= allowed or not clean_action_step(row)
                    or not isinstance(row.get("id"), str) or not row["id"] or len(row["id"]) > 80
                    or row["id"] in seen or type(row.get("stage")) is not int or row["stage"] != value["stage"]
                    or not isinstance(row.get("status"), str) or row["status"] not in _STATUSES
                    or not _number(row.get("accepted_at"))
                    or row["accepted_at"] < 0 or now is not None and row["accepted_at"] > now + 300):
                raise ValueError("共同活动动作身份或计时无效")
            seen.add(row["id"])
            status = row["status"]
            if "joint_id" in row and (not row["requires_peer"] or not isinstance(row["joint_id"], str)
                                       or not row["joint_id"] or len(row["joint_id"]) > 161):
                raise ValueError("协作动作关联无效")
            controls = {key: row[key] for key in ("ended_at", "controlled_by", "control_operation", "control_action_id", "control_reason")
                        if key in row}
            if status in _CLOSED:
                if (not _number(row.get("ended_at")) or row["ended_at"] < row["accepted_at"]
                        or now is not None and row["ended_at"] > now + 300
                        or not isinstance(row.get("controlled_by"), str) or row["controlled_by"] not in actors
                        or not clean_action_control({"action_id": row.get("control_action_id"),
                                                     "operation": row.get("control_operation"),
                                                     **({"reason": row["control_reason"]} if "control_reason" in row else {})})
                        or "settled_revision" in row):
                    raise ValueError("动作取消或拒绝证明无效")
            elif controls:
                raise ValueError("未终止动作不能附带取消证明")
            if status == "awaiting_peer":
                if not row["requires_peer"] or any(key in row for key in ("started_at", "until", "joint_id", "elapsed_at", "settled_revision", "peer_action_id")):
                    raise ValueError("协作动作尚未获双方确认")
            elif status in _CLOSED and "started_at" not in row:
                if (not row["requires_peer"] or any(key in row for key in ("until", "joint_id", "elapsed_at", "peer_action_id"))
                        or status == "interrupted"):
                    raise ValueError("未启动动作的终止状态无效")
            else:
                if (not _number(row.get("started_at")) or not _number(row.get("until"))
                        or row["started_at"] < row["accepted_at"]
                        or not math.isclose(row["until"] - row["started_at"], row["seconds"], abs_tol=0.000001)
                        or outing is not None and row["started_at"] < outing.get("started_at", 0)
                        or now is not None and row["started_at"] > now + 1 and (
                            outing is None or not outing.get("travel_seconds", 0)
                            or row["started_at"] != outing.get("started_at"))
                        or row["requires_peer"] and (not isinstance(row.get("joint_id"), str) or not row["joint_id"])):
                    raise ValueError("共同活动动作起止无效")
                if status == "elapsed":
                    if row.get("elapsed_at") != row["until"] or now is not None and row["until"] > now + 1:
                        raise ValueError("共同活动不能提前计时完成")
                elif status not in _CLOSED and ("elapsed_at" in row or "settled_revision" in row):
                    raise ValueError("进行中动作不能宣称已结算")
                if status in _CLOSED and "elapsed_at" in row and (
                        row["elapsed_at"] != row["until"] or row["elapsed_at"] > row["ended_at"]):
                    raise ValueError("终止动作的计时证明无效")
            if "settled_revision" in row and (type(row["settled_revision"]) is not int
                                               or not 0 < row["settled_revision"] <= value["revision"]):
                raise ValueError("共同活动结算编号无效")
            if status in {"running", "awaiting_peer"}:
                if row["waiting"] in running:
                    raise ValueError("同一角色存在冲突的并行动作")
                running.add(row["waiting"])
    for actor, records in value["action_steps"].items():
        for row in records:
            if row["status"] == "cancelled" and (row["controlled_by"] != actor or row["control_operation"] != "cancel"
                                                  or row["control_action_id"] != row["id"]):
                raise ValueError("不能替对方取消动作")
            if row["status"] == "declined" and (row["controlled_by"] == actor or row["control_operation"] != "decline"
                                                 or row["control_action_id"] != row["id"] or "started_at" in row):
                raise ValueError("不能替对方拒绝已开始的协作")
            if row["status"] == "interrupted" and (row["controlled_by"] == actor or row["control_operation"] != "cancel"
                                                     or row["control_action_id"] == row["id"] or not row.get("joint_id")):
                raise ValueError("协作中断缺少另一方取消证明")
    last, evidence_ids = -1, set()
    for milestone in value["milestones"]:
        if (not isinstance(milestone, dict) or set(milestone) != {
                "id", "stage", "at", "goal", "summary", "actions", "objects", "pending", "done"}
                or type(milestone["id"]) is not int or not last < milestone["id"] <= value["revision"]
                or type(milestone["stage"]) is not int or not 0 <= milestone["stage"] <= value["stage"]
                or not _number(milestone["at"]) or milestone["at"] < 0
                or now is not None and milestone["at"] > now + 300
                or not isinstance(milestone["goal"], str) or len(milestone["goal"]) > 120
                or not clean_scene_update(milestone) or not isinstance(milestone["actions"], dict)
                or not milestone["actions"] or not set(milestone["actions"]) <= actors
                or any(not isinstance(ids, list) or not ids or len(ids) > MAX_ACTIONS_PER_ACTOR
                       or any(not isinstance(item, str) or not item or len(item) > 80 for item in ids)
                       for ids in milestone["actions"].values())):
            raise ValueError("共同活动里程碑无效")
        last = milestone["id"]
        for ids in milestone["actions"].values():
            if len(ids) != len(set(ids)) or evidence_ids.intersection(ids):
                raise ValueError("同一动作不能重复结算")
            evidence_ids.update(ids)
    # A paired operation cannot be asserted using just one actor's signed choice.
    pairs: dict[str, list[tuple[str, dict]]] = {}
    for actor, records in value["action_steps"].items():
        for row in records:
            if row.get("joint_id") and not row.get("settled_revision") and row["status"] not in _CLOSED:
                pairs.setdefault(row["joint_id"], []).append((actor, row))
    for records in pairs.values():
        if ({actor for actor, _ in records} != actors or len(records) != len(actors)
                or len({row["started_at"] for _, row in records}) != 1):
            raise ValueError("协作动作缺少双方启动证明")
        for actor, row in records:
            if "peer_action_id" in row and not any(owner != actor and other["id"] == row["peer_action_id"]
                                                   for owner, other in records):
                raise ValueError("协作动作引用了另一件事")
    return copy.deepcopy(value)


# Short aliases for consumers that treat the module as a public state port.
scene_update = apply_scene_update
context = activity_context
next_due = next_activity_due
validation = validate_shared_activity
