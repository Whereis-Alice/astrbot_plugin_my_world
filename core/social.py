"""Administrator relationship starting points; runtime state stays per session."""

from __future__ import annotations

import math
import re
import time
from typing import Any

from .state import WorldState


def qq_id(value: Any) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"[1-9][0-9]{0,19}", text):
        raise ValueError("请输入有效的 QQ 号，只能包含数字且不能以 0 开头")
    return text


def validate_state(store: Any, affinity: Any, relations: Any) -> tuple[float, list[str]]:
    if isinstance(affinity, bool):
        raise ValueError("好感必须是有限数值")
    try:
        value = float(affinity)
    except (TypeError, ValueError) as exc:
        raise ValueError("好感必须是有限数值") from exc
    if not math.isfinite(value):
        raise ValueError("好感必须是有限数值")
    config = store.config
    if not config.affinity_min <= value <= config.affinity_max:
        raise ValueError(f"好感需在 {config.affinity_min:g} 至 {config.affinity_max:g} 之间")
    if not isinstance(relations, list) or len(relations) > 32 or any(not isinstance(x, str) for x in relations):
        raise ValueError("关系必须是最多 32 项的名称列表")
    names: list[str] = []
    groups: set[str] = set()
    slots: set[str] = set()
    for raw in relations:
        bond = config.bond_by_name(raw.strip())
        if bond is None:
            raise ValueError(f"关系表中没有「{raw}」，请先添加此关系类型")
        if bond.name in names:
            continue
        if (bond.group and bond.group in groups) or (bond.slot and bond.slot in slots):
            raise ValueError(f"「{bond.name}」与已选关系属于同一替代组或槽位，请保留一种")
        if bond.group:
            groups.add(bond.group)
        if bond.slot:
            slots.add(bond.slot)
        names.append(bond.name)
    if config.default_bond in names and len(names) > 1:
        raise ValueError("默认初始关系不能与其他正式关系同时设置")
    return value, names


def rule_conflicts(config: Any, rows: list[dict[str, Any]]) -> tuple[set, set]:
    """Identify conflicting pairs so unrelated edits tolerate legacy overlaps."""

    identities: dict[tuple[str, str], list] = {}
    slots: dict[tuple[str, str], list] = {}
    unique_slots = {bond.slot for bond in config.bonds if bond.unique and bond.slot}
    identity_conflicts, unique_conflicts = set(), set()
    for row in rows:
        bond = config.bond_by_name(row["type"])
        if bond is None:
            continue
        previous = identities.setdefault((row["group_id"], row["user_id"]), [])
        for old_row, old in previous:
            if bond.group and old.group == bond.group or bond.slot and old.slot == bond.slot:
                identity_conflicts.add(tuple(sorted((row["id"], old_row["id"]))))
        previous.append((row, bond))
        if bond.slot in unique_slots:
            occupants = slots.setdefault((row["group_id"], bond.slot), [])
            for old_row in occupants:
                if old_row["user_id"] != row["user_id"]:
                    unique_conflicts.add(tuple(sorted((row["id"], old_row["id"]))))
            occupants.append(row)
    return identity_conflicts, unique_conflicts


def check_occupancy(store: Any, session: str, user_id: str, relations: list[str]) -> None:
    slots = {store.config.bond_by_name(name).slot for name in relations
             if store._unique_slot(store.config.bond_by_name(name).slot)}
    for bond in store.db.list_user_bonds(group_id=session, statuses=["current"]):
        if bond["slot"] in slots and bond["user_id"] != user_id:
            raise ValueError(f"唯一关系「{bond['type']}」已有其他人占用，请先在该会话解除")


def set_state(store: Any, session: str, user_id: str, *, affinity: float,
              relations: list[str], name: str = "", revision: int | None = None,
              strict: bool = True, now: float | None = None) -> None:
    """Replace just current identities and affinity. Preserve knowledge and history."""

    if not store.enabled():
        raise ValueError("该会话的用户画像功能未开启")
    stamp = float(now if now is not None else time.time())
    if strict:
        check_occupancy(store, session, user_id, relations)
    row = store.profile(session, user_id)
    if row is None:
        row = store.touch(session, user_id, name, count_message=False, seed=False, now=stamp)
        if row is not None:
            # Creating an administrative starting point is not a meeting.
            row["payload"]["days"] = []
            store.db.upsert_user_profile(group_id=session, user_id=user_id,
                payload=row["payload"], affinity=row["affinity"], first_seen_at=0,
                last_seen_at=0, message_count=0, digest=row["digest"])
    if row is None:
        raise ValueError("无法建立联系人档案")
    for current in store.bonds(session, user_id, statuses=["current"]):
        if current["type"] not in relations:
            store.db.close_user_bond(bond_id=current["id"], until=stamp)
    issues: list[str] = []
    for relation in relations:
        result = store.note_bond(session, user_id, type=relation, confidence=1.0,
                                 asserted_by="管理员设置", evidence="关系与好感页统一设定" if revision else "管理员调整当前会话",
                                 lift_affinity=False, policy="reject", now=stamp)
        if not result.get("ok") or result.get("action") == "claim":
            if strict:
                raise ValueError(result.get("reason") or "关系未能确立")
            issues.append(result.get("reason") or "关系需要管理员确认")
    current = store.profile(session, user_id)
    result = store.adjust_affinity(session, user_id, affinity - current["affinity"],
                                    reason="应用统一起点" if revision else "管理员调整当前会话",
                                    source="baseline" if revision else "manual", now=stamp)
    if not result.get("ok"):
        raise ValueError(result.get("reason") or "好感未能保存")
    current = store.profile(session, user_id)
    payload = dict(current["payload"])
    if revision is not None:
        payload["relationship_baseline_revision"] = revision
        payload["relationship_baseline_at"] = stamp
    payload["relationship_baseline_issue"] = "；".join(issues)
    store._save(session, user_id, payload)


