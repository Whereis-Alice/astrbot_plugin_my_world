"""引擎的数据载体：一次 tick / 一轮回复 / 一次对话的中间产物。"""

from __future__ import annotations

from dataclasses import (
    dataclass,
    field,
)
from typing import Any
from uuid import uuid4
from ..state import chat_event_key


@dataclass
class TickOutcome:
    """一次 tick 对某个会话产生的可见结果。"""

    session_id: str
    messages: list[str] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    """这一拍「她在哪、什么状态、什么心情」的结构化摘要。

    以前这段是拼成一行文本塞进 ``notes`` 再写进后台日志的；现在留给「运行记录」页，
    后台日志不再打印 tick。
    """

    routed: dict[str, list[str]] = field(default_factory=dict)
    """要说给**别的会话**的话：``{session_id: [文本…]}``。

    一个人可以在几个地方同时说话（群里应付一句、私聊里再吐槽一句），
    所以"说给谁"是逐句的，不能只挂在整轮的 session 上。
    """

    routed_images: dict[str, list[str]] = field(default_factory=dict)
    """同上，按会话分开的图片。"""

    broadcast_targets: list[str] = field(default_factory=list)
    """日程一次执行后，默认文本和图片还要复制到的具体会话。"""

    locked_targets: list[str] = field(default_factory=list)
    """日程这一轮的「允许落点」全集：勾了具体会话 = 那几个；勾了会话组 = 组里全部成员。

    只有日程（``run_schedules`` / ``run_schedule_now``）会设置它。锁上之后，动作模型
    写的 ``send_to`` 必须落在集合里，否则这句话不发（见 ``_speech_target``）——不然
    「只勾了一个私聊」的日程会被那句 ``send_to`` 改发到没勾的会话去。
    空 list = 没锁 = 普通回复 / 事件 / 主动开口照旧，不受任何落点限制。
    """

    speech_home: str = ""
    """这一轮"她正在跟谁说话"：没写 ``send_to`` 的话（动作文案、续说）默认落这儿。

    动作是"在哪个会话里被安排的就回到哪儿"——私聊里起的做饭，做完那句续说回私聊，
    不会跑到群里去。空 = 就用 ``session_id``。
    """

    def home(self) -> str:
        """默认落点：没有"正在说的会话"时就是这一轮的会话。"""

        return str(self.speech_home or self.session_id)

    chat_claims: dict[str, list[str]] = field(default_factory=dict)
    """Exact observed chat identities; commit only after accepted speech to that target."""
    reply_targets: dict[str, str] = field(default_factory=dict)
    """Validated destination-local platform message IDs, never part of spoken text."""
    chat_expires_at: dict[str, float] = field(default_factory=dict)
    """A queued interjection cannot outlive the conversation it was written for."""
    chat_media_scenes: dict[str, dict] = field(default_factory=dict)
    """Observed media evidence, retained for policy checks at actual sending."""
    chat_state_keys: dict[str, str] = field(default_factory=dict)
    chat_replied_seq: dict[str, int] = field(default_factory=dict)

    def take_messages(self) -> list[str]:
        """取走"还没发出去"的几句话（调用方负责真的发出去）。"""

        taken = [str(item) for item in self.messages if str(item).strip()]
        self.messages = []
        return taken

    def said_messages(self) -> list[str]:
        """这一轮她**说过**的全部话：已经即时发出去的 + 还没发的。"""

        return [*self.live_messages, *self.messages]

    notes: list[str] = field(default_factory=list)
    llm_calls: int = 0
    auto_travel: list[str] = field(default_factory=list)
    """插件替她补的移动目的地（她想去别处做某事时）。"""

    live_messages: list[str] = field(default_factory=list)
    """已经**当场发出去**的那几句（慢动作之前先说的）。收尾时不再重复发。"""

    debug_messages: list[str] = field(default_factory=list)
    """开启「把动作发到群里」时，这些行会作为普通消息发出去（不计入"她说了话"）。"""

    debug_positions: list[int] = field(default_factory=list)
    """与 ``debug_messages`` 一一对应：记下它产生时 ``messages`` 里已经有多少条。

    发送时按这个下标把两类消息重新插回真实顺序——她先调了工具、拿到结果才说话，
    群里也该是这个次序，而不是"话发完再补一句我刚查了天气"。
    """

    echoed_event_ids: set[int] = field(default_factory=set)
    """已经在发生的当下插好位置的事件 id：收尾那次批量回显要跳过它们，不然会重复发。"""

    speech_kind: str = ""
    """这一轮发言的性质（``""`` = 旧版自主来源，``reply`` = 正常回复）。

    事件里的求助不算「她主动找人聊天」：不该吃无人回应保护，也不该把她的冷却期拖长。
    """

    place: str = ""
    """这一轮**发生在哪个会话**（被搭话就是那个群 / 私聊；自主轮留空）。

    一个会话组里的几个群、私聊共用一份状态与一册日志（她的日志挂在组代表会话名下），
    所以要单独记下"这件事发生在哪儿"，不然私聊里说的话看起来像是群里说的。
    """

    images: list[str] = field(default_factory=list)
    """这一轮要发到群里的图片（工具 / 指令生成的结果图）。

    和她的话分开排队：先说内容、再把图贴上去，群里读起来才顺。
    """

    def add_debug(self, line: str) -> None:
        """记一条调试回显，并记住它落在哪两条正式回复之间。"""

        if not str(line).strip():
            return
        self.debug_messages.append(str(line))
        self.debug_positions.append(len(self.messages))

    speech_kinds: dict[str, list[str]] = field(default_factory=dict)
    image_kinds: dict[str, list[str]] = field(default_factory=dict)
    image_ids: dict[str, list[str]] = field(default_factory=dict)
    image_notes: dict[str, dict[str, str]] = field(default_factory=dict)
    live_kinds: list[str] = field(default_factory=list)
    live_origins: list[str] = field(default_factory=list)
    contact_id: str = field(default_factory=lambda: uuid4().hex)
    contact_tokens: dict[str, str] = field(default_factory=dict)
    contact_people: set[str] = field(default_factory=set)
    native_events: dict[str, Any] = field(default_factory=dict, repr=False)
    delivered_messages: dict[str, list[str]] = field(default_factory=dict)
    speech_decisions: list[dict[str, Any]] = field(default_factory=list)
    speech_ids: dict[str, list[str]] = field(default_factory=dict)
    live_ids: list[str] = field(default_factory=list)
    delivered_decisions: dict[str, list[str]] = field(default_factory=dict)
    delivered_image_decisions: dict[str, list[str]] = field(default_factory=dict)
    accepted_speech_at: dict[str, float] = field(default_factory=dict)
    accepted_receipts: dict[tuple, dict[str, Any]] = field(default_factory=dict)
    """Acceptance remains true even while its local persistence needs retrying."""
    remembered_speech: set[tuple] = field(default_factory=set)
    speech_receipt_batch: str = field(default_factory=lambda: uuid4().hex)
    """Messages accepted by the platform, used to distinguish thinking from speaking."""

    def add_speech(self, text: str, session_id: str = "", *, kind: str | None = None) -> None:
        """记一句她要说的：没说给谁就是这一轮的会话，说了就投到那个会话去。

        ``session_id`` 传 ``None`` 表示"她想去的地方根本不存在"：这句话丢掉，
        免得本该私下说的内容当众说出来。
        """

        body = str(text or "").strip()
        if not body:
            return
        if session_id is None:
            return
        target = str(session_id or "").strip()
        key = "" if not target or target == str(self.session_id) else target
        bucket = self.messages if not key else self.routed.setdefault(key, [])
        kinds = self.speech_kinds.setdefault(key, [])
        kinds.extend([self.speech_kind] * max(0, len(bucket) - len(kinds)))
        kinds.append(self.speech_kind if kind is None else kind)
        ids = self.speech_ids.setdefault(key, [])
        ids.extend([""] * max(0, len(bucket) - len(ids)))
        decision_id = ""
        for decision in reversed(self.speech_decisions):
            if decision.get("thought_id"):
                continue  # Plan receipts grow from each actual queued occurrence.
            if decision["target"] == (target or self.home()) and body in decision["drafts"]:
                assigned = decision.setdefault("assigned", [])
                if assigned.count(body) < decision["drafts"].count(body):
                    assigned.append(body)
                    decision_id = decision["id"]
                    break
        if not decision_id:
            from .speech_receipts import bind_plan_speech
            decision_id = bind_plan_speech(self, body, target or self.home())
        ids.append(decision_id)
        if not target or target == str(self.session_id):
            self.messages.append(body)
            return
        self.routed.setdefault(target, []).append(body)

    def add_images(self, images: list[str], session_id: str = "", *, decision_id: str = "",
                   kind: str | None = None) -> None:
        """按会话分开记图片（和 ``add_speech`` 配套）。"""

        target = str(session_id or "").strip()
        if session_id is None:
            return
        for item in images or []:
            ref = str(item or "").strip()
            if not ref:
                continue
            key = "" if not target or target == str(self.session_id) else target
            bucket = self.images if not key else self.routed_images.setdefault(target, [])
            if ref in bucket:
                continue
            kinds = self.image_kinds.setdefault(key, [])
            kinds.extend([self.speech_kind] * max(0, len(bucket) - len(kinds)))
            kinds.append(self.speech_kind if kind is None else kind)
            ids = self.image_ids.setdefault(key, [])
            ids.extend([""] * max(0, len(bucket) - len(ids)))
            from .speech_receipts import bind_plan_speech
            ids.append(decision_id or bind_plan_speech(self, ref, target or self.home(), image=True))
            bucket.append(ref)

    def ordered_messages(self) -> list[str]:
        """正式回复 + 调试回显，按实际发生的先后排好。"""

        return [line for line, _ in self.ordered_entries()]

    def ordered_entries(self) -> list[tuple[str, int | None]]:
        """Keep exact speech positions even when an echo has identical text."""
        if not self.debug_messages:
            return [(line, i) for i, line in enumerate(self.messages)]
        buckets: dict[int, list[str]] = {}
        for index, line in enumerate(self.debug_messages):
            position = (
                self.debug_positions[index]
                if index < len(self.debug_positions)
                else len(self.messages)
            )
            buckets.setdefault(min(position, len(self.messages)), []).append(line)
        result = []
        for slot in range(len(self.messages) + 1):
            result.extend((line, None) for line in buckets.get(slot, []))
            if slot < len(self.messages):
                result.append((self.messages[slot], slot))
        return result


