"""群聊短期窗口、插话与上下文管理。"""

from __future__ import annotations

from typing import Any
from ..decider import reply_willingness
from ..state import (
    WorldState,
    chat_core_text,
    chat_item_is_fresh,
    group_chat_items,
    take_last_chat_groups,
)
from .models import (
    MessageContext,
    TickOutcome,
)
from .util import (
    _clip_text,
    _same_text,
    _summarize_batch,
)


class GroupChatMixin:
    # ---------------- 群聊上下文 / 插话 ----------------

    def _watermark(self, state: WorldState, session_id: str = "") -> tuple[float, int]:
        """这个会话的"已回应水位线"。

        分过组时留档只有一份，但"回过话"是逐会话的：在群里答过，不该把
        私聊里还没答的留言也一起标成答过了。
        """

        target = str(session_id or state.session_id)
        if target == str(state.session_id):
            return (
                float(state.chat_replied_until or 0.0),
                int(state.chat_replied_seq or 0),
            )
        info = (getattr(state, "chat_watermarks", None) or {}).get(target) or {}
        return float(info.get("until") or 0.0), int(info.get("seq") or 0)

    def _set_watermark(
        self, state: WorldState, session_id: str, *, until: float, seq: int
    ) -> None:
        target = str(session_id or state.session_id)
        if target == str(state.session_id):
            state.chat_replied_until = float(until)
            state.chat_replied_seq = int(seq)
            return
        state.chat_watermarks = {
            **(getattr(state, "chat_watermarks", None) or {}),
            target: {"until": float(until), "seq": int(seq)},
        }

    def chat_context(
        self, state: WorldState, session_id: str = ""
    ) -> list[dict[str, Any]]:
        """进提示词的最近群聊（时间窗 + 条数双重限制）。

        注意：这里只裁剪「带进提示词」的那份视图，原始留档由 :meth:`note_presence`
        按 `context.chat_history_max` 维护，所以重启后还能恢复。
        """

        config = self.world.decider
        until, seq = self._watermark(state, session_id)
        return state.recent_chat_within(
            now=self._now(),
            seconds=max(60, int(config.chat_window_minutes) * 60),
            limit=max(1, int(self.world.context.chat_lines)),
            # 已经回应过的消息不再回放：她对那些话已经答过了，再带进去只会重复回应
            after=until,
            after_seq=seq,
            # 只算**这里**的留言：别处的话归别处，不然她会在群里答私聊的问题
            only_session=str(session_id or state.session_id),
        )

    def chat_window(
        self, state: WorldState, session_id: str = ""
    ) -> list[dict[str, Any]]:
        """时间窗内的全部群聊（含她已经回应过的那批）。

        提示词需要完整的一段：水位线以前的内容会被压成一条概览、之后的原样列出，
        但两边都不该从上下文里消失（否则她下一轮就像失忆）。

        时间窗只管「还没回应过」的那批；**她已经回过话的部分不受时间限制**——
        聊过一个小时之后，"刚才在说什么"不该只剩一行概览（那会导致接不上话）。

        「最多带几行」是**按会话各自算**的：这里那一份取 `chat_lines` 行，
        别处同时听到的取 `chat_elsewhere_lines` 行。以前是在所有会话的留档上一起取
        "最近 N 行"，于是私聊聊得多的时候，群里刚说的话会被挤出去。

        ``session_id`` 就是"这一轮要说给谁听 / 她的这句话落在哪儿"：
        **凡是有明确目标的定向入口（回复、续说、定向自主发言、测评、自主提示词预览）
        都必须传目标会话**，让 `current_session` 和这里取的是同一份——
        不传就等于按代表会话（``state.session_id``）取，目标会话的话可能只落进
        "别处听到的"那一块，她也会觉得"我明明在那边说话，上下文却是这边的"。
        没有明确目标的**全组视图**入口（例如 :meth:`_ask_llm_for_plan` 那种
        "她自己决定下一步做什么"的计划轮）才故意不传：那一轮她还没决定在哪儿说，
        所以按代表会话取一份、别处的当背景，正是想要的语义。
        """

        config = self.world.decider
        context = self.world.context
        target = str(session_id or state.session_id or "")
        rows = state.chat_window(
            now=self._now(),
            seconds=max(60, int(config.chat_window_minutes) * 60),
        )
        here: list[dict[str, Any]] = []
        elsewhere: list[dict[str, Any]] = []
        for item in rows:
            if self._chat_origin(item, state) == target:
                here.append(item)
            else:
                elsewhere.append(item)
        picked = [
            *take_last_chat_groups(here, max(1, int(context.chat_lines))),
            *take_last_chat_groups(elsewhere, max(0, int(context.chat_elsewhere_lines))),
        ]
        picked.sort(key=lambda item: int(item.get("seq") or 0))
        return self._merge_answered_chat(state, target, picked)

    @staticmethod
    def _chat_origin(item: dict[str, Any], state: WorldState) -> str:
        """一条留档算在哪个会话头上（老条目没有 origin：算作她的存档会话）。"""

        return str(item.get("origin") or state.session_id or "")

    def _merge_answered_chat(
        self, state: WorldState, target: str, current: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """把最近这批"已经回过话"的原样补进来（不限时间），按 seq 去重后按时间排好。"""

        until, seq = self._watermark(state, target)
        limit = max(0, int(self.world.context.chat_answered_lines))
        extra: list[dict[str, Any]] = []
        for item in reversed(list(state.recent_chat or [])):
            if str(item.get("origin") or state.session_id) != target:
                continue
            if chat_item_is_fresh(item, replied_until=until, replied_seq=seq):
                continue
            extra.append(item)
        # 按"合并后的行"限额（同一个人连着说的算一行）
        extra = take_last_chat_groups(list(reversed(extra)), limit)
        if not extra:
            return current
        seen = {int(item.get("seq") or 0) for item in current}
        # extra 已经是"从早到晚"（上面 reverse 回来过），直接按顺序补在前面
        older = [item for item in extra if int(item.get("seq") or 0) not in seen]
        return [*older, *current]

    def chat_context_for_reply(
        self,
        state: WorldState,
        ctx: MessageContext,
        *,
        exclude_entries: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """接管回复用的群聊背景：把**本轮 batch 里的事件**整个从背景里摘掉。

        本轮那几条紧接着会以聊天记录的格式单独交给模型（见 ``build_reply_user_prompt``），
        留在背景里等于同一个事件在提示词里出现两遍——模型会以为对方把话重复说了好几次。

        排除的判定依据是**事件身份**：留档条目身上带着 ``event_key``（登记时按
        ``origin + message_id`` / ``origin + 内部 id`` 算出来的），键相同就是同一个事件。

        ``exclude_entries``（本轮 batch 的结构化条目）里没有身份的条目才走**有限兜底**：
        要求「来源相同 + 用户相同 + 正文核心一致」，边界限于本轮这一批。
        它绝不会按正文删掉用户真实的重复发言——那些条目有自己的身份键，不在排除集合里。
        """

        context = self.chat_window(state, ctx.session_id)
        if not context:
            return context
        entries = [item for item in (exclude_entries or []) if isinstance(item, dict)]
        if not entries:
            # 老路径（调用方没给 batch）：只尝试摘掉最后一条对得上的
            last = context[-1]
            if last.get("is_self"):
                return context
            same_user = str(last.get("user_id") or "") == str(ctx.user_id or "unknown")
            if same_user and _same_text(
                str(last.get("text") or ""), str(ctx.text or "")
            ):
                return context[:-1]
            return context
        keys = {
            str(item.get("event_key") or "")
            for item in entries
            if str(item.get("event_key") or "")
        }
        # 兜底表：只有"没有身份"的条目才登记，键是 (来源, 用户, 正文核心)
        loose: set[tuple[str, str, str]] = set()
        for entry in entries:
            if str(entry.get("event_key") or ""):
                continue
            core = " ".join(chat_core_text(str(entry.get("text") or "")).split())
            if not core:
                continue
            loose.add(
                (
                    str(entry.get("origin") or ""),
                    str(entry.get("user_id") or ""),
                    core,
                )
            )
        kept: list[dict[str, Any]] = []
        for item in context:
            item_key = self.event_key_of(item)
            if item_key and item_key in keys:
                continue
            if not item_key and loose:
                origin = str(item.get("origin") or state.session_id or "")
                who = str(item.get("user_id") or "")
                core = " ".join(chat_core_text(str(item.get("text") or "")).split())
                if core and (origin, who, core) in loose:
                    continue
            kept.append(item)
        return kept

    async def quote_in_records(self, session_id: str, text: str) -> bool:
        """被引用的那条，聊天记录里还看得到吗。

        看得到就只给她一个开头（让她自己往上对照：省 token，也更像人）；
        看不到（很久以前的、已经掉出留档的）才需要把原文写进提示词。
        """

        probe = " ".join(str(text or "").split())[:24]
        if not probe or not self.is_enabled(session_id):
            return False
        try:
            state = await self.load_state(session_id, cold_start=False)
        except Exception:
            return False
        # 只看"这一轮真的会带进提示词"的那一份（时间窗 + 已回过的那截），
        # 原始留档里还留着、但早就没带进去的那几条，说了"照上面看"她也找不到
        for item in self.chat_window(state, session_id):
            body = " ".join(str(item.get("text") or "").split())
            if probe in body:
                return True
        return False

    async def chat_images_for_reply(
        self, session_id: str, limit: int
    ) -> list[dict[str, Any]]:
        """聊天记录里最近几张图（可以直接交给多模态主模型看的那几张）。

        返回按时间排好、带 ``label``（图1 / 图2…）——编号要和这一轮真的附上去的
        那几张一一对应，提示词里的「（见图N）」才有意义。
        """

        if int(limit or 0) <= 0 or not self.is_enabled(session_id):
            return []
        async with self.session_state(session_id) as state:
            return self.pick_chat_images(state, session_id, int(limit))

    def pick_chat_images(
        self, state: WorldState, session_id: str, limit: int
    ) -> list[dict[str, Any]]:
        """（调用方负责持锁）取这个会话聊天记录里最近的几张图。"""

        limit = max(0, int(limit or 0))
        if limit <= 0:
            return []
        target = str(session_id or state.session_id)
        items = [
            item
            for item in self.chat_window(state, target)
            if str(item.get("origin") or state.session_id) == target
            and not item.get("is_self")
        ]
        picks: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in reversed(items):
            for ref in reversed(list(item.get("images") or [])):
                key = str(ref or "")
                if not key or key in seen:
                    continue
                seen.add(key)
                picks.append({"url": key, "at": float(item.get("at") or 0.0)})
                if len(picks) >= limit:
                    break
            if len(picks) >= limit:
                break
        picks.reverse()
        for index, pick in enumerate(picks, start=1):
            pick["label"] = f"图{index}"
        return picks

    def mark_chat_replied(
        self,
        state: WorldState,
        session_id: str = "",
        *,
        keys: set[str] | None = None,
    ) -> None:
        """她真的开口了：把这一批群聊压成概览，并把"已回应水位线"往前推。

        水位线之后的消息才是"还没回应过的"，会原样进提示词；水位线以内（也就是她刚
        回应过的这批）压成一条概览，供下一轮了解背景，不再重复回应。

        ``keys``：这一轮**实际处理过的事件身份**。给了它就只推到这些事件的边界——
        她说话这会儿新到的消息不该被一起标成"已回应"（下一轮就再也看不见了）。
        没给（老调用方）就沿用"推到本会话此刻最新那条"的老行为。
        """

        now = self._now()
        target = str(session_id or state.session_id)
        previous, previous_seq = self._watermark(state, target)
        if keys is not None:
            # 只算"这一轮真的回过"的那几条：本轮之后才到的消息留在水位线外。
            # **预览必须和它同源**：下面这几条是这一轮真的处理过的事件集合，
            # 发送期间新到的消息不算，否则下一轮的「你刚回应过的那批」里会冒出
            # 她根本没看过的新话——她会当成"我刚才回过这个"。
            answered = [
                item
                for item in state.recent_chat
                if str(item.get("origin") or state.session_id) == target
                and str(item.get("event_key") or "") in keys
            ]
            preview = _summarize_batch(answered)
            if preview:
                # 按会话存：群里回的那批不该跑到私聊里当"刚聊过的"
                state.chat_previews = {
                    **dict(state.chat_previews or {}),
                    target: {"text": preview, "at": now},
                }
            if not answered:
                return
            bound_seq = max([int(item.get("seq") or 0) for item in answered] or [0])
            bound_at = max([float(item.get("at") or 0.0) for item in answered] or [0.0])
            self._set_watermark(
                state,
                target,
                until=max(previous, bound_at),
                seq=max(previous_seq, bound_seq),
            )
            return
        # 老调用方（没给 keys）：沿用"推到本会话此刻最新那条"的老行为，
        # 预览也还是这个会话里全部没回应过的消息——它没法知道哪几条真的处理了。
        batch = [
            item
            for item in state.recent_chat
            if (
                str(item.get("origin") or state.session_id) == target
                and chat_item_is_fresh(
                    item,
                    replied_until=previous,
                    replied_seq=previous_seq,
                )
            )
        ]
        preview = _summarize_batch(batch)
        if preview:
            state.chat_previews = {
                **dict(state.chat_previews or {}),
                target: {"text": preview, "at": now},
            }
        _, seq_now = self._watermark(state, target)
        # 水位线推到"这个会话里最新那条"：跨会话的水位线不能互相顶掉
        session_seq = max(
            [
                int(item.get("seq") or 0)
                for item in state.recent_chat
                if str(item.get("origin") or state.session_id) == target
            ]
            or [int(state.chat_seq or 0)]
        )
        self._set_watermark(
            state, target, until=now, seq=max(session_seq, seq_now)
        )

    def _remember_replied_batch(self, batch: list[dict[str, Any]]) -> None:
        """记下这一轮**实际处理过**的事件身份（按会话分组），等真发出去了再提交。

        **覆盖而不是累积**：这一轮被作废（打断 / 没发出去）时，上一轮留下的旧身份
        不该在下一轮被顺手标成"已回应"——那正好会吞掉没处理过的消息。
        """

        grouped: dict[str, set[str]] = {}
        for record in batch:
            key = self.event_key_of(record)
            target = str(record.get("origin") or record.get("session") or "")
            if key and target:
                grouped.setdefault(target, set()).add(key)
        for target, keys in grouped.items():
            self._replied_batch_keys[target] = keys
        # 只留最近若干个会话：这是个短命的"待提交"表，不是缓存
        if len(self._replied_batch_keys) > 32:
            for stale in list(self._replied_batch_keys)[:-32]:
                self._replied_batch_keys.pop(stale, None)

    async def mark_reply_batch_replied(
        self, session_id: str, *, batch: list[dict[str, Any]] | None = None
    ) -> None:
        """回复**真的发出去**之后，把水位线推到这一轮的边界（见 `_remember_replied_batch`）。

        没有这一轮的批次记录（例如只是"说给别处"的旁听投递）就**不推进**：
        宁可不标已回应，也不要把没处理过的消息吞掉。
        """

        if not self.is_enabled(session_id):
            return
        if batch is None:
            keys = self._replied_batch_keys.pop(str(session_id), None)
        else:
            # 原生 Agent 的多个回合可以同时在飞。提交发完的那个回合自己的事件，
            # 不能取会话里被下一轮覆盖的待提交表，否则会吞掉尚未发送的消息。
            target = str(session_id)
            keys = {self.event_key_of(item) for item in batch if isinstance(item, dict)
                    and str(item.get("origin") or item.get("session") or "") == target}
            keys.discard("")
            pending = self._replied_batch_keys.get(target)
            if pending is not None:
                pending.difference_update(keys)
                if not pending:
                    self._replied_batch_keys.pop(target, None)
        if not keys:
            return
        async with self.session_state(session_id) as state:
            self.mark_chat_replied(state, session_id, keys=keys)

    async def mark_chat_replied_by_session(self, session_id: str) -> None:
        """按会话推进"已回应水位线"（不知道具体批次时用；推到本会话此刻最新那条）。"""

        if not self.is_enabled(session_id):
            return
        async with self.session_state(session_id) as state:
            self.mark_chat_replied(state, session_id)

    def note_chat_note(
        self, state: WorldState, text: str, session_id: str = ""
    ) -> None:
        """记下「刚才在聊什么」（模型顺手写的），下一轮当背景用。

        **按会话存**：一个会话组里群和私聊共用一份状态，写在一起的后果是
        私聊刚聊的背景跑到群里当"这里刚才在聊的事"。
        """

        note = " ".join(str(text or "").split())
        if not note:
            return
        target = str(session_id or state.session_id)
        state.chat_notes = {
            **dict(state.chat_notes or {}),
            target: {"text": _clip_text(note, 80), "at": self._now()},
        }

    def chat_note_for(self, state: WorldState, session_id: str = "") -> str:
        """这个会话「刚才在聊什么」（老存档回落全局那份；过期的不算）。"""

        limit = max(0, int(getattr(self.world.context, "chat_note_max_minutes", 30) or 0))
        return state.chat_note_text(session_id, max_minutes=limit, now=self._now())

    def chat_preview_for(self, state: WorldState, session_id: str = "") -> str:
        """这个会话「她刚回应过的那批」的概览（留档被裁掉时的兜底背景）。"""

        return state.chat_preview_text(session_id)

    def _chat_counts(self, state: WorldState, session_id: str = "") -> dict[str, int]:
        """编辑器「群聊上下文」那几个数：**只算这个会话**，而且按"合并后的行"。

        以前"已回应"是用留档总数减出来的——那个差值里混着别处会话的消息和时间窗之外
        的消息，数字看着大，其实跟她看到的对不上。
        """

        target = str(session_id or state.session_id or "")
        view = self.chat_window(state, target)
        here = [item for item in view if self._chat_origin(item, state) == target]
        until, seq = self._watermark(state, target)
        fresh: list[dict[str, Any]] = []
        answered: list[dict[str, Any]] = []
        for item in here:
            fresh_flag = chat_item_is_fresh(
                item, replied_until=until, replied_seq=seq
            )
            (fresh if fresh_flag else answered).append(item)
        return {
            "chat_history_count": sum(
                1 for item in state.recent_chat if self._chat_origin(item, state) == target
            ),
            "chat_unreplied_count": len(group_chat_items(fresh)),
            "chat_replied_count": len(group_chat_items(answered)),
        }

    def group_is_chatting(self, state: WorldState) -> bool:
        """群里最近是否真的有人在聊。

        只数**别人**还没被她回过的消息：她自己刚说的话不是"有人在聊"的证据，
        否则她会把自己的上一句当成没人接的话题，一路追着往下说。
        """

        need = max(1, int(self.world.decider.min_messages_to_interject))
        others = [
            item for item in self.chat_context(state) if not item.get("is_self")
        ]
        return len(others) >= need

    def group_is_alive(self, state: WorldState, *, minutes: int = 0) -> bool:
        """群里最近有没有人在说话（她自己的话不算）。

        和 :meth:`group_is_chatting` 的区别：那个问的是"有没有新消息等着她回"，
        这个问的是"群里现在有人吗"。开口求助看的是后者——群里空着的时候喊一声
        「有人吗」，群友视角就是一句没头没尾的怪话。
        """

        window = max(1, int(minutes or self.world.decider.chat_window_minutes or 20)) * 60
        for item in state.chat_window(now=self._now(), seconds=window):
            if item.get("is_self"):
                continue
            if str(item.get("text") or "").strip():
                return True
        return False

    @staticmethod
    def plan_speaks(plan: dict[str, Any] | None) -> bool:
        """这份计划里有没有"她主动开口"的动作。"""

        for step in (plan or {}).get("steps") or []:
            if not isinstance(step, dict):
                continue
            if step.get("interject"):
                return True
            if str(step.get("action") or "") in ("say", "share"):
                return True
        return False

    def willingness(self, state: WorldState) -> float:
        """她对「现在开口」的整体意愿（0~1）：给决策器与外部联动共用。"""

        return reply_willingness(state)

    # ---------------- 群聊上下文：有限消息窗口 ----------------

    def chat_history_limit(self) -> int:
        keep = max(20, int(self.world.context.chat_history_max))
        # Reuse the bounded short-term archive; ordinary prompt views keep their own limits.
        if self.world.chat_browse.enabled:
            keep = max(keep, self.world.chat_browse.max_messages)
        return keep

    async def clear_chat_context(self, session_id: str) -> dict[str, Any]:
        """清空本会话的近期消息与话题备忘，不改生活记忆和人物资料。

        会话组里别的群 / 私聊的窗口与备忘不受影响。

        清的是本会话的短期窗口（``recent_chat``）、
        「刚才在聊什么」（``chat_notes[target]``）和「她刚回应过的那批」
        （``chat_previews[target]``）。后两个也是**分会话上下文**：留着的话
        下一轮提示词里还会写着"上一轮你们在聊：…"，看起来就像清空没生效。

        ``recent_replies`` **故意不清**：它是"别重复自己的句式"的**风格记忆**，
        不是会话上下文——清掉之后她会把刚说过的口头禅、称呼原样再说一遍。
        """

        async with self.session_state(session_id) as state:
            target = str(session_id or state.session_id)
            def _from_here(item: dict[str, Any]) -> bool:
                return str(item.get("origin") or state.session_id) == target

            kept = [item for item in state.recent_chat if not _from_here(item)]
            removed = len(state.recent_chat) - len(kept)
            state.recent_chat = kept
            from ..chat_browse import dialogue_origin
            state.thoughts = [item for item in state.thoughts
                              if item.get("browse_session") != target
                              and dialogue_origin(item) != target]
            reasoning = state.last_reasoning or {}
            if reasoning.get("_session") == target:
                state.last_reasoning = {}
            if (state.current_action or {}).get("browse_session") == target:
                self._stop_chat_browse(state, state.current_action)
            # 会话上下文的小尾巴：不留的话"清空上下文"之后她还记得刚才在聊什么
            state.chat_notes = {
                key: value
                for key, value in dict(state.chat_notes or {}).items()
                if str(key) != target
            }
            state.chat_previews = {
                key: value
                for key, value in dict(state.chat_previews or {}).items()
                if str(key) != target
            }
            if target == str(state.session_id):
                # 老字段（全局备忘 / 全局预览）就是这个会话的，一起清掉
                state.chat_note = ""
                state.chat_preview = ""
            await self._log_event(
                state,
                "context",
                {
                    "note": "手动清空群聊上下文",
                    "removed": removed,
                    "session": self.session_label(target, state),
                },
            )
        return {"removed": removed}

    def interject_allowed(self, state: WorldState) -> bool:
        """是否允许主动插话：插话冷却 + 无人回应冷却 + 每小时上限。"""

        return self.interject_gate(state) == ""

    def interject_gate(self, state: WorldState) -> str:
        """插话被哪道闸拦住（空字符串 = 放行）。

        分工：`cooldown` 是两次插话的间隔，`engage` 是无人回应保护，
        `hourly` 是每小时自主额度，`reply_cd` 是"刚回过话就别主动开口"。
        把它们分开报出来，才能知道到底是谁在限流。
        """

        if not self.proactive_chat_allowed(state.session_id):
            return "paused"
        if not self.world.decider.enabled:
            return "disabled"
        cooldown = max(0, int(self.world.decider.interject_cooldown_minutes)) * 60
        if self._now() - float(state.last_interject_at or 0.0) < cooldown:
            return "cooldown"
        if not self.engagement.can_speak(state):
            return "engage"
        if not self._within_hourly_limit(state):
            return "hourly"
        if self.engagement.proactive_blocked(state):
            return "reply_cd"
        return ""

    def _count_interject_gate(self, state: WorldState, gate: str) -> None:
        """记一笔「她这轮想插话，结果如何」——按小时归零。"""

        hour = int(self._now() // 3600)
        if state.interject_hour_marker != hour:
            state.interject_hour_marker = hour
            state.interject_stats = {}
        key = gate or "allowed"
        stats = dict(state.interject_stats or {})
        stats[key] = int(stats.get(key, 0)) + 1
        state.interject_stats = stats

    def _refresh_interject_closed(self, state: WorldState) -> None:
        """长期低落时关掉「想被注意到」这条插话动机，并且至少关 15 分钟。

        动机 B（想发作）不受影响：被惹毛的人反而是想开口的。
        """

        now = self._now()
        if float(state.valence) >= 0.2:
            return
        started = float(state.low_valence_since or 0.0)
        if not started or now - started < 30 * 60:
            return
        state.interject_closed_until = max(
            float(state.interject_closed_until or 0.0), now + 15 * 60
        )

    def _forced_plan_allowed(self, state: WorldState) -> bool:
        """极端保护也要守规矩：间隔、每小时上限、无人回应冷却一个都不能少。"""

        limits = self.world.limits
        if self._now() - float(state.last_forced_plan_at or 0.0) < max(
            0, int(limits.forced_plan_min_interval_seconds)
        ):
            return False
        if not self._within_hourly_limit(state):
            return False
        if not self.engagement.can_speak(state):
            return False
        return True
