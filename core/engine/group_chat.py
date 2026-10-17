"""群聊上下文、插话与留档压缩。"""

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

    async def mark_reply_batch_replied(self, session_id: str) -> None:
        """回复**真的发出去**之后，把水位线推到这一轮的边界（见 `_remember_replied_batch`）。

        没有这一轮的批次记录（例如只是"说给别处"的旁听投递）就**不推进**：
        宁可不标已回应，也不要把没处理过的消息吞掉。
        """

        if not self.is_enabled(session_id):
            return
        keys = self._replied_batch_keys.pop(str(session_id), None)
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

    # ---------------- 群聊上下文：留档与压缩 ----------------

    def chat_history_limit(self) -> int:
        return max(20, int(self.world.context.chat_history_max))

    def chat_summary_text(self, state: WorldState) -> str:
        """默认取"存档会话"那一份（老存档回落到全局摘要）。"""

        return self.chat_summary_for(state, state.session_id)

    def chat_summary_for(self, state: WorldState, session_id: str) -> str:
        """某个会话"更早聊过的"那一段（分会话存；老存档只有全局那份）。

        老存档那份全局摘要的**主人是 ``state.session_id``**（和
        :meth:`chat_summary_map`、``clear_chat_context`` 的口径一致）：
        只有问的就是它才回落。别的会话没有自己的摘要时返回空串——
        把存档会话的旧摘要当成这里的，她会把在别处说过的事讲成“我们刚才在这儿聊的”。
        """

        key = str(session_id or state.session_id or "")
        entry = dict((state.chat_summaries or {}).get(key) or {})
        text = str(entry.get("text") or "").strip()
        if text:
            return text
        if key != str(state.session_id or ""):
            return ""
        return str(state.chat_summary or "").strip()

    def chat_summary_map(self, state: WorldState) -> dict[str, str]:
        """每个会话一份"更早聊过的"，按"最近更新"排序（提示词按这个顺序渲染）。"""

        rows: list[tuple[float, str, str]] = []
        for key, entry in dict(state.chat_summaries or {}).items():
            info = dict(entry or {}) if isinstance(entry, dict) else {}
            text = str(info.get("text") or "").strip()
            if not text:
                continue
            try:
                at = float(info.get("at") or 0.0)
            except (TypeError, ValueError):
                at = 0.0
            rows.append((at, str(key), text))
        if not rows and str(state.chat_summary or "").strip():
            # 老存档（升级前压过摘要）：归到存档会话名下
            rows.append(
                (
                    float(state.chat_summary_at or 0.0),
                    str(state.session_id),
                    str(state.chat_summary).strip(),
                )
            )
        rows.sort(key=lambda item: item[0])
        return {key: text for _at, key, text in rows}

    async def clear_chat_context(self, session_id: str) -> dict[str, Any]:
        """清空**这个会话**的群聊留档与摘要（调试用：想看她"第一次听到"的反应时很有用）。

        会话组里别的群 / 私聊的留档与摘要不受影响——它们本来就是分开记的。

        清的是**这个会话的上下文**：留档（``recent_chat`` / ``chat_dropped``）、
        「更早聊过的」摘要（``chat_summaries[target]`` 与老字段 ``chat_summary``）、
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
            had_summary = bool(self.chat_summary_for(state, target))
            state.recent_chat = kept
            state.chat_dropped = [
                item for item in list(getattr(state, "chat_dropped", None) or []) if not _from_here(item)
            ]
            state.chat_summaries = {
                key: value
                for key, value in dict(state.chat_summaries or {}).items()
                if str(key) != target
            }
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
                # 老字段（全局摘要 / 全局备忘 / 全局预览）就是这个会话的，一起清掉
                state.chat_summary = ""
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
        return {"removed": removed, "had_summary": had_summary}

    async def _maybe_compress_chat(
        self, state: WorldState, outcome: TickOutcome
    ) -> None:
        """留档攒到阈值时，把较早的部分交给压缩模型压成一段摘要。

        **按会话分开压**：一个会话组里的群和私聊共用一份留档，一起压的话
        群里聊的和私聊聊的会混进同一段摘要（提示词里她就分不清哪句是哪儿的）。
        """

        config = self.world.context
        if str(config.chat_overflow) != "compress":
            await self._warn_chat_overflow(state)
            return
        threshold = max(10, int(config.chat_compress_threshold))
        refresh = max(60, int(config.summary_refresh_minutes)) * 60
        keep = max(1, int(config.chat_keep_after_compress))
        now = self._now()
        dropped = list(getattr(state, "chat_dropped", None) or [])
        # 按"在哪儿说的"分堆：先放"被上限顶掉、还没进过摘要"的那几条（它们更早），
        # 不带上就等于凭空消失了（聊得快的时候正是这样丢的记忆）。
        buckets: dict[str, list[dict[str, Any]]] = {}
        for item in [*dropped, *state.recent_chat]:
            key = str(item.get("origin") or state.session_id)
            buckets.setdefault(key, []).append(item)
        dropped_ids = {id(item) for item in dropped}
        summaries: dict[str, dict[str, Any]] = {
            str(key): dict(info)
            for key, info in dict(state.chat_summaries or {}).items()
            if isinstance(info, dict)
        }
        keep_ids: set[int] = set()
        compressed = 0
        for key, items in buckets.items():
            # 有"被上限顶掉、还没进过摘要"的那几条时必须压：不压就等于凭空消失
            has_dropped = any(id(item) in dropped_ids for item in items)
            if len(items) < threshold and not has_dropped:
                keep_ids.update(id(item) for item in items)
                continue
            last_at = float((summaries.get(key) or {}).get("at") or 0.0)
            if last_at <= 0.0:
                # 老存档只有一个全局的节流时间：第一次按它算
                last_at = float(state.chat_summary_at or 0.0)
            if now - last_at < refresh:
                keep_ids.update(id(item) for item in items)
                continue
            older = items[: max(0, len(items) - keep)]
            if not older:
                keep_ids.update(id(item) for item in items)
                continue
            summary = await self._summarize_chat(state, older, session_id=key)
            if not summary:
                keep_ids.update(id(item) for item in items)
                continue
            summaries[key] = {"text": summary, "at": now}
            compressed += len(older)
            keep_ids.update(id(item) for item in items[-keep:])
        if compressed <= 0:
            return
        state.chat_summaries = summaries
        state.chat_summary_at = now
        # 已经按会话存了，老字段清掉：留着会在提示词里重复出现一次
        state.chat_summary = ""
        state.recent_chat = [item for item in state.recent_chat if id(item) in keep_ids]
        state.chat_dropped = [item for item in dropped if id(item) in keep_ids]
        outcome.notes.append(f"已把较早的 {compressed} 条群聊压成摘要")
        await self._log_event(
            state,
            "context",
            {
                "note": "压缩较早群聊",
                "compressed": compressed,
                "kept": keep,
                "sessions": len(buckets),
            },
            outcome=outcome,
        )

    async def _warn_chat_overflow(self, state: WorldState) -> None:
        """留档已经顶到上限、而且是"直接丢弃"：提醒一次，免得她"忘事"却查不出原因。

        一小时最多提醒一次。
        """

        limit = self.chat_history_limit()
        # 留档是按会话各自留的：这里要看"最多的那个会话"够没够上限
        buckets: dict[str, int] = {}
        for item in state.recent_chat:
            key = self._chat_origin(item, state)
            buckets[key] = buckets.get(key, 0) + 1
        if max(buckets.values(), default=0) < limit:
            return
        now = self._now()
        if now - float(getattr(state, "chat_overflow_warned_at", 0.0) or 0.0) < 3600:
            return
        state.chat_overflow_warned_at = now
        await self._log_event(
            state,
            "context",
            {
                "note": "群聊留档已满，最早的会被直接丢弃",
                "hint": "想让她记得久一点：全局设置 → 上下文 → 留档超了怎么办 → 压成摘要",
                "limit": limit,
            },
        )

    async def _summarize_chat(
        self,
        state: WorldState,
        history: list[dict[str, Any]],
        *,
        session_id: str = "",
    ) -> str:
        if self.context_llm is None or not history:
            return ""
        limit = max(40, int(self.world.context.history_max_chars))
        lines = []
        for item in history:
            who = "她" if item.get("is_self") else (item.get("name") or item.get("user_id") or "")
            text = _clip_text(item.get("text"), limit)
            if text:
                lines.append(f"- {who}: {text}")
        if not lines:
            return ""
        # 只接着"这个会话"的上一份摘要往下写：群里和私聊的脉络本来就不一样
        previous = (
            self.chat_summary_for(state, session_id)
            if str(session_id or "").strip()
            else self.chat_summary_text(state)
        )
        system_prompt = (
            "你负责把群聊记录整理成一段背景摘要，供同一个角色之后回忆这段日子。\n"
            "要写清：参与的人（谁在跟谁说话）、正在聊的话题及其来龙去脉、"
            "已经约定或承诺过的事、还没回应的事，以及**具体细节**"
            "（时间、数字、物品、称呼、喜欢/讨厌的东西）——细节比概括有用得多，"
            "她后面要靠它接话。\n"
            "已经有更早的摘要时：接着它往下写，把仍然成立的事并进去，过时的不再写。"
            "不要逐条复述、不要评价、不要编造。只输出摘要本身，不超过 600 字。"
        )
        prompt = (
            (f"已有的更早摘要：\n{previous}\n\n" if previous else "")
            + "需要压缩的群聊记录：\n"
            + "\n".join(lines)
            + "\n\n请输出更新后的摘要。"
        )
        try:
            reply = await self.context_llm.generate(
                session_id=state.session_id,
                system_prompt=system_prompt,
                prompt=prompt,
                temperature=0.2,
            )
        except Exception as exc:
            self._log("debug", f"上下文压缩失败：{exc}")
            return ""
        if not getattr(reply, "ok", False):
            return ""
        return str(reply.text or "").strip()[:600]

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
