"""记仇与「她自己的事」：答应过的、想做的。"""

from __future__ import annotations

import time
import json
from typing import Any
from ..memory import INNER
from ..prompt import readable_caption
from ..state import WorldState
from .constants import PENDING_IMAGE_KEEP
from .models import (
    MessageContext,
    TickOutcome,
)
from .util import _clip_text


class RelationsMixin:
    def _emotion_belongs(self, state: WorldState, item: dict, persona_key: str | None = None) -> bool:
        key = self._matter_persona_key(state) if persona_key is None else persona_key
        return not item.get("persona_key") or item["persona_key"] == key

    @staticmethod
    def _emotion_origin(state: WorldState, session_id: str = "") -> str:
        from .life_inference import STATE_LEASE
        lease = STATE_LEASE.get() or {}
        return str(session_id or (lease.get("origin") if lease.get("state") is state else "") or state.session_id)

    def _emotion_visible(self, state: WorldState, item: dict, session_id: str = "", *,
                         persona_key: str | None = None) -> bool:
        return (self._emotion_belongs(state, item, persona_key)
                and str(item.get("session") or state.session_id) == self._emotion_origin(state, session_id))

    def _archive_emotion(self, state: WorldState, item: dict, *, kind: str, content: str, weight: float) -> None:
        """Keep the original person, character and conversation after a feeling fades."""
        uid = str(item.get("about") or item.get("user_id") or "")
        self.memory.remember(session_id=str(item.get("session") or state.session_id), persona_id="",
            node_id=state.node_id, content=content, memory_type=INNER, emotion=state.mood, weight=weight,
            affect=state.affect, valence=state.valence, participants=[uid] if uid else [], source="runtime",
            context=json.dumps({"kind": kind, "persona_key": item.get("persona_key") or "",
                "emotion_user": uid,
                "original": str(item.get("text") or item.get("reason") or ""),
                "source_scoped": bool(item.get("session") or uid)}, ensure_ascii=False))

    # ---------------- 她记着的账（记仇） ----------------

    def grudge_for(self, state: WorldState, user_id: str, *, session_id: str = "",
                   persona_key: str | None = None) -> dict[str, Any] | None:
        """这个人身上有没有她还记着的账。"""

        uid = str(user_id or "")
        if not uid:
            return None
        for item in list(state.grudges or []):
            if (isinstance(item, dict) and str(item.get("user_id") or "") == uid
                    and self._emotion_visible(state, item, session_id, persona_key=persona_key)):
                return item
        return None

    def note_grudge(
        self,
        state: WorldState,
        reason: str,
        *,
        user_id: str = "",
        user_name: str = "",
        session_id: str = "",
        force: bool = False,
    ) -> bool:
        """记一笔账：他做了什么让她现在还气着。

        门槛（配额）是刻意卡紧的：同一个人最多一笔、一天最多记一笔新的、
        同时最多 ``grudge_max`` 笔——不然她会变成一个天天记仇的人。
        ``force``：编辑器里手动加的（不走"每天最多一笔"那道闸）。
        """

        config = self.world.profile
        if not bool(getattr(config, "grudge_enabled", True)):
            return False
        body = " ".join(str(reason or "").split())[:60]
        uid = str(user_id or "")
        if not body or not uid:
            return False
        now = self._now()
        origin = self._emotion_origin(state, session_id)
        persona_key = self._matter_persona_key(state) or ""
        day = self._today_key(now)
        if str(state.grudge_day or "") != day:
            state.grudge_day = day
            state.grudge_count = 0
        daily = max(0, int(getattr(config, "grudge_daily_max", 1) or 0))
        if not force and int(state.grudge_count or 0) >= daily:
            self._log("debug", "今天记的账够多了，这一笔不记")
            return False
        items = [dict(item) for item in (state.grudges or []) if isinstance(item, dict)]
        # 同一个人只留一笔：这件事比那件事更气人，就换掉，但不叠加
        existing = next((item for item in items if str(item.get("user_id") or "") == uid
                         and self._emotion_visible(state, item, origin)), None)
        if existing is not None and body in str(existing.get("reason") or ""):
            return False
        keep_days = max(1, int(getattr(config, "grudge_days", 7) or 7))
        record = {
            "user_id": uid,
            "who_name": str(user_name or "") or self._user_label(state, uid),
            "reason": body,
            "at": now,
            "until": now + keep_days * 86400.0,
            "session": origin,
            "persona_key": persona_key,
        }
        items = [item for item in items if item is not existing]
        items.append(record)
        limit = max(1, int(getattr(config, "grudge_max", 2) or 2))
        own = [item for item in items if self._emotion_belongs(state, item)][-limit:]
        state.grudges = [item for item in items if not self._emotion_belongs(state, item)] + own
        state.grudges = state.grudges[-32:]
        state.grudge_count = int(state.grudge_count or 0) + 1
        # 当场气一下：只给一次脉冲，之后靠"冷一档 + 提示词"，不再反复扣
        self.dynamics.apply_event(state, "grudge", now=float(now))
        self._log("debug", f"记了一笔账：{body}")
        return True

    def resolve_grudge(self, state: WorldState, *, user_id: str = "", session_id: str = "") -> bool:
        """这笔账算了：他道歉 / 补上 / 解释清楚了。返回有没有真的划掉一笔。"""

        uid = str(user_id or "")
        record = self.grudge_for(state, uid, session_id=session_id)
        if record is None:
            return False
        state.grudges = [
            item
            for item in (state.grudges or [])
            if item is not record
        ]
        reason = str(record.get("reason") or "").strip()
        name = str(record.get("who_name") or "") or self._user_label(state, uid)
        self._archive_emotion(state, record, kind="grudge", content=f"（跟他算清了一笔账）{name}：{reason}", weight=.35)
        self._log("debug", f"这笔账算了：{reason}")
        return True

    def _decay_grudges(self, state: WorldState, *, now: float) -> None:
        """到期的账自己淡掉（写进记忆），不留着一个半年前的仇。"""

        config = self.world.profile
        if not bool(getattr(config, "grudge_enabled", True)):
            state.grudges = [item for item in state.grudges if not self._emotion_belongs(state, item)]
            return
        keep: list[dict[str, Any]] = []
        for item in list(state.grudges or []):
            if not isinstance(item, dict):
                continue
            if not self._emotion_belongs(state, item):
                keep.append(item)
                continue
            until = float(item.get("until") or 0.0)
            if until > 0 and float(now) >= until:
                reason = str(item.get("reason") or "").strip()
                name = str(item.get("who_name") or "")
                if reason:
                    self._archive_emotion(state, item, kind="grudge", content=f"（气过一阵就算了）{name}：{reason}", weight=.3)
                continue
            keep.append(item)
        state.grudges = keep

    def _grudge_block(
        self, state: WorldState, ctx: Any = None, *, autonomy: bool = False, persona_key: str | None = None
    ) -> str:
        """「他还欠着我一笔」那一段。

        只对当事人说：跟别人说话时一个字都不提（这是"她记着这个人的账"，
        不是"她今天心情不好"）。
        """

        config = self.world.profile
        if not bool(getattr(config, "grudge_enabled", True)):
            return ""
        who = str(getattr(ctx, "user_id", "") or "")
        lines: list[str] = []
        for item in list(state.grudges or []):
            if not isinstance(item, dict):
                continue
            if not self._emotion_visible(state, item, str(getattr(ctx, "session_id", "") or ""), persona_key=persona_key):
                continue
            uid = str(item.get("user_id") or "")
            if who and uid and uid != who:
                continue
            reason = str(item.get("reason") or "").strip()
            if not reason:
                continue
            days = max(0.0, (self._now() - float(item.get("at") or 0.0)) / 86400.0)
            age = f"（{days:.0f} 天前的事）" if days >= 1 else ""
            label = ""
            if autonomy and uid and uid != who:
                name = str(item.get("who_name") or "") or self._user_label(state, uid)
                label = f"{name}：" if name else ""
            lines.append(f"- {label}{reason}{age}")
            if len(lines) >= 2:
                break
        if not lines:
            return ""
        return (
            "# 你还记着他一笔账\n"
            "他有一件事到现在还没给你个说法。**你现在对他还气着**：说话可以短一点、"
            "淡一点、可以带刺，但**别翻旧账、别每轮都提**，也别拿这件事去跟别人说。\n"
            "**不过他要是问起你为什么生气（或者主动道歉），就把这笔账说出来**："
            "可以只给个理由、可以继续嘴硬，但不能一问三不知。闷着不说、只摆脸色不算本事。\n"
            "他要是道歉了、补上了，你心里那口气就松了——可以顺台阶下，也可以再嘴硬两句。\n"
            + "\n".join(lines)
        )

    # ---------------- 她自己的事（答应过的、想做的） ----------------

    async def note_own_topic(
        self,
        state: WorldState,
        text: str,
        *,
        user_id: str = "",
        user_name: str = "",
        session_id: str = "",
    ) -> bool:
        """记一件**她自己**的事：答应过别人的、自己想做的，还没做。"""

        if not self.world.state_dynamics.own_topic_enabled:
            return False
        _, persona_key = await self._journal_persona(
            str(session_id or state.session_id)
        )
        return self.add_life_matter(state, text, who=str(user_id or ""), who_name=str(user_name or ""),
                                    session=str(session_id or state.session_id), kind="promise" if user_id else "project",
                                    persona_key=persona_key) is not None

    def mark_own_topic_done(self, state: WorldState, *, user_id: str = "", matter_id: str = "",
                            evidence: str = "", confirmed: bool = False) -> int:
        """A boolean or user ID alone is never authority to close a personal project."""
        from .. import life_matters as matters
        matters.ensure(state, self._now())
        item = matters.find(state, matter_id)
        if (not item or not matters.belongs_to(item, self._matter_persona_key(state))
                or not confirmed or not evidence or item["status"] in matters.CLOSED
                or (item.get("who") and item["who"] != str(user_id))):
            return 0
        self._stop_matter_work(state, matter_id)
        item.update(status="completed", progress=100, result=evidence[:500], completed_at=self._now())
        matters.history(item, "completed", evidence, self._now(), kind_of_evidence="confirmed_result")
        matters.sync(state)
        return 1

    def _decay_own_topics(self, state: WorldState, *, now: float) -> None:
        from .. import life_matters as matters
        matters.ensure(state, now)
        days = max(1, self.world.state_dynamics.own_topic_days)
        for item in state.life_matters:
            if not matters.belongs_to(item, self._matter_persona_key(state)):
                continue
            # Projects and promises persist until an explicit outcome; fleeting legacy ideas may fade.
            if (item.get("legacy") or item.get("kind") == "idea") and not item.get("steps") and item["status"] not in matters.CLOSED:
                if now - float(item.get("at") or now) >= days * 86400:
                    self._archive_unfinished_topic(state, item)
                    item["status"] = "expired"
                    matters.history(item, "expired", "搁置，尚未确认完成", now)
        matters.sync(state)

    def _archive_unfinished_topic(self, state: WorldState, item: dict[str, Any]) -> None:
        text = str(item.get("text") or "").strip()
        if text:
            self.memory.remember(session_id=str(item.get("session") or state.session_id), persona_id="", node_id=state.node_id,
                content=f"（后来搁置、尚未确认完成的打算）{text}", memory_type=INNER, weight=0.3,
                source="life_matter", context=json.dumps({"matter_id": item.get("id") or "",
                    "persona_key": item.get("persona_key") or "", "goal": text}, ensure_ascii=False),
                participants=[str(item["who"])] if item.get("who") else [])

    def _own_topic_block(self, state: WorldState, ctx: Any = None, *, autonomy: bool = False,
                         persona_key: str = "") -> str:
        from ..life_matters import context_block
        if not self.world.state_dynamics.own_topic_enabled:
            return ""
        return context_block(state, session_id=str(getattr(ctx, "session_id", "") or state.session_id),
                             user_id=str(getattr(ctx, "user_id", "") or ""), now=self._now(), persona_key=persona_key)

    def _said_when_text(self, stamp: float) -> str:
        """「（今天记的）/（3 天前记的）」这种尾巴。"""

        if stamp <= 0:
            return ""
        days = (self._now() - stamp) / 86400.0
        if days >= 1:
            return f"（{days:.0f} 天前记的）"
        return ""

    def _decay_heart_knots(self, state: WorldState, *, now: float) -> None:
        """心事随时间淡掉、到期放下（写进记忆）。"""

        config = self.world.state_dynamics
        if not bool(getattr(config, "heart_knot_enabled", True)):
            state.heart_knots = [item for item in state.heart_knots if not self._emotion_belongs(state, item)]
            return
        keep: list[dict[str, Any]] = []
        per_hour = max(0.0, float(getattr(config, "heart_knot_decay_per_hour", 0.0) or 0.0))
        for item in list(state.heart_knots or []):
            if not isinstance(item, dict):
                continue
            if not self._emotion_belongs(state, item):
                keep.append(item)
                continue
            last = float(item.get("at") or item.get("since") or now)
            hours = max(0.0, (float(now) - last) / 3600.0)
            strength = float(item.get("strength") or 0.0) - per_hour * hours
            until = float(item.get("until") or 0.0)
            expired = until > 0 and float(now) >= until
            if strength <= 0.02 or expired:
                text = str(item.get("text") or "").strip()
                if text:
                    self._archive_emotion(state, item, kind="heart_knot", content=f"（心里搁过的事）{text}", weight=.3)
                continue
            keep.append({**item, "strength": strength, "at": max(last, float(now))})
        state.heart_knots = keep

    def note_heart_knot(self, state: WorldState, text: str, about: str = "", *, session_id: str = "") -> None:
        """记下一件心事（她自己在回复里写的，或者整理时发现的）。"""

        config = self.world.state_dynamics
        if not bool(getattr(config, "heart_knot_enabled", True)):
            return
        body = " ".join(str(text or "").split())[:60]
        if not body:
            return
        now = self._now()
        max_days = max(0.0, float(getattr(config, "heart_knot_max_days", 5.0) or 5.0))
        items = [
            item
            for item in list(state.heart_knots or [])
            if (str(item.get("text") or "").strip() != body
                or str(item.get("about") or "") != str(about or "")
                or not self._emotion_visible(state, item, session_id))
        ]
        items.append(
            {
                "text": body,
                "session": self._emotion_origin(state, session_id),
                "persona_key": self._matter_persona_key(state) or "",
                "about": " ".join(str(about or "").split())[:20],
                "strength": 0.7,
                "since": now,
                "at": now,
                "until": now + max_days * 86400.0 if max_days > 0 else 0.0,
            }
        )
        limit = max(1, int(getattr(config, "heart_knot_max", 2) or 2))
        # 超了先扔最旧的（新的那件说明她现在更在意）
        own = [item for item in items if self._emotion_belongs(state, item)][-limit:]
        state.heart_knots = [item for item in items if not self._emotion_belongs(state, item)] + own
        state.heart_knots = state.heart_knots[-32:]

    def _person_places(self, state: WorldState, user_id: str) -> str:
        """这个人最近在哪个会话跟她说过话（"找他发到哪儿"）。"""

        activity = dict(getattr(state, "session_activity", None) or {})
        hits: list[str] = []
        for session_id, info in activity.items():
            if str((info or {}).get("user_id") or "") != str(user_id):
                continue
            hits.append(self.session_label(str(session_id), state))
        return "、".join(hits[:2])

    def _note_presence(self, state: WorldState, ctx: MessageContext) -> None:
        """记下「谁在说话」（调用方负责持锁）。"""

        now = self._now()
        self._note_images(state, ctx.image_urls)
        # 「好久没来的人回来了」：要在 touch_user 覆盖 last_seen **之前**算
        self._note_greet_candidate(state, ctx, now=now)
        state.touch_user(
            ctx.user_id,
            name=ctx.user_name,
            anchor="near:bot" if ctx.is_wake else "topic_center",
            max_tracked=self.world.limits.max_active_users_tracked,
        )
        # 画像：每条消息都更新"他是谁"（昵称、最后出现、聊了多少、来过几天），
        # 第一次见到就按默认关系建档（默认陌生人）。
        self.profiles.touch(ctx.session_id, ctx.user_id, ctx.user_name, now=now)
        # 「混脸熟」：他露个面就加一点好感（不用 @ 她，也不用等她回）
        self.profiles.note_contact(ctx.session_id, ctx.user_id, now=now)
        if ctx.is_wake or ctx.is_mentioned or ctx.is_private:
            # 「聊过」单独记一份：他直接跟她说话了，才算"陪过她"——
            # 想念看的是这个时间，不是"他有没有在群里露过面"
            self.profiles.note_talked(ctx.session_id, ctx.user_id, now=now)
        self._note_session_activity(state, ctx)
        # 旁观这条钩子也会收到每一句话：指令（``/mw …``）在这儿同样得挡住，
        # 不然它会从这条路溜进她的聊天记录——她就会一本正经地回一句"你怎么又敲这指令"。
        if self._passes_safety(ctx.text) and not self.is_plugin_command(ctx.text):
            recorded = state.note_chat(
                user_id=ctx.user_id,
                name=ctx.user_name,
                text=ctx.text,
                now=self._now(),
                keep=self.chat_history_limit(),
                images=ctx.chat_images,
                raw_text=ctx.raw_text or ("" if ctx.has_images or ctx.chat_images or ctx.has_videos else None),
                has_images=ctx.has_images,
                has_videos=ctx.has_videos,
                origin=ctx.session_id,
                at=ctx.at_targets,
                reply_to=ctx.reply_to,
                addressing=ctx.addressing,
                message_id=ctx.message_id,
                event_id=ctx.event_id,
            )
            self._tag_chat_origin(state, ctx.session_id, recorded, original_text=ctx.text)
            # 意图路由放行时交过来的事前判定：按事件身份盖到这一批对应的留档上
            self._stamp_router_view(state, ctx, recorded)
            if recorded is not None:
                self.note_peer_chat_input(ctx.session_id, recorded)
        state.last_user_activity_at = self._now()
        self.note_contact_response(state, ctx)

    def _note_images(self, state: WorldState, urls: list[str] | None) -> None:
        """记住自上次回复以来收到的图片。

        真正交给主模型看的张数由 ``context.image_max`` 决定（超出上限的旧图会被转成文字），
        但这里要多留几张：不留就没有"超出的那几张"可以转述，只能白白丢掉。
        """

        if not urls:
            return
        limit = max(1, int(self.world.context.image_max), PENDING_IMAGE_KEEP)
        seen = {str(item.get("url") or "") for item in state.pending_images}
        for url in urls:
            text = str(url or "").strip()
            if not text or text in seen:
                continue
            state.pending_images.append({"url": text, "at": self._now()})
            seen.add(text)
        if len(state.pending_images) > limit:
            state.pending_images = state.pending_images[-limit:]

    async def take_pending_images(self, session_id: str) -> list[str]:
        """取走并清空「自上次回复以来收到的图片」。"""

        if not self.is_enabled(session_id):
            return []
        async with self.session_state(session_id) as state:
            urls = [
                str(item.get("url") or "")
                for item in state.pending_images
                if str(item.get("url") or "")
            ]
            state.pending_images = []
            return urls

    def uncaptioned_images(self, urls: list[str]) -> list[str]:
        """这几张里哪些还没转述过（同一张图不再转述第二遍）。"""

        return [
            str(url)
            for url in urls
            if str(url).strip() and str(url) not in self._image_captioned
        ]

    def mark_images_captioned(self, urls: list[str]) -> None:
        """记下"这几张已经转述过了"。"""

        now = time.time()
        for url in urls:
            key = str(url or "").strip()
            if key:
                self._image_captioned[key] = now
        if len(self._image_captioned) > 400:
            # 只留最近的一批：早期那几张早就掉出聊天窗口了
            keep = sorted(self._image_captioned.items(), key=lambda item: item[1])[-200:]
            self._image_captioned = dict(keep)

    async def attach_image_captions(
        self, session_id: str, captions: dict[str, str]
    ) -> int:
        """把转述结果挂回"当初发了这张图的那条聊天记录"上，返回挂上了几条。

        给"没随这次请求交给主模型的旧图"用：图片本身留在留档里，
        但下一轮（以及更晚的压缩）只能读到文字，所以描述要落在它自己那一行上，
        而不是蹭在最新那条消息的注解里。
        """

        if not captions or not self.is_enabled(session_id):
            return 0
        attached = 0
        async with self.session_state(session_id) as state:
            for item in state.recent_chat:
                refs = [str(ref) for ref in (item.get("images") or []) if str(ref)]
                hits = [ref for ref in refs if ref in captions]
                if not hits:
                    continue
                stored = dict(item.get("captions") or {})
                for ref in hits:
                    text = readable_caption(str(captions.pop(ref) or ""))
                    if text:
                        # 这条消息里第几张图：跟「图1/图2」的编号对齐
                        index = refs.index(ref) + 1
                        label = f"第{index}张：" if len(refs) > 1 else ""
                        stored[ref] = f"{label}{text}"
                        attached += 1
                # 转述**单独存一份**，不拼进正文：这一轮真把图发给主模型时，
                # 渲染会跳过它（"已经转述过"的那份就不再重复念一遍）。
                item["captions"] = stored
        return attached

    async def note_vision(
        self, session_id: str, *, ok: bool, images: int, detail: str
    ) -> None:
        """记一条图片相关的事件：转述成功（写了什么）、失败（为什么）、或者直接交给主模型。"""

        if not self.is_enabled(session_id):
            return
        try:
            async with self.session_state(session_id) as state:
                echo = TickOutcome(session_id=session_id)
                await self._log_event(
                    state,
                    "vision",
                    {"ok": bool(ok), "images": int(images or 0), "detail": _clip_text(detail, 200)},
                    outcome=echo,
                )
                self._queue_echo(session_id, echo.debug_messages)
        except Exception as exc:  # 记日志失败不该影响回复
            self._log("debug", f"图片事件写入失败: {exc}")