@dataclass
class MessageContext:
    """处理一条用户消息需要的上下文。"""

    session_id: str
    user_id: str = ""
    user_name: str = ""
    text: str = ""
    is_wake: bool = False
    is_mentioned: bool = False
    """这条消息是不是真的 @ 了她（或私聊）。

    和 ``is_wake`` 分开是因为：意图路由这类插件会把 ``is_wake`` 置 True
    （它标记的是"这条消息要交给大模型"，不等于"有人在跟她说话"）。
    """
    is_private: bool = False
    persona_id: str = ""
    other_context: str = ""
    is_group_lively: bool = False
    mood_signal: str = ""
    group_name: str = ""
    """平台给的群名（拿不到就是空串）。她名册里的名字优先用编辑器里的备注。"""

    is_soft_wake: bool = False
    """这条消息是**语义上**冲着她说的（意图路由放行），但原话里没人 @ 她。

    路由插件为了让消息重新走通管道会补一个 @，那会让她以为"群里有人点名找我"；
    有这个标记时按"顺着话题对她说"处理。
    """

    image_urls: list[str] = field(default_factory=list)
    """这次要直接交给多模态主模型的图片地址（没配转述模型时才用）。"""

    chat_images: list[str] = field(default_factory=list)
    """这条消息带的图片地址（不管转述成没成功都留着）。

    存进聊天留档用：聊天记录里出现过的图，可以按配置把最近几张直接交给
    多模态主模型看，并在记录里标上「图1 / 图2」方便对照。
    """

    image_marks: dict[str, str] = field(default_factory=dict)
    """这一轮真的附给主模型的聊天记录图片：``地址 -> 图N``（提示词里标「（见图N）」）。"""

    at_targets: list[dict] = field(default_factory=list)
    """这条消息 @ 了谁：``[{"id": "42", "name": "小明", "self": False}]``（含她自己在内）。

    渲染聊天记录时用它写清"这句是冲谁说的"：光看正文分不清别人话里的「你」
    指的是谁，尤其是一条消息同时 @ 了两个人的时候。
    """

    reply_to: dict = field(default_factory=dict)
    """这条消息引用/回复的是谁：``{"id": "42", "name": "小明"}``；没引用就是空的。"""

    addressing: str = ""
    """这句是冲谁说的：``me``（点了她）/ ``others``（在叫别人）/ 空（看不出来）。"""

    router: dict = field(default_factory=dict)
    """意图路由交过来的**事前**判定：``{"policy": ..., "batch": [...]}``（空 = 没有）。

    ``batch`` 里每条是 ``{"sender", "sender_id", "message_id", "text", "released",
    "worth", "directed", "to", "reply_score", "reason"}``，按到达顺序列出这一批的
    **每一条**（含被合并成一次回复、正文没进请求的那几条），``directed`` 就是路由
    判断模型答的「这句是不是冲她说的」。

    和 ``addressing`` 的区别：``addressing`` 是主模型**回完之后**自己标的（只认
    结构化的 @ 与引用），这个在收到消息的那一刻就有，所以能提前给被合并掉的那几条
    作证；两边都不覆盖对方，记录里各占一个字段。
    """

    raw_text: str = ""
    """注解**之前**的原文（``text`` 是注解之后、真正给模型看的文本）。

    事件身份认的是这条消息本身，不认正文：图片说明 / @ / 转发摘要都会改
    ``text``，改不动 ``raw_text``，也改不动下面两个 id。
    """

    message_id: str = ""
    """平台给的消息 id（拿得到就用它认"是不是同一个事件"）。"""

    event_id: str = ""
    """平台不给 message_id 时，由监听侧生成、存在 event extra 里的稳定内部 id。"""

    has_images: bool = False
    """原消息含图片，即使没有可用图片地址也保留占位。"""

    @property
    def event_key(self) -> str:
        """这条消息的事件身份键（两样 id 都没有时是空串）。"""

        return chat_event_key(self.session_id, self.message_id, self.event_id)


