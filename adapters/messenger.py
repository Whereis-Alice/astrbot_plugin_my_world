"""发送适配：文本、图片、群名片、拍一拍与失败冷却。
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
import time
from typing import TYPE_CHECKING, Any

from astrbot.api.event import MessageChain
from astrbot.api.message_components import At, Image, Plain, Reply

from ..core.message_parts import _sendable_images
from ..core.plugin_compat import group_card_owner
from ..core.ports import CardResult, PokeResult
from ..core.speech_policy import segment_allowed, reply_target, accept_segment, contact_targets
from .group_names import GroupNamesMixin
from .group_mentions import GroupMentions, addressed_chain, has_body

if TYPE_CHECKING:
    from .. import MyWorldPlugin


class AstrBotMessenger(GroupNamesMixin):
    """MessagePort 实现。"""

    # 发送失败后，这个会话多少秒内不再尝试发送。
    # 协议端（NapCat）掉线/卡住时，一次发送可能要等 AstrBot 那边的超时才失败（默认 180 秒），
    # 期间不断重试只会把更多消息塞进协议端队列，恢复后一次性喷出来刷屏。
    SEND_FAIL_COOLDOWN_SECONDS = 180

    def __init__(self, plugin: "MyWorldPlugin") -> None:
        self.plugin = plugin
        self._mentions = GroupMentions(plugin)
        self._blocked_until: dict[str, float] = {}
        self._fail_notes: dict[str, str] = {}
        self._sent_texts: ContextVar[list[str] | None] = ContextVar("my_world_sent_texts", default=None)
        self._sent_images: ContextVar[list[str] | None] = ContextVar("my_world_sent_images", default=None)

    def take_sent_texts(self, session_id: str) -> list[str]:
        """Accepted segments from this task's last send, including partial delivery."""

        texts = self._sent_texts.get() or []
        self._sent_texts.set(None)
        return list(texts)

    def take_sent_images(self, session_id: str) -> list[str]:
        """This task's last accepted image group, after transport filtering."""
        refs = self._sent_images.get() or []
        self._sent_images.set(None)
        return list(refs)

    def proactive_delivery(self, session_id, outcome, *, echo_count=0, instant=False):
        from .proactive_delivery import delivery
        return delivery(self.plugin, session_id, outcome, echo_count=echo_count, instant=instant)

    def proactive_segment(self, session_id, index):
        from .proactive_delivery import segment
        return segment(session_id, index)

    async def proactive_accepted(self, session_id, text, index):
        from .proactive_delivery import accepted
        await accepted(self.plugin, session_id, text, index)

    def cleanup_proactive(self, outcome):
        for event in outcome.native_events.values():
            cleanup = getattr(event, "cleanup_temporary_local_files", None)
            if callable(cleanup):
                cleanup()

    # ---------------- 失败冷却（不重试） ----------------

    def blocked(self, session_id: str) -> bool:
        """这个会话现在是不是处于"刚发送失败"的冷却里。"""

        return self._blocked_until.get(session_id, 0.0) > time.time()

    def blocked_seconds(self, session_id: str) -> int:
        """还要等多少秒才会重新尝试发送（没在冷却里就是 0）。"""

        remain = self._blocked_until.get(session_id, 0.0) - time.time()
        return max(0, int(remain))

    def take_fail_note(self, session_id: str) -> str:
        """取走一次「发送失败」的说明（每个冷却窗口只会有一条）。"""

        return self._fail_notes.pop(session_id, "")

    def mark_sent(self, session_id: str) -> None:
        self._blocked_until.pop(session_id, None)
        self._fail_notes.pop(session_id, None)

    def mark_failed(self, session_id: str, exc: Any) -> None:
        """记一次失败：本会话进入冷却，日志每个窗口只打一条。"""

        self._blocked_until[session_id] = time.time() + self.SEND_FAIL_COOLDOWN_SECONDS
        note = f"{type(exc).__name__}: {exc}"
        if session_id in self._fail_notes:
            return
        self._fail_notes[session_id] = note
        self.plugin.logger.warning(
            f"[my_world] 发送失败，{self.SEND_FAIL_COOLDOWN_SECONDS} 秒内不再尝试"
            f"（失败就失败，不重发）：{note}"
        )

    async def send_text(self, session_id: str, messages: list[str]) -> bool:
        accepted_texts: list[str] = []
        self._sent_texts.set(accepted_texts)
        texts = [m for m in messages if m and str(m).strip()]
        if not texts:
            return False
        if self.blocked(session_id):
            return False  # 刚失败过：直接放弃这一批，连平台都不碰
        # 一条消息一个消息链：平台侧才会显示成多条（分段回复）。
        # 全部塞进同一个 chain 的话，多数平台会拼成一条发出去。
        ok = False
        failed = False
        for index, text in enumerate(texts):
            if not segment_allowed(session_id, index):
                break
            try:
                from .proactive_delivery import prepared_chain
                chain = prepared_chain(session_id, str(text), index)
                if chain is None:
                    chain = [Plain(text=str(text))]
                if not has_body(chain):
                    continue
                people = contact_targets(session_id, index) if ":GroupMessage:" in session_id else ()
                chain = addressed_chain(chain, people, reply_target(session_id, index))
                chain = await self._mentions.prepare(session_id, chain)
                # Permissions can change while the bounded member read is waiting.
                if not segment_allowed(session_id, index):
                    break
                result = await self.plugin.context.send_message(
                    session_id, MessageChain(chain=chain)
                )
                ok = bool(result) or ok
                if result:
                    accepted_texts.append(str(text))
                else:
                    self.mark_failed(session_id, "平台没有接收消息")
                    failed = True
                    break
            except Exception as exc:
                # 失败就失败：不重试、不补发，后面的几条也不再试（平台已经不通了）
                self.mark_failed(session_id, exc)
                failed = True
                break
            # A follow-up may arrive during the typing wait. Make each accepted
            # segment visible to the world before waiting for the next one.
            try:
                await accept_segment(session_id, str(text), index)
            except Exception as exc:
                # Receipt persistence must not turn accepted text into a send
                # failure or cause the caller to replay it. Final delivery retries it.
                self.plugin.logger.warning(f"[my_world] 保存发送回执失败：{exc}")
            if index == len(texts) - 1:
                break  # 最后一条不用再等
            delay = self.plugin.typing_delay_for(text)
            if delay > 0:
                # 分段之间停一下，群里看起来像她在一句句打字
                await asyncio.sleep(delay)
        if ok and not failed:
            self.mark_sent(session_id)
        return ok

    async def send_images(self, session_id: str, images: list[str]) -> bool:
        """把工具 / 指令生成出来的图片发到会话里。

        多张图放**同一条消息**：群里显示成一组，不会被拆成刷屏的连发。
        地址可以是 http、file://、base64://，AstrBot 发送时会自己转成平台要的格式。
        """

        self._sent_images.set([])
        refs = _sendable_images(images)
        if not refs:
            return False
        if self.blocked(session_id):
            return False  # 刚失败过：连同图片一起放弃这一批
        if not segment_allowed(session_id):
            return False
        chain = [Image(file=ref) for ref in refs]
        try:
            result = await self.plugin.context.send_message(
                session_id, MessageChain(chain=chain)
            )
        except Exception as exc:
            self.mark_failed(session_id, exc)
            return False
        if result:
            self._sent_images.set(list(refs))
            self.mark_sent(session_id)
        return bool(result)

    async def set_group_card(self, session_id: str, card: str) -> CardResult:
        """设置群名片。只有 aiocqhttp（OneBot）这类支持 call_action 的平台才生效。"""

        owner = self.group_card_owner(session_id)
        if owner:
            return CardResult(ok=False, reason=f"群名片由{owner}管理，MW 不覆盖它的名片")
        if not card:
            return CardResult(ok=False, reason="算出来的名片是空的")
        if ":GroupMessage:" not in session_id:
            return CardResult(ok=False, reason="这不是群聊，没有群名片")
        event = self.plugin.last_event(session_id)
        if event is None:
            # 这个群还没来过消息时，拿别处收到过的事件取机器人句柄也行——
            # 句柄是同一个 Bot，真正决定改哪个群的还是上面的 session_id。
            event = self.plugin.last_event_any()
        if event is None:
            return CardResult(ok=False, reason="还没收到过这个群的消息，拿不到机器人句柄")
        bot = getattr(event, "bot", None)
        if bot is None or not hasattr(bot, "call_action"):
            return CardResult(ok=False, reason="当前平台不支持改群名片（需要 OneBot/aiocqhttp）")
        group_id = session_id.split(":")[-1]
        self_id = event.get_self_id()
        try:
            await bot.call_action(
                "set_group_card",
                group_id=int(group_id) if str(group_id).isdigit() else group_id,
                user_id=int(self_id) if str(self_id).isdigit() else self_id,
                card=card,
            )
            return CardResult(ok=True, card=card)
        except Exception as exc:
            # 常见原因：机器人不是群管理/群主、群号不对、协议端不支持这个接口
            # 退避由引擎负责，这里只留 debug 日志，避免协议端挂掉时每分钟刷一条
            self.plugin.logger.debug(f"[my_world] 设置群名片失败：{exc}")
            return CardResult(ok=False, reason=f"平台拒绝了：{exc}", card=card)

    def group_card_owner(self, session_id: str) -> str:
        return group_card_owner(
            self.plugin.context, session_id, self.plugin.last_event(session_id)
        )

    async def fetch_group_card(self, session_id: str) -> str:
        """读她当前在群里的名片，用作"原名"的兜底。"""

        if ":GroupMessage:" not in session_id:
            return ""
        event = self.plugin.last_event(session_id)
        if event is None:
            return ""
        bot = getattr(event, "bot", None)
        if bot is None or not hasattr(bot, "call_action"):
            return ""
        group_id = session_id.split(":")[-1]
        self_id = event.get_self_id()
        try:
            info = await bot.call_action(
                "get_group_member_info",
                group_id=int(group_id) if str(group_id).isdigit() else group_id,
                user_id=int(self_id) if str(self_id).isdigit() else self_id,
                no_cache=True,
            )
        except Exception as exc:
            self.plugin.logger.debug(f"[my_world] 读取群名片失败：{exc}")
            return ""
        if not isinstance(info, dict):
            return ""
        return str(info.get("card") or info.get("nickname") or "").strip()

    async def poke(self, session_id: str, user_id: str) -> PokeResult:
        """戳一戳某个人。

        群聊和私聊走的是**两条不同的路**：

        - **群聊**：先发 OneBot 的 ``poke`` 消息段（群里的标准做法），不通再退到
          ``send_poke`` / ``group_poke`` 接口；
        - **私聊**：``poke`` 消息段是给群聊用的，私聊里发出去客户端会显示成一个认不出的
          占位（红叉破图），所以这里**不试消息段**，直接调 ``friend_poke``。

        每次都会把"走了哪条路"记进 ``PokeResult.route``：两条都不通就把原因带回去，
        她会退化成一句话说，而日志里要能看出究竟是为什么。
        """

        target = str(user_id or "").strip()
        if not target:
            return PokeResult(False, "没有指定要戳谁")
        if self.blocked(session_id):
            return PokeResult(False, "上一次发送失败，正在冷却")
        if not segment_allowed(session_id):
            return PokeResult(False, "自主发言授权已撤销")
        group_id = ""
        parts = str(session_id).split(":")
        if len(parts) >= 3 and "group" in parts[1].lower():
            group_id = parts[-1]
        errors: list[str] = []
        # 路线一：消息段。**只在群聊用**——私聊里这个段协议端不认。
        if group_id:
            try:
                from astrbot.api.message_components import Poke

                await self.plugin.context.send_message(
                    session_id, MessageChain(chain=[Poke(id=target)])
                )
                return PokeResult(True, route="poke 消息段")
            except Exception as exc:
                errors.append(f"消息段：{type(exc).__name__}: {exc}")
        # 路线二：直接调协议端接口（私聊只有这一条：friend_poke）
        event = getattr(self.plugin, "_last_events", {}).get(session_id)
        bot = getattr(event, "bot", None)
        if bot is not None:
            payload: dict[str, Any] = {
                "user_id": int(target) if target.isdigit() else target
            }
            if group_id.isdigit():
                payload["group_id"] = int(group_id)
            names = (
                ("send_poke", "group_poke")
                if group_id
                else ("friend_poke", "send_poke")
            )
            for name in names:
                if not segment_allowed(session_id):
                    return PokeResult(False, "自主发言授权已撤销")
                try:
                    await bot.call_action(name, **payload)
                    return PokeResult(True, route=name)
                except Exception as exc:
                    errors.append(f"{name}：{type(exc).__name__}: {exc}")
        else:
            errors.append("拿不到协议端连接（这条会话还没收到过消息）")
        return PokeResult(False, "；".join(errors) or "没有可用的戳一戳通道")
