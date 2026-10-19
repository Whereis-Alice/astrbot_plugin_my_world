"""Ordinary friendship progression and a shared explanation for the editor/model.

No model calls and no synthetic trust scores: use observed conversation days,
existing feelings and the configured relationship boundaries.
"""

from __future__ import annotations

from typing import Any


def grounded_relationship(item: dict[str, Any], records: list[dict[str, Any]], *, exclusive: bool) -> dict[str, Any] | None:
    """Match quotes to their speaker and to one unambiguous local exchange."""
    def normalized(value: Any) -> str:
        return "".join(char for char in str(value or "").casefold() if char.isalnum())

    uid = str(item.get("user_id") or "")
    evidence = normalized(item.get("evidence"))
    confirmation = normalized(item.get("confirmation"))
    matches = [index for index, row in enumerate(records)
               if not row.get("is_self") and str(row.get("user_id")) == uid
               and len(evidence) >= 4 and evidence in normalized(row.get("text"))]
    if not matches:
        return None
    candidate = dict(item)
    confirmed = False
    for index in matches:
        source = records[index]
        origin = str(source.get("origin") or "")
        for row in records[index + 1:]:
            if str(row.get("origin") or "") != origin:
                continue
            # Another human speaking makes an unaddressed "you" ambiguous.
            if not row.get("is_self") and str(row.get("user_id")) != uid:
                break
            elapsed = float(row.get("at") or 0) - float(source.get("at") or 0)
            if elapsed > 600:
                break
            if row.get("is_self") and len(confirmation) >= 4 and confirmation in normalized(row.get("text")):
                confirmed = True
                break
        if confirmed:
            break
    if exclusive and not confirmed:
        candidate["asserted_by"] = "他自称"
    return candidate


def friendship_progress(store: Any, session_id: str, user_id: str) -> dict[str, Any]:
    config = store.config
    row = store.profile(session_id, user_id) or {}
    payload = row.get("payload") or {}
    days = len(set(str(day) for day in payload.get("talk_days", []) if day))
    current = store.visible_bonds(session_id, user_id, statuses=["current"])
    stages = []
    for stage in config.friendship_stages:
        bond = config.bond_by_name(stage.bond)
        # Unique commitments, identities and negative relationships never grow
        # out of message frequency. Custom ordinary types opt in via this table.
        if bond is not None and bond.auto_form and bond.group and not bond.unique and not bond.negative:
            stages.append((stage, bond))
    names = {bond.name for _, bond in stages} | {config.default_bond}
    eligible = bool(current) and all(
        str(item.get("type") or "") in names
        and str(item.get("asserted_by") or "") != "管理员设置"
        for item in current
    )
    # Crossing a distance ceiling does not change the order of identity stages.
    cap = max((bond.cap for bond in (config.bond_by_name(str(item.get("type") or ""))
                                    for item in current) if bond is not None), default=-1)
    remaining = [(stage, bond) for stage, bond in stages if bond.cap > cap]
    remaining.sort(key=lambda pair: (pair[1].cap, pair[0].min_talk_days, pair[0].min_affinity))
    next_stage = remaining[0][0] if remaining and eligible else None
    affinity = float(row.get("affinity") or 0)
    return {
        "enabled": bool(config.relationship_auto_enabled),
        "eligible": eligible,
        "talk_days": days,
        "next": ({
            "bond": next_stage.bond,
            "min_talk_days": next_stage.min_talk_days,
            "min_affinity": next_stage.min_affinity,
            "days_left": max(0, next_stage.min_talk_days - days),
            "affinity_left": round(max(0, next_stage.min_affinity - affinity), 2),
        } if next_stage is not None else None),
    }


def evolve_friendship(store: Any, session_id: str, user_id: str, *, now: float) -> None:
    progress = friendship_progress(store, session_id, user_id)
    if not progress["enabled"] or not progress["eligible"]:
        return
    stage = progress["next"]
    if stage is None or stage["days_left"] or stage["affinity_left"]:
        return
    store.note_bond(
        session_id, user_id, type=stage["bond"], now=now, confidence=1.0,
        asserted_by="日常相处",
        automatic=True,
        evidence=f"已在 {progress['talk_days']} 个不同日期直接交谈，好感达到 {stage['min_affinity']:g}，逐渐熟悉起来",
    )


def interaction_trend(logs: list[dict], *, now: float) -> dict:
    """Explain observed recent interactions without recomputing affinity or rights."""
    rows = [row for row in logs if row.get("source") == "chat" and
            0 <= now - float(row.get("at") or 0) <= 14 * 86400][:20]
    positive = sum(float(row.get("delta") or 0) > 0 for row in rows)
    negative = sum(float(row.get("delta") or 0) < 0 for row in rows)
    if len(rows) < 3:
        direction, label = "insufficient", "近期互动依据还少"
    elif positive and negative and min(positive, negative) / len(rows) >= .3:
        direction, label = "mixed", "近期相处有起伏"
    elif positive > negative:
        direction, label = "warming", "近期相处较融洽"
    elif negative > positive:
        direction, label = "cooling", "近期相处有些摩擦"
    else:
        direction, label = "steady", "近期相处平稳"
    return {"direction": direction, "label": label, "days": 14, "count": len(rows),
            "explanation": "近14天最多20条直接交谈记录，仅作观察；手动设定、关系下限和自然衰减不参与，不改变好感、身份或权限。",
            "evidence": [{"id": row.get("id"), "at": row["at"], "reason": row.get("reason") or "未记录具体原因",
                          "delta": row.get("delta")} for row in rows[:3]]}


def relationship_summary(store: Any, session_id: str, user_id: str, person: dict[str, Any], *, now: float = 0) -> dict[str, Any]:
    names = list(person.get("affinities") or [])
    config = store.config
    affinity = float(person.get("affinity") or 0)
    cap = max(0, min(config.bond_cap(names), max(0, len(config.levels) - 1)))
    floor = max(0, min(config.bond_floor(names), cap))
    base = config.level_index_for(affinity, cap=cap, floor=floor)
    natural = config.level_index(affinity)
    reasons = []
    if person.get("negative"):
        reasons.append("负面关系优先限制距离，不因其他身份放宽")
    if affinity >= 0 and floor > natural:
        reasons.append("已确立的关系提供熟悉程度下限")
    if natural > cap:
        reasons.append("当前关系设有距离上限，好感增加不会自动越过")
    if any(bond is not None and bond.allow_boundary_crossing
           for bond in (config.bond_by_name(name) for name in names)) and not person.get("negative"):
        reasons.append("此关系允许随好感越过通常距离上限；身份和当前动作边界仍按各自配置")
    if (person.get("level") or {}).get("cold"):
        reasons.append("她还在生气，暂时比平常疏远；关系本身没有解除")
    levels = config.levels
    return {
        "base_level": levels[base].name if levels else "",
        "effective_level": str((person.get("level") or {}).get("name") or ""),
        "cap_level": levels[cap].name if levels else "",
        "floor_level": levels[floor].name if levels else "",
        "reasons": reasons,
        "affinity_min": config.affinity_min,
        "affinity_max": config.affinity_max,
        "decay_grace_days": config.affinity_decay_grace_days,
        "progress": friendship_progress(store, session_id, user_id),
        "trend": interaction_trend(store.affinity_logs(session_id, user_id, limit=200), now=now),
    }
