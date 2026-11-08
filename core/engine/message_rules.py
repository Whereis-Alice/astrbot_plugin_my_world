"""Current-message provenance, command detection and content rules."""
from __future__ import annotations

from ..evidence import direct_text, source_snapshot


class MessageRulesMixin:
    def mark_context_sources(self, state, ctx, batch) -> None:
        """Keep provenance for profile extraction without saving chat memories."""
        selected = [*self.chat_context_for_reply(state, ctx, exclude_entries=self.reply_batch_entries(batch)), *batch]
        local = [row for row in self.chat_window(state, ctx.session_id)
                 if isinstance(row, dict) and self._chat_origin(row, state) == ctx.session_id]
        for row in selected:
            if not isinstance(row, dict):
                continue
            if str(row.get("origin") or row.get("session") or ctx.session_id) != ctx.session_id:
                continue
            # Pending batches use a monotonic clock and omit the archive's source
            # ID. Mark the original message, preserving its author/body/wall time.
            source_id = str(row.get("source_id") or "")
            event_key = self.event_key_of(row)
            original = next((item for item in reversed(local) if
                             (source_id and item.get("source_id") == source_id)
                             or (event_key and self.event_key_of(item) == event_key)), None)
            if original is None and not source_id and not event_key:
                body = direct_text(str(row.get("raw_text") or row.get("text") or ""))
                original = next((item for item in reversed(local) if body
                                 and str(item.get("user_id") or "") == str(row.get("user_id") or "")
                                 and bool(item.get("is_self")) == bool(row.get("is_self"))
                                 and direct_text(str(item.get("text") or "")) == body), None)
            if original is None:
                continue
            original["eligible"] = True
            source = source_snapshot(original, ctx.session_id)
            row["source_id"] = source["id"]
            row["eligible"] = True
            self.db.raw.observe_source(source, eligible=True)

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
        if not parts or parts[0] not in ("mw", "小世界", "我的世界", "myworld"):
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
            and parts[0] in ("mw", "小世界", "我的世界", "myworld")
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
