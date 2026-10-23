"""她的世界引擎。

把配置、状态、决策、动作、记忆串起来，是整个插件的核心 seam（见 docs/SEAMS.md）：
这一层只依赖 ``core/ports.py`` 里的 Protocol，没有 AstrBot 也能完整测试。

实现按职责拆在同一个包里的多个 mixin 里，``MyWorldEngine`` 只负责把它们拼起来：
constants（常量）/ models（数据载体）/ util（纯函数）；
config、sessions、state_store、events、event_flow、persona、commands、sleep、
dialogue_memory、addressing、reply、profiles、recall、cuddle、relations、tools、
schedule、weather、autonomy、actions、schedule_actions、memory_actions、search、
actions_run、interrupt、generation、limits、group_chat、send、snapshot、turn。
"""

from __future__ import annotations

import asyncio
import random
from collections import deque
from typing import Any
from ..config_scope import ScopedConfigMixin
from ..config_store import ConfigStore
from ..db import AsyncDatabase
from ..models import (
    SessionDef,
    WorldConfig,
)

from .constants import (
    DEFAULT_TICK_SECONDS,
    EXTENSION_LAYER_HEADING,
    SEARCH_BUDGET_SECONDS,
    READ_CACHE_SECONDS,
    READ_PASSAGE_CHARS,
    PENDING_IMAGE_KEEP,
    DEBUG_ECHO_DEBOUNCE_SECONDS,
    _DEBUG_ECHO_TYPES,
    DEBUG_ONLY_ECHO_TYPES,
    DEFAULT_SEARCH_TOPIC,
    MERGE_WINDOW_SECONDS,
    MERGE_MAX_RESTARTS,
    MERGE_MAX_TEXTS,
    INTERRUPT_STREAK_MAX,
    INTERRUPT_STREAK_WINDOW_SECONDS,
    REPLY_INTERRUPTED,
    PROMPT_SNAPSHOT_KEEP,
    REPLY_MUTED,
    REPLY_GUARD_RETRIES,
    MAX_GROUP_IMAGES,
    EVENT_ROLL_MAX_GAP_HOURS,
    SEARCH_LOG_KEY,
    SEARCH_LOG_MINUTES,
    ASK_ABOUT_CURIOSITY,
    OUTDOOR_HINT_HOURS,
    TONE_INTIMACY_FLOOR,
    TONE_INTIMACY_CEIL,
    NEGATIVE_VALENCE_ROUND_CAP,
    NEGATIVE_VALENCE_ROUND_WINDOW,
    NEGATIVE_TONE_FRESH_HOURS,
    NEGATIVE_TONE_STREAK_SCALE,
    NEGATIVE_VALENCE_LOW_LINE,
    DESIRE_TEASE_FLOOR,
    DESIRE_TEASE_CEIL,
    SEARCH_QUERY_RULES,
    TICK_LOG_KEEP,
)
from .models import (
    TickOutcome,
    MessageContext,
    SleepReply,
    ReplyOutcome,
    DialoguePlan,
)
from .util import (
    _clean_search_topic,
    _echo_payload,
    _CN_DIGITS,
    _DURATION_PATTERN,
    _RELATIVE_WHEN,
    _SPOKEN_HOUR,
    _spoken_hour,
    _duration_number,
    _guess_duration_seconds,
    _ARGUMENT_ERROR_MARKERS,
    _looks_like_argument_error,
    _BROKEN_TOOL_MARKERS,
    _looks_like_tool_broken,
    _SCHEMA_RESERVED_KEYS,
    normalize_param_schema,
    render_param_text,
    _as_float,
    _summarize_batch,
    _signed,
    _clip_text,
    _clip_log,
    _to_int_or_default,
    _same_text,
)