def initialize_baseline(store: Any, session: str, user_id: str,
                        baseline: dict[str, Any], *, now: float) -> None:
    """Called only when a profile is first created, never on ordinary reads."""

    value = max(store.config.affinity_min, min(store.config.affinity_max, float(baseline["affinity"])))
    names, issues = [], []
    for name in baseline.get("relations", []):
        try:
            _, names = validate_state(store, value, [*names, name])
        except ValueError as exc:
            issues.append(str(exc))
    if value != baseline["affinity"]:
        issues.append("统一起点的好感已按此会话的范围调整")
    with store.db.atomic():
        set_state(store, session, user_id, affinity=value, relations=names,
                  revision=int(baseline["revision"]), strict=False, now=now)
        if issues:
            row = store.profile(session, user_id)
            payload = dict(row["payload"])
            existing = payload.get("relationship_baseline_issue")
            payload["relationship_baseline_issue"] = "；".join(([existing] if existing else []) + issues)
            store._save(session, user_id, payload)


class SocialDirectory:
    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self.db = engine.db.raw

    def _label(self, session: str) -> str:
        return self.engine.session_label(session) or session

    def person(self, user_id: str) -> dict[str, Any]:
        baseline = self.db.relationship_baseline(user_id)
        scopes: list[dict[str, Any]] = []
        name = str((baseline or {}).get("name") or "")
        for row in sorted(self.db.all_user_profiles(user_id), key=lambda x: -x["last_seen_at"]):
            sid = row["group_id"]
            with self.engine.config_scope(sid):
                store = self.engine.profiles
                view = store.view(sid, user_id)
                if view is None:
                    continue
                state_key = self.engine.state_key(sid)
                snapshot = self.db.get_state(state_key)
                current = {"affinity": view.affinity, "affinities": list(view.affinities), "negative": view.negative,
                           "level": {"name": view.level.name, "index": view.level_index}}
                if snapshot is not None:
                    current = self.engine._apply_grudge_coldness(
                        WorldState.from_payload(snapshot, state_key), user_id, current, session_id=sid)
                name = name or view.call_me or view.name
                scopes.append({"session": sid, "label": self._label(sid), "affinity": view.affinity,
                               "relations": list(view.affinities), "level": current["level"]["name"],
                               "base_level": view.level.name, "cold": bool(current["level"].get("cold")),
                               "last_seen_at": view.last_seen_at, "talk_days": view.talk_days,
                               "baseline_revision": row["payload"].get("relationship_baseline_revision"),
                               "issue": row["payload"].get("relationship_baseline_issue", ""),
                               "configured": self.engine.session_config(sid) is not None})
        return {"user_id": user_id, "name": name or user_id, "baseline": baseline, "sessions": scopes}

    async def with_emotions(self, data: dict[str, Any]) -> dict[str, Any]:
        """Resolve current characters for visible cards without mutating their lives."""

        people = data.get("people", [data.get("person", data)])
        snapshots: dict[str, tuple[WorldState, str] | None] = {}
        for person in people:
            for item in person.get("sessions", []):
                sid = item["session"]
                with self.engine.config_scope(sid):
                    if sid not in snapshots:
                        snapshot = self.db.get_state(self.engine.state_key(sid))
                        snapshots[sid] = None
                        if snapshot is not None and snapshot.get("grudges"):
                            _, persona_key = await self.engine._journal_persona(sid)
                            snapshots[sid] = (WorldState.from_payload(snapshot, self.engine.state_key(sid)), persona_key)
                    source = snapshots[sid]
                    if source is None:
                        continue
                    view = self.engine.profiles.view(sid, person["user_id"])
                    if view is None:
                        continue
                    current = self.engine._apply_grudge_coldness(source[0], person["user_id"],
                        {"affinity": view.affinity, "affinities": list(view.affinities), "negative": view.negative,
                         "level": {"name": view.level.name, "index": view.level_index}},
                        session_id=sid, persona_key=source[1])
                    item["level"] = current["level"]["name"]
                    item["cold"] = bool(current["level"].get("cold"))
        return data

    def catalog(self, *, q: str = "", page: int = 1, size: int = 24,
                filter_by: str = "all", sort: str = "recent") -> dict[str, Any]:
        # Index once before reading the visible cards; hidden pages don't incur
        # profile/view queries. There is no arbitrary first-200-people cutoff.
        index: dict[str, dict[str, Any]] = {}
        for baseline in self.db.relationship_baselines():
            index[baseline["user_id"]] = {"name": baseline.get("name", ""), "baseline": True,
                "affinity": baseline["affinity"], "last_seen_at": 0, "scores": [], "texts": []}
        for row in self.db.all_user_profiles():
            uid = row["user_id"]
            if not re.fullmatch(r"[1-9][0-9]{0,19}", uid):
                continue
            item = index.setdefault(uid, {"name": "", "baseline": False, "affinity": 0,
                "last_seen_at": 0, "scores": [], "texts": []})
            payload = row["payload"]
            item["name"] = item["name"] or payload.get("call_me") or payload.get("qq_name") or uid
            item["texts"].extend([str(payload.get("qq_name", "")), str(payload.get("call_me", "")),
                                   str(payload.get("note", ""))])
            item["last_seen_at"] = max(item["last_seen_at"], row["last_seen_at"])
            item["scores"].append(float(row["affinity"]))
        query = q.casefold().strip()
        users = [uid for uid, item in index.items()
                 if (not query or query in " ".join([uid, str(item["name"]), *item["texts"]]).casefold())
                 and (filter_by != "configured" or item["baseline"])
                 and (filter_by != "unconfigured" or not item["baseline"])]
        if sort not in {"recent", "affinity", "affinity_asc", "sessions", "name"}:
            sort = "recent"

        def order(uid: str) -> tuple:
            item = index[uid]
            scores = item["scores"]
            average = sum(scores) / len(scores) if scores else float(item["affinity"])
            primary = {"recent": -item["last_seen_at"], "affinity": -average,
                       "affinity_asc": average, "sessions": -len(scores),
                       "name": str(item["name"] or uid).casefold()}[sort]
            return primary, uid

        users.sort(key=order)
        size = max(1, min(60, size))
        pages = max(1, math.ceil(len(users) / size))
        page = max(1, min(page, pages))
        return {"people": [self.person(uid) for uid in users[(page - 1) * size:page * size]],
                "total": len(users), "people_total": len(index),
                "baselines_total": sum(bool(x["baseline"]) for x in index.values()),
                "page": page, "pages": pages, "sort": sort,
                "sessions_total": len(self.engine.sessions.sessions)}

    def apply(self, payload: dict[str, Any], *, only_session: str = "") -> dict[str, Any]:
        # Include revision and unique-baseline checks in the same lock as writes.
        with self.db.atomic():
            return self._apply(payload, only_session=only_session)

    def _apply(self, payload: dict[str, Any], *, only_session: str) -> dict[str, Any]:
        uid = qq_id(payload.get("user_id"))
        raw_names = payload.get("relations")
        name = " ".join(str(payload.get("name") or "").split())[:80]
        baseline = self.db.relationship_baseline(uid)
        if not only_session and payload.get("revision") != (baseline["revision"] if baseline else 0):
            raise ValueError("统一起点已被其他操作更新，请刷新后再保存")
        targets = [only_session] if only_session else sorted(
            {row.session_id for row in self.engine.sessions.sessions}
            | {row["group_id"] for row in self.db.all_user_profiles(uid)})
        if only_session and self.engine.session_config(only_session) is None:
            raise ValueError("该会话已不在配置中，不能直接修改")
        plans: list[tuple[str, float, list[str]]] = []
        with self.engine.config_scope(only_session):
            value, names = validate_state(self.engine.profiles, payload.get("affinity"), raw_names)
            if not only_session:
                slots = {self.engine.world.profile.bond_by_name(x).slot for x in names
                         if self.engine.profiles._unique_slot(self.engine.world.profile.bond_by_name(x).slot)}
                for other in self.db.relationship_baselines():
                    if other["user_id"] == uid:
                        continue
                    if any(bond is not None and bond.slot in slots for bond in
                           (self.engine.world.profile.bond_by_name(x) for x in other.get("relations", []))):
                        raise ValueError("其他 QQ 的统一起点已占用这个唯一关系，请先调整对方的起点")
        # Validate every target before the first write. A conflict cannot leave
        # half the conversations updated or silently replace someone else.
        with self.db.atomic():
            for sid in targets:
                with self.engine.config_scope(sid):
                    store = self.engine.profiles
                    try:
                        local_value, local_names = validate_state(store, value, names)
                        if not store.enabled():
                            raise ValueError("用户画像功能未开启")
                        check_occupancy(store, sid, uid, local_names)
                    except ValueError as exc:
                        raise ValueError(f"{self._label(sid)}：{exc}") from exc
                    plans.append((sid, local_value, local_names))
            revision = None
            if not only_session:
                self.db.save_relationship_baseline(uid, {"name": name, "affinity": value, "relations": names})
                revision = self.db.relationship_baseline(uid)["revision"]
            for sid, local_value, local_names in plans:
                with self.engine.config_scope(sid):
                    set_state(self.engine.profiles, sid, uid, affinity=local_value,
                              relations=local_names, name=name, revision=revision)
        return {"ok": True, "applied_sessions": len(plans), "person": self.person(uid)}
