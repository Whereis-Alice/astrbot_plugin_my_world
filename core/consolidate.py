"""睡眠整理：把这段时间的经历消化成记忆与画像，顺手把细节折叠掉。

对着人类的记忆机制做的三件事：

1. **消化**（慢波睡眠的系统巩固）：原始聊天 + 现有记忆 + 画像现状 → 要点、事实、关系、缩略版；
2. **重组**：同一个人的同一件事合并、被确认过的旧记忆更新、回访时间按间隔重复往后排；
3. **遗忘**：只"折叠"（raw → gist，原文进 ``context``），**不删除**——忘的是细节，不是那件事。

小睡只做轻整理：要点化 + 缩略版 + 一条梦，不碰关系与事实（小睡短，做重判断容易记错）。
"""

from __future__ import annotations

import json
import hashlib
from .evidence import source_snapshot, direct_text
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .json_actions import extract_json_object
from .memory import INTERACTION, MemoryEngine
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

DREAM_TYPE = "dream"
"""梦：低权重的记忆，只进"想起"那一段，不进画像。"""

MAX_FACTS_PER_RUN = 8
"""一次整理最多记几条"关于他的事"（模型一次给太多时，多半是在凑数）。"""

MAX_BONDS_PER_RUN = 4
"""一次整理最多改几条关系（关系本来就该慢）。"""