from .config import ConfigMixin
from .sessions import SessionsMixin
from .state_store import StateStoreMixin
from .events import EventsMixin
from .event_flow import EventFlowMixin
from .persona import PersonaMixin
from .commands import CommandsMixin
from .sleep import SleepMixin
from .sleep_rhythm import SleepRhythmMixin
from .recovery import RecoveryMixin
from .message_rules import MessageRulesMixin
from .addressing import AddressingMixin
from .reply import ReplyMixin
from .profiles import ProfilesMixin
from .recall import RecallMixin
from .cuddle import CuddleMixin
from .relations import RelationsMixin
from .matters import MattersMixin
from .life_control import LifeControlMixin
from .background import BackgroundMixin
from .chat_browse import ChatBrowseMixin
from .tools import ToolsMixin
from .schedule import ScheduleMixin
from .weather import WeatherMixin
from .tick import TickMixin
from .autonomy import AutonomyMixin
from .actions import ActionsMixin
from .schedule_actions import ScheduleActionsMixin
from .memory_actions import MemoryActionsMixin
from .search import SearchMixin
from .actions_run import ActionsRunMixin
from .interrupt import InterruptMixin
from .generation import GenerationMixin
from .calendar_diary import CalendarDiaryMixin
from .limits import LimitsMixin
from .group_chat import GroupChatMixin
from .send import SendMixin
from .proactive_contact import ProactiveContactMixin
from .participation import ParticipationMixin
from .speech_decisions import SpeechDecisionsMixin
from .snapshot import SnapshotMixin
from .turn import TurnMixin
from .social import SocialMixin
from .context import ContextMixin

# --- 兼容再导出：这些名字以前挂在 core.engine 上，测试与第三方仍按旧路径引用 ---
from ..state import STATE_NAPPING as STATE_NAPPING
from ..state import STATE_SLEEPING as STATE_SLEEPING


__all__ = [
    'ASK_ABOUT_CURIOSITY',
    'AsyncDatabase',
    'ConfigStore',
    'DEBUG_ECHO_DEBOUNCE_SECONDS',
    'DEBUG_ONLY_ECHO_TYPES',
    'DEFAULT_SEARCH_TOPIC',
    'DEFAULT_TICK_SECONDS',
    'DESIRE_TEASE_CEIL',
    'DESIRE_TEASE_FLOOR',
    'DialoguePlan',
    'EVENT_ROLL_MAX_GAP_HOURS',
    'EXTENSION_LAYER_HEADING',
    'INTERRUPT_STREAK_MAX',
    'INTERRUPT_STREAK_WINDOW_SECONDS',
    'MAX_GROUP_IMAGES',
    'MERGE_MAX_RESTARTS',
    'MERGE_MAX_TEXTS',
    'MERGE_WINDOW_SECONDS',
    'MessageContext',
    'MyWorldEngine',
    'NEGATIVE_TONE_FRESH_HOURS',
    'NEGATIVE_TONE_STREAK_SCALE',
    'NEGATIVE_VALENCE_LOW_LINE',
    'NEGATIVE_VALENCE_ROUND_CAP',
    'NEGATIVE_VALENCE_ROUND_WINDOW',
    'OUTDOOR_HINT_HOURS',
    'PENDING_IMAGE_KEEP',
    'PROMPT_SNAPSHOT_KEEP',
    'READ_CACHE_SECONDS',
    'READ_PASSAGE_CHARS',
    'REPLY_GUARD_RETRIES',
    'REPLY_INTERRUPTED',
    'REPLY_MUTED',
    'ReplyOutcome',
    'SEARCH_BUDGET_SECONDS',
    'SEARCH_LOG_KEY',
    'SEARCH_LOG_MINUTES',
    'SEARCH_QUERY_RULES',
    'STATE_NAPPING',
    'STATE_SLEEPING',
    'ScopedConfigMixin',
    'SessionDef',
    'SleepReply',
    'TONE_INTIMACY_CEIL',
    'TONE_INTIMACY_FLOOR',
    'TickOutcome',
    'WorldConfig',
    '_ARGUMENT_ERROR_MARKERS',
    '_BROKEN_TOOL_MARKERS',
    '_CN_DIGITS',
    '_DEBUG_ECHO_TYPES',
    '_DURATION_PATTERN',
    '_RELATIVE_WHEN',
    '_SCHEMA_RESERVED_KEYS',
    '_SPOKEN_HOUR',
    '_as_float',
    '_clean_search_topic',
    '_clip_log',
    '_clip_text',
    '_duration_number',
    '_echo_payload',
    '_guess_duration_seconds',
    '_looks_like_argument_error',
    '_looks_like_tool_broken',
    '_same_text',
    '_signed',
    '_spoken_hour',
    '_summarize_batch',
    '_to_int_or_default',
    'normalize_param_schema',
    'render_param_text',
]


