"""接管回复：本轮 batch 与三阶段执行。"""

from __future__ import annotations

from ..model_calls import model_operation

import asyncio
import time
import weakref
from hashlib import sha256
from functools import wraps
from typing import Any
from ..config_scope import session_scoped
from ..json_actions import parse_action_payload
from ..mood import keyword_signal
from ..state import (
    STATE_AWAKENING,
    STATE_DROWSY,
    STATE_IDLE,
    WorldState,
    chat_event_key,
)
from .constants import (
    INTERRUPT_STREAK_MAX,
    INTERRUPT_STREAK_WINDOW_SECONDS,
    MERGE_MAX_RESTARTS,
    MERGE_MAX_TEXTS,
    REPLY_GUARD_RETRIES,
    REPLY_INTERRUPTED,
    REPLY_MUTED,
)
from .models import (
    DialoguePlan,
    MessageContext,
    ReplyOutcome,
    TickOutcome,
)
from .util import (
    _clip_log,
    _clip_text,
)


def reply_work(fn):
    """Track real preparation separately from speculative incoming registration."""
    @wraps(fn)
    async def run(self, ctx, *args, **kwargs):
        scope = self.merge_scope(ctx.session_id)
        active = getattr(self, '_active_reply_tasks', None)
        if active is None:
            active = self._active_reply_tasks = {}
        tasks = active.setdefault(scope, set())
        task = asyncio.current_task()
        tasks.add(task)
        try:
            return await fn(self, ctx, *args, **kwargs)
        finally:
            tasks.discard(task)
            if not tasks:
                active.pop(scope, None)
    return run


