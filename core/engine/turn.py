"""注入模式：回合收尾与复盘。"""

from __future__ import annotations

from typing import Any
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
        """宿主替她说完之后，把她这句话写回世界：聊天留档 + 待总结 + 反重复。

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
            self.note_dialogue(state, text=clean, is_self=True, persona_id=persona_id)
            self._tag_chat_origin(state, session_id, recorded)
            self._count_passive_reply(state)

    @session_scoped
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
    ) -> dict[str, Any] | None:
        """回合复盘：她说完之后，把这一轮的心情 / 好感 / 话题 / 心事补记上。

        主回复已经由宿主说完，这次调用**只写账**：不改她说过的话、不碰工具、
        失败也只影响这一轮记账。成功后整条复盘会留在 ``state.last_reflection``，
        调试页与日志页可以直接看她的"状态和心思"。
        """

        if not self.is_enabled(session_id):
            return None
        ctx = MessageContext(
            session_id=session_id,
            user_id=str(user_id or ""),
            user_name=str(user_name or ""),
            text=str(user_text or ""),
            raw_text=str(user_text or ""),
            persona_id=str(persona_id or ""),
        )
        async with self.session_state(session_id) as state:
            node = self.node(state.node_id) or self.node(self.default_node_id())
            memories = self.memory.recall(
                session_id=session_id,
                persona_id=persona_id,
                node_id=state.node_id,
                focus_user=user_id,
                limit=4,
            )
            recent_chat = [
                dict(item)
                for item in (state.recent_chat or [])
                if isinstance(item, dict)
            ][-8:]
            profile_text = self.profile_block(state, ctx)
            system, prompt = self.prompts.build_reflection_prompt(
                state=state,
                node=node,
                user_name=user_name,
                user_text=user_text,
                reply_text=reply_text,
                recent_chat=recent_chat,
                profile_text=profile_text,
                memories=memories,
                other_context="",
                pronoun=pronounce_for(getattr(self.world, "gender", "female")),
            )
        raw = await self._ask_judge(session_id, system, prompt)
        if not raw or extract_json_object(raw) is None:
            return None
        parsed = parse_action_payload(
            raw,
            available_actions=set(),
            max_actions=0,
            max_messages=0,
            json_fields=self.extensions.json_fields(),
        )
        notes: list[str] = []
        async with self.session_state(session_id) as state:
            if parsed.reasoning:
                state.note_reasoning(parsed.reasoning, source="reflect")
            # 复盘里那句「心里冒出来的一两句」也存下来：这样每聊一轮，
            # 「内心活动」都会跟着动，而不是等她自己想起来写 think 动作。
            self._remember_inner_thought(
                state,
                str((parsed.reasoning or {}).get("inner") or ""),
                source="聊完一轮",
            )
            applied_affinity = 0.0
            if ctx.user_id:
                applied_affinity = self.profiles.apply_reply_affinity(
                    session_id,
                    ctx.user_id,
                    parsed.affinity_delta,
                    reason=str(
                        (parsed.reasoning or {}).get("intent") or "聊了这一轮"
                    )[:60],
                    now=self._now(),
                )
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
                self.mark_own_topic_done(
                    state, user_id=str(getattr(ctx, "user_id", "") or "")
                )
            self.mark_open_topics_asked(state, ctx)
            tone = parsed.tone or keyword_signal(ctx.text)
            await self._apply_tone_pulse(
                state,
                tone,
                user_id=str(getattr(ctx, "user_id", "") or ""),
            )
            state.last_user_tone = str(parsed.tone or "")
            self._mark_reply_addressing(state, ctx, parsed.addressing)
            if parsed.memory:
                self.note_memory_hint(state, parsed.memory)
            reflection = {
                "at": self._now(),
                "world_time": int(state.world_time),
                "user": str(user_name or user_id or ""),
                "user_text": _clip_text(user_text, 160),
                "reply": _clip_text(reply_text, 300),
                "provider_reasoning": _clip_text(reasoning_text, 600),
                "reasoning": dict(parsed.reasoning or {}),
                "memory": str(parsed.memory or ""),
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
