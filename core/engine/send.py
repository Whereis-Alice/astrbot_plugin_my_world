"""群名片与发送。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from ..config_scope import session_scoped
from ..models import NodeDef
from ..nickname import (
    compute_nickname,
    should_update,
)
from ..ports import CardResult
from ..state import WorldState
from .constants import MAX_GROUP_IMAGES
from .models import (
    MessageContext,
    TickOutcome,
)


class SendMixin:
    # ================= 群名片 =================

    async def _sync_nickname(self, state: WorldState, node: NodeDef | None) -> None:
        config = self.world.nickname_sync
        if not config.enabled or self.messenger is None:
            return
        if state.bot_nickname_locked:
            return
        # 一个人的名片要跟着她走：组里的每个群都改一遍（私聊没有群名片，跳过）
        targets = [
            str(item)
            for item in self.group_sessions(state.session_id)
            if ":GroupMessage:" in str(item) and self.is_enabled(str(item))
        ]
        owner = getattr(self.messenger, "group_card_owner", None)
        if callable(owner):
            targets = [target for target in targets if not owner(target)]
        if not targets:
            return
        primary = targets[0]
        # 第一次遇到这个会话时，先把她当前的名片读回来当"原名"
        if not state.bot_base_nickname:
            try:
                fetched = await self.messenger.fetch_group_card(primary)
            except Exception as exc:
                self._log("debug", f"读取群名片失败: {exc}")
                fetched = ""
            if fetched:
                state.bot_base_nickname = fetched
                state.bot_current_nickname = state.bot_current_nickname or fetched
                await self._log_event(
                    state, "nickname", {"base": fetched, "note": "记下了她原来的群名片"}
                )
        desired = compute_nickname(self.world, state, node, base=state.bot_base_nickname)
        now = self._now()
        # 连续失败时按倍数退避（1× → 2× → 4× …，最多 1 小时一次），
        # 否则协议端一挂，这里会每隔一个冷却就重试并打一条错误日志。
        cooldown = max(10, int(config.cooldown_seconds))
        backoff = cooldown * (2 ** min(int(state.nickname_fail_count or 0), 6))
        if self._messenger_blocked(state.session_id) or state.nickname_fail_count:
            backoff = max(backoff, 300)
        if not should_update(
            state,
            desired,
            now=now,
            cooldown_seconds=min(backoff, 3600),
        ):
            return
        try:
            result = await self.messenger.set_group_card(primary, desired)
        except Exception as exc:
            result = CardResult(ok=False, reason=str(exc), card=desired)
        if result.ok:
            state.bot_current_nickname = desired
            state.last_nickname_update_at = now
            state.nickname_fail_count = 0
            self.note_local_card(primary, desired)
            await self._log_event(state, "nickname", {"to": desired, "ok": True})
            # 同一个她在别的群里的名片跟着一起改：不再各算一次冷却
            for session_id in targets[1:]:
                try:
                    await self.messenger.set_group_card(session_id, desired)
                except Exception as exc:
                    self._log("debug", f"同步群名片到 {session_id} 失败：{exc}")
            return
        outcome_reason = result.reason or "没有说明原因"
        state.last_nickname_update_at = now  # 失败也占一次冷却，避免每 tick 重试
        state.nickname_fail_count = min(int(state.nickname_fail_count or 0) + 1, 8)
        await self._log_event(
            state, "nickname", {"to": desired, "ok": False, "note": outcome_reason}
        )
        # 只在第一次失败时打日志，后面交给退避；（事件日志里每一条都还在，方便排查）
        if state.nickname_fail_count == 1:
            self._log(
                "debug",
                f"群名片没改成：{outcome_reason}（接下来会按 5 分钟起退避重试）",
            )

    # ================= 发送 =================

    def _messenger_blocked(self, session_id: str) -> bool:
        """发送方是否正处在"刚失败"的冷却里（宿主适配器不一定实现这个方法）。"""

        if self.messenger is None:
            return False
        checker = getattr(self.messenger, "blocked", None)
        if not callable(checker):
            return False
        try:
            return bool(checker(session_id))
        except Exception:
            return False

    def _image_home(self, outcome: TickOutcome | None, action: Any = None) -> str:
        """这一步生成的图该落在哪个会话。

        和同一步的正文必须是**同一个落点**：正文走 ``add_speech(text, 落点)``，
        图以前一律挂在 ``outcome.session_id`` 上，于是私聊里起的动作会把图
        发到群里（正文私聊、图片群聊）。

        优先级：计划里冻结的落点（``speech_home``）→ 这一步自己带的落点 →
        这一轮的会话（兜底）。兜底意味着"没定下落点"，多会话时很容易发错地方，
        所以补一条警告日志。
        """

        if outcome is None:
            return ""
        frozen = str(getattr(outcome, "speech_home", "") or "").strip()
        if frozen:
            return frozen
        stepped = str(self._act_get(action, "session", "") or "").strip() if action is not None else ""
        if stepped:
            return stepped
        session_id = str(getattr(outcome, "session_id", "") or "").strip()
        place = str(getattr(outcome, "place", "") or "").strip()
        if place:
            # 有人跟她说话、或者规则决策刚敲定：这一轮发生在哪儿是确定的，
            # 图落在那儿本来就对，没什么可警告的
            return session_id
        if len(self.group_sessions(session_id)) > 1:
            # 单会话时"没定落点"是正常事，不用吵；多会话才是真隐患
            # （老计划里的步骤没写 session 就会走到这儿）
            self._log(
                "warning",
                f"这一轮没定下落点，图片按当前会话发：{session_id}",
            )
            outcome.notes.append("这一轮没定下落点，图片按当前会话发")
        return session_id

    def _attach_images(
        self,
        outcome: TickOutcome | None,
        images: list[Any] | None,
        *,
        session_id: str = "",
    ) -> None:
        """把工具 / 指令返回的图片挂到这一轮的结果上（去重、限量）。

        只挂在"这一轮的结果"上，不写进动作状态——生图工具给的常常是 base64，
        落进 state 里会把存档撑得很丑。

        ``session_id`` 是这些图要落在哪个会话：留空 = 这一轮的会话；
        给了别的会话（私聊里起的动作，而这一拍推的是群里）就投到那个会话去，
        ``_deliver`` 会照着分开发。图和正文分家就是这么来的，所以这里必须收落点。
        """

        if outcome is None or not images:
            return
        target = str(session_id or "").strip() or str(outcome.session_id or "")
        refs = [str(item or "").strip() for item in images]
        refs = [item for item in refs if item]
        if not refs:
            return
        if target and target != str(outcome.session_id or ""):
            outcome.add_images(refs, target)
            return
        for ref in refs:
            if ref not in outcome.images:
                outcome.images.append(ref)
        if len(outcome.images) > MAX_GROUP_IMAGES:
            outcome.notes.append(
                f"这一轮生成的图片太多（{len(outcome.images)} 张），只贴前 {MAX_GROUP_IMAGES} 张"
            )
            del outcome.images[MAX_GROUP_IMAGES:]

    def _note_own_image(
        self, state: WorldState, label: str, session_id: str = ""
    ) -> None:
        """她发出去的图片也要进"她自己的聊天记录"。

        不然下一轮有人引用那张图，她只会看到"对方引用了某某发的［图片］"，
        既不知道那图是自己发的、也想不起来自己刚发过照片。
        """

        note = " ".join(str(label or "").split())[:40]
        text = f"［我发了一张图片：{note}］" if note else "［我发了一张图片］"
        recorded = state.note_chat(
            user_id="__self__",
            name=state.bot_current_nickname or state.bot_base_nickname or "你",
            text=text,
            now=self._now(),
            keep=self.chat_history_limit(),
            is_self=True,
            origin=session_id or state.session_id,
        )
        self._tag_chat_origin(state, session_id or state.session_id, recorded)

    def _tag_chat_origin(
        self,
        state: WorldState,
        session_id: str,
        item: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """给刚记下的那条留档标上"在哪儿说的"。

        分过组的会话共用同一份留档，别的地方说的话要标出来源——
        提示词里她才知道那句不是当前这个群里说的。

        ``item``：``state.note_chat(...)`` 的返回值（**刚记下的那一条**）。
        判定为重复而没记时它返回 ``None``，这时**一个字都不能改**——
        以前无条件改 ``recent_chat[-1]``，去重命中时被改的是上一条（很可能是
        别的会话的记录），于是当前会话凭空多出一条"在这儿说过的话"。
        没传 ``item`` 时退回"最后一条"，但会先确认它**真的就是刚记下的那条**：
        序号等于当前的 ``chat_seq``，而且**还没有来源**（已经标过的说明它早就
        记下过了，这次调用很可能只是去重命中）。对不上就不动，
        免得又回到"改错人"的老毛病上。
        """

        origin = str(session_id or state.session_id)
        target = item
        if target is not None:
            if not any(entry is target for entry in state.recent_chat):
                return None
        else:
            if not state.recent_chat:
                return None
            last = state.recent_chat[-1]
            if int(last.get("seq") or -1) != int(state.chat_seq or 0):
                return None
            if str(last.get("origin") or ""):
                # 已经有来源：不是"这次刚记下、还没标"的那条（去重命中时就是这个样子）
                return None
            target = last
        target["origin"] = origin
        target.pop("via", None)
        from ..evidence import source_snapshot
        source = source_snapshot(target, origin)
        target["source_id"] = source["id"]
        target.setdefault("eligible", bool(target.get("is_self")))
        self.db.raw.observe_source(source, eligible=bool(target.get("eligible")))
        return target

    def session_labels(self, state: WorldState) -> dict[str, str]:
        """通讯录里每个会话怎么称呼：渲染"你在别处听到的"时用。"""

        labels: dict[str, str] = {}
        for session_id in self.group_sessions(state.session_id):
            labels[str(session_id)] = self.session_label(session_id, state)
        return labels

    def reply_place(self, state: WorldState, session_id: str) -> str:
        """这条消息来自哪个会话：多会话（同一个她有好几个地方）时才写。"""

        if len(self.group_sessions(state.session_id)) <= 1:
            return ""
        return self.session_label(session_id, state)

    def _note_session_activity(self, state: WorldState, ctx: MessageContext) -> None:
        """记下"他在哪个会话里跟她说话"。

        她挑落点、提示词标来源、通讯录里的"最近有人跟你说话是……"都读这一份。
        """

        now = self._now()
        record = dict(state.user_presence.get(ctx.user_id) or {})
        record["session"] = ctx.session_id
        state.user_presence[ctx.user_id] = record
        state.session_activity[ctx.session_id] = {
            "at": now,
            "user_id": ctx.user_id,
            "user_name": ctx.user_name or ctx.user_id,
        }
        if ctx.group_name:
            self.note_session_name(ctx.session_id, ctx.group_name)

    def _group_images(self, outcome: TickOutcome) -> list[str]:
        """这一轮真要发出去的图片（去重、限量，顺序保持生成顺序）。"""

        picked: list[str] = []
        for item in list(outcome.images or []):
            ref = str(item or "").strip()
            if ref and ref not in picked:
                picked.append(ref)
        return picked[:MAX_GROUP_IMAGES]

    async def _send_group_images(self, session_id: str, images: list[str]) -> bool:
        """把结果图发到群里。发送通道没实现这个方法时安静跳过。"""

        sender = getattr(self.messenger, "send_images", None)
        if not callable(sender):
            return False
        try:
            return bool(await sender(session_id, list(images)))
        except Exception as exc:
            # 图片发送失败不算"她没说话"：只留一条日志，别把这一轮的其他内容也搅黄
            self._log("warning", f"发送结果图片失败：{exc}")
            return False

    def _filter_proactive_speech(self, outcome: TickOutcome) -> None:
        """Filter before recording speech, including already queued autonomous lines."""
        removed = 0

        def keep(lines, key, metadata, *, images=False):
            nonlocal removed
            kinds = metadata.get(key, [])
            mask = [
                self.speech_allowed(key or outcome.session_id, kinds[i] if i < len(kinds) else outcome.speech_kind)
                for i in range(len(lines))
            ]
            if key == "" and not images:
                outcome.debug_positions = [sum(mask[:int(pos)]) for pos in outcome.debug_positions]
            removed += len(mask) - sum(mask)
            metadata[key] = [kinds[i] if i < len(kinds) else outcome.speech_kind for i, ok in enumerate(mask) if ok]
            return [line for line, ok in zip(lines, mask) if ok]

        outcome.messages = keep(outcome.messages, "", outcome.speech_kinds)
        outcome.images = keep(outcome.images, "", outcome.image_kinds, images=True)
        outcome.routed = {sid: keep(lines, sid, outcome.speech_kinds) for sid, lines in outcome.routed.items()}
        outcome.routed_images = {sid: keep(lines, sid, outcome.image_kinds, images=True) for sid, lines in outcome.routed_images.items()}
        if removed:
            outcome.notes.append(f"自主搭话已暂停，未发送 {removed} 条内容")

    @session_scoped
    async def _deliver(self, outcome: TickOutcome) -> None:
        if self.messenger is None:
            return
        self._filter_proactive_speech(outcome)
        # 已经即时发出去的算"她说过话"（进日志、进聊天上下文、算主动发言冷却），
        # 但不再发一遍。
        said = [m for m in outcome.said_messages() if m and str(m).strip()]
        # 下面取的是"还没发出去的那部分"：即时发言已经从 messages 里取走了，
        # 所以按真实顺序合成时不会再把她刚说过的话发第二遍。
        # 说给别的会话的话（群里应付一句、私聊里再吐槽一句）：逐条投过去
        routed = {
            str(sid): [m for m in (msgs or []) if m and str(m).strip()]
            for sid, msgs in (outcome.routed or {}).items()
        }
        routed = {sid: msgs for sid, msgs in routed.items() if msgs}
        routed_images = {
            str(sid): [str(ref) for ref in (refs or []) if str(ref).strip()]
            for sid, refs in (outcome.routed_images or {}).items()
        }
        routed_images = {sid: refs for sid, refs in routed_images.items() if refs}
        pending = self.take_pending_echo(outcome.session_id)
        images = self._group_images(outcome)
        broadcast_targets = []
        for target in [outcome.session_id, *(outcome.broadcast_targets or [])]:
            target = str(target or "").strip()
            if target and target not in broadcast_targets:
                broadcast_targets.append(target)
        if (
            not said
            and not routed
            and not routed_images
            and not outcome.debug_messages
            and not pending
            and not images
        ):
            return
        # 让她的话和调试回显按真实顺序出现：先调工具、再说话。
        # 门禁攒下的回显（被叫醒之类）发生在这轮之前，排在最前面。
        ordered = [
            m
            for m in (pending + outcome.ordered_messages())
            if m and str(m).strip()
        ]
        state = await self.load_state(outcome.session_id, cold_start=False)
        # 事件里的发言（求助 / 事件结果）不算"她主动找人聊天"：
        # 不走无人回应保护，也不会把她的冷却期拖长。
        kinds = {outcome.speech_kind, *outcome.live_kinds}
        for group in (outcome.speech_kinds, outcome.image_kinds):
            for values in group.values():
                kinds.update(values)
        is_event_speech = bool(kinds & {"event", "schedule"})
        # 排在后面的回话（当时有人跟她说话，她在忙，轮到她时才说）同样不算"主动开口"：
        # 被"主动发言冷却"吞掉的话，对方再也等不到那句回应，而计划上却记成"已经说过了"
        is_reply_speech = "reply" in kinds
        # 已经即时发出去的几句挡不回来：这一轮不能再看冷却，否则群里会"说了一句就没下文"，
        # 那句即时发言也不进聊天记录（下一轮她就不知道自己刚说过）
        spoke_live = bool(outcome.live_messages)
        if (
            not is_event_speech
            and not is_reply_speech
            and not spoke_live
            and not self.engagement.can_speak(state)
        ):
            outcome.notes.append("冷却期内不发送")
            return
        async with self.session_state(outcome.session_id) as state:
            if said or images or routed or routed_images:
                if not is_event_speech and not is_reply_speech:
                    self.engagement.on_bot_spoke(state)
                await self._log_event(
                    state,
                    "bot_message",
                    {
                        "messages": said,
                        # 生图 / 出图的动作：日志里能看出这一轮除了话还贴了几张图
                        "images": len(images),
                        "style_cell": state.last_style_cell,
                        "say_limit": int(state.last_say_limit or 0),
                    },
                )
                # 自己说过的话也进聊天上下文：模型才知道"刚才那句是我说的"，
                # 并被要求换一种说法，避免每轮都用同一个句式开场。
                for message in said:
                    recorded = state.note_chat(
                        user_id="__self__",
                        name=state.bot_current_nickname or state.bot_base_nickname or "你",
                        text=message,
                        now=self._now(),
                        keep=self.chat_history_limit(),
                        is_self=True,
                        origin=outcome.session_id,
                    )
                    state.note_reply(message, session_id=outcome.session_id)
                    self.note_dialogue(state, text=message, is_self=True, origin=outcome.session_id)
                    # 这句是在哪个会话里说的（留档只有一份，来源要标出来）
                    self._tag_chat_origin(state, outcome.session_id, recorded)
                for target, messages in routed.items():
                    await self._log_event(
                        state,
                        "bot_message",
                        {
                            "messages": messages,
                            "session": self.session_label(target, state),
                            "images": len(routed_images.get(target) or []),
                            "note": "这一句说给了另一个会话",
                        },
                    )
                    for message in messages:
                        recorded = state.note_chat(
                            user_id="__self__",
                            name=state.bot_current_nickname
                            or state.bot_base_nickname
                            or "你",
                            text=message,
                            now=self._now(),
                            keep=self.chat_history_limit(),
                            is_self=True,
                            origin=target,
                        )
                        state.note_reply(message, session_id=target)
                        self.note_dialogue(state, text=message, is_self=True, origin=target)
                        self._tag_chat_origin(state, target, recorded)
            # 调试用的动作回显不算"她说过的话"：不进聊天上下文、不计无人回应保护
        sent = False
        attempted = False
        # Send each default result exactly once per selected concrete destination.
        for target in broadcast_targets:
            target_view = replace(
                outcome, session_id=target, messages=list(outcome.messages), images=list(outcome.images),
                routed={}, routed_images={}, notes=[],
                speech_kinds={"": list(outcome.speech_kinds.get("", []))},
                image_kinds={"": list(outcome.image_kinds.get("", []))},
            )
            self._filter_proactive_speech(target_view)
            target_ordered = pending + target_view.ordered_messages()
            target_images = self._group_images(target_view)
            try:
                target_sent = True
                if target_ordered:
                    # 返回假值 = 平台没收下这批话：必须点名是哪个落点失败了，
                    # 其余落点照旧继续发（一次落点失败不该拖累别的会话）
                    attempted = True
                    target_sent = bool(await self.messenger.send_text(target, target_ordered))
                    sent = sent or target_sent
                    if not target_sent:
                        outcome.notes.append(f"发到 {target} 失败：平台没有接收")
                        self._log("warning", f"发到 {target} 失败：平台没有接收")
                if target_images:
                    await self._send_group_images(target, target_images)
            except Exception as exc:
                outcome.notes.append(f"发到 {target} 失败：{exc}")
                self._log("warning", f"发到 {target} 失败：{exc}")
        for target, messages in routed.items():
            try:
                await self.messenger.send_text(target, messages)
            except Exception as exc:
                outcome.notes.append(f"发到 {target} 失败：{exc}")
                self._log("warning", f"发到 {target} 失败：{exc}")
        for target, refs in routed_images.items():
            await self._send_group_images(target, refs)
        note = getattr(self.messenger, "take_fail_note", None)
        failed_note = note(outcome.session_id) if callable(note) else ""
        if failed_note:
            # 没发出去这件事也要留痕：日志里能看到"这条她说过、但平台没收"
            async with self.session_state(outcome.session_id) as state:
                self.dynamics.apply_event(state, "send_failed", now=self._now())
                await self._log_event(
                    state,
                    "send_failed",
                    {"messages": ordered, "note": failed_note},
                )
        elif not sent and ordered and attempted:
            outcome.notes.append("平台没有接收这批消息")