class ReplyMixin:
    # ================= 接管回复（被 @ 时由本插件回复） =================

    def merge_scope(self, session_id: str) -> str:
        """合并/打断按"她"算，不按会话算。

        同一个会话组里的群和私聊是同一个她，所以一条在群里、一条在私聊也能
        互相合并 / 互相打断；不然"连说两句"永远各回一次。
        """

        return self.state_key(session_id)

    def register_incoming(
        self,
        session_id: str,
        text: str,
        *,
        identity: dict[str, Any] | None = None,
        expected_event: Any = None,
        expected_owner: asyncio.Task | None = None,
    ) -> dict[str, Any]:
        """记下"这条消息刚进来"。

        如果她正在为前一条调模型，这条可能会被**并进那一次回复**（窗口很短），
        调用方拿到这个记录后，排队轮到自己时要问一句 :meth:`was_absorbed`。

        ``identity``（可选）带这条消息的**事件身份**：``message_id`` / ``event_id``
        / ``user_id`` / ``user_name`` / ``raw_text`` / ``at_targets`` / ``reply_to``
        / ``addressing`` / ``images`` / ``wall_at``。有身份时同事件只登记一条，
        依据是身份而不是正文——正文一样的两条是用户真的说了两遍，不能被吞。
        """

        scope = self.merge_scope(session_id)
        queue = [
            item
            for item in self._pending_replies.get(scope, [])
            if not item.get("absorbed") and not item.get("picked")
        ]
        info = dict(identity or {})
        origin = str(session_id or "")
        message_id = str(info.get("message_id") or "").strip()
        event_id = str(info.get("event_id") or "").strip()
        event_key = chat_event_key(origin, message_id, event_id)
        text_value = str(text or "")
        now = time.monotonic()
        # 同一条消息被两个钩子（或同一个事件被记了两次）各记一次时只留一条：
        # 留两条的话提示词里会出现"对方把同一句话说了两遍"。
        for item in reversed(queue):
            if str(item.get("session") or "") != origin:
                continue
            item_key = self.event_key_of(item)
            if event_key:
                # 有身份：**只按身份**认同一个事件（正文一样也不当同一条）
                if item_key == event_key:
                    self._watch_reply_event(scope, item, expected_event, expected_owner)
                    return item
                continue
            if item_key:
                # 队列里那条有身份、这条没有：认不出来就别猜
                continue
            # 两样都没有（老路径）：退回到"同一个人、短时间内、内容基本一样"的有限兜底
            if str(item.get("user_id") or "") and str(info.get("user_id") or ""):
                if str(item.get("user_id")) != str(info.get("user_id")):
                    break
            if " ".join(str(item.get("text") or "").split()) != " ".join(
                text_value.split()
            ):
                break
            if now - float(item.get("at") or 0.0) <= self._merge_window_seconds():
                self._watch_reply_event(scope, item, expected_event, expected_owner)
                return item
            break
        seq = int(getattr(self, "_incoming_seq", 0) or 0) + 1
        self._incoming_seq = seq
        record = {
            # ``text`` 是"给模型看的正文"（注解后），老调用方都读这个键
            "text": text_value,
            # 注解前的原文：事件身份认的是消息本身，注解改不了它
            "raw_text": str(info.get("raw_text") or text_value),
            "model_text": str(info.get("model_text") or text_value),
            # 这条是从哪个会话来的：合并只认同一个会话的连发
            # （跨会话并成一条会在群里答私聊的话），跨会话靠"按她分锁"串行就够
            "session": origin,
            "origin": origin,
            "message_id": message_id,
            "event_id": event_id,
            "user_id": str(info.get("user_id") or ""),
            "user_name": str(info.get("user_name") or ""),
            # 到达顺序：batch 按它排（``at`` 是单调钟，只用来算窗口/安静期）
            "arrival_order": seq,
            "seq": seq,
            "wall_at": float(info.get("wall_at") or self._now()),
            "at_targets": list(info.get("at_targets") or []),
            "reply_to": dict(info.get("reply_to") or {}),
            "addressing": str(info.get("addressing") or ""),
            "images": list(info.get("images") or []),
            "at": now,
            "absorbed": False,
            "picked": False,
        }
        queue.append(record)
        self._pending_replies[scope] = queue[-8:]
        self._watch_reply_event(scope, record, expected_event, expected_owner)
        return record

    def _watch_reply_event(self, scope: str, record: dict, event: Any,
                           owner: asyncio.Task | None = None) -> None:
        """Weak observations stay outside serializable message/batch records."""
        if not hasattr(self, '_pending_reply_events'):
            self._pending_reply_events = {}
        live = {id(item) for item in self._pending_replies.get(scope, [])}
        watches = {key: ref for key, ref in self._pending_reply_events.get(scope, {}).items()
                   if key in live}
        if event is not None:
            try:
                watches[id(record)] = weakref.ref(event)
            except TypeError:
                pass  # Some third-party event wrappers cannot be weakly observed.
        self._pending_reply_events[scope] = watches
        owners = getattr(self, '_pending_reply_owners', None)
        if owners is None:
            owners = self._pending_reply_owners = {}
        owners[scope] = {key: task for key, task in owners.get(scope, {}).items() if key in live}
        key = id(record)
        if owner is not None and owners[scope].get(key) is not owner:
            owners[scope][key] = owner
            record.pop('dispatch_done', None)

            def finished(completed: asyncio.Task) -> None:
                if owners.get(scope, {}).get(key) is completed:
                    owners[scope].pop(key, None)
                    if not owners[scope]:
                        owners.pop(scope, None)
                    record['dispatch_done'] = True
                    # Revisit the current scene, not the original reply draft.
                    target = str(record.get('session') or '')
                    if target in getattr(self, '_participation_pending', {}).get(scope, {}):
                        self.queue_participation(target)

            owner.add_done_callback(finished)

    def _reply_event_open(self, scope: str, record: dict) -> bool:
        owner = getattr(self, '_pending_reply_owners', {}).get(scope, {}).get(id(record))
        if record.get('dispatch_done') or (owner is not None and owner.done()):
            return False
        ref = getattr(self, '_pending_reply_events', {}).get(scope, {}).get(id(record))
        if ref is None:
            return True
        event = ref()
        if event is None:
            return False
        stopped = getattr(event, 'is_stopped', None)
        return not (callable(stopped) and stopped())

    def refresh_incoming(
        self,
        record: dict[str, Any] | None,
        text: str,
        *,
        identity: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """把监听侧登记的那条记录补全成 LLM hook 看到的样子。

        预处理（图片说明、转发摘要、@ 注解）会改正文，所以这里更新 ``text`` /
        ``model_text`` / ``raw_text`` 和身份里**能补的**字段；``message_id`` /
        ``event_id`` / ``arrival_order`` / ``at`` **只增不改**——事件身份绝不能
        因为正文被注解过就变成另一条消息。
        """

        if not isinstance(record, dict):
            return record
        info = dict(identity or {})
        value = str(text or "")
        if value:
            record["text"] = value
            record["model_text"] = value
        raw = str(info.get("raw_text") or "")
        if raw:
            record["raw_text"] = raw
        for field_name in ("message_id", "event_id", "user_id", "user_name"):
            if not str(record.get(field_name) or "").strip() and str(
                info.get(field_name) or ""
            ).strip():
                record[field_name] = str(info[field_name]).strip()
        for field_name in ("at_targets", "reply_to", "addressing", "images"):
            incoming = info.get(field_name)
            if not record.get(field_name) and incoming:
                record[field_name] = (
                    dict(incoming) if isinstance(incoming, dict) else list(incoming)
                )
        return record

    @staticmethod
    def event_key_of(record: dict[str, Any] | None) -> str:
        """一条待回记录 / 留档条目的事件身份（两样 id 都没有时是空串）。"""

        if not isinstance(record, dict):
            return ""
        stored = str(record.get("event_key") or "").strip()
        if stored:
            return stored
        return chat_event_key(
            str(record.get("origin") or record.get("session") or ""),
            str(record.get("message_id") or ""),
            str(record.get("event_id") or ""),
        )

    @staticmethod
    def was_absorbed(record: dict[str, Any] | None) -> bool:
        """这条消息是不是已经并进别人的那次回复里了（那它自己就不用再回一遍）。"""

        return bool(record and record.get("absorbed"))

    def _take_merge_texts(
        self,
        session_id: str,
        started_at: float,
        *,
        exclude_keys: set[str] | None = None,
        exclude_text: str = "",
        window: float | None = None,
        limit: int = MERGE_MAX_TEXTS,
        same_user: str = "",
    ) -> list[dict[str, Any]]:
        """调用期间到达、且还在窗口内的新消息（会被并进这次请求）。

        ``exclude_keys``：事件身份键——**这一轮正在答的那条（以及已经进了 batch 的）
        按身份排除**，不再拿正文比（触发消息被注解过、正文和 ``ctx.text`` 对不上时，
        拿正文比会把它当成"调用期间新来的"，于是同一句话被答两遍）。

        返回的记录会标 ``picked=True``（同一轮里不会再被捞第二次）。**最多返回
        ``limit`` 条**：超出的留在队列里（``picked`` / ``absorbed`` 都不动），
        各自那一轮的钩子会照常把它们当触发消息处理，不会被丢掉。

        ``same_user``：这一轮触发消息的发送者。给了就**只合并同一个人的连发**——
        不同人几乎同时 @ 她时，各回各的，不把第二个人的话并进第一轮（更不能让
        那条事件被吸收后一声不吭）。候选没有 ``user_id`` 时也跳过：认不出是谁，
        就不能赌是同一个人。
        """

        style = getattr(self.world, "reply_style", None)
        if not bool(getattr(style, "merge_continuous_messages", False)):
            return []
        scope = self.merge_scope(session_id)
        budget = max(0, int(limit))
        if budget <= 0:
            return []
        window_seconds = float(self._merge_window_seconds() if window is None else window)
        queue = self._pending_replies.get(scope) or []
        picked: list[dict[str, Any]] = []
        keys = {str(item) for item in (exclude_keys or set()) if str(item)}
        current = " ".join(str(exclude_text or "").split())
        origin = str(session_id or "")
        trigger_user = str(same_user or "").strip()
        for item in queue:
            if len(picked) >= budget:
                break
            if item.get("absorbed") or item.get("picked"):
                continue
            if not self._reply_event_open(scope, item):
                continue
            if str(item.get("session") or "") != origin:
                continue
            if trigger_user and str(item.get("user_id") or "").strip() != trigger_user:
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            item_key = self.event_key_of(item)
            if item_key:
                if item_key in keys:
                    # 这一轮正在答的就是它：别把它当成"调用期间新来的"（否则她会答两遍）
                    item["picked"] = True
                    continue
            elif current and " ".join(text.split()) == current:
                # 老路径（两边都没身份）才拿正文兜底
                item["picked"] = True
                continue
            if abs(float(item.get("at") or 0.0) - float(started_at)) > window_seconds:
                continue
            item["picked"] = True  # 只可能被并进一次，别被下一轮又捞出来
            picked.append(item)
        self._pending_replies[scope] = [
            item for item in queue if not item.get("absorbed")
        ]
        return picked

    def _latest_incoming_at(self, session_id: str, same_user: str = "") -> float:
        """这个"她"最近一条还在等着的消息是什么时候到的（安静期用）。

        ``same_user``：只看这个人的消息。别人在群里打字不该拖着她的回复等，
        更不该把别人的消息算作"他还有下文"。
        """

        queue = self._pending_replies.get(self.merge_scope(session_id)) or []
        user = str(same_user or "").strip()
        stamps = [
            float(item.get("at") or 0.0)
            for item in queue
            if not item.get("absorbed")
            and not item.get("picked")
            and self._reply_event_open(self.merge_scope(session_id), item)
            and str(item.get("session") or "") == str(session_id or "")
            and (not user or str(item.get("user_id") or "").strip() == user)
        ]
        return max(stamps) if stamps else 0.0

    def _release_records(
        self, session_id: str, records: list[dict[str, Any]]
    ) -> None:
        """这一轮作废了：把并进来的那几条恢复成"还没回过"。

        被作废的那次生成等于没发生，所以它带上的消息必须还给下一次接管——
        否则那几条会既没被回过、又谁都不再管。
        """

        scope = self.merge_scope(session_id)
        queue = [
            item
            for item in self._pending_replies.get(scope, [])
            if not item.get("absorbed") and not item.get("picked")
        ]
        for record in records:
            if not isinstance(record, dict):
                continue
            record["absorbed"] = False
            record["picked"] = False
            if not any(item is record for item in queue):
                queue.append(record)
        queue.sort(key=lambda item: float(item.get("at") or 0.0))
        self._pending_replies[scope] = queue[-8:]

    # ---- 本轮 batch：唯一的数据源（排序 / 去重 / 渲染 / 释放都看它） ----

    @staticmethod
    def _record_text(record: dict[str, Any] | None) -> str:
        """一条待回记录"给模型看的正文"（注解后的那份）。"""

        if not isinstance(record, dict):
            return ""
        return str(record.get("model_text") or record.get("text") or "")

    def _sort_reply_batch(
        self, records: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """batch 按**真实到达顺序**排（同一事件只留一条）。

        到达顺序用 ``arrival_order``（登记时递增的内部序号）；老记录没有就退回
        ``at``（单调钟）。同一条记录被两个钩子各记一次时，这里按事件身份只留一份。
        """

        ordered: list[dict[str, Any]] = []
        seen: set[str] = set()
        seen_ids: set[int] = set()
        for record in records:
            if not isinstance(record, dict):
                continue
            if id(record) in seen_ids:
                continue
            key = self.event_key_of(record)
            if key:
                if key in seen:
                    continue
                seen.add(key)
            seen_ids.add(id(record))
            ordered.append(record)
        ordered.sort(
            key=lambda item: (
                int(item.get("arrival_order") or 0),
                float(item.get("at") or 0.0),
            )
        )
        return ordered

    def _start_reply_batch(self, ctx: MessageContext) -> list[dict[str, Any]]:
        """这一轮的触发记录：优先拿监听侧登记的那条（带完整身份）。

        拿不到（没经过旁观钩子、或队列里已经被吸收/清掉）就按 ``ctx`` 现场合成
        一条——合成记录同样带上身份字段，只是不进待回队列。合成的那条**排在最前**
        （``arrival_order`` 取 0）：它是这一轮的起点，而"找不到"最常见的原因就是
        它被待回队列的长度上限挤掉了——挤掉它的都是比它晚到的。
        """

        scope = self.merge_scope(ctx.session_id)
        key = ctx.event_key
        record: dict[str, Any] | None = None
        if key:
            for item in self._pending_replies.get(scope, []):
                if item.get("absorbed"):
                    continue
                if self.event_key_of(item) == key:
                    record = item
                    break
        if record is None:
            record = {
                "text": str(ctx.text or ""),
                "raw_text": str(ctx.raw_text or ctx.text or ""),
                "model_text": str(ctx.text or ""),
                "session": str(ctx.session_id or ""),
                "origin": str(ctx.session_id or ""),
                "message_id": str(ctx.message_id or ""),
                "event_id": str(ctx.event_id or ""),
                "user_id": str(ctx.user_id or ""),
                "user_name": str(ctx.user_name or ""),
                # 0 = 「这一轮的第一条」：合成记录没有真实到达序号，见上面的说明
                "arrival_order": 0,
                "seq": 0,
                "wall_at": self._now(),
                "at_targets": list(ctx.at_targets or []),
                "reply_to": dict(ctx.reply_to or {}),
                "addressing": str(ctx.addressing or ""),
                "images": [*(ctx.chat_images or [])],
                "at": 0.0,
                # 触发消息本身是这一轮的主角：不放进待回队列，只进 batch
                "absorbed": False,
                "picked": False,
                "synthetic": True,
            }
        # 这条是触发者：渲染时它的名字/身份决定"这句是冲谁说的"那一层
        record["trigger"] = True
        # 意图路由的事前判定跟着触发者走（留档那条由 _note_presence 盖，
        # 待回队列这条在这儿盖，渲染时两边都看得到）
        record.update(self._router_row_fields(self._router_row_for(ctx)))
        return [record]

    def reply_batch_entries(
        self,
        batch: list[dict[str, Any]],
        *,
        chat_rows: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """把 batch 转成渲染用的结构化条目（``build_reply_user_prompt`` 的入参）。

        每条都带 ``name`` / ``user_id`` / ``text`` / ``at`` / ``origin`` 和
        ``at_targets`` / ``reply_to`` / ``addressing`` ——渲染器和背景共用同一套
        `「- [HH:MM] 名字(id): 正文」` 输出与分组规则。

        ``chat_rows``：本会话的留档（``state.recent_chat``）。一条消息带的图和它的
        图转述都存在**留档那一行**上（附件范围按留档算、转述也挂回它自己那条），
        所以本轮 batch 里同事件的那几条**借留档的图和转述**——同一个事件只在一处
        存这两样数据。给不出留档时（老调用方）就只用记录自带的。
        """

        stored: dict[str, dict[str, Any]] = {}
        for row in chat_rows or []:
            if not isinstance(row, dict):
                continue
            row_key = self.event_key_of(row)
            if row_key:
                stored.setdefault(row_key, row)
        entries: list[dict[str, Any]] = []
        fallback_at = self._now()
        for record in batch:
            if not isinstance(record, dict):
                continue
            at = float(record.get("wall_at") or 0.0) or fallback_at
            images = [str(ref) for ref in (record.get("images") or []) if str(ref)]
            captions = {
                str(ref): str(value)
                for ref, value in dict(record.get("captions") or {}).items()
            }
            row = stored.get(self.event_key_of(record)) if stored else None
            if row is not None:
                if not images:
                    images = [str(ref) for ref in (row.get("images") or []) if str(ref)]
                if not captions:
                    captions = {
                        str(ref): str(value)
                        for ref, value in dict(row.get("captions") or {}).items()
                    }
            entries.append(
                {
                    "name": str(record.get("user_name") or ""),
                    "user_id": str(record.get("user_id") or ""),
                    "text": self._record_text(record),
                    "raw_text": str(record.get("raw_text") or ""),
                    "at": at,
                    "origin": str(
                        record.get("origin") or record.get("session") or ""
                    ),
                    "at_targets": list(record.get("at_targets") or []),
                    "reply_to": dict(record.get("reply_to") or {}),
                    "addressing": str(record.get("addressing") or ""),
                    # 意图路由的事前判定（没装路由 / 那条没判定就没有这几个键）：
                    # 待回队列里那几条是**判定之前**登记的，自己没这份数据，就借留档
                    # 那一份——`_stamp_router_view` 在触发那条到达时已经把整批的判定
                    # 补盖到留档上了（没有留档时才真的没有）。
                    **(
                        self._router_row_fields(record)
                        or self._router_row_fields(row)
                    ),
                    "images": list(images),
                    "captions": dict(captions),
                    "is_trigger": bool(record.get("trigger")),
                    "event_key": self.event_key_of(record),
                }
            )
        return entries

    async def _wait_for_quiet(self, session_id: str, *, same_user: str = "") -> None:
        """安静期：先等几秒看对方还有没有下文；有新消息就重新计时（有硬顶）。

        这是"像真人打字"的核心——连发两句她会听完一起回，而不是答一句、再答一句。
        只算**同一个会话**里的话：群里说一句、私聊里说一句是两件事，
        并成一条会让她在群里答私聊的问题（跨会话只靠"按她分锁"串行）。
        也只算**同一个人的话**：别人在群里打字不该拖着这一条等。
        """

        style = getattr(self.world, "reply_style", None)
        if not bool(getattr(style, "merge_continuous_messages", False)):
            return
        window = float(getattr(style, "merge_wait_seconds", 0.0) or 0.0)
        if window <= 0:
            return
        cap = float(getattr(style, "merge_wait_max_seconds", 0.0) or 0.0)
        deadline = time.monotonic() + (cap if cap > 0 else window)
        while True:
            latest = self._latest_incoming_at(session_id, same_user)
            now = time.monotonic()
            if latest <= 0 or (now - latest) >= window or now >= deadline:
                return
            await asyncio.sleep(min(0.25, max(0.05, latest + window - now)))

    def _interrupt_allowed(self, session_id: str) -> bool:
        """连着被打断太多次就先让她说完，免得刷屏的人让她永远开不了口。"""

        scope = self.merge_scope(session_id)
        count, at = self._interrupt_streak.get(scope, (0, 0.0))
        if time.monotonic() - float(at or 0.0) > INTERRUPT_STREAK_WINDOW_SECONDS:
            return True
        return count < INTERRUPT_STREAK_MAX

    def _note_interrupt(self, session_id: str) -> None:
        """记一次打断（连着打断的计数）。"""

        scope = self.merge_scope(session_id)
        count, at = self._interrupt_streak.get(scope, (0, 0.0))
        now = time.monotonic()
        if now - float(at or 0.0) > INTERRUPT_STREAK_WINDOW_SECONDS:
            count = 0
        self._interrupt_streak[scope] = (int(count) + 1, now)

    @session_scoped
    @model_operation("chat", "收到消息，准备对话回复")
    @reply_work
    async def handle_reply(
        self,
        ctx: MessageContext,
        *,
        history: list[dict[str, Any]] | None = None,
        inject_only: bool = False,
    ) -> ReplyOutcome | DialoguePlan:
        """接管一次回复：自己调大模型 → 解析 reasoning/actions → 执行 → 返回要发的话。

        ``ok=False`` 表示本插件没能完成这次回复（没有模型、调用失败、模型没给出任何动作），
        调用方**不该**换人来答：这一条就不回了，只在日志页留一条记录——不能让她中途换成
        主人格的腔调，宁可少回一条。

        ``inject_only=True``：**不调自己的模型、不执行任何动作**，只把这一轮该看的
        提示词（人设 + 世界 + 场景 + 状态 + 记忆）装进 :class:`DialoguePlan` 交回调用方，
        由调用方注入到宿主自己的请求里。宿主照旧跑原生工具循环、照旧由原生管线发送；
        她说什么、用了什么工具，由调用方通过观察钩子写回世界（见 ``reflect_turn``）。

        注意：调用方应当先调用 ``handle_incoming()``（记录存在感/数值/互动记忆），
        本方法只负责"读状态 → 生成回复 → 执行动作"。

        状态记录、prompt 组装在锁内完成；大模型调用放在锁外，避免拖住世界时钟。
        """

        if not self.is_enabled(ctx.session_id):
            return ReplyOutcome(ok=False, error="会话未启用")
        if not self._passes_safety(ctx.text):
            return ReplyOutcome(ok=False, error="命中内容安全屏蔽词")

        # 压测护栏：本小时被动回复额度用完了就接管但静默（不落回主人格）
        async with self.session_state(ctx.session_id) as state:
            allowed = self._passive_reply_allowed(state)
            if not allowed:
                used, limit = self.reply_quota(state)
                await self._log_event(
                    state,
                    "engagement",
                    {"reason": "被动回复触顶，这条静默", "used": used, "limit": limit},
                    place=ctx.session_id,
                )
        if not allowed:
            return ReplyOutcome(ok=False, error=REPLY_MUTED)

        # 她正在等群友拿主意：这条回应先给事件线看一眼。
        # 能消化（是冲着这件事说的）就由事件线走完剩下的流程；完全无关就照常回复。
        try:
            consumed = await self._consume_help_reply(ctx)
        except Exception as exc:
            self._log("warning", f"处理求助回应失败：{exc}")
            consumed = None
        if consumed is not None:
            return consumed

        # --- 第一阶段：记录消息影响 + 组装提示词（持锁，很快） ---
        #
        # 「合并重启」：她还在调模型时又来了新消息，就把几条并成一次请求重新发起。
        # 只在还没开始发送的窗口里做（最多 MERGE_MAX_RESTARTS 次），所以不会有"说了半句又改口"。
        # 先等一个「安静期」：对方连着打字就等他打完，一起回（新消息会刷新计时）
        wait_started = time.monotonic()
        await self._wait_for_quiet(ctx.session_id, same_user=str(ctx.user_id or ""))
        # 这一轮要回的就是一个**显式 batch**：触发她的那条 + 安静期里并进来的几条。
        # batch 是这一轮唯一的数据源（排序 / 去重 / 渲染 / 释放都看它），
        # 后面合并重启也只是往同一个 batch 里追加。
        batch = self._start_reply_batch(ctx)
        ctx_key = ctx.event_key
        early = self._take_merge_texts(
            ctx.session_id,
            wait_started,
            exclude_keys={ctx_key} if ctx_key else set(),
            exclude_text="" if ctx_key else ctx.text,
            window=max(self._merge_window_seconds(), time.monotonic() - wait_started + 1.0),
            limit=max(0, MERGE_MAX_TEXTS - len(batch)),
            same_user=str(ctx.user_id or ""),
        )
        # 按真实到达顺序排：她要回的是"对方连着说的这几句"，顺序反了模型会读成两轮对话
        batch = self._sort_reply_batch([*batch, *early])
        for record in batch:
            record["absorbed"] = True
            record["picked"] = True
        if self.is_event_command(ctx.text):
            # 「/mw event h」这种指令是开关，不是他说的话：原样交给她，她会一本正经地
            # 回一句"你怎么又敲这指令"。换成一句世界内的话，让她按"他刚把这一场开了起来"往下接。
            command_record = next(
                (record for record in batch if record.get("trigger")), None
            )
            if command_record is not None:
                command_record["text"] = "（他把这一场开了起来）"
                command_record["model_text"] = command_record["text"]
                batch = [command_record]
        texts = [self._record_text(record) for record in batch]
        async with self.session_state(ctx.session_id) as state:
            query_records = [*self.chat_context_for_reply(state, ctx, exclude_entries=self.reply_batch_entries(batch)), *batch]
        if (self.world.memory.semantic_enabled and self.world.memory.automatic_recall != "manual"
                and self.world.memory.recall_max_chars > 0):
            life_persona = (await self._journal_persona(ctx.session_id))[1]
            for query in self.memory.participant_queries(query_records, ctx.session_id).values():
                await self.memory.semantic.prepare(ctx.session_id, ctx.persona_id, query, automatic=True,
                                                   life_persona_key=life_persona)
        # 这一轮实际回了哪几条：等真的发出去了再拿去推水位线
        self._remember_replied_batch(batch)
        # handle_incoming 攒下的临时提示（冷启动这类它自己算不出来的）：这一轮用完就丢
        incoming_notes = self._incoming_notes.pop(ctx.session_id, [])
        restarts = 0
        guard_strikes = 0
        wake_note_kept = ""
        raw: str | None = None
        blocked_actions: set[str] = set()
        for attempt in range(MERGE_MAX_RESTARTS + 1 + REPLY_GUARD_RETRIES):
            async with self.session_state(ctx.session_id) as state:
                node = self.node(state.node_id) or self.node(self.default_node_id())
                # 「刚被叫醒」之类的临时提示：接管模式也要带上，否则她会以完全清醒的状态回话
                wake_note = wake_note_kept or self._take_wake_note(state)
                wake_note_kept = wake_note
                extra_notes = [
                    note
                    for note in [
                        *incoming_notes,
                        wake_note,
                        *self.sleep_notes(state),
                    ]
                    if note
                ]
                # 同一句只留一份：wake note 与临睡期提示两边都可能提到
                extra_notes = list(dict.fromkeys(extra_notes))
                density = self.speech_density_hint(state, direct=True)
                if density:
                    extra_notes.append(density)
                # 「这一批要一起回应」那句话跟在消息本身后面（core/prompt.py 拼
                # user prompt 时写），别放在这儿：这些 notes 渲染在提示词最末尾，
                # 离那一批隔了几十行，"这几句"就没有指代对象了。
                extra_notes = extra_notes or None
                missing = self.extra_reminders(state, ctx=ctx, include_life=False)
                if missing:
                    extra_notes = [*(extra_notes or []), missing]
                remember_note = self.remember_hint(" ".join(texts), ctx=ctx)
                if remember_note:
                    extra_notes = [*(extra_notes or []), remember_note]
                layer = self._extension_prompt(state, ctx.session_id)
                if layer:
                    extra_notes = [*(extra_notes or []), layer]
                # 「你还不知道他的事」：只有知道是谁在说话时才算得出来
                ask = self._ask_about_hint(state, ctx)
                if ask:
                    extra_notes = [*(extra_notes or []), ask]
                self.mark_context_sources(state, ctx, batch)
                recall_records = [*self.chat_context_for_reply(state, ctx, exclude_entries=self.reply_batch_entries(batch)), *batch]
                vocabulary = self.vocabulary_block(ctx.session_id, recall_records)
                if vocabulary:
                    extra_notes = [*(extra_notes or []), vocabulary]
                request = self.context_request(state, ctx.session_id, task="reply", text="\n".join(texts),
                    focus=ctx.user_id, persona=ctx.persona_id)
                persona_text = await self._persona_text(state.session_id)
                request.life_persona = sha256(persona_text.encode("utf-8")).hexdigest()[:24]
                memories = self.context_memories(state, request, recall_records)
                background_chat = self.context_chat(state, request,
                    self.chat_context_for_reply(state, ctx, exclude_entries=self.reply_batch_entries(batch)))
                profile_context = self.context_profiles(state, request)
                # 有人正在跟她说话：话太密时只提醒别刷屏，不把她按成哑巴
                _cell, say_limit, style_text = self.style_for(
                    state, ctx.session_id, direct=True
                )
                # 配额用尽的动作：这一轮既不列进提示词，也不接受模型写它
                blocked_actions = self._hidden_actions(state, ctx.session_id)
                runtime_notes = await self.runtime_notes(state.session_id)
                samples = self.voice_sample_lines(
                    state, ctx.session_id, signal=self.tone_signal(state, ctx.text)
                )
                if inject_only:
                    # 注入模式：只拼提示词，不列工具、不列动作 type、不要 JSON。
                    system_prompt, runtime_part = (
                        self.prompts.build_dialogue_prompt(
                            persona_text=persona_text,
                            state=state,
                            node=node,
                            memories=memories,
                            engagement_hint=self.engagement.hint(state),
                            recent_chat=background_chat,
                            # 别家插件写进请求的上下文由宿主原样带着走（见 main.py
                            # 注入时保留 system_prompt 与 extra_user_content_parts），
                            # 这里再抄一份只会让同一段设定出现两遍。
                            other_context="",
                            extra_notes=extra_notes,
                            style_block=style_text,
                            session_directory=self.session_directory(state),
                            current_session=ctx.session_id,
                            session_labels=self.session_labels(state),
                            chat_images=dict(ctx.image_marks or {}),
                            profile_text=profile_context,
                            context_request=request,
                            samples=samples,
                            **runtime_notes,
                        )
                    )
                    user_prompt = self.prompts.build_reply_user_prompt(
                        entries=self.reply_batch_entries(
                            batch, chat_rows=list(state.recent_chat)
                        ),
                        is_private=ctx.is_private,
                        addressing=self.reply_addressing(state, ctx),
                        from_session=self.reply_place(state, ctx.session_id),
                        image_marks=dict(ctx.image_marks or {}),
                    )
                else:
                    system_prompt = self.prompts.build_autonomous_system_prompt(
                        persona_text=persona_text,
                        state=state,
                        node=node,
                        available_tools=self.available_tools(),
                        memories=memories,
                        engagement_hint=self.engagement.hint(state),
                        recent_chat=background_chat,
                        other_context=ctx.other_context,
                        extra_notes=extra_notes,
                        max_messages=say_limit,
                        reasoning=bool(self.world.reasoning_enabled),
                        style_block=style_text,
                        hidden_actions=blocked_actions,
                        session_directory=self.session_directory(state),
                        current_session=ctx.session_id,
                        session_labels=self.session_labels(state),
                        chat_images=dict(ctx.image_marks or {}),
                        profile_text=profile_context,
                        context_request=request,
                        samples=samples,
                        wants_touch=self.extensions.wants("touch"),
                        json_fields=self.extensions.json_fields(),
                        **runtime_notes,
                    )
                    user_prompt = self.prompts.build_reply_user_prompt(
                        entries=self.reply_batch_entries(
                            batch, chat_rows=list(state.recent_chat)
                        ),
                        is_private=ctx.is_private,
                        addressing=self.reply_addressing(state, ctx),
                        from_session=self.reply_place(state, ctx.session_id),
                        image_marks=dict(ctx.image_marks or {}),
                    )
                node_id = state.node_id
                self.record_context_selection(request)
                # 注入模式先记准备阶段；main 完成宿主请求注入后补成该钩子的快照。
                prompt_snapshot = self._remember_prompt_snapshot(
                    session_id=ctx.session_id,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    contexts=history,
                    batch=batch,
                    chat_rows=list(state.recent_chat),
                    context_selection=request.trace,
                    runtime_part=runtime_part if inject_only else "",
                    stage="engine_prepared" if inject_only else "legacy_before_model",
                    image_urls=ctx.image_urls,
                )
                if inject_only:
                    # 注入模式到此为止：剩下的交给宿主的原生工具循环与发送管线。
                    return DialoguePlan(
                        ok=True,
                        recalled_ids=[item.id for item in memories],
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        runtime_part=runtime_part,
                        prompt_snapshot=prompt_snapshot,
                        batch=list(batch),
                        texts=list(texts),
                        say_limit=int(say_limit or 0),
                    )

            # --- 第二阶段：调用大模型（不持锁） ---
            started = time.monotonic()
            raw = await self._ask_llm(
                ctx.session_id,
                system_prompt,
                user_prompt,
                contexts=history,
                image_urls=list(ctx.image_urls) or None,
            )
            if raw is None:
                return ReplyOutcome(ok=False, error="大模型调用失败")
            # 扩展可以判"这一轮是废的"（模型丢了一句"我不能生成这类内容"）：
            # 不解析、不执行、不发送，直接重问一次；问满次数就这一轮不出声。
            if self.extensions.reply_is_bad(state, raw):
                guard_strikes += 1
                self._log(
                    "debug",
                    f"这一轮被扩展判为拒答/废输出，重新生成（第 {guard_strikes} 次）",
                )
                if guard_strikes <= REPLY_GUARD_RETRIES:
                    continue
                return ReplyOutcome(
                    ok=False,
                    error=REPLY_MUTED,
                    warnings=["模型拒答，重试后仍失败：这一轮不出声"],
                )
            if attempt >= MERGE_MAX_RESTARTS or len(batch) >= MERGE_MAX_TEXTS:
                break
            merged = self._take_merge_texts(
                ctx.session_id,
                started,
                exclude_keys={
                    key for key in (self.event_key_of(item) for item in batch) if key
                },
                exclude_text="" if ctx_key else ctx.text,
                limit=max(0, MERGE_MAX_TEXTS - len(batch)),
                same_user=str(ctx.user_id or ""),
            )
            if not merged:
                break
            if self._interrupt_pending() and self._interrupt_allowed(ctx.session_id):
                # 打断：这一轮生成直接作废，让新来的那条走自己的回复管线。
                # 水位线不推进（见 mark_chat_replied 的调用点），所以新那一轮
                # 仍然看得到刚才那几条——上一次回复等于没触发过。
                self._release_records(ctx.session_id, [*batch, *merged])
                self._log(
                    "debug",
                    f"又来了一条消息，这次生成作废（共 {len(batch) + len(merged)} 条待回）",
                )
                self._note_interrupt(ctx.session_id)
                return ReplyOutcome(
                    ok=False,
                    error=REPLY_INTERRUPTED,
                    debug_messages=self.take_pending_echo(ctx.session_id),
                )
            restarts += 1
            # merge 回来的都是"刚到的"：并进**同一个 batch**（按到达顺序重排），
            # 不再维护一条脱离身份的字符串列表
            batch = self._sort_reply_batch([*batch, *merged])
            for record in merged:
                record["absorbed"] = True
                record["picked"] = True
            texts = [self._record_text(record) for record in batch]
            # 合并重启会改变这一轮的范围：重记一遍（后面提交水位线时用它）
            self._remember_replied_batch(batch)
            self._log(
                "debug",
                f"又来了一条消息，合并成一次回复重新发起（共 {len(batch)} 条）",
            )
        if raw is None:
            return ReplyOutcome(ok=False, error="大模型调用失败")

        parsed = parse_action_payload(
            raw,
            available_actions=self._parseable_action_ids(
                node_id, blocked=blocked_actions
            ),
            valid_nodes=set(self.world.node_map()),
            max_actions=self.world.limits.max_actions_per_message,
            max_messages=say_limit,
            json_fields=self.extensions.json_fields(),
        )
        # 空内容的 say 直接丢掉：模型没想好要说什么时不该发一条空气泡
        actions = [
            item
            for item in parsed.actions
            if not (item.type == "say" and not item.messages)
        ]
        if not actions:
            return ReplyOutcome(
                ok=False,
                reasoning=parsed.reasoning,
                warnings=parsed.warnings,
                error="模型没有给出任何动作",
            )

        # --- 第三阶段：执行动作（重新持锁） ---
        outcome = TickOutcome(session_id=ctx.session_id, speech_kind="reply")
        # 这一轮是在哪儿发生的：日志里要标出来（她的存档与日志挂在组代表会话名下）
        outcome.place = ctx.session_id
        phase3_started = time.monotonic()
        async with self.session_state(ctx.session_id) as state:
            echo_marker = await self._event_marker(state)
            node = self.node(state.node_id) or self.node(self.default_node_id())
            state.note_reasoning(parsed.reasoning, source="reply", session_id=ctx.session_id,
                                 persona_key=self._matter_persona_key(state) or "")
            # 这一轮她对**当前说话人**的好感变化（模型自己判断，代码再削一次上限）
            if ctx.user_id:
                applied = self.profiles.apply_reply_affinity(
                    ctx.session_id,
                    ctx.user_id,
                    parsed.affinity_delta,
                    reason=str((parsed.reasoning or {}).get("intent") or "聊了这一轮")[:60],
                    now=self._now(),
                )
                if abs(applied) > 1e-9:
                    outcome.notes.append(f"好感 {applied:+.2f}")
            # 被吵醒的迷糊窗口里只准说话：不然她一边"半睡半醒"一边出门逛街，看着很假
            if self._startled_active(state):
                allowed = {"say", "think"}
                kept = [item for item in actions if item.type in allowed]
                dropped = [item.type for item in actions if item.type not in allowed]
                if dropped:
                    parsed.warnings.append(
                        "被吵醒的这一轮只让她说话 / 想事情，已丢掉："
                        + "、".join(dict.fromkeys(dropped))
                    )
                actions = kept
                if not actions:
                    return ReplyOutcome(
                        ok=False,
                        reasoning=parsed.reasoning,
                        warnings=parsed.warnings,
                        error="被吵醒的这一轮只允许说话",
                    )
            # 对方明确要求停下时，先把她的动作/安排停掉，再执行这一轮的动作
            await self.apply_cancel(state, parsed.cancel, ctx.text, outcome)
            await self._execute_actions(
                state, node, outcome, actions, depth=0, autonomous=False
            )
            # 「只要还没发出去就能打断」：动作这一段可能花很久，期间来的新消息
            # 仍然可以把这一轮作废——注意这时她的话还没写进留档、更没发出去。
            if restarts < MERGE_MAX_RESTARTS and not outcome.live_messages:
                late = self._take_merge_texts(
                    ctx.session_id,
                    phase3_started,
                    exclude_keys={
                        key for key in (self.event_key_of(item) for item in batch) if key
                    },
                    exclude_text="" if ctx_key else ctx.text,
                    limit=max(0, MERGE_MAX_TEXTS - len(batch)),
                    same_user=str(ctx.user_id or ""),
                )
                if (
                    late
                    and self._interrupt_pending()
                    and self._interrupt_allowed(ctx.session_id)
                ):
                    self._release_records(ctx.session_id, [*batch, *late])
                    self._log(
                        "debug",
                        f"这一轮还没发出去，被新消息打断（共 {len(late)} 条待回）",
                    )
                    self._note_interrupt(ctx.session_id)
                    return ReplyOutcome(
                        ok=False,
                        error=REPLY_INTERRUPTED,
                        # 慢动作之前她已经说出口的那几句已经发出去了：调用方别再发，
                        # 但也别当成"什么都没发生"
                        live_messages=list(outcome.live_messages),
                        live_sent=bool(outcome.live_messages),
                        debug_messages=self.take_pending_echo(ctx.session_id),
                    )
            # 扩展要补的一段（例如叙述/正文）：接在她这一轮说完之后、同一个发送批次。
            # 直接塞进 outcome.messages，所以不占"一次最多说几条"的额度。
            extra_text = await self.extensions.reply_extra(
                state, parsed.extra, session_id=str(getattr(ctx, "session_id", "") or "")
            )
            if extra_text:
                outcome.messages.append(extra_text)
                await self._log_event(
                    state,
                    "ext_reply_extra",
                    {"chars": len(extra_text), "text": extra_text[:200]},
                    outcome=outcome,
                )
            # 她这次说出去的话也要进聊天上下文：下一轮提示词里才有「你最近说过的话」，
            # 模型才知道自己刚用了什么说法，才能被要求换一种。
            # 已经即时发出去的那几句也算她说过（不然她刚说"等我两分钟"就不记得了）。
            for message in outcome.said_messages():
                recorded = state.note_chat(
                    user_id="__self__",
                    name=state.bot_current_nickname or state.bot_base_nickname or "你",
                    text=message,
                    now=self._now(),
                    keep=self.chat_history_limit(),
                    is_self=True,
                    origin=ctx.session_id,
                )
                state.note_reply(message, session_id=ctx.session_id)
                if recorded is not None:
                    recorded["speech_turn"] = outcome.speech_receipt_batch
                # 这句话是在哪个会话里说的：留档只有一份，来源要标出来。
                # 用 note_chat 的返回值定位那一条（重复没记时是 None → 什么都不改）
                self._tag_chat_origin(state, ctx.session_id, recorded)
            # 水位线不在这里推进：改成**确实发出去之后**由发送方通知
            # （见 main.py 里的 mark_chat_replied_by_session）。这样被丢弃的、
            # 或发送失败的回复不会把"这批消息已经回过"记下来，下一轮还会看到它们。
            self.note_chat_note(state, parsed.chat_note, ctx.session_id)
            # 模型自己标的心情变化（主路径）：只在这一轮是她真的跟人说话时接受，
            # 自主轮不写，免得她凭空给自己加心情。
            if parsed.valence_delta:
                self._apply_valence_delta(state, parsed.valence_delta)
            # 「他还有件没说完的事」：记下来，之后到点可以接一句
            self.note_open_topic(state, ctx, parsed.open_topic)
            # 「你心里搁着的事」：她自己的事，挂着，会自己淡掉
            if parsed.heart_knot:
                self.note_heart_knot(state, parsed.heart_knot, about=str(ctx.user_id or ""), session_id=ctx.session_id)
            # 「他这笔账我记着了」：冲人的，会让她对他冷一档，而且只在他面前提
            if parsed.grudge:
                self.note_grudge(
                    state,
                    parsed.grudge,
                    user_id=str(getattr(ctx, "user_id", "") or ""),
                    user_name=str(getattr(ctx, "user_name", "") or ""),
                    session_id=str(getattr(ctx, "session_id", "") or ""),
                )
            # 「他道歉了 / 补上了」：那笔账划掉
            if parsed.forgive:
                self.resolve_grudge(state, user_id=str(getattr(ctx, "user_id", "") or ""), session_id=ctx.session_id)
            # 「我自己答应过的事」：她自己的账本，跟 open_topic 正好对称
            if parsed.own_topic:
                await self.note_own_topic(
                    state,
                    parsed.own_topic,
                    user_id=str(getattr(ctx, "user_id", "") or ""),
                    user_name=str(getattr(ctx, "user_name", "") or ""),
                    session_id=str(getattr(ctx, "session_id", "") or ""),
                )
            # Legacy boolean completions are deliberately ignored. Execution receipts own progress.
            self.mark_open_topics_asked(state, ctx)
            # 对方的口吻：模型判的优先，没判才用关键词表兜底（表只认字面，会认错反话）
            # 带上"谁说的"：同样一句难听的，亲近的人说她更受伤（见 _tone_magnitude）
            await self._apply_tone_pulse(
                state,
                parsed.tone or keyword_signal(ctx.text),
                user_id=str(getattr(ctx, "user_id", "") or ""),
                outcome=outcome,
            )
            # 「他这一轮碰了她哪儿」：主插件不解释这些词，只转给声明要它的扩展
            # （没人声明时 parsed.touch 一定是空的，这里什么都不做）
            if parsed.touch:
                # 连"这一轮在哪个会话里"一起给：留档是整组共用的，扩展要凭它
                # 认这句话是不是这会儿、在这儿说的（见 extensions.on_touch 契约）
                handled = await self.extensions.touch(
                    state,
                    parsed.touch,
                    session_id=str(getattr(ctx, "session_id", "") or ""),
                )
                if handled:
                    await self._log_event(
                        state,
                        "touch",
                        {"parts": list(parsed.touch), "by": ctx.user_name or ctx.user_id},
                        outcome=outcome,
                    )
                    # 被碰一碰（亲密接触）也把困意压下去：当场有人在跟她亲热，
                    # 不该因为困意到了阈值就把她送进睡眠——这是"亲密优先"那条。
                    self.relieve_sleep_pressure(
                        state,
                        getattr(self.world.sleep, "pressure_touch_relief", 0.2),
                        reason="touch",
                    )
            # 扩展**自己声明**的字段：主插件同样不解释，只按声明的形状转交
            # （没人声明时 parsed.extra 一定是空的，这里什么都不做）
            if parsed.extra:
                handled = await self.extensions.extra(state, parsed.extra)
                if handled:
                    await self._log_event(
                        state,
                        "ext_fields",
                        {
                            "fields": dict(parsed.extra),
                            "by": ctx.user_name or ctx.user_id,
                        },
                        outcome=outcome,
                    )
            # 记下来给下一轮抽声音样例用：模型判的口吻比关键词表准得多
            state.last_user_tone = str(parsed.tone or "")
            # 主模型判的"这句在跟谁说话"：它比结构化信息看得多（没有 @ 的一句
            # 「你倒是说啊」它也能判出来），所以它说是别人就信它
            self._mark_reply_addressing(state, ctx, parsed.addressing)
            await self._echo_events_since(state, outcome, echo_marker)
            await self._log_event(
                state,
                "reply",
                {
                    "user": ctx.user_name or ctx.user_id,
                    "wake": ctx.is_wake,
                    # 已经即时发出去的那几句也要算：日志里记的是她这一轮说过的全部话
                    "messages": outcome.said_messages(),
                    "reasoning": parsed.reasoning,
                    "actions": [item.type for item in actions],
                    "plan_mode": parsed.plan_mode,
                    "cancel": parsed.cancel,
                    # 模型原话 + 解析时丢掉的东西：以后排查"她为什么没做这件事"不用再猜
                    "raw": _clip_text(parsed.raw_text, 500),
                    "warnings": list(parsed.warnings),
                    "auto_travel": list(outcome.auto_travel),
                    # 这一轮用的是哪个表达格、实际允许几条：日志页能直接对照
                    "style_cell": state.last_style_cell,
                    "say_limit": int(state.last_say_limit or 0),
                    # 模型自己标的心情变化（正 = 变好，负 = 变差）
                    "valence_delta": round(float(parsed.valence_delta or 0.0), 3),
                    # 对方这一轮的口吻（模型判的）：信号不对时，日志里能一眼看出来
                    "tone": str(parsed.tone or ""),
                    # 这句是在跟谁说话（模型判的）：判成 "others" 时这一条会被标出来
                    "addressing": str(parsed.addressing or ""),
                    # 这一轮生成出来的图（生图 / 出图的动作）：日志里能看出贴了几张
                    "images": len(self._group_images(outcome)),
                },
                persona_id=ctx.persona_id,
                place=ctx.session_id,
            )

        # 没有对外发言（例如她只 think / 只换了个地方）就交回主人格，
        # 保证用户不会因为"她今天不想说话"而收不到任何回应。
        # 前面的门禁（被叫醒等）攒下的回显排在最前面——它们本来就发生在这轮之前。
        pending = self.take_pending_echo(ctx.session_id)
        quote_mode = str(
            getattr(getattr(self.world, "reply_style", None), "quote_mode", "smart")
            or "smart"
        )
        if quote_mode == "always":
            quote_hint = "always"
        elif quote_mode == "smart" and len(texts) > 1:
            # 智能：这一轮要回的是一串消息（她还在回上一条时又来新的）才引用
            quote_hint = "always"
        else:
            quote_hint = "off"
        if outcome.said_messages() or outcome.routed or outcome.routed_images:
            # 她真的开口了（哪怕只是私下补了一句）：记进本小时的被动回复额度
            async with self.session_state(ctx.session_id) as state:
                self._count_passive_reply(state)
        # 扩展可以决定"这一轮她**到底发什么**"（H 模式：整段正文由扩展写出来）。
        # 返回 None = 照常发；返回空串 = 这一轮她的独立发言被静默（什么都不发）；
        # 返回文本 = 用这段原样替换她本来要发的内容。
        silenced = False
        if outcome.messages:
            raw_text = "\n".join(
                str(item) for item in outcome.messages if str(item).strip()
            )
            try:
                picked = await self.extensions.reply_replace(
                    state, raw_text, session_id=ctx.session_id
                )
            except Exception:
                picked = None  # 扩展出错 = 不干预：绝不能因为扩展有问题就发不出话
            if picked is not None:
                if str(picked).strip():
                    outcome.messages = [str(picked)]
                else:
                    # 静默：连"只说给别的会话"的那几句也一起收掉——这一轮她什么都没发。
                    # 但"她回过话"这件事必须照常记：水位线要推进（见 ReplyOutcome.silenced），
                    # 不然世界以为这批消息没被回过，下一轮会把同一批原样再喂一遍。
                    outcome.messages = []
                    outcome.routed = {}
                    outcome.routed_images = {}
                    outcome.live_messages = []
                    silenced = True
        return ReplyOutcome(
            # 只要她在**任何地方**说了话，这一轮就算成立（不然只写私聊那句时
            # 会被判成"没产生对外发言"，交回主人格又多回一条）；被静默的一轮
            # 也算成立——那句话已经处理过了，只是没发出去。
            ok=bool(
                outcome.said_messages() or outcome.routed or outcome.routed_images
            )
            or silenced,
            silenced=silenced,
            # 静默时水位线按它推进：只算当前会话，别处那几句已经被收掉了
            replied_sessions=[str(ctx.session_id)] if silenced else [],
            messages=list(outcome.messages),
            # 已经即时发出去的那几句：调用方别再发一遍，但日志与额度要算上
            live_messages=list(outcome.live_messages),
            live_sent=bool(outcome.live_messages),
            images=self._group_images(outcome),
            routed={
                str(key): list(value)
                for key, value in (outcome.routed or {}).items()
                if list(value)
            },
            routed_images={
                str(key): list(value)
                for key, value in (outcome.routed_images or {}).items()
                if list(value)
            },
            quote_hint=quote_hint,
            reasoning=parsed.reasoning,
            warnings=parsed.warnings,
            tail=parsed.tail,
            debug_messages=pending + list(outcome.debug_messages),
            debug_positions=[0] * len(pending) + list(outcome.debug_positions),
            error=(
                "" 
                if (
                    outcome.said_messages()
                    or outcome.routed
                    or outcome.routed_images
                )
                else "模型没有产生对外发言"
            ),
        )

    def _record_user_message(
        self, state: WorldState, ctx: MessageContext
    ) -> tuple[list[str], bool]:
        """记录一条用户消息带来的全部影响，返回要给提示词的额外说明。

        接管回复和自主行为共用这一段，保证两条路径的状态演化完全一致。
        第二个返回值表示这次是不是把她叫醒了（调用方负责写一条事件日志）。
        """

        now = self._now()
        self._note_images(state, ctx.image_urls)
        state.touch_user(
            ctx.user_id or "unknown",
            name=ctx.user_name,
            anchor="near:bot" if ctx.is_wake else "topic_center",
            max_tracked=self.world.limits.max_active_users_tracked,
        )
        if not self.is_plugin_command(ctx.text):
            # 插件自己的指令（/mw …）不算"群里说的话"：它是管理入口，不是有人在跟她讲话。
            # 不挡的话她会把「/mw event 逛街被跟了」当成"主人讲给我听的事"。
            recorded = state.note_chat(
                user_id=ctx.user_id or "unknown",
                name=ctx.user_name,
                text=ctx.text,
                raw_text=ctx.raw_text or ("" if ctx.has_images or ctx.chat_images or ctx.has_videos else None),
                has_images=ctx.has_images,
                has_videos=ctx.has_videos,
                now=now,
                keep=self.chat_history_limit(),
                images=ctx.chat_images,
                origin=ctx.session_id,
                message_id=ctx.message_id,
                event_id=ctx.event_id,
            )
            self._tag_chat_origin(state, ctx.session_id, recorded)
        self._note_session_activity(state, ctx)
        state.last_user_activity_at = now
        self.note_contact_response(state, ctx)
        # 名字/昵称也在这儿再刷一次（不带计数，免得和 note_presence 里那次重复）
        self.profiles.touch(
            ctx.session_id, ctx.user_id, ctx.user_name, now=now, count_message=False
        )
        if ctx.is_wake or ctx.is_mentioned or ctx.is_private:
            # 他直接跟她说话了：对他的想念清零（群里别人聊得再热都不算）
            state.miss[str(ctx.user_id or "")] = 0.0
            # 而且下一次"想他"也要重新随机等一段时间，别刚聊完转头又惦记
            state.miss_ready_at[str(ctx.user_id or "")] = self._next_miss_open_at(now)
            # 「聊过」和「见过」分开记：想念只认"他直接跟她说过话"
            self.profiles.note_talked(ctx.session_id, ctx.user_id, now=now)
        self.dynamics.apply_event(state, "topic_engaged", now=now)
        # 对方的口吻**由主模型判**（`tone` 字段），在回复生成之后统一落地
        # （见 ``handle_reply`` 里的 ``_apply_tone_pulse``）。
        # 这里只做「有人叫了她」这一件事：口吻不再在这里二次触发，
        # 否则同一句"亲亲"会被算两次脉冲。
        if ctx.is_wake:
            self.dynamics.apply_event(state, "mention_bot", now=now)
        # 群里热闹这条**要限流**：以前每来一条消息就结算一次，刷屏时把孤独一路扣光。
        # 现在每 5 分钟最多算一次，而且它只给一点心潮——孤独与无聊都不动
        # （无聊驱动她换地方，不能因为群里吵就一直待着）。
        if (
            ctx.is_group_lively or self._group_is_lively(state, now=now)
        ) and self._group_lively_allowed(ctx.session_id, now):
            self.dynamics.apply_event(state, "group_lively", now=now)
            if not ctx.is_wake:
                # 群里在聊、但没人在跟她说话：更闷、更想找人说两句
                self.dynamics.apply_event(state, "left_out", now=now)

        # 醒来：冷启动之后第一次有消息，从"刚醒"转成空闲
        if state.state == STATE_AWAKENING and state.world_time > 0:
            state.state = STATE_IDLE

        notes: list[str] = []
        woke = False
        # 睡眠中被明确叫醒 -> 打断睡眠
        if (self._is_asleep(state) or state.state == STATE_DROWSY) and self._wake_hit(ctx):
            self._wake_up_state(state, ctx)
            if state.wake_note:
                notes.append(state.wake_note)
            woke = True
        # 「等会儿再睡」：说话算数——她说好了再待一会儿，就真的把这轮睡眠往后推。
        # 放在 sleep_notes 之前：这一轮给她的说明就该是"别催他"那版。
        delay_word = self._sleep_delay_hit(ctx, state)
        if delay_word:
            self.delay_sleep(
                state,
                reason=f"他说「{delay_word}」",
                by=str(ctx.user_name or ctx.user_id or ""),
            )
        if not state.is_sleeping:
            # 有人跟她说话本身就压一点困意：聊起来了就不会一下就困死过去
            self.relieve_sleep_pressure(state)
        notes.extend(self.sleep_notes(state))
        if not state.cold_start_done:
            notes.append(
                "你刚从沉睡中苏醒，还有点迷糊。说话可以简短、带点困意。"
                "这是你第一次在这个会话里醒来。"
            )
            state.cold_start_done = True
        return notes, woke

    async def note_presence(self, ctx: MessageContext) -> None:
        """轻量记录「谁在说话」，不触发任何 LLM 调用。

        群聊里大部分消息不会走到主人格，但 Bot 需要知道谁在场，因此单独提供这个入口。
        每条消息都会记一笔（存在感 + 最近聊天上下文），状态体量很小、写入在 SQLite WAL 下开销可控。
        """

        if not self.is_enabled(ctx.session_id):
            return
        if not ctx.user_id:
            return
        now = self._now()
        async with self.session_state(ctx.session_id) as state:
            self._note_presence(state, ctx)
            # 到达记录只在调试模式下写：正常运行时"谁说了什么、判断成什么"
            # 由意图路由插件那本流水负责，这边不用再留一份。
            if self.debug:
                await self._log_event(
                    state,
                    "incoming",
                    {
                        "user": ctx.user_name or ctx.user_id,
                        "user_id": ctx.user_id,
                        "text": _clip_log(ctx.text),
                        "wake": bool(ctx.is_wake),
                        "mentioned": bool(ctx.is_mentioned),
                        "soft": bool(ctx.is_soft_wake),
                        "private": bool(ctx.is_private),
                        "images": len(list(ctx.image_urls or [])),
                    },
                )
