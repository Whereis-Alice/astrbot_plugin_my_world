"""对话记忆：攒片段再总结。"""

from __future__ import annotations

from typing import Any
from ..config_scope import session_scoped
from ..memory import INTERACTION
from ..state import WorldState
from .constants import MEMORY_REFUSAL_HINTS
from .models import TickOutcome
from .util import _clip_text


class DialogueMemoryMixin:
    # ---------------- 对话记忆：攒片段，再总结 ----------------

    def note_dialogue(
        self,
        state: WorldState,
        *,
        user_id: str = "",
        name: str = "",
        text: str = "",
        is_self: bool = False,
        persona_id: str = "",
    ) -> None:
        """把一条「她参与的对话」放进待总结缓冲（不立刻写记忆）。"""

        if not self.world.memory.dialogue_summary:
            return
        clean = " ".join(str(text or "").split())
        if not clean:
            return
        if not state.pending_memory:
            state.pending_memory_node = state.node_id
        state.pending_memory.append(
            {
                "user_id": "" if is_self else str(user_id or ""),
                "name": "我" if is_self else str(name or user_id or "有人"),
                "text": _clip_text(clean, 160),
                "is_self": bool(is_self),
                "persona_id": str(persona_id or ""),
                "at": self._now(),
                "world_time": int(state.world_time),
            }
        )
        limit = 60
        if len(state.pending_memory) > limit:
            state.pending_memory = state.pending_memory[-limit:]

    def note_memory_hint(self, state: WorldState, hint: str) -> None:
        """大模型顺手写的一句话总结，作为写记忆时的提示（不会单独成为一条记忆）。"""

        text = " ".join(str(hint or "").split())
        if not text:
            return
        state.memory_hints.append(_clip_text(text, 80))
        if len(state.memory_hints) > 5:
            state.memory_hints = state.memory_hints[-5:]

    def _memory_flush_reason(self, state: WorldState) -> str:
        """这段对话该总结了吗？返回触发原因，空字符串表示再攒攒。"""

        config = self.world.memory
        if not config.dialogue_summary or not state.pending_memory:
            return ""
        if state.memory_flush_wanted:
            return "她换地方了"
        if len(state.pending_memory) >= max(1, int(config.summary_trigger_messages)):
            return "这段对话够长了"
        idle_minutes = max(0, int(config.summary_idle_minutes))
        if idle_minutes:
            last_at = float(state.pending_memory[-1].get("at") or 0.0)
            if last_at and (self._now() - last_at) >= idle_minutes * 60:
                return "这段聊完了"
        return ""

    @staticmethod
    def _fallback_memory_text(entries: list[dict[str, Any]]) -> str:
        """模型不可用时的兜底：把这段对话压成一行，而不是什么都不记。"""

        recent = entries[:6]
        if recent and all(item.get("is_self") for item in recent):
            # 只有她自己：直接留下她自己说的话，别套「我说」那层壳
            own = [
                _clip_text(str(item.get("text") or ""), 50)
                for item in recent
                if str(item.get("text") or "").strip()
            ]
            return _clip_text("；".join(part for part in own if part), 120)
        parts: list[str] = []
        for item in recent:
            text = _clip_text(str(item.get("text") or ""), 40)
            if not text:
                continue
            who = "我" if item.get("is_self") else str(item.get("name") or "有人")
            parts.append(f"{who}说「{text}」")
        if not parts:
            return ""
        return _clip_text("；".join(parts), 120)

    @session_scoped
    async def flush_pending_memory(self, session_id: str) -> TickOutcome | None:
        """把攒着的对话片段总结成一条记忆（攒够 / 聊完 / 换地点三种触发）。"""

        config = self.world.memory
        if not config.dialogue_summary or not self.is_enabled(session_id):
            return None
        async with self.session_state(session_id) as state:
            reason = self._memory_flush_reason(state)
            if not reason:
                return None
            entries = [dict(item) for item in state.pending_memory]
            hints = list(state.memory_hints)
            node_id = state.pending_memory_node or state.node_id
            persona_id = next(
                (
                    str(item.get("persona_id") or "")
                    for item in reversed(entries)
                    if item.get("persona_id")
                ),
                "",
            )
            mood = state.mood
            affect = float(state.affect)
            valence = float(state.valence)
            state.pending_memory = []
            state.memory_hints = []
            state.pending_memory_node = ""
            state.memory_flush_wanted = False
            state.memory_flush_at = self._now()

        outcome = TickOutcome(session_id=session_id)
        outcome.notes.append(f"总结这段对话（{reason}）")
        summary = ""
        if self.llm is not None:
            where = self.node(node_id)
            system_prompt, prompt = self.prompts.build_memory_summary_prompt(
                node_name=(where.name if where else "") or "",
                lines=[
                    f"{item.get('name') or '有人'}: {item.get('text')}" for item in entries
                ],
                hints=hints,
                max_chars=max(10, int(config.summary_max_chars)),
            )
            reply = await self._ask_llm(session_id, system_prompt, prompt)
            outcome.llm_calls += 1
            summary = self._clean_memory_summary(reply)
        generated = bool(summary)
        if not summary:
            summary = self._fallback_memory_text(entries)
        if not summary:
            return outcome

        summary = _clip_text(summary, max(10, int(config.summary_max_chars)))
        users = [
            str(item.get("user_id"))
            for item in entries
            if item.get("user_id")
        ]
        users = list(dict.fromkeys(users))
        async with self.session_state(session_id) as state:
            echo_marker = await self._event_marker(state)
            self.memory.remember(
                session_id=session_id,
                persona_id=persona_id,
                node_id=node_id,
                content=summary,
                memory_type=INTERACTION,
                related_users=users,
                emotion=mood,
                weight=0.5,
                source="llm" if generated else "runtime",
                affect=affect,
                valence=valence,
            )
            await self._log_event(
                state,
                "memory",
                {"content": summary, "reason": reason, "where": node_id},
            )
            await self._echo_events_since(state, outcome, echo_marker)
        return outcome

    @staticmethod
    def _clean_memory_summary(reply: str | None) -> str:
        """模型有时候会带引号、前缀或者多写几行，这里只取第一句有用的。"""

        text = " ".join(str(reply or "").split())
        if not text:
            return ""
        if any(hint in text for hint in MEMORY_REFUSAL_HINTS):
            # 她一个人做事的时候，模型偶尔会反过来"要人名"。
            # 这种话不是记忆，丢掉，让调用方用兜底文案重写一条。
            return ""
        for quote in ("「", "『", '"', "'"):
            if text.startswith(quote) and text.endswith(quote) and len(text) > 2:
                text = text[1:-1]
        text = text.strip("。；;，,")
        return _clip_text(text, 80)

    def _passes_safety(self, text: str) -> bool:
        blocked = self.world.content_safety.blocked_words or []
        if not blocked or not text:
            return True
        return not any(word and word in text for word in blocked)

    @staticmethod
    def command_words(text: str) -> list[str]:
        """把一条指令拆成词：``/mw event h`` 和斜杠被吃掉的 ``mw event h`` 都认。"""

        parts = [item for item in str(text or "").strip().split() if item]
        if not parts:
            return []
        parts[0] = parts[0].lstrip("/／!！").lower()
        return parts

    @classmethod
    def is_plugin_command(cls, text: str) -> bool:
        """是不是「本插件的指令」（``/mw …``）。

        这种消息是管理入口，不是有人在跟她讲话：不能进她的聊天记录，
        否则她会把「/mw event 逛街被跟了」当成"主人讲给我听的一件事"。
        """

        raw = str(text or "").strip()
        body = str(text or "").strip().lower()
        for prefix in ("/mw", "！mw", "!mw", "／mw"):
            if body.startswith(prefix):
                return True
        # AstrBot 在派发指令时会把**开头的斜杠**吃掉：她那边看到的其实是
        # 「mw event h」。光认带斜杠那几种写法挡不住，这里再按「第一个词 + 子命令」认一次，
        # 不然指令会被记成"他刚说的话"，她会一本正经地回一句"你怎么又敲这指令"。
        parts = cls.command_words(raw)
        if not parts or parts[0] not in ("mw", "我的世界", "myworld"):
            return False
        if len(parts) == 1:
            return True
        return parts[1] in cls.PLUGIN_SUBCOMMANDS

    @classmethod
    def is_event_command(cls, text: str) -> bool:
        """``/mw event …``（斜杠被吃掉也算）：这一条是"开一件事"，不是"跟她说话"。"""

        parts = cls.command_words(text)
        return bool(
            len(parts) >= 2
            and parts[0] in ("mw", "我的世界", "myworld")
            and parts[1] in ("event", "事件")
        )

    PLUGIN_SUBCOMMANDS = frozenset(
        {
            "state", "状态", "plan", "计划", "stop", "停", "停下", "打断",
            "memories", "记忆", "memory", "session", "会话", "tools", "工具",
            "prompt", "提示词", "autoprompt", "自主提示词", "tick", "推进",
            "decide", "决策", "schedule", "日程", "map", "地图",
            "nickname", "名片", "debug", "调试", "reload", "重载",
            "reset", "重置", "restore-default", "恢复默认", "event", "事件",
            "ability", "能力", "能力值", "thread", "线索", "未了",
            "help", "帮助", "?",
        }
    )
    """``/mw <这些>`` 认得出来的子命令：斜杠被吃掉之后靠它兜底。"""
