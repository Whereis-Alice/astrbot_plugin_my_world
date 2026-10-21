"""睡眠时分别整理人物资料与已发生的生活经历，不保存聊天往事。"""

from __future__ import annotations

import json
import hashlib
from .evidence import source_snapshot, direct_text
import time
from datetime import datetime
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .json_actions import extract_json_object
from .memory import DREAM, MemoryEngine
from .profile import ProfileStore

REVIEW_STEPS = (1.0, 3.0, 7.0, 30.0)
"""间隔重复：一条值得记住的事，隔多久回访一次（天）。"""


def _fallback_session_label(session_id: str) -> str:
    """没有引擎侧标签时的兜底：「群 123」/「私聊 456」。

    整理模型要能看出「这句话是在哪儿说的」——组里的群和私聊共用一份留档，
    不标来源的话它会把两个地方的话揉成一件"当面发生的事"。
    """

    text = str(session_id or "").strip()
    if not text:
        return "说不清是哪儿"
    number = text.split(":")[-1] or text
    kind = ""
    for mark, name in (("PrivateMessage", "私聊"), ("FriendMessage", "私聊"), ("GroupMessage", "群")):
        if mark in text:
            kind = name
            break
    return f"{kind} {number}" if kind else f"会话 {number}"

MAX_FACTS_PER_RUN = 8
"""一次整理最多记几条"关于他的事"（模型一次给太多时，多半是在凑数）。"""

MAX_BONDS_PER_RUN = 4
"""一次整理最多改几条关系（关系本来就该慢）。"""


@dataclass
class ConsolidateResult:
    """一次整理做了什么（写日志与调试输出用）。"""

    mode: str = "full"
    ok: bool = False
    retry_needed: bool = False
    note: str = ""
    memories: int = 0
    merged: int = 0
    folded: int = 0
    facts: int = 0
    relations: int = 0
    digests: int = 0
    affinity: int = 0
    facts_merged: int = 0
    dream: str = ""
    skipped: list[str] = field(default_factory=list)
    raw: str = ""
    """模型的原始返回（截断一份）：整理结果不对时，日志里能直接看到它写了什么。"""

    parsed: dict[str, Any] = field(default_factory=dict)
    """解析出来的 JSON（预览时给编辑器看）。"""

    preview: dict[str, Any] = field(default_factory=dict)
    """``{"system": …, "user": …}``：预览模式下把"喂进去的东西"也带出来。"""

    def summary(self) -> str:
        bits = []
        if self.memories:
            bits.append(f"要点 {self.memories} 条")
        if self.merged:
            bits.append(f"合并 {self.merged} 条")
        if self.folded:
            bits.append(f"折叠 {self.folded} 条")
        if self.facts:
            bits.append(f"关于他们的事 {self.facts} 条")
        if self.facts_merged:
            bits.append(f"合并重复说法 {self.facts_merged} 条")
        if self.relations:
            bits.append(f"关系 {self.relations} 条")
        if self.digests:
            bits.append(f"缩略版 {self.digests} 份")
        if self.affinity:
            bits.append(f"好感变化 {self.affinity} 次")
        if self.dream:
            bits.append("做了一个梦")
        return "、".join(bits) or "没什么可整理的"


