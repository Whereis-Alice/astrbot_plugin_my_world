"""注入模式：回合收尾与复盘。"""

from __future__ import annotations

from ..model_calls import model_operation

from typing import Any
import json
from ..config_scope import session_scoped
from ..json_actions import (
    extract_json_object,
    parse_action_payload,
)
from ..models import pronoun_for as pronounce_for
from ..mood import keyword_signal
from .models import MessageContext
from .util import _clip_text


class TurnMixin:
    # ---------------- 注入模式：回合收尾与复盘 ----------------

    async def record_self_reply(
        self, session_id: str, text: str, *, persona_id: str = ""
    ) -> None:
        """宿主替她说完之后，把她这句话写回世界：短期聊天窗口 + 反重复。

        注入模式下主回复由宿主发送，引擎这边不再经手内容；但她的世界必须知道
        她刚说过什么，否则下一轮提示词里的「你最近说过的话」会是空的。
        """

        clean = " ".join(str(text or "").split()).strip()
        if not clean:
            return
        async with self.session_state(session_id) as state:
            recorded = state.note_chat(
                user_id="__self__",
                name=state.bot_current_nickname or state.bot_base_nickname or "你",
                text=clean,
                now=self._now(),
                keep=self.chat_history_limit(),
                is_self=True,
                origin=session_id,
            )
            state.note_reply(clean, session_id=session_id)
            self._tag_chat_origin(state, session_id, recorded, original_text=clean)
            self._count_passive_reply(state)

    @session_scoped
    @model_operation("reflection", "本轮结束，复盘对话与状态")
    async def reflect_turn(
        self,
        session_id: str,
        *,
        user_id: str = "",
        user_name: str = "",
        user_text: str = "",
        reply_text: str = "",
        reasoning_text: str = "",
        tool_trace: list[dict[str, Any]] | None = None,
        persona_id: str = "",
        interaction_entries: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        """回合复盘：她说完之后，把这一轮的心情 / 好感 / 话题 / 心事补记上。

        主回复已经由宿主说完，这次调用**只写账**：不改她说过的话、不碰工具、
        失败也只影响这一轮记账。成功后整条复盘会留在 ``state.last_reflection``，
        调试页与日志页可以直接看她的"状态和心思"。
        """

        if not self.is_enabled(session_id):
            return None
        world = self.world
        state_key = self.state_key(session_id)
        sources = self.interaction_sources(session_id, str(user_id or ""), user_text, interaction_entries)
        persona_text = await self._persona_text(session_id)
        ctx = MessageContext(
            session_id=session_id,
            user_id=str(user_id or ""),
            user_name=str(user_name or ""),
            text=str(user_text or ""),
            raw_text=str(user_text or ""),
            persona_id=str(persona_id or ""),
        )
        async with self.session_state(session_id) as state:
            if self.world is not world or state.session_id != state_key or not self.is_enabled(session_id):
                return None
            node = self.node(state.node_id) or self.node(self.default_node_id())
            recent_chat = [
                dict(item)
                for item in (state.recent_chat or [])
                if isinstance(item, dict)
            ][-8:]
            system, prompt = self.prompts.build_reflection_prompt(
                state=state,
                node=node,
                user_name=user_name,
                user_text=user_text,
                reply_text=reply_text,
                recent_chat=recent_chat,
                profile_text="",
                memories=[],
                other_context="",
                pronoun=pronounce_for(getattr(self.world, "gender", "female")),
            )
            prompt += "\n\n# 基础人设（判断行为的依据，不得改写）\n" + persona_text
            prompt += "\n\n# 本轮可用的直接发言证据\n" + json.dumps(sources, ensure_ascii=False)
            prompt += "\n\n" + self._own_topic_block(state, ctx)
        if tool_trace:
            prompt += ("\n\n# 本轮原生工具的实际记录\n只有成功的实际结果覆盖具体目标才可确认该事项完成。"
                       "检索到旧记录、获取资料、准备成功不等于外部操作已完成；失败或结果不明确就留空。\n"
                       + json.dumps(tool_trace[:20], ensure_ascii=False)[:8000])
        raw = await self._ask_judge(session_id, system, prompt)
        payload = extract_json_object(raw or "")
        if payload is None:
            return None
        # Reflection only writes metadata. Some providers omit the empty actions
        # array; normalize it here without weakening the action parser elsewhere.
        payload["actions"] = []
        parsed = parse_action_payload(
            json.dumps(payload, ensure_ascii=False),
            available_actions=set(),
            max_actions=0,
            max_messages=0,
            json_fields=self.extensions.json_fields(),
        )
        notes: list[str] = []
        if self.world is not world or self.state_key(session_id) != state_key or not self.is_enabled(session_id):
            return None
        async with self.session_state(session_id) as state:
            # The provider and the state lock can both outlive a persona/config change.
            current_persona = await self._persona_text(session_id)
            if (current_persona != persona_text or self.world is not world
                    or state.session_id != state_key or not self.is_enabled(session_id)):
                return None
            if parsed.reasoning:
                state.note_reasoning(parsed.reasoning, source="reflect", session_id=session_id)
            # 复盘里那句「心里冒出来的一两句」也存下来：这样每聊一轮，
            # 「内心活动」都会跟着动，而不是等她自己想起来写 think 动作。
            self._remember_inner_thought(
                state,
                str((parsed.reasoning or {}).get("inner") or ""),
                source="聊完一轮",
                dialogue_session=session_id,
                # 这一轮是回谁的、对方说了什么、她回了什么：内心活动那一页要看得见
                target=str(user_name or user_id or ""),
                quote=str(user_text or ""),
                reply=str(reply_text or ""),
                intent=str((parsed.reasoning or {}).get("intent") or ""),
            )
            applied_affinity = 0.0
            if ctx.user_id:
                applied_affinity = self.record_turn_interaction(session_id, ctx.user_id, sources,
                    parsed.affinity_delta, str(payload.get("interaction_reason") or
                    (parsed.reasoning or {}).get("intent") or "本轮真实互动")[:200])
                if abs(applied_affinity) > 1e-9:
                    notes.append(f"好感 {applied_affinity:+.2f}")
            if parsed.valence_delta:
                self._apply_valence_delta(state, parsed.valence_delta)
                notes.append(f"心情 {parsed.valence_delta:+.2f}")
            if parsed.chat_note:
                self.note_chat_note(state, parsed.chat_note, session_id)
            if parsed.open_topic:
                self.note_open_topic(state, ctx, parsed.open_topic)
            if parsed.heart_knot:
                self.note_heart_knot(
                    state, parsed.heart_knot, about=str(ctx.user_id or "")
                )
            if parsed.grudge:
                self.note_grudge(
                    state,
                    parsed.grudge,
                    user_id=str(getattr(ctx, "user_id", "") or ""),
                    user_name=str(getattr(ctx, "user_name", "") or ""),
                    session_id=str(getattr(ctx, "session_id", "") or ""),
                )
            if parsed.forgive:
                self.resolve_grudge(state, user_id=str(getattr(ctx, "user_id", "") or ""))
            if parsed.own_topic:
                self.note_own_topic(
                    state,
                    parsed.own_topic,
                    user_id=str(getattr(ctx, "user_id", "") or ""),
                    user_name=str(getattr(ctx, "user_name", "") or ""),
                    session_id=str(getattr(ctx, "session_id", "") or ""),
                )
            if parsed.own_topic_done:
                self.confirm_matter_from_turn(state, ctx, parsed.own_topic_done,
                                              parsed.own_topic_evidence, tool_trace or [])
            self.mark_open_topics_asked(state, ctx)
            tone = parsed.tone or keyword_signal(ctx.text)
            await self._apply_tone_pulse(
                state,
                tone,
                user_id=str(getattr(ctx, "user_id", "") or ""),
            )
            state.last_user_tone = str(parsed.tone or "")
            self._mark_reply_addressing(state, ctx, parsed.addressing)
            reflection = {
                "at": self._now(),
                "world_time": int(state.world_time),
                "user": str(user_name or user_id or ""),
                "user_text": _clip_text(user_text, 160),
                "reply": _clip_text(reply_text, 300),
                "provider_reasoning": _clip_text(reasoning_text, 600),
                "reasoning": dict(parsed.reasoning or {}),
                "chat_note": str(parsed.chat_note or ""),
                "tone": str(parsed.tone or ""),
                "addressing": str(parsed.addressing or ""),
                "valence_delta": round(float(parsed.valence_delta or 0.0), 3),
                "affinity_delta": round(float(applied_affinity or 0.0), 3),
                "open_topic": str(parsed.open_topic or ""),
                "heart_knot": str(parsed.heart_knot or ""),
                "grudge": str(parsed.grudge or ""),
                "own_topic": str(parsed.own_topic or ""),
                "notes": list(notes),
                "tools": [
                    dict(item)
                    for item in (tool_trace or [])
                    if isinstance(item, dict)
                ][:20],
            }
            state.last_reflection = reflection
            self.learn_vocabulary(session_id, payload.get("vocabulary") if isinstance(payload.get("vocabulary"), list) else [], sources)
            log = [
                dict(item)
                for item in (getattr(state, "reflection_log", []) or [])
                if isinstance(item, dict)
            ]
            log.append(reflection)
            state.reflection_log = log[-30:]
            await self._log_event(
                state,
                "reflection",
                reflection,
                persona_id=persona_id,
                place=session_id,
            )
        return reflection