@dataclass
class SleepReply:
    """她在睡觉时对一条消息的处理结果。

    ``mode``：

    - ``template``：用配置的固定文案回一句（``messages`` 就是要发的内容）；
    - ``silent``：什么都不发，也不要交给主人格。

    （能把她叫醒的消息不会走到这里——那种情况返回 ``None``，交回正常回复路径。）
    """

    mode: str
    messages: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass
class ReplyOutcome:
    """一次「接管回复」的结果。"""

    ok: bool
    messages: list[str] = field(default_factory=list)
    live_messages: list[str] = field(default_factory=list)
    """已经**当场发出去**的几句（慢动作之前先说的）：调用方别再发一遍。"""

    live_sent: bool = False
    """这一轮是不是已经即时发过话了：收尾那条就不用再引用她的消息。"""

    reasoning: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    debug_messages: list[str] = field(default_factory=list)
    """开启「把动作发到群里」时，决定/动作/工具调用的说明行。"""
    debug_positions: list[int] = field(default_factory=list)
    """与 ``debug_messages`` 一一对应：产生时 ``messages`` 里已有多少条。"""
    tail: str = ""
    """JSON 之后留下的短尾巴（例如别的插件要求模型追加的 `[好感度 持平]`）。

    只交给「回复钩子」那一步，让靠标记工作的插件还能读到；不会跟着她的话发出去。
    """
    error: str = ""

    quote_hint: str = ""
    """这条回复要不要引用触发她的消息：``always`` / ``off``（按配置与合并情况算好）。"""

    images: list[str] = field(default_factory=list)
    """工具 / 指令这一轮生成出来的图片，跟着她的话一起发到群里。"""

    routed: dict[str, list[str]] = field(default_factory=dict)
    """说给**别的会话**的话：``{session_id: [文本…]}``。

    回复里也可以有一句"只想单独跟某人说"，那几句不走 ``messages``（当前会话），
    而是按会话分开放，由发送方逐条投递。
    """

    routed_images: dict[str, list[str]] = field(default_factory=dict)
    """同上，按会话分开的图片。"""

    silenced: bool = False
    """她这一轮**回过话**，但被扩展的 ``reply_replace`` 静默了（什么都不发）。

    调用方要照常推进「回过话」的水位线 / 已读游标，也不能落回主人格——
    不然世界以为这批消息没被回过，下一轮会原样再喂一遍。
    """

    replied_sessions: list[str] = field(default_factory=list)
    """这一轮算"回过话"的会话（静默时用：水位线按它推进）。"""

    def routed_sessions(self) -> list[str]:
        """这一轮额外说到了哪些会话。"""

        return [
            str(item)
            for item in list(self.routed or {}) + list(self.routed_images or {})
            if str(item)
        ]

    def ordered_messages(self) -> list[str]:
        """正式回复 + 调试回显，按实际发生的先后排好。"""

        if not self.debug_messages:
            return list(self.messages)
        buckets: dict[int, list[str]] = {}
        for index, line in enumerate(self.debug_messages):
            position = (
                self.debug_positions[index]
                if index < len(self.debug_positions)
                else len(self.messages)
            )
            buckets.setdefault(min(position, len(self.messages)), []).append(line)
        result: list[str] = []
        for slot in range(len(self.messages) + 1):
            result.extend(buckets.get(slot, []))
            if slot < len(self.messages):
                result.append(self.messages[slot])
        return result


@dataclass
class DialoguePlan:
    """注入式接管的一轮计划：只把提示词交给宿主，自己不说话、不调模型。

    ``system_prompt`` 是稳定层（人设 / 世界规则 / 说话格式 / 当前场景），
    ``runtime_part`` 是易变层（状态 / 记忆 / 聊天背景 / 提醒），交给宿主当作
    「用户消息附加块」拼进去，尽量少破坏别家插件的前缀缓存。
    """

    ok: bool = False
    recalled_ids: list[int] = field(default_factory=list)
    system_prompt: str = ""
    user_prompt: str = ""
    runtime_part: str = ""
    prompt_snapshot: dict[str, Any] | None = None
    batch: list[dict[str, Any]] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    say_limit: int = 0
    reason: str = ""
    silent: bool = False