@dataclass
class ConsolidateResult:
    """一次整理做了什么（写日志与调试输出用）。"""

    mode: str = "full"
    ok: bool = False
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
    ) -> None:
        self.world = world
        self.memory = memory
        self.profiles = profiles
        self.prompts = prompts
        self._sessions_of = sessions_of or (lambda session_id: [session_id])
        # 「这是在哪儿说的 / 这条记忆是哪儿的」：默认按 id 现推一个人话标签，
        # 引擎会传自己的 session_label 进来（群 123「名字」/ 私聊 456）。
        self._session_label = session_label or _fallback_session_label

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
            stamp = time.strftime(
                "%m-%d %H:%M", time.localtime(float(item.get("at") or 0.0))
            )
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
            limit=max(1, int(limit)),
        )
        labels = dict(session_labels or {})
        lines: list[str] = []
        for item in rows:
            stamp = time.strftime(
                "%m-%d", time.localtime(float(item.get("created_at") or 0.0))
            )
            tier = "要点" if str(item.get("tier")) == "gist" else "原文"
            people = ",".join(str(entry) for entry in (item.get("participants") or []))
            tail = f"（{people}）" if people else ""
            where = self._where(str(item.get("session_id") or ""), labels)
            lines.append(
                f"#{item['id']} [{stamp}·{tier}·{where}] "
                f"{str(item.get('content') or '')[:120]}{tail}"
            )
        return lines

    def profile_lines(self, session_id: str, *, limit: int = 40) -> list[str]:
        """她现在认识的人：给整理模型当"已知"，免得重复记同一件事。"""

        rows: list[str] = []
        for row in self.profiles.list_people(session_id, limit=limit):
            user_id = str(row.get("user_id") or "")
            payload = dict(row.get("payload") or {})
            names = [str(item) for item in (payload.get("names") or []) if str(item)]
            bonds = self.profiles.bonds(session_id, user_id, statuses=["current"])
            view = self.profiles.view(session_id, user_id)
            bits = [
                f"QQ {user_id}",
                names[-1] if names else user_id,
                "、".join(str(bond.get("type") or "") for bond in bonds) or "没定关系",
                f"好感 {round(float(row.get('affinity') or 0.0))}",
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
        self,
        *,
        session_id: str,
        llm: Callable[[str, str], Awaitable[str | None]] | None,
        mode: str = "full",
        chat_records: list[dict[str, Any]] | None = None,
        persona_text: str = "",
        pending: list[str] | None = None,
        now: float | None = None,
        dry_run: bool = False,
        session_labels: dict[str, str] | None = None,
        current_session: str = "",
    ) -> ConsolidateResult:
        """跑一次整理。``llm(system, prompt)`` 由调用方提供（独立整理模型）。

        ``dry_run=True``：照常组装输入、调模型、解析，但**一个字都不写库**，
        把提示词与模型原话一起返回——用来先看"它整理得像不像样"。
        """

        result = ConsolidateResult(mode=str(mode or "full"))
        if llm is None or self.prompts is None:
            result.note = "没有可用的整理模型"
            return result
        stamp = float(now if now is not None else time.time())
        max_items = 12 if result.mode == "full" else 6
        records = []
        source_map = {}
        observed = {}
        for row in chat_records or []:
            snapshot = source_snapshot(row, session_id)
            stored = self.memory.db.source_record(snapshot["id"])
            if stored is not None and not stored["eligible"]:
                continue
            if row.get("eligible") is False and stored is None:
                continue
            if snapshot["kind"] not in {"direct_speech", "self_statement"}:
                continue
            original = stored or snapshot
            body = direct_text(str(original.get("text") or ""))
            if not body:
                continue
            record = {**row, "text": body, "source_id": snapshot["id"]}
            records.append(record)
            source_map[snapshot["id"]] = {**original, "text": body}
            if not stored:
                observed[snapshot["id"]] = snapshot

        batch_key = self._batch_key(session_id, result.mode, records)
        if records and not dry_run and self.memory.db.kv_get(batch_key):
            result.ok = True
            result.note = "这批来源已经整理过，不重复记账"
            return result

        here = str(current_session or session_id or "")
        system, prompt = self.prompts.build_consolidate_prompt(
            persona_text=persona_text,
            # 素材带来源标签（群 / 私聊）：模型才知道"这是哪里说的话"，
            # 免得把两处的对话压成同一件事。素材本身不按会话过滤。
            chat_lines=self.chat_lines(
                records,
                session_labels=session_labels,
                current_session=here,
            ),
            # New evidence extraction never sees recalled conclusions or existing feelings.
            memories=[],
            profiles=[],
            pending=list(pending or []),
            date_text=time.strftime("%Y-%m-%d", time.localtime(stamp)),
            mode=result.mode,
            max_items=max_items,
        )
        try:
            reply = await llm(system, prompt)
        except Exception as exc:
            result.note = f"整理调用失败：{exc}"
            return result
        payload = extract_json_object(reply or "")
        result.raw = str(reply or "")[:2000]
        if dry_run:
            result.ok = bool(reply)
            result.note = "预览：只跑模型，没有写库"
            result.parsed = payload if isinstance(payload, dict) else {}
            result.preview = {"system": system, "user": prompt}
            if not isinstance(payload, dict) or not payload:
                result.note = "预览：模型没给出可用的 JSON"
            return result
        if not isinstance(payload, dict) or not payload:
            result.note = "整理模型没有给出可用的 JSON"
            return result
        # Scores belong to the blind per-turn evaluation, never to sleep or old-memory review.
        if payload.pop("affinity", None):
            result.skipped.append("睡眠整理不重复修改好感；会话来源不明确或已有的互动评价留给逐轮评估处理")
        payload.pop("merge", None)
        payload.pop("forget", None)
        review_versions = {}
        if result.mode == "full":
            old = self.memory_lines(session_id, session_labels=session_labels)
            old_ids = {int(line.split(" ", 1)[0][1:]) for line in old}
            review_versions = {mid: self.memory.db.memory_by_id(mid) for mid in old_ids}
            if old:
                try:
                    reviewed = extract_json_object(await llm(
                        "只整理已经存在的记忆。不能提取新事实、判断用户动机、改变好感、关系或人设。"
                        "保留日期和事实变化，不把旧总结作为新证据。只输出 JSON："
                        '{"merge":[{"keep_id":1,"drop_ids":[2],"text":"保持原意的概括"}],'
                        '"forget":[{"id":3,"text":"保留原意的简短要点"}]}。可以输出空对象。',
                        json.dumps({"existing_memories": old}, ensure_ascii=False)) or "")
                    if isinstance(reviewed, dict):
                        payload.update({key: reviewed.get(key, []) for key in ("merge", "forget")})
                except Exception:
                    result.skipped.append("旧记忆整理暂缓；新证据仍独立保存")
            payload["_review_ids"] = old_ids
        for field in ("memories", "notes"):
            for item in self._rows(payload.get(field)):
                raw_ids = item.get("source_ids")
                ids = [str(sid) for sid in raw_ids if str(sid) in source_map] if isinstance(raw_ids, list) else []
                if not ids and item.get("context"):
                    quote = " ".join(str(item["context"]).split())
                    ids = [sid for sid, record in source_map.items() if len(quote) >= 4 and quote in str(record.get("text") or "")]
                item["_evidence"] = [source_map[sid] for sid in ids]
                if not ids:
                    item["_ungrounded"] = True
                if ids:
                    item["context"] = "\n".join(f"{source_map[sid].get('name') or source_map[sid].get('user_id')}: {source_map[sid].get('text')}" for sid in ids)
        if not records:
            # Preserve idempotency for an old-memory-only review, without trusting new facts.
            batch_key = self._batch_key(session_id, result.mode, records, {
                k: v for k, v in payload.items() if not k.startswith("_")
            })
        if self.memory.db.kv_get(batch_key):
            result.ok = True
            result.note = "这批来源已经整理过，不重复记账"
            return result
        result.ok = True
        if result.mode != "nap":
            payload = {**payload, "relations": self._grounded_relations(payload, records, result)}
        with self.memory.db.atomic():
            if self.memory.db.kv_get(batch_key):
                result.note = "这批来源已经整理过，不重复记账"
                return result
            for snapshot in observed.values():
                self.memory.db.observe_source(snapshot, eligible=True)
            payload["_review_ids"] = {
                mid for mid, before in review_versions.items()
                if before is not None and (after := self.memory.db.memory_by_id(mid))
                and all(before.get(key) == after.get(key) for key in (
                    "content", "context", "session_id", "persona_id", "scope", "tier"
                ))
            }
            scoped = self._scoped_profile_updates(session_id, payload, records, result)
            for sid, local in scoped.items():
                local_records = [row for row in records if str(row.get("origin") or session_id) == sid]
                local_known = self.known_ids(sid, local_records)
                if result.mode == "nap":
                    self._apply_nap(sid, local, result, now=stamp, known=local_known)
                else:
                    self._apply_full(sid, local, result, now=stamp, known=local_known)
            self.memory.db.kv_set(batch_key, {"at": stamp})
        return result

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
        known = {sid: self.known_ids(sid, rows) for sid, rows in origin_records.items()}
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
                return explicit if explicit in sessions and uid in known[explicit] and (not matches or explicit in matches) else ""
            if len(matches) == 1:
                return next(iter(matches))
            if not matches:
                candidates = {sid for sid in sessions if uid in known[sid]}
                if len(candidates) == 1:
                    return next(iter(candidates))
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
        for uid, text in (payload.get("digests") or {}).items() if isinstance(payload.get("digests"), dict) else []:
            sid = destination({"user_id": str(uid)})
            if sid:
                local[sid]["digests"][str(uid)] = text
        routed_memories = {sid: [] for sid in sessions}
        for item in self._rows(payload.get("memories")):
            if item.get("_ungrounded"):
                result.skipped.append("新记忆没有可核对的来源，暂不保存")
                continue
            origins = {record["session"] for record in item.get("_evidence", [])}
            if len(origins) == 1 and next(iter(origins)) in sessions:
                routed_memories[next(iter(origins))].append(item)
            elif not origins:
                routed_memories[session_id].append(item)
            else:
                result.skipped.append("一条记忆混入多个会话来源，暂不保存")
        for sid, items in routed_memories.items():
            local[sid]["memories"] = items
        # Shared memory processing and the dream run once, in the original life.
        main = {**payload, **local.pop(session_id)}
        local[session_id] = main
        return {sid: part for sid, part in local.items() if sid == session_id
                or any(part.get(field) for field in ("facts", "relations", "affinity", "digests", "memories"))}

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
            drops = [self._int(value) for value in (item.get("drop_ids") or [])]
            drops = [value for value in drops if value]
            text = str(item.get("text") or "").strip()
            allowed = payload.get("_review_ids", set())
            kept = self.memory.db.memory_by_id(keep) if keep in allowed else None
            if kept is None or not text:
                continue
            drops = [value for value in drops if value != keep and value in allowed]
            drops = [value for value in drops if (row := self.memory.db.memory_by_id(value))
                     and all(row.get(key) == kept.get(key) for key in ("session_id", "persona_id", "scope"))]
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
        for user_id in sorted(set(touched_facts) | set(list(known)[:12])):
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
            if memory_id in payload.get("_review_ids", set()):
                self.memory.fold(memory_id, text=str(item.get("text") or ""), when=now)
                result.folded += 1
        # Apply fresh evidence last: reviewing an old ID must not overwrite a
        # new fact that updates that same ID through the conflict policy.
        for item in self._rows(payload.get("memories")):
            if self._write_memory(session_id, item, result, now=now):
                result.memories += 1
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
        for item in self._rows(payload.get("notes")):
            if self._write_memory(session_id, item, result, now=now):
                result.memories += 1
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

    def _write_memory(
        self,
        session_id: str,
        item: dict[str, Any],
        result: ConsolidateResult,
        *,
        now: float,
    ) -> bool:
        if item.get("_ungrounded"):
            result.skipped.append("新记忆没有可核对的来源，暂不保存")
            return False
        text = " ".join(str(item.get("text") or "").split())[:200]
        if not text:
            return False
        people = [
            str(value)
            for value in (item.get("participants") or [])
            if str(value).strip()
        ]
        days = float(item.get("review_in_days") or 0.0)
        next_at = now + days * 86400 if days > 0 else 0.0
        memory_id = self.memory.remember(
            session_id=session_id,
            persona_id="",
            node_id=str(item.get("node_id") or ""),
            content=text,
            memory_type=str(item.get("type") or INTERACTION),
            related_users=people,
            participants=people,
            emotion=str(item.get("emotion") or ""),
            weight=float(item.get("weight") or 0.55),
            source="consolidate",
            tier="gist",
            context=str(item.get("context") or ""),
            evidence=item.get("_evidence") or [],
            keywords=[
                str(value) for value in (item.get("keywords") or []) if str(value)
            ],
            next_review_at=next_at,
            pinned=bool(item.get("pinned")),
        )
        return bool(memory_id)

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
            memory_type=INTERACTION,
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