class Consolidator:
    """睡眠整理器：拿到（世界 + 记忆 + 画像 + 一个模型通道）就能跑。"""

    def __init__(
        self,
        *,
        world: Any,
        memory: MemoryEngine,
        profiles: ProfileStore,
        prompts: Any = None,
        sessions_of: Callable[[str], list[str]] | None = None,
        session_label: Callable[[str], str] | None = None,
        datetime_provider: Callable[[float], datetime] | None = None,
    ) -> None:
        self.world = world
        self.memory = memory
        self.profiles = profiles
        self.prompts = prompts
        self._sessions_of = sessions_of or (lambda session_id: [session_id])
        # 「这是在哪儿说的 / 这条记忆是哪儿的」：默认按 id 现推一个人话标签，
        # 引擎会传自己的 session_label 进来（群 123「名字」/ 私聊 456）。
        self._session_label = session_label or _fallback_session_label
        self._datetime = datetime_provider or datetime.fromtimestamp

    def set_world(self, world: Any) -> None:
        self.world = world

    # ---------------- 输入 ----------------

    def chat_lines(
        self,
        records: list[dict[str, Any]],
        *,
        limit: int = 400,
        session_labels: dict[str, str] | None = None,
        current_session: str = "",
    ) -> list[str]:
        """这段时间的原始聊天记录（带时间、谁说的、**在哪说的**）；这是整理的上下文。

        素材**不按会话过滤**（组里的话共享同一段经历），但必须标出来源：
        少了一条来源标签，模型会把"私聊里说的悄悄话"和"群里当众说的话"
        压成同一条要点，以后再拿出来用就说错地方了。
        """

        labels = dict(session_labels or {})
        rows: list[str] = []
        for item in records:
            text = " ".join(str(item.get("text") or "").split())
            if not text:
                continue
            who = str(item.get("name") or item.get("user_id") or "有人")
            uid = str(item.get("user_id") or "")
            stamp = self._datetime(float(item.get("at") or 0.0)).strftime("%m-%d %H:%M")
            where = str(item.get("origin") or current_session or "")
            tag = "（她自己）" if item.get("is_self") else f"（QQ {uid}）" if uid else ""
            source_id = str(item.get("source_id") or "")
            rows.append(f"[{stamp}·{self._where(where, labels)}·session={where}·source={source_id}] {who}{tag}: {text}")
        return rows[-max(1, int(limit)) :]

    def _where(self, session_id: str, labels: dict[str, str]) -> str:
        """来源会话的人话标签（引擎给的优先，缺了按 id 现推）。"""

        key = str(session_id or "")
        known = str(labels.get(key) or "")
        if known:
            return known
        return self._session_label(key)

    def memory_lines(
        self,
        session_id: str,
        *,
        limit: int = 120,
        session_labels: dict[str, str] | None = None,
    ) -> list[str]:
        """现有记忆（只给相关的、最近的）：带 id 与**来源会话**，让模型按 id 合并或折叠。"""

        rows = self.memory.db.query_memories(
            session_ids=self._sessions_of(session_id),
            memory_types=["scene", "event", "inner"],
            exclude_source_scoped=True,
            limit=max(1, int(limit)),
        )
        labels = dict(session_labels or {})
        lines: list[str] = []
        for item in rows:
            stamp = self._datetime(float(item.get("created_at") or 0.0)).strftime("%Y-%m-%d %H:%M")
            tier = "要点" if str(item.get("tier")) == "gist" else "原文"
            people = ",".join(str(entry) for entry in (item.get("participants") or []))
            tail = f"（{people}）" if people else ""
            where = self._where(str(item.get("session_id") or ""), labels)
            node_id = str(item.get("node_id") or "")
            node = self.world.node_map().get(node_id)
            place = str(node.name or node_id) if node else node_id or "未标地点"
            lines.append(
                f"#{item['id']} [{stamp}·{tier}·{item['type']}·{where}·地点={place}"
                f"·persona={item.get('persona_id') or '未标人设'}·scope={item.get('scope')}] "
                f"{str(item.get('content') or '')[:400]}{tail}"
            )
        return lines

    def profile_lines(self, session_id: str, *, limit: int = 40, user_ids: set[str] | None = None) -> list[str]:
        """保留本批说话人已有的稳定速写；它不是新的原话证据。"""

        rows: list[str] = []
        for row in self.profiles.list_people(session_id, limit=limit):
            user_id = str(row.get("user_id") or "")
            if user_ids is not None and user_id not in user_ids:
                continue
            payload = dict(row.get("payload") or {})
            names = [str(item) for item in (payload.get("names") or []) if str(item)]
            view = self.profiles.view(session_id, user_id)
            bits = [
                f"session={session_id}",
                f"QQ {user_id}",
                names[-1] if names else user_id,
            ]
            if view is not None and view.digest:
                bits.append(f"印象：{view.digest}")
            rows.append("｜".join(bits))
        return rows

    def known_ids(
        self, session_id: str, records: list[dict[str, Any]] | None = None
    ) -> set[str]:
        """这一轮允许写进画像的人：**真的出现过**的那些 QQ 号。

        来源有两处——通讯录里已经建过档的人，以及这段时间的聊天记录里发过言的人。
        整理模型偶尔会顺着昵称编一个号出来（例如把「主人」写成某个不存在的数字），
        不挡的话通讯录里会凭空多出一个从没说过话的人。
        """

        ids: set[str] = set()
        for row in self.profiles.list_people(session_id, limit=500):
            user_id = str(row.get("user_id") or "").strip()
            if user_id:
                ids.add(user_id)
        for item in records or []:
            user_id = str(item.get("user_id") or "").strip()
            if user_id:
                ids.add(user_id)
        return ids

    # ---------------- 跑一次整理 ----------------

    async def run(
        self, *, session_id: str,
        llm: Callable[[str, str], Awaitable[str | None]] | None,
        mode: str = "full", chat_records: list[dict[str, Any]] | None = None,
        persona_text: str = "", now: float | None = None, dry_run: bool = False,
        session_labels: dict[str, str] | None = None, current_session: str = "",
        context_current: Callable[[], Awaitable[bool]] | None = None,
    ) -> ConsolidateResult:
        """Two independent stages: direct speech -> profiles; lived events -> journal review."""
        result = ConsolidateResult(mode=str(mode or "full"))
        world = self.world
        if llm is None or self.prompts is None:
            result.note = "没有可用的整理模型"
            return result
        stamp = float(now if now is not None else time.time())
        records, observed, originals = [], {}, {}
        for row in chat_records or []:
            snapshot = source_snapshot(row, session_id)
            stored = self.memory.db.source_record(snapshot["id"])
            if stored is not None and not stored["eligible"] or row.get("eligible") is False and stored is None:
                continue
            original = stored or snapshot
            if original["kind"] not in {"direct_speech", "self_statement"} or original["id"] in originals:
                continue
            body = direct_text(str(original.get("text") or ""))
            if not body:
                continue
            # A stored quote's author, origin and role are immutable too.
            records.append({**row, **original, "text": body, "source_id": original["id"],
                            "origin": original["session"], "is_self": original["kind"] == "self_statement"})
            originals[original["id"]] = original
            if not stored:
                observed[snapshot["id"]] = snapshot
        stages = []
        if records and self.world.profile.enabled and self.world.profile.consolidate_enabled:
            # Recognize a completed batch from before per-source receipts existed.
            if self.memory.db.kv_get(self._batch_key(session_id, result.mode, records)) and not dry_run:
                with self.memory.db.atomic():
                    for snapshot in observed.values():
                        self.memory.db.observe_source(snapshot, eligible=True)
                    self.memory.db.mark_profile_reviewed([row["source_id"] for row in records], result.mode, stamp)
            reviewed = self.memory.db.reviewed_profile_sources([row["source_id"] for row in records], result.mode)
            records = [row for row in records if row["source_id"] not in reviewed][:400]
        profile_key = self._batch_key(session_id, result.mode, records)
        if records and self.world.profile.enabled and self.world.profile.consolidate_enabled and not self.memory.db.kv_get(profile_key):
            known_profiles = []
            for sid in self._sessions_of(session_id):
                users = {str(row.get("user_id") or "") for row in records
                         if not row.get("is_self") and str(row.get("origin") or session_id) == sid}
                known_profiles.extend(self.profile_lines(sid, user_ids=users))
            system, prompt = self.prompts.build_consolidate_prompt(
                persona_text=persona_text,
                known_profiles=known_profiles[:40],
                chat_lines=self.chat_lines(records, session_labels=session_labels,
                                          current_session=current_session or session_id),
                date_text=self._datetime(stamp).date().isoformat(), mode=result.mode,
                max_items=12 if result.mode == "full" else 6,
            )
            before = {(str(row.get("origin") or session_id), str(row.get("user_id") or ""))
                      for row in records if not row.get("is_self")}
            versions = {(sid, uid): self._profile_version(sid, uid) for sid, uid in before}
            stages.append(("profiles", profile_key, system, prompt, versions))
        old = self.memory_lines(session_id, session_labels=session_labels)
        old_ids = {int(line.split(" ", 1)[0][1:]) for line in old}
        fields = ("content", "context", "session_id", "persona_id", "scope", "tier", "type", "source", "pinned",
                  "node_id", "created_at", "emotion", "weight", "participants", "related_users", "keywords")
        def version(mid):
            row = self.memory.db.memory_by_id(mid)
            return {key: row.get(key) for key in fields} if row else None
        versions = {mid: version(mid) for mid in old_ids}
        life_key = self._life_review_key(session_id, result.mode, versions)
        if old and self.world.memory.sleep_review and not self.memory.db.kv_get(life_key):
            system, prompt = self.prompts.build_life_review_prompt(
                memories=old, persona_text=persona_text, mode=result.mode)
            stages.append(("life", life_key, system, prompt, versions))
        if not stages:
            result.ok = True
            result.note = "没有新的生活经历或待整理的画像来源"
            return result
        previews, parsed = [], {}
        for kind, batch_key, system, prompt, before in stages:
            if self.world is not world or context_current is not None and not await context_current():
                result.skipped.append("角色或世界已改变，剩余整理结果不保存")
                break
            from .context_selection import evidence_context, request_for
            request = request_for(task="review", session=session_id, now=stamp)
            selected = evidence_context(request, prompt, key=kind)
            prompt = selected.render()
            try:
                reply = await llm(system, prompt)
                payload = extract_json_object(reply or "")
            except Exception as exc:
                result.retry_needed = True
                result.skipped.append(f"{'画像' if kind == 'profiles' else '生活'}整理暂缓：{exc}")
                continue
            if self.world is not world or context_current is not None and not await context_current():
                result.skipped.append("角色或世界已改变，本次整理结果不保存")
                break
            result.raw = (result.raw + f"[{kind}] " + str(reply or ""))[:2000]
            previews.append({"stage": kind, "system": system, "user": prompt,
                             "context_selection": request.trace})
            if not isinstance(payload, dict):
                result.retry_needed = True
                result.note = "整理模型没有给出可用的 JSON"
                continue
            # Strict output allowlists: ignore even valid JSON from the other stage.
            if kind == "profiles":
                keys = {"digests"} if result.mode == "nap" else {"facts", "relations", "digests"}
            else:
                keys = {"forget", "dream"} if result.mode == "nap" else {"merge", "forget", "dream"}
            payload = {key: value for key, value in payload.items() if key in keys}
            parsed[kind] = payload
            if dry_run:
                result.ok = True
                continue
            if kind == "profiles" and result.mode != "nap":
                payload["relations"] = self._grounded_relations(payload, records, result)
            with self.memory.db.atomic():
                if self.memory.db.kv_get(batch_key):
                    continue
                if kind == "profiles":
                    source_ids = [row["source_id"] for row in records]
                    if self.memory.db.reviewed_profile_sources(source_ids, result.mode):
                        result.skipped.append("部分原话已被另一轮整理，本次结果不保存，剩余原话留待下次")
                        continue
                    if not self._sources_current({sid: originals[sid] for sid in source_ids}):
                        result.skipped.append("原话来源或使用资格已改变，本次人物整理结果不保存")
                        continue
                    for source_id in source_ids:
                        if source_id in observed:
                            self.memory.db.observe_source(observed[source_id], eligible=True)
                    updates = self._scoped_profile_updates(session_id, payload, records, result)
                    changed = {key for key, value in before.items() if self._profile_version(*key) != value}
                    for sid, local in updates.items():
                        for uid in {uid for where, uid in changed if where == sid}:
                            for field in ("facts", "relations"):
                                local[field] = [item for item in local[field] if str(item.get("user_id")) != uid]
                            local["digests"].pop(uid, None)
                            result.skipped.append(f"人物资料已改变，保留当前资料：{sid} / {uid}")
                        known = {uid for where, uid in before if where == sid and (where, uid) not in changed}
                        apply = self._apply_nap if result.mode == "nap" else self._apply_full
                        apply(sid, local, result, now=stamp, known=known)
                    self.memory.db.mark_profile_reviewed(source_ids, result.mode, stamp)
                else:
                    valid = {mid for mid, original in before.items() if original is not None
                             and version(mid) == original}
                    # A stale review/dream cannot describe an edited or deleted experience.
                    if valid != set(before):
                        result.skipped.append("生活材料已改变，本次整理结果不保存")
                        continue
                    payload["_review_ids"] = valid
                    self._apply_full(session_id, payload, result, now=stamp, known=set())
                self.memory.db.kv_set(batch_key, {"at": stamp})
                if kind == "life":
                    after = {mid: version(mid) for mid in before}
                    after_key = self._life_review_key(session_id, result.mode, after)
                    self.memory.db.kv_set(after_key, {"at": stamp})
                result.ok = True
        if dry_run:
            result.preview = {**(previews[0] if previews else {}), "stages": previews}
            result.parsed = parsed
            result.note = "预览：画像与生活分开整理，没有写库"
        return result

    def _profile_version(self, session_id: str, user_id: str) -> dict | None:
        """Ignore normal presence counters; protect edits to stable contact data."""
        row = self.profiles.profile(session_id, user_id)
        if row is None:
            return None
        payload = row.get("payload") or {}
        return {"digest": row.get("digest"), "first_seen_at": row.get("first_seen_at"),
                "identity": {key: payload.get(key) for key in ("note", "call_me", "call_him")},
                "facts": self.profiles.facts(session_id, user_id),
                "bonds": self.profiles.bonds(session_id, user_id)}

    def _sources_current(self, originals: dict[str, dict]) -> bool:
        for source_id, original in originals.items():
            current = self.memory.db.source_record(source_id)
            if current is not None and (not current["eligible"] or any(
                current.get(key) != value for key, value in original.items() if key != "eligible"
            )):
                return False
            if current is None and original.get("eligible"):
                return False
        return True

    @staticmethod
    def _life_review_key(session_id: str, mode: str, versions: dict) -> str:
        # Recall strengthens weight automatically. It is checked during an
        # in-flight review but does not turn unchanged material into a new batch.
        material = {mid: {key: value for key, value in row.items() if key != "weight"} if row else None
                    for mid, row in versions.items()}
        return "life-review:" + hashlib.sha256(json.dumps(
            [session_id, mode, material], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


    @staticmethod
    def _batch_key(session_id, mode, records, payload=None):
        return "consolidate:" + hashlib.sha256(json.dumps(
            [session_id, mode, sorted({row["source_id"] for row in records}), payload or ""],
            ensure_ascii=False, sort_keys=True,
        ).encode()).hexdigest()

    def _scoped_profile_updates(self, session_id, payload, records, result):
        """Route social changes to their actual origin, never to all shared lives.

        The model may see shared memories. That does not authorize changing the
        same person's feelings in another conversation. Ambiguous changes wait.
        """
        sessions = set(self._sessions_of(session_id)) | {session_id}
        origin_records = {sid: [row for row in records if str(row.get("origin") or session_id) == sid]
                          for sid in sessions}
        local = {sid: {"facts": [], "relations": [], "affinity": [], "digests": {}} for sid in sessions}

        def normalized(text):
            return "".join(ch for ch in str(text or "").casefold() if ch.isalnum())

        def destination(item):
            uid = str(item.get("user_id") or "")
            explicit = str(item.get("source_session") or "")
            evidence = normalized(item.get("evidence"))
            if not origin_records or not any(origin_records.values()):
                return ""
            matches = {sid for sid, rows in origin_records.items() if any(
                not row.get("is_self") and str(row.get("user_id")) == uid
                and (not evidence or len(evidence) >= 4 and evidence in normalized(row.get("text")))
                for row in rows)}
            if item.get("evidence") and not matches:
                return ""
            if explicit:
                return explicit if explicit in matches else ""
            if len(matches) == 1:
                return next(iter(matches))
            return ""

        for field in ("facts", "relations", "affinity"):
            rows = self._rows(payload.get(field))
            if field == "facts":
                rows = rows[:MAX_FACTS_PER_RUN]
            elif field == "relations":
                rows = rows[:MAX_BONDS_PER_RUN]
            for item in rows:
                sid = destination(item)
                if sid:
                    local[sid][field].append(item)
                else:
                    result.skipped.append(f"材料里没这个人或会话来源不明确，未修改{field}：{item.get('user_id')}")
        digests = payload.get("digests")
        rows = [{"user_id": str(uid), "text": text} for uid, text in digests.items()] if isinstance(digests, dict) else self._rows(digests)
        for item in rows[:40]:
            sid = destination(item)
            if sid:
                local[sid]["digests"][str(item.get("user_id"))] = item.get("text")
            else:
                result.skipped.append(f"速写缺少明确的本人发言来源：{item.get('user_id')}")
        return {sid: part for sid, part in local.items()
                if any(part.get(field) for field in ("facts", "relations", "digests"))}

    def _grounded_relations(
        self, payload: dict[str, Any], records: list[dict[str, Any]], result: ConsolidateResult
    ) -> list[dict[str, Any]]:
        """Relationship evidence must belong to the identified person.

        Exclusive commitments also need her own explicit confirmation. Without
        that second quote, preserve the user's statement as a candidate only.
        """
        from .relationship import grounded_relationship
        kept = []
        for item in self._rows(payload.get("relations"))[:MAX_BONDS_PER_RUN]:
            uid = str(item.get("user_id") or "")
            bond = self.world.profile.bond_by_name(str(item.get("type") or ""))
            source = str(item.get("source_session") or "")
            local_records = [row for row in records if str(row.get("origin") or "") == source] if source else records
            candidate = grounded_relationship(item, local_records, exclusive=bool(
                bond is not None and (self.profiles._unique_slot(bond.slot)
                                      or bond.name not in {stage.bond for stage in self.world.profile.friendship_stages}
                                      and not bond.negative)))
            if candidate is None:
                result.skipped.append(f"关系依据不在本人的原话中：{uid} / {item.get('type')}")
                continue
            kept.append(candidate)
        return kept

    # ---------------- 落库：整觉 ----------------

    def _apply_full(
        self,
        session_id: str,
        payload: dict[str, Any],
        result: ConsolidateResult,
        *,
        now: float,
        known: set[str] | None = None,
    ) -> None:
        for item in self._rows(payload.get("merge")):
            keep = self._int(item.get("keep_id"))
            requested = item.get("drop_ids") or []
            if not isinstance(requested, list):
                result.skipped.append("合并编号格式不正确，保留原记录")
                continue
            drops = {self._int(value) for value in requested} - {keep}
            text = str(item.get("text") or "").strip()
            allowed = payload.get("_review_ids", set())
            kept = self.memory.db.memory_by_id(keep) if keep in allowed else None
            if kept is None or kept.get("pinned") or not text:
                continue
            compatible = {value for value in drops if value in allowed and (row := self.memory.db.memory_by_id(value))
                          and not row.get("pinned") and all(row.get(key) == kept.get(key) for key in ("session_id", "persona_id", "scope", "type", "node_id"))}
            if drops != compatible:
                # Reject the whole rewrite: its text may have combined material
                # from a protected or unrelated record, including the keep row.
                result.skipped.append("合并包含固定、未提供或不同归属/地点的记录，保留本组合并原文")
                continue
            self.memory.fold(keep, text=text[:200], when=now)
            for drop in drops:
                sources = self.memory.db.memory_sources(drop)["sources"]
                self.memory.db.attach_evidence(keep, [row["id"] for row in sources])
                self.memory.fold(drop, text=text, when=now)
                result.folded += 1
            if keep and (text or drops):
                result.merged += 1
        touched_facts: set[str] = set()
        for item in self._rows(payload.get("facts"))[:MAX_FACTS_PER_RUN]:
            # 没带"他的原话"的事实一律不记：这是最容易被模型编出来的那一类
            if not str(item.get("evidence") or "").strip():
                result.skipped.append(f"没带原话的事实被跳过：{item.get('text')}")
                continue
            user_id = str(item.get("user_id") or "").strip()
            if not self._known(user_id, known):
                result.skipped.append(f"材料里没这个人的事实被跳过：{user_id}")
                continue
            outcome = self.profiles.note_fact(
                session_id,
                user_id,
                text=str(item.get("text") or ""),
                kind=str(item.get("kind") or "other"),
                evidence=str(item.get("evidence") or ""),
                context=str(item.get("context") or ""),
                confidence=float(item.get("confidence") or 0.6),
                source_session=session_id,
            )
            if outcome.get("ok"):
                result.facts += 1
                touched_facts.add(user_id)
            else:
                result.skipped.append(str(outcome.get("reason") or "事实没记下"))
        # 顺手把同一个人的重复说法合并掉：同一件事换个措辞记六遍，
        # 提示词里那一类（每类最多 6 条）就全是废话了。
        # 除了这一轮写到的人，材料里出现的人一起过一遍——这样早先攒下来的重复
        # 也能在她第一次睡觉整理时清掉。
        for user_id in sorted(set(touched_facts) | set(list(known or set())[:12])):
            merged = self.profiles.dedupe_facts(session_id, user_id)
            if merged:
                result.facts_merged += merged
        for item in self._rows(payload.get("relations"))[:MAX_BONDS_PER_RUN]:
            if not str(item.get("evidence") or "").strip():
                result.skipped.append(f"没带原话的关系被跳过：{item.get('type')}")
                continue
            user_id = str(item.get("user_id") or "").strip()
            if not self._known(user_id, known):
                result.skipped.append(f"材料里没这个人的关系被跳过：{user_id}")
                continue
            outcome = self.profiles.note_bond(
                session_id,
                user_id,
                type=str(item.get("type") or ""),
                evidence=str(item.get("evidence") or ""),
                confidence=float(item.get("confidence") or 0.6),
                asserted_by=str(item.get("asserted_by") or "她的判断"),
                now=now,
                automatic=True,
            )
            if outcome.get("ok"):
                result.relations += 1
            else:
                result.skipped.append(str(outcome.get("reason") or "关系没记下"))
        result.digests += self._apply_digests(
            session_id, payload.get("digests"), known=known
        )
        for item in self._rows(payload.get("forget")):
            memory_id = self._int(item.get("id"))
            row = self.memory.db.memory_by_id(memory_id)
            if memory_id in payload.get("_review_ids", set()) and row and not row.get("pinned"):
                self.memory.fold(memory_id, text=str(item.get("text") or ""), when=now)
                result.folded += 1
        result.dream = self._apply_dream(session_id, payload.get("dream"), now=now)

    # ---------------- 落库：小睡 ----------------

    def _apply_nap(
        self,
        session_id: str,
        payload: dict[str, Any],
        result: ConsolidateResult,
        *,
        now: float,
        known: set[str] | None = None,
    ) -> None:
        result.digests += self._apply_digests(
            session_id, payload.get("digests"), known=known
        )
        result.dream = self._apply_dream(session_id, payload.get("dream"), now=now)

    # ---------------- 小工具 ----------------

    @staticmethod
    def _rows(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, dict):
            return [value]
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        return []

    @staticmethod
    def _int(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _known(user_id: str, known: set[str] | None) -> bool:
        """这个号在材料里出现过没有；对不上就不写进画像。"""

        return bool(user_id) and user_id in (known or set())



    def _apply_digests(
        self, session_id: str, value: Any, *, known: set[str] | None = None
    ) -> int:
        if not isinstance(value, dict):
            return 0
        count = 0
        for user_id, digest in value.items():
            text = " ".join(str(digest or "").split())
            if not text or not str(user_id).strip():
                continue
            if known and str(user_id).strip() not in known:
                continue
            self.profiles.set_digest(session_id, str(user_id), text)
            count += 1
        return count

    def _apply_dream(self, session_id: str, value: Any, *, now: float) -> str:
        text = " ".join(str(value or "").split())[:200]
        if not text:
            return ""
        self.memory.remember(
            session_id=session_id,
            persona_id="",
            node_id="",
            content=f"做了个梦：{text}",
            memory_type=DREAM,
            emotion="迷糊",
            weight=0.25,
            source="dream",
            tier="gist",
            context=text,
            participants=[],
        )
        return text


def next_review_at(*, now: float, step: int = 0) -> float:
    """按间隔重复算下一次回访时间（第 0 步 = 1 天后）。"""

    days = REVIEW_STEPS[max(0, min(int(step), len(REVIEW_STEPS) - 1))]
    return float(now) + days * 86400


def dump_payload(payload: dict[str, Any]) -> str:
    """调试用：把整理模型返回的 JSON 压成一行。"""

    return json.dumps(payload, ensure_ascii=False)[:400]
