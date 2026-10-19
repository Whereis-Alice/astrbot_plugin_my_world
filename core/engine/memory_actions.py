"""recall / remember 这两条记忆动作。"""

from __future__ import annotations

from ..model_calls import model_operation

from typing import Any
from ..json_actions import (
    PlannedAction,
    extract_json_object,
)
from ..models import NodeDef
from ..state import WorldState
from .models import TickOutcome
from .util import _clip_log


class MemoryActionsMixin:
    async def _run_recall(
        self,
        state: WorldState,
        node: NodeDef | None,
        outcome: TickOutcome,
        action: PlannedAction,
    ) -> None:
        """主动回想：把「想回忆什么」翻成检索条件，翻记忆，再带着结果问她一次。"""

        intent = (action.intent or action.content or "").strip()
        if not intent and action.target_node:
            intent = f"回忆一下{self._node_name(action.target_node)}里的事"
        if not intent:
            outcome.notes.append("回想没有说要回忆什么，已跳过")
            await self._log_event(
                state, "recall_start", {"intent": "", "note": "没有说要回忆什么"}
            )
            return

        query = await self._parse_recall_query(state, intent, action.target_node)
        await self._log_event(
            state,
            "recall_start",
            {"intent": intent, "query": query},
            outcome=self._echo_into(outcome, state),
        )

        limit = max(1, min(5, int(query.get("limit") or 3)))
        memories = self.memory.recall(
            session_id=state.session_id,
            persona_id="",
            nodes=list(query.get("nodes") or []),
            keyword=str(query.get("keyword") or ""),
            strict_keyword=True,
            memory_type=str(query.get("type") or ""),
            mode=(self.world.memory_scope_mode if self.world else None),
            limit=limit,
            now=self._now(),
        )
        # 「回想一下关于主人的事」：她点名了某个人时，优先给跟这个人有关的记忆
        focus = self._recall_focus_user(state, f"{intent} {query.get('keyword') or ''}")
        if focus:
            related = [
                item
                for item in memories
                if focus in [str(entry) for entry in (item.participants or [])]
                or focus in [str(entry) for entry in (item.related_users or [])]
            ]
            if related:
                memories = related
        try:
            stamp_now = self.local_now()
        except Exception:
            stamp_now = None
        detail = "\n".join(
            item.render(stamp=self.prompts._memory_stamp(item.created_at, stamp_now))
            for item in memories
        )
        keyword = str(query.get("keyword") or "")
        where = "、".join(query.get("node_names") or []) or "任何地方"
        await self._log_event(
            state,
            "recall_done",
            {
                "count": len(memories),
                "keyword": keyword,
                "where": where,
                "detail": detail or "什么都没想起来",
            },
            outcome=self._echo_into(outcome, state),
        )

        hint = (
            "把你想起来的这些用第一人称说一句（可以带一点当时的情绪），别逐条念、别编新的细节。"
            if detail
            else "这次什么都没想起来——照实说一句「想不起来」就好，不要编。也可以只输出 think 或空动作列表。"
        )
        if detail and bool(getattr(self.world.profile, "enabled", True)):
            # 翻旧账时如果发现"关于他的事 / 你们的关系已经变了"，允许她当场用 remember 更新
            hint += (
                "如果翻出来的旧事里，关于某个人的信息或者你们的关系**已经和现在不一样了**"
                "（称呼变了、喜好变了、约定作废了、亲疏变了），可以顺手写一个 remember "
                "把新的情况改掉（记得带上他的原话）；没有变化就不用写。"
            )
        await self._llm_followup(
            state, node, outcome, hint, detail or "（翻了一遍记忆，什么都没想起来）"
        )

    async def _run_remember(
        self, state: WorldState, outcome: TickOutcome, action: PlannedAction
    ) -> None:
        """内置动作「记住」：把这一刻值得记的事当场写进通讯录。

        走的是和睡眠整理**同一套写入**（``ProfileStore``）与同一套校验：

        - 没带"他的原话"（evidence）一律不记——最容易编的就是这一类；
        - 只认她**真的认识的人**（会话里有档案的），免得顺着昵称编出一个号；
        - 事实同类去重、正反冲突自动替换；关系冲突按配置策略处理
          （默认记成"他自称"，唯一槽位按实际会话检查）；同类关系只留一条。
        """

        session_id = str(outcome.place or outcome.session_id or state.session_id)
        store = getattr(self, "profiles", None)
        params = dict(action.params or {})
        user_id = str(
            params.get("user") or params.get("user_id") or action.target or ""
        ).strip()
        text = " ".join(str(params.get("text") or "").split())
        evidence = " ".join(str(params.get("evidence") or "").split())
        kind = str(params.get("kind") or "近况").strip()
        bond = str(params.get("bond") or "").strip()
        delta = params.get("affinity_delta")
        notes: list[str] = []
        if store is None or not bool(getattr(self.world.profile, "enabled", True)):
            outcome.notes.append("通讯录功能没开，这一条不记")
            return
        if not user_id:
            outcome.notes.append("「记住」没写清是记谁的，已跳过")
            await self._log_event(
                state,
                "skip",
                {"action": "remember", "note": "没有指定是记谁的"},
                outcome=outcome,
            )
            return
        known = {str(row.get("user_id") or "") for row in store.list_people(session_id, limit=500)}
        if user_id not in known:
            # 只认她真的见过的人：编出来的号码不进通讯录
            outcome.notes.append(f"「记住」里那个人（{user_id}）她还没见过，已跳过")
            await self._log_event(
                state,
                "skip",
                {"action": "remember", "note": f"材料里没这个人：{user_id}"},
                outcome=outcome,
            )
            return
        if not text and not bond and delta in (None, ""):
            outcome.notes.append("「记住」没说清要记什么，已跳过")
            return
        if (text or bond) and not evidence:
            outcome.notes.append("没带上他的原话，这一条不记（免得编）")
            await self._log_event(
                state,
                "skip",
                {"action": "remember", "note": "没带原话，事实 / 关系不记"},
                outcome=outcome,
            )
            return
        written: list[str] = []
        if text:
            result = store.note_fact(
                session_id,
                user_id,
                text=text,
                kind=kind,
                evidence=evidence,
                context="她当场记住的",
                confidence=0.9,
                source_session=session_id,
            )
            if result.get("ok"):
                written.append(f"记住了「{text}」（{kind}）")
            else:
                notes.append(str(result.get("reason") or "事实没记下"))
        if bond:
            from ..relationship import grounded_relationship
            definition = self.world.profile.bond_by_name(bond)
            candidate = grounded_relationship({
                "user_id": user_id, "evidence": evidence,
                "confirmation": params.get("confirmation"), "asserted_by": "她的判断",
            }, [row for row in self.chat_window(state, session_id)
                if str(row.get("origin") or session_id) == session_id], exclusive=bool(
                definition and (store._unique_slot(definition.slot)
                                or definition.name not in {stage.bond for stage in self.world.profile.friendship_stages}
                                and not definition.negative)))
            if candidate is None:
                notes.append("关系依据不在本人的近期原话中")
        if bond and candidate is not None:
            result = store.note_bond(
                session_id,
                user_id,
                type=bond,
                evidence=evidence,
                confidence=0.9,
                asserted_by=candidate["asserted_by"],
                automatic=True,
            )
            if result.get("ok"):
                action_name = str(result.get("action") or "")
                if action_name == "claim":
                    written.append(f"「{bond}」这条先记成他自称")
                else:
                    written.append(f"关系改成「{bond}」")
            else:
                notes.append(str(result.get("reason") or "关系没记下"))
        if delta not in (None, ""):
            try:
                amount = float(delta)
            except (TypeError, ValueError):
                amount = 0.0
            if amount:
                actual = store.apply_reply_affinity(
                    session_id,
                    user_id,
                    amount,
                    reason="她自己记住的",
                    source="remember",
                )
                if actual:
                    written.append(f"好感 {actual:+.2f}")
        await self._log_event(
            state,
            "remember",
            {
                "user": user_id,
                "written": written,
                "skipped": notes,
                "evidence": _clip_log(evidence),
            },
            outcome=outcome,
        )
        summary = "；".join([*written, *notes]) or "没什么可记的"
        outcome.notes.append(f"「记住」：{summary}")
        self._log("debug", f"[my_world] 记住：{user_id} -> {summary}")

    @staticmethod
    def _echo_into(outcome: TickOutcome, state: WorldState) -> TickOutcome:
        """这几条事件也走调试回显（勾了对应类型就会发出来）。

        复制一份 TickOutcome 是为了"只回显、不掺进这一轮的动作输出"，
        但**发生地必须带过去**：不带的话回显会掉回她的存档会话——
        在私聊里让她拍腿照，🧩「执行指令 /看看腿」那行就跑到群里去了。
        """

        echo = TickOutcome(session_id=outcome.session_id)
        echo.place = str(getattr(outcome, "place", "") or "")
        return echo

    @model_operation("memory", "动作需要理解记忆查询条件")
    async def _parse_recall_query(
        self, state: WorldState, intent: str, target_node: str = ""
    ) -> dict[str, Any]:
        """解析检索条件：优先问辅助模型，拿不到就退化成"按名字猜"。"""

        nodes = list(self.world.node_map().values())
        node_names = [item.name or item.id for item in nodes]
        zone_names = [zone.name or zone.id for zone in self.world.zones]
        query: dict[str, Any] = {}
        if self.helper_llm is not None and self._tool_param_allowed(state):
            system_prompt, prompt = self.prompts.build_recall_query_prompt(
                intent=intent,
                node_names=node_names,
                zone_names=zone_names,
                memory_types=["scene", "interaction", "relation", "inner", "event"],
            )
            reply = await self._ask_judge(state.session_id, system_prompt, prompt)
            self._count_tool_param(state)
            payload = extract_json_object(reply) if reply else None
            if isinstance(payload, dict):
                query = payload

        # 地点/区域：结构化字段优先，其次从意图里出现的名字里找
        node_id = self._match_node(str(query.get("node") or "").strip() or target_node)
        zone_id = self._match_zone(str(query.get("zone") or "").strip())
        if not node_id and not zone_id:
            node_id = self._match_node_in_text(intent)
            zone_id = self._match_zone_in_text(intent)

        wanted: list[str] = []
        if node_id:
            wanted = [node_id]  # 指定地点：只看那个地点
        elif zone_id:
            wanted = [item.id for item in self.world.nodes_in_zone(zone_id)]
        return {
            "keyword": str(query.get("keyword") or "").strip() or ("" if (node_id or zone_id) else intent),
            "nodes": wanted,
            "node_names": [self._node_name(item) for item in wanted],
            "zone": zone_id,
            "type": str(query.get("type") or "").strip(),
            "limit": query.get("limit") or 3,
        }

    def _match_node(self, text: str) -> str:
        if not text:
            return ""
        key = text.strip()
        for node in self.world.node_map().values():
            if key in (node.id, node.name):
                return node.id
        return ""

    def _match_zone(self, text: str) -> str:
        if not text:
            return ""
        key = text.strip()
        for zone in self.world.zones:
            if key in (zone.id, zone.name):
                return zone.id
        return ""

    def _match_node_in_text(self, text: str) -> str:
        for node in self.world.node_map().values():
            name = node.name or node.id
            if name and name in text:
                return node.id
        return ""

    def _match_zone_in_text(self, text: str) -> str:
        for zone in self.world.zones:
            name = zone.name or zone.id
            if name and name in text:
                return zone.id
        return ""