class MyWorldEngine(
    ContextMixin,
    ConfigMixin,
    SessionsMixin,
    StateStoreMixin,
    EventsMixin,
    EventFlowMixin,
    PersonaMixin,
    CommandsMixin,
    SleepMixin,
    SleepRhythmMixin,
    RecoveryMixin,
    MessageRulesMixin,
    AddressingMixin,
    ReplyMixin,
    ProfilesMixin,
    RecallMixin,
    CuddleMixin,
    RelationsMixin,
    MattersMixin,
    LifeControlMixin,
    BackgroundMixin,
    ChatBrowseMixin,
    ToolsMixin,
    ScheduleMixin,
    WeatherMixin,
    TickMixin,
    AutonomyMixin,
    ActionsMixin,
    ScheduleActionsMixin,
    MemoryActionsMixin,
    SearchMixin,
    ActionsRunMixin,
    InterruptMixin,
    GenerationMixin,
    CalendarDiaryMixin,
    LimitsMixin,
    GroupChatMixin,
    SendMixin,
    ProactiveContactMixin,
    ParticipationMixin,
    SpeechDecisionsMixin,
    SnapshotMixin,
    TurnMixin,
    SocialMixin,
    ScopedConfigMixin,
):
    """世界状态机。"""

    def __init__(
        self,
        *,
        store: ConfigStore,
        db: AsyncDatabase,
        llm=None,
        helper_llm=None,
        judge_llm=None,
        creator_llm=None,
        event_llm=None,
        consolidate_llm=None,
        embedding=None,
        describer=None,
        messenger=None,
        tools=None,
        commands=None,
        persona=None,
        clock=None,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
        decider_interval: float = 300.0,
        debug: bool = False,
        logger=None,
        extensions=None,
    ) -> None:
        self._init_config_scopes()
        self.store = store
        self.db = db
        self.db.on_close(self.close_background)
        # 扩展挂载点：没装扩展时就是个空壳（见 core/extensions.py）
        from ..extensions import ExtensionHost

        self.extensions = extensions if extensions is not None else ExtensionHost()
        self.llm = llm
        self.embedding = embedding
        # 辅助模型：只负责"把意图翻译成工具参数"，留空时复用主模型
        self.helper_llm = helper_llm or llm
        # 判断模型：「挑一个 / 打分 / 抽字段」这类纯判断走它；没配就跟随打杂模型。
        # 和打杂分开是有原因的——打杂里混着"要文笔"的活（写事件结局、她求助的话），
        # 那些塞给最小的模型会明显掉质量。
        self.judge_llm = judge_llm or self.helper_llm
        # 内容生成模型：编事件包、以及编辑器里"批量生成动作 / 地点"
        self.creator_llm = creator_llm or self.llm
        # 事件模型：写"这件事最后怎么样了"。它决定事件读起来尬不尬，
        # 所以单独留一个槽位——想变强就换它，不用连带换掉别的打杂活。
        self.event_llm = event_llm or self.helper_llm
        # 睡眠整理模型：消化记忆、更新画像、写梦（留空时复用辅助模型）
        self.consolidate_llm = consolidate_llm or self.helper_llm
        # 看图：天气工具返回的图也要读出来（插件那边传 AstrBotVision；没有就跳过图片）
        self.describer = describer
        self.messenger = messenger
        self.tools = tools
        # 指令通道：把别家插件的指令转发出去（「指令触发」型动作用）
        self.commands = commands
        self.persona = persona
        self.clock = clock
        self.tick_seconds = max(1.0, float(tick_seconds))
        self.decider_interval = max(5.0, float(decider_interval))
        self.debug = debug
        self.logger = logger

        self.world: WorldConfig
        self.schedules = None
        self.sessions = None
        self.load_warnings: list[str] = []
        self._sessions_index: dict[str, SessionDef] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._auto_schedule_lock = asyncio.Lock()
        self._last_decider_at: dict[str, float] = {}
        self._last_presence_at: dict[str, float] = {}
        self._event_writes: dict[str, int] = {}
        self._history_writes: dict[str, int] = {}
        self._event_ids: dict[str, int] = {}
        """每个会话最后写入的事件 id，用来给「把动作发到群里」划一条起跑线。"""
        self._pending_echo: dict[str, list[str]] = {}
        # 平台报上来的群名（拿不到就只用号码 / 编辑器里的备注）
        self._session_names: dict[str, dict[str, Any]] = {}
        """回复路径之外产生的调试回显，等下一次发送时带出去。"""
        self._filled_params: dict[tuple[str, str], dict[str, Any]] = {}
        self.debug_sink = None
        """调试行"立刻发到群里"的通道（由适配层注入；没注入就攒到收尾一起发）。"""

        self.say_sink = None
        """她"先说的那几句"在慢动作之前立刻发出去的通道（同上）。

        没有它的话，「坐好等我两分钟～」要等整段检索跑完，和结果一起冒出来——
        群里看起来就是她半天不吭声、然后一口气说三句。
        """
        """实时调试通道：插件层注入 ``async (session_id, message) -> bool``。

        有它的时候，调试回显在**发生的事情当时**就发出去（不再是整轮跑完才一起发）；
        测试里没有这个通道，就退回"挂到 outcome 上、收尾批量发"的老办法。
        """
        self._echoed_ids: dict[str, set[int]] = {}
        """已经实时发过的事件 id：收尾那次批量回显要跳过，不然会重复。"""
        self._echo_debounce: dict[tuple[str, str, str], float] = {}
        """``(会话, 事件类型, 工具名) -> 上次发的时间``：同一串调用只发第一条。"""
        self._read_cache: dict[str, tuple[float, str]] = {}
        """URL -> (写入时刻, 正文)，抓过的网页短期内不再重复抓。"""

        self._live_card_cache: dict[str, tuple[float, str]] = {}
        """会话 -> (读取时刻, 平台上的真实群名片)。编辑器轮询状态时用它做短缓存。"""

        self._image_captioned: dict[str, float] = {}
        """已经转述过的图（按图片地址记）：同一张图不再转述第二遍。"""

        self._group_lively_at: dict[str, float] = {}
        """每个会话上次结算"群里很热闹"的时刻（给这条脉冲限流）。"""

        self._pending_replies: dict[str, list[dict[str, Any]]] = {}
        # 连着被打断的次数（按"她"记，带时间窗）：刷屏时先让她把话说完
        self._interrupt_streak: dict[str, tuple[int, float]] = {}
        """刚进来的消息（可能被合并进正在进行的那次回复）：会话 -> [{text, at, absorbed}]。"""

        self._replied_batch_keys: dict[str, set[str]] = {}
        """本轮**实际处理过**的事件身份：会话 -> {event_key}。

        水位线只能推到这一批的边界（见 :meth:`mark_reply_batch_replied`）。以前是在
        "发出去之后"按**当时最新**的序号推，于是她说话这会儿新到的消息会被一起标成
        "已回应"，下一轮就再也看不见了。
        """

        self._prompt_snapshots: list[dict[str, Any]] = []
        """最近几次带记录阶段的有界请求副本（见 :meth:`preview_last_reply_request`）。"""

        self._tick_log: deque[dict[str, Any]] = deque(maxlen=TICK_LOG_KEEP)
        """最近几拍 tick 的结果（编辑器「运行记录」页读它；不进后台日志）。"""

        self._incoming_notes: dict[str, list[str]] = {}
        """handle_incoming 攒下的临时提示（冷启动、"刚被叫醒"这类）：会话 -> [提示]。

        收到消息时先记下，等这一轮真正组装提示词（``handle_reply``）时取走，
        取走即失效——这些提示只该出现一次。
        """

        self._tool_failures: dict[str, dict[str, Any]] = {}
        """工具的连续失败次数与退避到期时间（熔断用，只在内存里）。"""
        self._last_llm: dict[str, dict[str, Any]] = {}
        """每个会话最近一次主模型调用的结果（编辑器「模型通道」那行要看）。"""
        self._schedule_signature: list[tuple[Any, ...]] | None = None
        self._schedule_reset_pending = False
        # 一次性日程的过期清理每天只做一遍（值就是做过的那一天）
        self._once_pruned_day = {}
        self._last_tick_at: float = 0.0
        self.rng = random.Random()
        """事件系统的骰子。测试里可以换成固定序列的假随机。"""

        self.miss_rng = random.Random()
        """「想念」下次开闸的随机时长。跟事件骰子分开，免得互相打扰。"""

        self.reload_config()

    # ================= 运行参数 =================

    # 「合并重启」的窗口走这个方法，而不是让 reply.py 直接读模块常量：
    # 测试会临时改 ``core.engine.MERGE_WINDOW_SECONDS``，得让它读到改过的那份。
    def _merge_window_seconds(self) -> float:
        return MERGE_WINDOW_SECONDS

