"""AstrBot 我的世界插件（Star 入口）。

两条路径：

- **注入式接管（默认，`reply_mode=inject`）**：`on_llm_request` 只做三件事——
  门禁（这一轮要不要她回）、换人设（她的人设 + 世界规则，替换宿主那份 persona）、
  加处境（状态 / 记忆 / 场景 / 虚拟动作写进 `extra_user_content_parts`），然后**放行**。
  调模型、工具循环、发送都走 AstrBot 原生 Agent；本插件用 `on_llm_response` /
  `on_using_llm_tool` / `on_llm_tool_respond` / `after_message_sent` 只做观察与记账
  （把她的话写回世界、推进水位线、跑回合复盘）。
- **旧版接管（`reply_mode=takeover`，已废弃）**：自己调 LLM、执行动作、自己发送，
  仅作迁移兜底，后续版本会移除。

**自主行为**（日程、发呆、主动搭话）仍由插件自己调 LLM 并用 JSON 约束输出、自己发送；
这些调用按协程打标记，不会被自己重复接管。

配置热加载、状态与记忆等核心逻辑在 ``core/``，宿主绑定（模型 / 看图 / 指令 / 发送 / 工具 /
人设）在 ``adapters/``，编辑器接口与访问密码在 ``web/``；本文件只做编排与接线。
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import os
import shutil
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain, Reply
from astrbot.api.star import Context, Star, register

try:
    from astrbot.api import ToolSet
except ImportError:  # pragma: no cover - 测试桩 / 更老宿主
    ToolSet = None  # type: ignore[assignment]

try:  # AstrBot 4.24.2+：附加内容块走这条正规通道
    from astrbot.core.agent.message import TextPart
except Exception:  # pragma: no cover - 老版本没有这个类
    TextPart = None  # type: ignore[assignment]

from .core.constants import (
    DATA_DIR_ENV,
    DEFAULT_REPLY_MODE,
    LLM_HOOK_PRIORITY,
    MIGRATE_FROM_ENV,
    MW_INCOMING_EXTRA,
    MW_TURN_EXTRA,
    PLUGIN_NAME,
    PLUGIN_VERSION,
    PRESENCE_PRIORITY,
    REPLY_TURN_WAIT_SECONDS,
    SCHEDULE_TOOL_NAMES,
    SLEEP_GUARD_PRIORITY,
    SOURCE_PLUGIN_NAMES,
    STATE_ACTION_TIMEOUT,
    STATE_TICK_WAIT_SECONDS,
)
from .core.config_scope import session_scoped
from .core.config_store import ConfigStore
from .core.db import AsyncDatabase
from .core.engine import (
    PENDING_IMAGE_KEEP,
    REPLY_INTERRUPTED,
    REPLY_MUTED,
    MessageContext,
    MyWorldEngine,
    ReplyOutcome,
)
from .core.extensions import ExtensionHost
from .core.message_parts import (
    _FACE_COMPONENT_NAMES,
    _component_named_any,
    _forward_digest,
    _forward_fingerprint,
    _image_components,
    _image_components_debug,
    _image_sources,
    _is_forwarded,
    _provider_supports_images,
    _quoted_parts,
    _quoted_text,
    _sendable_images,
    _sticker_note,
    _with_forward_summary,
)
from .core.messages import (
    _addressing_kind,
    _at_targets,
    _group_name,
    _incoming_identity,
    _is_at_bot,
    _is_command,
    _is_soft_wake,
    _mention_note,
    _reply_target,
    _router_view,
)
from .core.models import pronoun_for
from .core.nickname import compute_nickname
from .core.plugin_compat import profile_weaver_active
from .core.profile_weaver_service import ProfileWeaverService
from .core.prompt import clip_line, prompt_section_index, readable_caption
from .core.request_context import (
    _append_extra_text,
    _drop_persona_instructions,
    _has_voice_component,
    _is_command_event,
    _merged_request_context,
    _safe_brief,
    _selected_provider_extra,
    _split_prompt_injection,
)
from .adapters.commands import AstrBotCommands
from .adapters.llm import AstrBotLLM
from .core.model_call_runtime import observe_native_response
from .core.model_calls import call_context, model_operation
from .adapters.messenger import AstrBotMessenger
from .adapters.persona import AstrBotPersona
from .adapters.profile_commands import (
    ProfileCommandsMixin,
    install_profile_commands,
)
from .adapters.profile_weaver_tools import register_profile_tools, unregister_profile_tools
from .adapters.tools import AstrBotTools, _clip_brief, _tool_result_text
from .adapters.vision import AstrBotVision, _images_head
from .web.api import WebApiMixin
from .web.auth import EditorAuth

# --- 兼容再导出：这些名字从 main.py 搬进了子模块，测试与第三方插件仍按旧路径引用 ---
from astrbot.api.web import request as request

from .core.extensions import ExtensionSpec as ExtensionSpec
from .adapters.session_utils import _swap_event_session as _swap_event_session
from .adapters.tools import MAX_RESULT_CHARS as MAX_RESULT_CHARS
from .adapters.tools import _exception_where as _exception_where
from .adapters.tools import _result_text as _result_text
from .adapters.vision import (
    _caption_prefix as _caption_prefix,
    _clean_forward_summary as _clean_forward_summary,
    _image_fingerprint as _image_fingerprint,
    _strip_image_index as _strip_image_index,
)
from .core.message_parts import (
    _strip_forward_placeholders as _strip_forward_placeholders,
)



def _legacy_plugin_data_dirs(plugin_dir: str) -> list[str]:
    """按优先级列出可能存着旧数据的目录（拿不到宿主路径时退回插件目录旁边）。"""

    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        base = get_astrbot_plugin_data_path()
    except Exception:
        base = os.path.dirname(os.path.abspath(plugin_dir))
    return [os.path.join(base, name) for name in SOURCE_PLUGIN_NAMES]


def _original_plugin_data_dir(plugin_dir: str) -> str:
    """兼容旧调用：列表里的第一个候选目录。"""

    return _legacy_plugin_data_dirs(plugin_dir)[0]


def migrate_from_original(data_dir: str, plugin_dir: str, logger: Any) -> list[str]:
    """第一次启动时，把原插件的数据整份搬过来（只搬一次、绝不覆盖已有数据）。

    两个插件的目录结构完全一样（``world.json`` / ``schedules.json`` /
    ``sessions.json`` / ``state.db`` + 历史快照），所以"直接复制"就是正确做法。
    适合从原插件换过来的人：地图、动作、日程、记忆、日志一条不丢。

    几个边界：
    - 本插件已经有数据（``world.json`` 或 ``state.db``）→ 什么都不做；
    - 原插件的数据目录不存在 / 也是空的 → 什么都不做；
    - SQLite 的 ``-wal`` / ``-shm`` 不复制（那是运行时的临时文件，复制过去反而更危险）；
    - 想关掉：插件配置里关掉「自动接管原插件的数据」，或者设
      ``MY_WORLD_MIGRATE_FROM=none``。

    注意：**别让两个插件同时启用**。两边会各写各的库，谁的改动都不会进对方那份。
    """

    target = Path(data_dir)
    if (target / "world.json").exists() or (target / "state.db").exists():
        return []
    env = (os.environ.get(MIGRATE_FROM_ENV) or "").strip()
    if env.lower() in ("none", "off", "0", "false"):
        return []
    candidates = [Path(env)] if env else [
        Path(item) for item in _legacy_plugin_data_dirs(plugin_dir)
    ]
    source: Path | None = None
    for candidate in candidates:
        try:
            if candidate.resolve() == target.resolve():
                continue
        except OSError:
            continue
        if (candidate / "world.json").exists() or (candidate / "state.db").exists():
            source = candidate
            break
    if source is None:
        return []
    target.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for item in sorted(source.iterdir()):
        if not item.is_file() or item.name.startswith("."):
            continue
        if item.suffix in (".db-wal", ".db-shm") or item.name.endswith(("-wal", "-shm")):
            continue
        destination = target / item.name
        if destination.exists():
            continue
        try:
            shutil.copy2(item, destination)
        except Exception as exc:  # noqa: BLE001 - 迁移失败不能拖垮启动
            logger.warning(f"[my_world] 迁移 {item.name} 失败：{exc}")
            continue
        copied.append(item.name)
    if copied:
        source_name = source.name
        logger.info(
            f"[my_world] 已从旧插件（{source_name}）搬来 {len(copied)} 个文件："
            f"{'、'.join(copied[:6])}{'…' if len(copied) > 6 else ''}。"
            "确认一切正常后可以把原插件停用（不要两个同时跑）。"
        )
    return copied


def _resolve_data_dir(plugin_dir: str) -> str:
    """数据目录：AstrBot 的 data/plugin_data/<插件名>/（可用环境变量覆盖）。"""

    override = (os.environ.get(DATA_DIR_ENV) or "").strip()
    if override:
        return override
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        return os.path.join(get_astrbot_plugin_data_path(), PLUGIN_NAME)
    except Exception:
        # 极少数情况下拿不到宿主路径，退回插件目录，保证插件仍然可用
        return os.path.join(plugin_dir, "data")


# ======================================================================
# 适配器：把 AstrBot 的能力包成 core/ports.py 里的 Protocol
# ======================================================================


def node_label(world: Any, node_id: str) -> str:
    """节点 id -> 中文名（拿不到就退回 id）。"""

    node = world.node_map().get(node_id) if world is not None else None
    return (node.name or node.id) if node is not None else (node_id or "？")










def _chain_items(chain: Any) -> list[Any]:
    """把 MessageChain / MessageEventResult / 裸 list 统一成组件列表。"""

    return [item for item in list(getattr(chain, "chain", chain) or []) if item is not None]


def _chain_texts(chain: Any) -> list[str]:
    """消息链里的文本段（去掉空行）。"""

    return [
        str(component.text)
        for component in _chain_items(chain)
        if isinstance(component, Plain) and str(component.text).strip()
    ]


def _extra_components(chain: Any, *, skip_plain: bool = True) -> list[Any]:
    """消息链里「不是文本」的那些段：图片、语音、文件、@…

    别的插件靠回复钩子把东西塞进消息链（表情包塞图、语音插件换成语音），
    这些段没有对应的文本，只留文本就会把它们丢掉——所以单独挑出来，
    等发送的时候跟着最后一条消息一起走。
    """

    picked: list[Any] = []
    for component in _chain_items(chain):
        if skip_plain and isinstance(component, Plain):
            continue
        picked.append(component)
    return picked


















# ======================================================================
# 插件主体
# ======================================================================


@register(
    PLUGIN_NAME,
    "Whereis-Alice（原版 exneverbur）",
    "给 Bot 一个私有空间、动作、日程、场景记忆和工具能力，让 ta 像住在群里一样生活。",
    PLUGIN_VERSION,
)
class MyWorldPlugin(ProfileCommandsMixin, WebApiMixin, Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context, config)
        self.config = config

        self.enabled = _cfg_bool(config.get("enabled"), True)
        self.web_enabled = _cfg_bool(config.get("web_enabled"), True)
        self.llm_provider_id = str(config.get("llm_provider_id") or "").strip()
        # 打杂模型（文本、便宜）：补工具参数、压上下文、判断图和话题的关系
        self.vision_provider_id = str(config.get("vision_provider_id") or "").strip()
        self.helper_provider_id = (
            str(config.get("helper_provider_id") or "").strip()
            # 老配置里"上下文压缩"和"关系分析"是两个独立模型：并进打杂模型，
            # 让用户只需要理解两个便宜档（看图 / 文本）
            or str(config.get("context_provider_id") or "").strip()
        )
        self.utility_provider_id = self.helper_provider_id
        # 判断模型：只接"挑一个 / 打分 / 抽字段"这类纯判断。留空跟随打杂模型。
        self.judge_provider_id = (
            str(config.get("judge_provider_id") or "").strip() or self.helper_provider_id
        )
        self.consolidate_provider_id = (
            str(config.get("consolidate_provider_id") or "").strip()
            or self.utility_provider_id
        )
        # 事件模型：写"这件事的结果"那个模型。留空跟随打杂模型。
        self.event_provider_id = (
            str(config.get("event_provider_id") or "").strip() or self.utility_provider_id
        )
        # 内容生成模型：只在编辑器里"批量生成动作 / 地点"时用，留空回落到主模型
        self.creator_provider_id = str(config.get("creator_provider_id") or "").strip()
        self._sampling_config = {
            "enabled": _cfg_bool(config.get("sampling_enabled"), False),
            "temperature": config.get("sampling_temperature"),
            "top_p": config.get("sampling_top_p"),
            "frequency_penalty": config.get("sampling_frequency_penalty"),
            "presence_penalty": config.get("sampling_presence_penalty"),
        }
        self.tick_interval = max(5, _cfg_int(config.get("tick_interval"), 60))
        self.decider_interval = max(
            self.tick_interval, _cfg_int(config.get("decider_interval"), 300)
        )
        self.debug = _cfg_bool(config.get("debug"), False)
        # 接管回复也要过一遍 AstrBot 的回复钩子，别的插件（好感度之类）才看得见
        self.reply_hook_bridge = _cfg_bool(config.get("reply_hook_bridge"), True)
        # 回复模式：默认只注入提示词；旧版接管（自己调模型自己发）仅在
        # reply_mode=takeover 时启用，留给还在迁移的用户，后续版本会移除。
        self.reply_mode = (
            str(config.get("reply_mode") or "").strip().lower() or DEFAULT_REPLY_MODE
        )
        # 「日程三件套」是给主人格用的工具：开着时它们会出现在 AstrBot 的工具列表里
        # （所有会话都能看到）。工具本来就多的用户可以把它们摘掉，保持工具列表干净。
        self.expose_schedule_tools = _cfg_bool(
            config.get("expose_schedule_tools"), True
        )
        # 从原插件换过来的人不用手动搬数据：新目录是空的就把原目录整份复制过来
        self.migrate_from_original = _cfg_bool(
            config.get("migrate_from_original"), True
        )
        self.log_level = str(config.get("log_level") or "INFO").upper()

        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        data_dir = _resolve_data_dir(plugin_dir)
        if self.migrate_from_original:
            migrate_from_original(data_dir, plugin_dir, self.logger)
        self.store = ConfigStore(data_dir)
        self.profile_weaver_service = ProfileWeaverService(
            self, str(self.store.data_dir.parent)
        )
        register_profile_tools(
            self, legacy_active=profile_weaver_active(self.context), toolset_type=ToolSet
        )
        # 扩展挂载点：独立插件（如果装了）会通过 `star_cls.extension_host` 找到它
        self.extension_host = ExtensionHost(self)
        created = self.store.ensure_files()
        self.db = AsyncDatabase(self.store.db_path)
        self.auth = EditorAuth(self, str(config.get("web_password") or ""))

        self._last_events: "OrderedDict[str, AstrMessageEvent]" = OrderedDict()
        self._llm_responses: "OrderedDict[str, Any]" = OrderedDict()
        """最近一次主模型调用的原始响应（按会话存，供回复钩子使用）。"""
        self._tick_task: asyncio.Task | None = None
        self._self_initiated_depth = 0
        self._self_initiated_tasks: set[asyncio.Task] = set()
        """正在调模型的那几条协程（我们自己的调用）。"""
        self._reflection_tasks: set[asyncio.Task] = set()
        """回合复盘的后台任务：发送完成后才跑，不拖慢聊天。"""
        self._self_initiated_untracked = 0
        self._reply_locks: dict[str, asyncio.Lock] = {}
        """每个会话一条：同一条会话的接管排队执行，避免两条回复互相不知道对方。"""
        self._live_say_tail: dict[str, str] = {}
        """刚即时发出去的那一句：连着说下一句时按它的字数停一下，保持打字的节奏。"""
        self._turn_provider_overrides: dict[str, str] = {}
        """这一轮别的插件临时指定的模型（按会话记，跑完就撤）。"""
        # 发送方持有「失败冷却」，所以自己留一份（回退响应路径也要用它判断）
        self.messenger = AstrBotMessenger(self)
        # 看图（多模态）：图片转述、天气工具返回的图都走它
        self.vision = AstrBotVision(self, self.vision_provider_id)
        # 人设：默认沿用 AstrBot 给这个会话选的那份，也可以在「全局设置 → 她是谁」里
        # 换成插件自己的一份（跟着预设走，不再随会话组漂移）
        self.persona_port = AstrBotPersona(self)

        self.engine = MyWorldEngine(
            store=self.store,
            db=self.db,
            llm=AstrBotLLM(self, sampling=self.sampling_params()),
            helper_llm=AstrBotLLM(self, self.helper_provider_id, role="helper", sampling=self.sampling_params()),
            judge_llm=AstrBotLLM(self, self.judge_provider_id, role="judge", sampling=self.sampling_params()),
            context_llm=AstrBotLLM(self, self.utility_provider_id, role="context", sampling=self.sampling_params()),
            creator_llm=AstrBotLLM(self, self.creator_provider_id, role="creator", sampling=self.sampling_params()),
            event_llm=AstrBotLLM(self, self.event_provider_id, role="event", sampling=self.sampling_params()),
            consolidate_llm=AstrBotLLM(self, self.consolidate_provider_id, role="consolidate", sampling=self.sampling_params()),
            describer=self.vision,
            messenger=self.messenger,
            tools=AstrBotTools(self),
            commands=AstrBotCommands(self),
            persona=self.persona_port,
            clock=None,
            tick_seconds=float(self.tick_interval),
            decider_interval=float(self.decider_interval),
            debug=self.debug,
            logger=self.logger,
            # 扩展挂载点：扩展包（独立插件）拿到这个实例后自己 register
            extensions=self.extension_host,
        )
        self.engine.profile_weaver_service = self.profile_weaver_service
        # 别的插件的数据目录在同一个 plugin_data 下（心迹画像的同步要用）
        self.engine.external_data_root = str(self.store.data_dir.parent)
        # 调试回显实时发：工具/指令调用在发生的那一下就走这条通道，
        # 不再等整轮动作跑完才一起推给群里
        self.engine.debug_sink = self._send_debug_live
        # 即时发言：慢动作（联网检索、生图、录视频这类）开始之前她已经说出口的那几句，
        # 当场发出去，不再等整段流水线跑完才一起冒出来
        self.engine.say_sink = self._send_live_say
        # 正在处理「推进 tick」的会话：连点时只认第一次，别攒成一串 tick
        self._advancing: set[str] = set()

        self._register_web_apis(PLUGIN_NAME)

        for name in created:
            self.logger.info(f"[my_world] 首次启动，已生成默认配置 {name}")
        for warning in self.engine.load_warnings:
            self.logger.warning(f"[my_world] 配置提醒：{warning}")
        if not self.engine.enabled_session_ids():
            self.logger.info(
                "[my_world] 会话白名单为空，请在群聊里发送「/mw session add」"
                "或在 WebUI 的我的世界编辑器里添加会话。"
            )

    # ================= 运行参数 =================

    # 编辑器那几个按钮（唤醒 / 打断 / 推进 tick）的等待上限走这两个方法，
    # 而不是让 web 层直接读模块常量：测试与热调都改 main 里的那份。
    def state_action_timeout(self) -> float:
        return STATE_ACTION_TIMEOUT

    def state_tick_wait_seconds(self) -> float:
        return STATE_TICK_WAIT_SECONDS

    # ================= 生命周期 =================

    async def initialize(self) -> None:
        if not self.enabled:
            self.logger.info("[my_world] 插件已禁用（enabled=false）")
            return
        self._sync_schedule_tools()
        if self._tick_task is None or self._tick_task.done():
            self._tick_task = asyncio.create_task(self._tick_loop())
        self.logger.info(
            f"[my_world] 世界时钟已启动：每 {self.tick_interval} 秒 1 tick，"
            f"决策间隔 {self.decider_interval} 秒"
        )
        if self.web_enabled:
            self.logger.info(
                "[my_world] 网页编辑器：WebUI 插件详情页 → 「我的世界编辑器」"
            )

    def _sync_schedule_tools(self) -> None:
        """按配置决定要不要把「日程三件套」留给宿主大模型。

        这三个工具是给主人格用的（用户可以直接跟她说「把日程改到七点」）。
        但工具一多，每个会话的请求都要多背三条，所以给一个开关：
        关掉就直接从 AstrBot 的工具管理器里摘掉，宿主这一侧完全看不到；
        打开（默认）时保持原样，什么都不做。
        """

        if self.expose_schedule_tools:
            return
        try:
            manager = self.context.get_llm_tool_manager()
        except Exception:
            return
        remover = getattr(manager, "remove_func", None)
        if not callable(remover):
            return
        for name in SCHEDULE_TOOL_NAMES:
            try:
                remover(name)
            except Exception:
                continue
        self.logger.info(
            "[my_world] 已按配置隐藏日程工具（" + "、".join(SCHEDULE_TOOL_NAMES) + "）"
        )

    def turn_provider_override(self, session_id: str) -> str:
        """这一轮里别的插件临时指定的模型 id（没有就返回空串）。"""

        return str(self._turn_provider_overrides.get(str(session_id or ""), "") or "").strip()

    async def _host_persona_prompt(self, event: Any, req: Any, session_id: str) -> str:
        """宿主这一轮真正写进 ``system_prompt`` 的那份人设原文。

        拿它的唯一用途是"精确摘掉"：宿主把人格拼成 ``# Persona Instructions`` 一段，
        而别的插件（世界树、有的记忆插件）经常紧跟着往同一个字段里追加内容。
        只有知道人设原文到哪儿结束，才能只摘人设、不误伤别人。

        拿不到就返回空串，调用方会退回原来的启发式。
        """

        manager = getattr(self.context, "persona_manager", None)
        if manager is None:
            return ""
        resolver = getattr(manager, "resolve_selected_persona", None)
        if callable(resolver):
            try:
                settings: Any = None
                config_getter = getattr(self.context, "get_config", None)
                if callable(config_getter):
                    raw_config = config_getter() or {}
                    if isinstance(raw_config, dict):
                        settings = raw_config.get("provider_settings")
                platform = ""
                platform_getter = getattr(event, "get_platform_name", None)
                if callable(platform_getter):
                    platform = str(platform_getter() or "")
                conversation = getattr(req, "conversation", None)
                _persona_id, persona, _forced, _webchat = await resolver(
                    umo=session_id,
                    conversation_persona_id=getattr(conversation, "persona_id", None),
                    platform_name=platform,
                    provider_settings=settings,
                )
                if isinstance(persona, dict):
                    text = str(persona.get("prompt") or "").strip()
                else:
                    text = str(getattr(persona, "prompt", "") or "").strip()
                if text:
                    return text
            except Exception:
                pass
        # 退路：插件自己那份「AstrBot 给这个会话的人格」解析
        try:
            return await self.persona_port.astrbot_persona_text(session_id)
        except Exception:
            return ""

    async def terminate(self) -> None:
        # 宿主重载插件时是「先调 terminate()，再解绑事件处理器」，
        # 这个窗口里进来的消息还会走到这个实例上。标记一下，让所有入口直接装死。
        self._retired = True
        task, self._tick_task = self._tick_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        pending = [task for task in self._reflection_tasks if not task.done()]
        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._reflection_tasks.clear()
        unregister_profile_tools(self)
        try:
            await self.db.close()
        except Exception:
            pass
        self.logger.info("[my_world] 已停止世界时钟")

    @property
    def retired(self) -> bool:
        """这个实例已经被终止（重载/卸载）了。"""

        return bool(getattr(self, "_retired", False))

    async def _tick_loop(self) -> None:
        # 固定节拍：按「上一次唤醒 + 间隔」推算下一次唤醒，而不是跑完再睡 60 秒。
        # 后者会被每轮 tick 自身的耗时（调模型可能要好几秒）一直往后推，
        # 攒着攒着就整分钟跳过去，那一分钟到点的日程永远等不到。
        next_at = time.time() + self.tick_interval
        while True:
            try:
                await asyncio.sleep(max(0.5, next_at - time.time()))
                next_at += self.tick_interval
                if next_at < time.time():
                    # 落后太多（宿主机卡顿 / 长时间睡眠）就重新对齐，别追赶
                    next_at = time.time() + self.tick_interval
                await self.engine.tick()
                # tick 的逐拍明细不再写后台日志（翻起来全是「t=1902 node=…」），
                # 只留在引擎的内存环里，编辑器「运行记录」页读它。
                # 一个会话组只催一次决策：按"会话"逐个催的话，同一个她会被催好几遍，
                # 而且第二次很可能挂在别的会话名下决定（落点就跟着跑了）
                for session_id in self.engine.tick_session_ids():
                    try:
                        await self.engine.maybe_decide(session_id)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self.logger.warning(f"[my_world] 自主决策失败 session={session_id}: {exc}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.warning(f"[my_world] 世界时钟出错：{exc}")

    # ================= 事件记录 =================

    def last_event(self, session_id: str) -> AstrMessageEvent | None:
        return self._last_events.get(session_id)

    async def pending_intervention(self, session_id: str) -> float:
        """联动接口：她正在等群友拿主意吗？返回等待截止时间戳（0 = 不在等）。

        「意图路由」在等待期间会把这个会话的消息**合并放行、跳过回复冷却**，
        让群友的回应尽快交到她手里；查不到或出错一律返回 0，路由照旧工作。
        """

        if not session_id:
            return 0.0
        try:
            return float(await self.engine.pending_intervention(session_id))
        except Exception:
            return 0.0

    def last_event_any(self) -> AstrMessageEvent | None:
        """最近一次见过的消息事件（拿不到会话 id 时给工具调用兜底）。"""

        if not self._last_events:
            return None
        return next(reversed(self._last_events.values()))

    def nearby_event(self, session_id: str) -> AstrMessageEvent | None:
        """同一个会话组里最近收到过的一条事件（这个会话自己的优先）。

        指令和工具都是拿着一条真实事件跑的，出图 / 发语音这类插件常按事件的
        会话来决定结果发到哪儿。借**别的会话组**的事件就会让结果跑错地方
        （私聊里让她拍照、图却出现在群里），所以这里只在这一组里找替身；
        组里一条都没有就返回 None，让调用方自己决定跳过还是报错。
        """

        exact = self.last_event(session_id)
        if exact is not None:
            return exact
        if not str(session_id or "").strip():
            # 调用方没说要发到哪儿（例如编辑器里试调工具）：最近一条就行，
            # 没有"指到哪个会话"的问题，也就没有跑错地方的风险。
            return self.last_event_any()
        try:
            siblings = {str(item) for item in self.engine.group_sessions(session_id)}
        except Exception:
            siblings = {str(session_id)}
        for candidate in reversed(list(self._last_events)):
            if str(candidate) in siblings:
                self.logger.debug(
                    f"[my_world] 借用了同组会话 {candidate} 的事件上下文"
                )
                return self._last_events[candidate]
        return None

    def remember_llm_response(self, session_id: str, response: Any) -> None:
        """记下主模型的原始响应（回复钩子要拿它给别的插件看）。"""

        if response is None or not session_id:
            return
        self._llm_responses[session_id] = response
        self._llm_responses.move_to_end(session_id)
        while len(self._llm_responses) > 50:
            self._llm_responses.popitem(last=False)

    def take_llm_response(self, session_id: str) -> Any:
        response = self._llm_responses.pop(session_id, None)
        return response

    def _pronoun(self) -> str:
        """由全局设置里的性别决定称呼：她 / 他 / ta。"""

        return pronoun_for(self.engine.world.gender)

    def _can_admin(self, event: AstrMessageEvent) -> bool:
        """能不能执行管理类指令。

        AstrBot 自己的管理员永远可以；另外「全局设置 → 管理员 QQ」里列出来的人也可以
        （有些群不希望为了用几条指令就去动 AstrBot 的管理员配置）。
        """

        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        listed = {str(item).strip() for item in (self.engine.world.admin_ids or [])}
        sender = str(getattr(event, "get_sender_id", lambda: "")() or "").strip()
        return bool(sender) and sender in listed

    # ================= 给其他插件用的只读接口 =================

    async def social_snapshot(self, session_id: str) -> dict[str, Any] | None:
        """某个会话当前的状态快照，供其他插件联动（例如意图路由按社交欲调阈值）。

        会话没启用本插件时返回 None，调用方应当回落到自己的默认行为。
        """

        if not self.enabled or not session_id:
            return None
        try:
            if not self.engine.is_enabled(session_id):
                return None
            state = await self.engine.load_state(session_id, cold_start=False)
        except Exception:
            return None
        return {
            # affect 是「心潮」；social 是老名字，保留给还没更新的联动方
            "affect": round(state.affect, 4),
            "social": round(state.affect, 4),
            # 效价：心情好坏（0.5 中性）。联动方可以据此区分"孤独想聊"和"恼火想怼"
            "valence": round(state.valence, 4),
            "storm": bool(state.storm),
            "loneliness": round(state.loneliness, 4),
            "energy": round(state.energy, 4),
            "curiosity": round(state.curiosity, 4),
            "boredom": round(state.boredom, 4),
            "willingness": round(self.engine.willingness(state), 4),
            "mood": state.mood,
            "state": state.state,
            "style_cell": state.last_style_cell,
            # 睡着时联动方最好直接不判断（意图路由会拿它当硬门）
            "sleeping": bool(self.engine.is_asleep(state)),
            "node_id": state.node_id,
            "world_time": state.world_time,
            "interject_enabled": bool(self.engine.world.decider.enabled),
        }

    async def reply_willingness(self, session_id: str) -> float | None:
        """她对「现在该不该开口」的整体意愿（0~1）。

        综合孤独感、无聊、好奇、心潮与疲惫、被冷落等因素，
        不启用本插件的会话返回 None，调用方应回落到自己的默认行为。
        """

        snapshot = await self.social_snapshot(session_id)
        if snapshot is None:
            return None
        return float(snapshot.get("willingness") or 0.0)

    def _remember_event(self, event: AstrMessageEvent) -> None:
        session_id = event.unified_msg_origin
        self._last_events[session_id] = event
        self._last_events.move_to_end(session_id)
        while len(self._last_events) > 200:
            self._last_events.popitem(last=False)

    async def _annotate_message(
        self,
        event: AstrMessageEvent,
        text: str,
        *,
        sources: list[str] | None = None,
        summarize_forward: bool = False,
        image_labels: dict[str, str] | None = None,
    ) -> str:
        """给消息补上模型看不到的部分：图片里有什么、这是不是一条转发。

        ``sources``：图片地址已经取过了就传进来，省得同一个组件解析两遍
        （NapCat 这类协议端解析一次可能要真去取图）。

        ``summarize_forward``：这条转发值得读一遍时（她要处理的 @ / 私聊 / 唤醒），
        用多模态模型把转发内容（含里面的图）压成摘要替换正文；关掉或读不出来
        就退回原来那句「这是一条转发的聊天记录」。
        """

        notes: list[str] = []
        if _is_forwarded(event):
            summary = await self._forward_summary_text(event) if summarize_forward else ""
            if summary:
                text = _with_forward_summary(text, summary)
                notes.append(
                    "这是一条转发消息，上面那段是它的内容摘要，不是当前群里正在说的话"
                )
            else:
                notes.append("这是一条转发的聊天记录，不是当前群里正在说的话")
        mention = _mention_note(event)
        if mention:
            notes.append(mention)
        quoted = await self._quoted_note(event)
        if quoted:
            notes.append(quoted)
        sticker = _sticker_note(event)
        if sticker:
            notes.append(sticker)
        notes.extend(
            await self._annotate_images(
                event, text, sources=sources, labels=image_labels
            )
        )
        if not str(text or "").strip() and _has_voice_component(event):
            # 纯语音消息：正文是空的，不说明一下她会以为自己收到了一条空白消息。
            # 内容转写要靠 AstrBot 那边装语音转文字的能力，这里只把"发生了什么"讲清楚。
            notes.append("这是一段语音消息，你听不到里面的内容")
        if not notes:
            return text
        return f"{text}\n［{'；'.join(notes)}］".strip()

    async def _quoted_note(self, event: AstrMessageEvent) -> str:
        """他引用了哪一条：聊天记录里还看得到就给个开头，看不到才写原文。

        引用内容本来完全没进正文（只喂给了看图那一步），于是「如何评价」这种
        靠引用才有意义的话，她只能对着聊天记录里被掐成半句的旧消息猜。
        """

        who, text = _quoted_parts(event)
        body = " ".join(str(text or "").split())
        if not body:
            return ""
        someone = who or "有人"
        context = self.engine.world.context
        line_limit = max(40, int(getattr(context, "chat_line_chars", 500) or 500))
        found = await self.engine.quote_in_records(event.unified_msg_origin, body)
        # 记录里就有**而且那一行没被截断**时，只点出开头让她自己往上对照；
        # 那条本身已经被截断（或者干脆不在留档里）时，还是把原文写全——
        # 否则她对着"还有 N 字没显示"的那半句，根本不知道被引用的是哪一段。
        if found and len(body) <= line_limit:
            head = body[:40] + ("…" if len(body) > 40 else "")
            return (
                f"他引用的是上面聊天记录里的那条（{someone}：「{head}」）"
                "——照上面那条看，这不是他这次打的字"
            )
        limit = max(0, int(getattr(context, "quote_chars", 1000) or 0))
        if limit <= 0:
            return f"他引用了一条更早的消息（{someone}），那条已经不在聊天记录里了"
        shown = body if len(body) <= limit else clip_line(body, limit)
        where = "上面聊天记录里那条太长，只显示了开头" if found else "聊天记录里已经没有了"
        return (
            f"他引用了一条更早的消息（{someone}），{where}，"
            f"原文：「{shown}」——这是引用，不是他这次打的字"
        )

    async def _forward_summary_text(self, event: AstrMessageEvent) -> str:
        """把这条转发读成摘要：关掉、没配多模态模型、读不出来都返回空串。"""

        vision = getattr(self, "vision", None)
        if vision is None or not vision.enabled:
            return ""
        try:
            settings = self.engine.world.vision
            if not bool(getattr(settings, "forward_summary", True)):
                return ""
            digest, images, forward_ids = await _forward_digest(event)
            if not digest.strip() and not images:
                return ""
            summary = await vision.summarize_forward(
                digest,
                images,
                fingerprint=_forward_fingerprint(forward_ids, digest, images),
            )
            if summary:
                await self.engine.note_vision(
                    event.unified_msg_origin,
                    ok=True,
                    images=len(images),
                    detail=f"转发聊天记录已压成摘要（{len(summary)} 字）",
                )
            return summary
        except Exception as exc:
            self.logger.debug(f"[my_world] 转发摘要失败：{exc}")
            return ""

    async def _annotate_images(
        self,
        event: AstrMessageEvent,
        text: str,
        *,
        sources: list[str] | None = None,
        labels: dict[str, str] | None = None,
    ) -> list[str]:
        """图片这一段的处理：转了述就写描述，没配转述模型就交给多模态主模型。

        图片本来在消息里是看不见的，出了任何岔子都要留下一条日志——
        否则用户只会看到"模型好像没看到图"，却没有任何线索。

        ``labels``：**准备回复时**才有的编号（``图片地址 -> 图1``，见 `_reply_images`）。
        给了它就说明交付决策已经做完了，这里只负责把"这一条消息的图是第几号"讲清楚，
        不再自己转述一遍。
        """

        components = _image_components(event)
        resolved = (
            [str(item) for item in sources if str(item).strip()]
            if sources is not None
            else await _image_sources(event)
        )
        if not components and not resolved:
            return []
        session_id = event.unified_msg_origin
        sources = resolved
        if labels is not None:
            mine = [str(labels[str(url)]) for url in sources if str(url) in labels]
            if not mine:
                # 这条消息的图这一轮没交给主模型看（比如被转述了）：转述那句由
                # `_reply_images` 的 notes 交回来，这里不重复说
                return []
            listed = "、".join(mine)
            # 编号说给她听，就得同时说清"这是内部编号、别照着念"——不然她会
            # 直接回"图2那张…"，而对方根本看不到编号（见 core/prompt.py 里同一句）
            guard = "（这套编号只给你自己对上用，回话里别写「图1」这种说法）"
            if len(mine) >= len(sources):
                return [
                    f"这条消息带了 {len(mine)} 张图片，就是{listed}，已经一起发给你了{guard}"
                ]
            return [
                f"这条消息带了 {len(sources)} 张图片，其中 {listed} 已经一起发给你，"
                f"其余几张只有文字描述{guard}"
            ]
        vision = getattr(self, "vision", None)
        if not sources:
            detail = "收到图片但拿不到可用地址"
            debug = _image_components_debug(event)
            if debug:
                detail = f"{detail}（组件字段：{debug}）"
            await self.engine.note_vision(
                session_id, ok=False, images=len(components), detail=detail
            )
            # QQ 表情包（大表情）几乎都取不到图：别让她以为"什么都没收到"，
            # 后面 _sticker_note 会把它的文案写进消息里。
            if components and all(
                _component_named_any(item, _FACE_COMPONENT_NAMES)
                for item in components
            ):
                return ["对方发的是 QQ 表情 / 表情包，插件取不到图（已记进日志）"]
            return ["对方发了一张图片，但插件没能取到图片地址（已记进日志）"]

        captions: list[str] = []
        if vision is None or not vision.enabled:
            return [_images_head(len(sources))]
        # 收到的图**现在就**看看是不是已经超过「一次带几张」的上限了：
        # 超出的那几张（先来的）立刻转成文字，不等调主模型那一刻。
        notes = await self._caption_overflow(event, sources)
        return [_images_head(len(sources)), *notes]

    async def _caption_overflow(
        self, event: AstrMessageEvent, sources: list[str]
    ) -> list[str]:
        """收到的图已经超过「一次带几张」：把更早、还没转述的那几张先转成文字。

        上限之内那几张会随这次请求交给主模型看，不必转述（同一张图只看一遍）；
        超出的那几张反正不会被带上，趁**收到图的时候**就转掉——等调主模型那一刻
        再转，她已经在等回复了。返回要写进这条消息注解的那几句。
        """

        session_id = event.unified_msg_origin
        cap = max(1, int(self.engine.world.context.image_max))
        history = await self.engine.chat_images_for_reply(session_id, PENDING_IMAGE_KEEP)
        candidates = list(
            dict.fromkeys(
                [
                    *[
                        str(item.get("url") or "").strip()
                        for item in history
                        if str(item.get("url") or "").strip()
                    ],
                    *[str(item).strip() for item in sources if str(item).strip()],
                ]
            )
        )
        keep = set(candidates[-cap:])
        older = [url for url in candidates if url not in keep]
        if not older:
            return []
        return await self._caption_old_images(event, older, sources=sources)

    async def _reply_images(
        self,
        event: AstrMessageEvent,
        sources: list[str],
        extra: list[str] | None = None,
    ) -> tuple[list[str], dict[str, str], list[str]]:
        """这一轮直接交给模型看的图、它们的编号（``图1``/``图2``…），以及要转成文字的那几张。

        **规则**：发送范围（这次真的会进提示词的聊天记录）里**最近的 ``image_max`` 张图**
        都交给主模型看——不管它是这一条消息带的、还是前面几条带的，按时间序编号 图1/图2/…，
        和附件顺序一一对应；比它们更旧、但仍在发送范围内的图只以文字转述出现
        （被压成概览的老图压根不在这个范围里，不用管）。

        - 主模型看不见图时：这几张全部转述成文字（一次打包），不附图；
        - **没配转述模型**时：主模型看不看得见都照样附图（否则这些图就彻底没了）；
        - 已经转述过的图这次被附上了：它的转述不再渲染出来（见
          ``engine.attach_image_captions`` 与 ``prompt._render_rows``）。

        转述只发生一次：本来就要交给主模型看的图不会再转述一遍（白花钱）。

        ``extra`` 是别的插件往这一轮请求里加的图（``req.image_urls``）：和这一条消息
        自己带的图一样对待，去重之后一起编号——那是别人已经认可的"要给模型看"的图，
        丢掉就等于这一轮少看了一张。
        """

        session_id = event.unified_msg_origin
        context = self.engine.world.context
        cap = max(1, int(context.image_max))
        vision = getattr(self, "vision", None)
        captioned = bool(vision is not None and vision.enabled)
        mode = str(getattr(context, "chat_image_inline", "auto") or "auto").lower()
        inline = await self._chat_images_inline(session_id)
        pending = await self.engine.take_pending_images(session_id)
        # 按时间排好（先来的在前）：被搭话的那几条消息里的图排前面，最新那张在最后
        own = list(
            dict.fromkeys(
                [
                    str(ref).strip()
                    for ref in [*pending, *sources, *list(extra or [])]
                    if str(ref).strip()
                ]
            )
        )
        # 「发送范围里」的图 = 这次真的会进提示词的聊天记录里的图（被压成概览的早就不在这里了，
        # 那种不用管）。取其中**最近的 ``image_max`` 张**交给主模型看，**不管它是这一条消息带的
        # 还是前面几条带的**——编号按时间序 图1/图2/…，和附件顺序一一对应。
        window = await self.engine.chat_images_for_reply(session_id, PENDING_IMAGE_KEEP)
        all_urls = list(
            dict.fromkeys(
                [
                    *[
                        str(item.get("url") or "").strip()
                        for item in window
                        if str(item.get("url") or "").strip()
                    ],
                    *own,
                ]
            )
        )
        # 附件 = 这些图里**最近的 cap 张**（`all_urls` 已经是时间序），编号按同一顺序：
        # 第 i 张就是 图i，和附件顺序一一对应。
        # 显式关掉（``never``）就一张都不附；检测不出主模型能不能看图、又没配转述模型时，
        # 照样附过去（否则这些图就彻底没人看了）
        attach = bool(inline) or (not captioned and mode != "never")
        refs: list[str] = list(all_urls[-cap:]) if attach else []
        marks: dict[str, str] = {
            url: f"图{index}" for index, url in enumerate(refs, start=1)
        }
        notes: list[str] = []
        if captioned:
            dropped = [
                url for url in all_urls if url and url not in set(refs)
            ]
            notes = await self._caption_old_images(event, dropped, sources=sources)
        if not refs:
            return [], {}, notes
        detail = f"把 {len(refs)} 张图交给多模态主模型"
        if marks:
            detail += f"（聊天记录里的 {len(marks)} 张已编号）"
        await self.engine.note_vision(
            session_id, ok=True, images=len(refs), detail=detail
        )
        return refs, marks, notes

    async def _caption_old_images(
        self,
        event: AstrMessageEvent,
        urls: list[str],
        *,
        sources: list[str] | None = None,
    ) -> list[str]:
        """把这几张图打包转述一次，返回要写进这条消息注解的那几句。

        挂在哪：属于**这条消息**的图（她正看着的那几张）写进这条消息的注解；
        更早的图把描述挂回它们原来那条聊天记录上——不然描述会串到别人头上，
        下一轮她也对不上"哪张图是什么"。
        """

        session_id = event.unified_msg_origin
        vision = getattr(self, "vision", None)
        # 已经转述过的（同一张图在群里发两遍、或者上一轮转过了）不再转第二遍
        targets = self.engine.uncaptioned_images(
            [str(url).strip() for url in urls if str(url).strip()]
        )
        if vision is None or not vision.enabled or not targets:
            return []
        try:
            captions = await vision.describe(
                targets,
                question=str(event.get_message_str() or ""),
                quoted=_quoted_text(event),
                context_lines=await self._recent_chat_lines(event),
            )
        except Exception as exc:
            self.logger.debug(f"[my_world] 图片转述异常：{exc}")
            captions = []
        pairs = [
            (url, str(caption).strip())
            for url, caption in zip(targets, captions or [])
            if str(caption or "").strip()
        ]
        if not pairs:
            reason = str(getattr(vision, "last_error", "") or "转述没有返回内容")
            await self.engine.note_vision(
                session_id, ok=False, images=len(targets), detail=reason
            )
            return ["有几张图没能转述成功，只当看过了（已记进日志）"]
        here = {str(item).strip() for item in (sources or [])}
        # 这条消息里的第几张：编号要跟「图1/图2」对得上，不能按"这次转述了几张"重排
        order = {
            str(url).strip(): index + 1
            for index, url in enumerate(sources or [])
            if str(url).strip()
        }
        notes: list[str] = []
        elsewhere: dict[str, str] = {}
        for url, caption in pairs:
            text = readable_caption(caption)
            if not text:
                continue
            if url in here:
                notes.append(f"图{order.get(url, len(notes) + 1)}：{text}")
            else:
                elsewhere[url] = text
        attached = await self.engine.attach_image_captions(session_id, elsewhere)
        self.engine.mark_images_captioned([url for url, _text in pairs])
        await self.engine.note_vision(
            session_id,
            ok=True,
            images=len(pairs),
            detail=(
                f"转述了 {len(pairs)} 张（没随这次请求交给主模型的那几张）"
                + (f"，其中 {attached} 张挂回了它们原来那条消息" if attached else "")
                + "："
                + "；".join(text for _url, text in pairs)
            ),
        )
        return notes

    async def _chat_images_inline(self, session_id: str) -> bool:
        """聊天记录里的图要不要直接发给主模型（配置 + Provider 勾选的模态）。"""

        mode = str(
            getattr(self.engine.world.context, "chat_image_inline", "auto") or "auto"
        ).lower()
        if mode == "never":
            return False
        if mode == "always":
            return True
        return bool(await self._main_model_sees_images(session_id))

    async def _main_model_sees_images(self, session_id: str) -> bool | None:
        """主模型能不能吃图：读 AstrBot 里这个 Provider 勾选的模态。

        读不到（老版本、没有这个字段、不是 AstrBot 的 Provider）返回 ``None``——
        调用方按"不能"处理，用户可以把它改成 ``always`` 强制打开。
        """

        try:
            provider_id = (getattr(self, "llm_provider_id", "") or "").strip()
            if not provider_id:
                provider_id = await self.context.get_current_chat_provider_id(session_id)
            if not provider_id:
                return None
            provider = self.context.get_provider_by_id(provider_id)
            return _provider_supports_images(provider)
        except Exception as exc:
            self.logger.debug(f"[my_world] 读主模型的模态失败：{exc}")
            return None

    async def _recent_chat_lines(self, event: AstrMessageEvent) -> list[str]:
        """最近几句群聊，用来给图片转述提供话题背景。"""

        session_id = event.unified_msg_origin
        try:
            state = await self.engine.load_state(session_id, cold_start=False)
            items = self.engine.chat_context(state)
        except Exception:
            return []
        lines: list[str] = []
        for item in items:
            who = "她" if item.get("is_self") else (item.get("name") or item.get("user_id") or "")
            content = " ".join(str(item.get("text") or "").split())
            if content:
                lines.append(f"{who}: {content[:80]}")
        return lines

    def set_self_initiated(self) -> int:
        """标记"接下来这段是我们自己发起的调用"。"""

        self._self_initiated_depth += 1
        task = self._current_task()
        if task is not None:
            self._self_initiated_tasks.add(task)
            # 防御：万一某次调用没走到 reset（异常被吞、任务被取消……），
            # 集合里堆着死任务会让后面的钩子判断出现意外，定期清一次只留当前任务。
            if len(self._self_initiated_tasks) > 8:
                self._self_initiated_tasks.clear()
                self._self_initiated_tasks.add(task)
        return self._self_initiated_depth

    def reset_self_initiated(self, _token: int = 0) -> None:
        self._self_initiated_depth = max(0, self._self_initiated_depth - 1)
        task = self._current_task()
        if task is not None:
            self._self_initiated_tasks.discard(task)

    @staticmethod
    def _current_task() -> asyncio.Task | None:
        try:
            return asyncio.current_task()
        except RuntimeError:
            # 没有正在跑的事件循环（同步收尾之类），当作"不在任务里"
            return None

    def in_self_initiated_call(self) -> bool:
        """当前这条协程是不是"我们自己正在调模型"的那一条。

        按协程判断（而不是"只要有调用在跑"）：我们自己的请求依旧不会被自己重复接管，
        但同一时间到达的真人消息走的是另一条协程，照常处理。
        """

        task = self._current_task()
        if task is None:
            # 没有正在跑的事件循环（同步收尾之类）：只能按"有没有调用在飞"粗判
            return self._self_initiated_depth > 0
        return task in self._self_initiated_tasks

    def _should_expect_reply(self, event: Any) -> bool:
        """这条消息"看起来会走到回复"吗（用来提前登记连发合并）。

        只有会被回复的消息才登记：群里没点名她的闲聊不登记，
        否则群一直有人说话，她的安静期就永远刷新不完。
        """

        text = str(event.get_message_str() or "")
        if _is_command(text) or _is_command_event(event):
            return False
        return bool(
            event.is_wake_up() or _is_at_bot(event) or event.is_private_chat()
        )

    def _reply_lock(self, session_id: str) -> asyncio.Lock:
        # 锁按"她"分（会话组的代表）：同一个她的两个会话不能同时开两条回复管线，
        # 否则两边会轮流改同一份状态，计划和动作互相踩
        key = self.engine.merge_scope(session_id) if self.engine else session_id
        lock = self._reply_locks.get(key)
        if lock is not None:
            return lock
        if len(self._reply_locks) > 128:
            # 会话多起来以后顺手回收没在用的那些，别一直攒着
            for key in [
                key
                for key, value in self._reply_locks.items()
                if not value.locked()
            ]:
                self._reply_locks.pop(key, None)
        lock = asyncio.Lock()
        self._reply_locks[key] = lock
        return lock

    @contextlib.asynccontextmanager
    async def _reply_turn(self, session_id: str):
        """同一会话里，接管按到达顺序排队执行。

        前一条还在等她开口时，后一条先等着——不然两条回复都是照着"回复前"的状态写出来的，
        会互相不知道对方说了什么、重复回应同一件事。等太久（前一条卡住了）就不再等，
        宁可多说一句，也别一直不说话。
        """

        lock = self._reply_lock(session_id)
        acquired = False
        try:
            await asyncio.wait_for(lock.acquire(), timeout=REPLY_TURN_WAIT_SECONDS)
            acquired = True
        except asyncio.TimeoutError:
            if self.debug:
                self.logger.info(
                    f"[my_world] 上一条回复还没结束，这条不再排队 session={session_id}"
                )
        try:
            yield
        finally:
            if acquired:
                lock.release()

    @contextlib.asynccontextmanager
    async def _reply_turn_provider(self, session_id: str, event: Any):
        """这一轮临时记下别的插件指定的模型，跑完就撤掉。

        世界树这类插件会在 ``on_waiting_llm_request`` 里给当前这条消息换模型；
        接管回复是我们自己调模型的，认下它才能和原生回复表现一致。
        只按会话记这一小会儿，不影响她自己发起的那些后台调用。
        """

        provider_id = _selected_provider_extra(event)
        if provider_id:
            self._turn_provider_overrides[session_id] = provider_id
        try:
            yield
        finally:
            if provider_id:
                self._turn_provider_overrides.pop(session_id, None)

    # ================= 回复路径（接管） =================

    @filter.on_llm_request(priority=LLM_HOOK_PRIORITY)
    @session_scoped
    @model_operation("chat", "收到用户消息，进入回复流程")
    async def on_llm_request(self, event: AstrMessageEvent, req) -> None:
        """消息走到大模型时触发。

        - 本插件接管这次回复，自己调大模型、按 JSON 动作执行并发送，然后 ``stop_event()``
          阻止主人格重复回复；
        - **接管失败（没配模型 / 调用出错 / 模型没给动作）就不回这一条**：绝不交回主人格——
          那是另一个人设，顶下来用户会看到「她」突然换了个说法；
        - 如果消息在这之前已经被别的插件处理掉（事件已 stop、或根本没走到大模型），我们什么都不做。
        """

        if not self.enabled:
            return
        if self.in_self_initiated_call():
            # 正常情况下这里只该拦下"我们自己发起的请求"。
            # 真有人 @ 她却被拦在这里，说明那道标记泄漏了——留一条日志，好排查。
            if event.get_sender_id() and event.get_sender_id() != event.get_self_id():
                self.logger.warning(
                    f"[my_world] 这条消息被「插件自己在调模型」的标记拦下了"
                    f"（depth={self._self_initiated_depth}）："
                    f"{str(event.get_message_str() or '')[:30]}"
                )
            return
        if event.is_stopped():
            return
        if self.retired:
            return
        if _is_command_event(event):
            # 指令不进她的上下文，也不让她回话（`/mw event h 在浴室` 这种只让她照做）
            return
        session_id = event.unified_msg_origin
        if not self.engine.is_enabled(session_id):
            return
        self._remember_event(event)
        if event.get_sender_id() and event.get_sender_id() == event.get_self_id():
            return

        # 宿主这一轮写进 system_prompt 的人设原文：拿它精确摘掉人设块，
        # 才不会误伤紧跟其后的其它插件内容（世界树的 <worldtree_persona> 正好紧跟在这后面）
        host_persona_prompt = await self._host_persona_prompt(event, req, session_id)

        # `req.prompt` 是管线处理过的用户消息（可能已带上图片描述等），优先用它
        user_text = (getattr(req, "prompt", "") or "").strip() or (
            event.get_message_str() or ""
        )
        # 老版本 AstrBot 上，别的插件（helper_tools 的环境感知这类）会把"只对
        # 这一轮有效"的信息直接拼在用户那句话后面。那不是群友说的话，摘出来单独带，
        # 免得她以为是有人在跟她讲"今天是法定节假日"。
        prompt_injection = ""
        if user_text:
            user_text, prompt_injection = _split_prompt_injection(user_text)
            if not user_text:
                user_text = event.get_message_str() or ""
        # 注解**之前**的原文：ctx.text 是注解后的文本，事件身份认的是这条消息本身
        raw_text = event.get_message_str() or ""
        identity = _incoming_identity(event, raw_text)
        sources = await _image_sources(event)
        # 先决定"这一轮把哪几张图交给主模型看"（编号也在这儿定），
        # 再拼注解——这样注解里的"就是图2、图3"和聊天记录里的编号是同一套
        # `req.image_urls` 里可能还有别的插件加进来的图（引用图片提取、视觉插件…）：
        # 一起算进去，否则那些图在她这一轮会凭空消失
        image_urls, image_marks, image_notes = await self._reply_images(
            event, sources, extra=list(getattr(req, "image_urls", None) or [])
        )
        user_text = await self._annotate_message(
            event,
            user_text,
            sources=sources,
            summarize_forward=True,
            image_labels=image_marks,
        )
        if image_notes:
            user_text = f"{user_text}\n［{'；'.join(image_notes)}］".strip()
        ctx = MessageContext(
            session_id=session_id,
            user_id=event.get_sender_id(),
            user_name=event.get_sender_name(),
            text=user_text,
            raw_text=raw_text,
            message_id=str(identity.get("message_id") or ""),
            event_id=str(identity.get("event_id") or ""),
            is_wake=bool(event.is_wake_up()),
            is_mentioned=_is_at_bot(event) or bool(event.is_private_chat()),
            is_soft_wake=_is_soft_wake(event),
            is_private=bool(event.is_private_chat()),
            group_name=_group_name(event),
            persona_id=_event_persona_id(event),
            # 其他插件（上下文理解、图片转文字、记忆、世界树…）写进 system_prompt 的内容
            # 原样带过去（AstrBot 自己的「人格设定」除外：她的人设由插件这边给，
            # 见 _drop_persona_instructions）；挂在 extra_user_content_parts 上的
            # 临时正文（世界树的设定走这条）也一起带上
            other_context=_merged_request_context(
                req,
                _drop_persona_instructions(
                    getattr(req, "system_prompt", "") or "", host_persona_prompt
                ),
                prompt_injection,
            ),
            image_urls=image_urls,
            chat_images=sources,
            image_marks=image_marks,
            at_targets=_at_targets(event),
            reply_to=_reply_target(event),
            addressing=_addressing_kind(
                _at_targets(event), _reply_target(event), str(event.get_self_id() or "")
            ),
            # 意图路由放行时交过来的事前判定（这批里哪几条是冲她说的）
            router=_router_view(event),
        )

        # 睡觉时的门禁：没被明确叫醒就只回固定文案（或保持安静），
        # 不调大模型、不执行动作，也不交给主人格替她熬夜聊天。
        sleep_reply = await self.engine.sleep_gate(ctx)
        if sleep_reply is not None:
            echo = self.engine.take_pending_echo(session_id)
            if sleep_reply.messages or echo:
                # 睡觉时的固定文案也是在回这条消息，同样按开关带引用
                await self._send_reply(
                    event, list(sleep_reply.messages) + echo, quote=True
                )
                await self._fire_hook(event, "OnAfterMessageSentEvent")
            if self.debug:
                self.logger.info(
                    f"[my_world] 睡觉门禁 {session_id}："
                    f"{sleep_reply.mode}（{sleep_reply.reason}）"
                )
            event.stop_event()
            return

        # 会话没启用 / 命中内容安全：本插件完全不干预，交给宿主
        if not await self.engine.handle_incoming(ctx):
            return

        # ---- 注入式接管：只改提示词；调模型、工具循环、发送都交回宿主 ----
        # 宿主这时已经拼好自己的请求（人设、工具、上下文、历史都在 req 上）。
        # 本插件只做三件事：
        #   1) 门禁：这一条要不要她回；不要就拦下，绝不换成主人格回答；
        #   2) 换人设：用她的人设 + 世界规则替换宿主那一份，别家插件写进
        #      system_prompt 的内容原样保留；
        #   3) 加处境：场景、状态、记忆、最近发生的事挂进附加内容块。
        # 之后宿主照常调用模型、跑原生工具循环、由原生管线发送；
        # 她说了什么、用了什么工具，由观察钩子在旁边记账（见 on_llm_response 等）。
        incoming = event.get_extra(MW_INCOMING_EXTRA, None)
        if not isinstance(incoming, dict):
            incoming = self.engine.register_incoming(
                session_id,
                ctx.text,
                identity={
                    **_incoming_identity(event, ctx.raw_text or ctx.text),
                    "model_text": ctx.text,
                },
            )
        else:
            # 监听侧登记的那条要补成"LLM 请求看到的样子"（注解后的正文、
            # 图片说明等），但**事件身份不变**——所以这里只刷新正文和缺的字段
            self.engine.refresh_incoming(
                incoming,
                ctx.text,
                identity={
                    **_incoming_identity(event, ctx.raw_text or ctx.text),
                    "model_text": ctx.text,
                },
            )
        if self.reply_mode == "takeover":
            # 旧版接管（已废弃）：自己调模型、执行动作、自己发送。
            # 只留给还在迁移的用户；默认路径是下面的注入式接管。
            await self._takeover_reply_legacy(event, req, ctx, session_id, incoming)
            return
        if self.engine.was_absorbed(incoming):
            if self.debug:
                self.logger.info(
                    f"[my_world] 这条被合并进上一次回复了，不单独回 session={session_id}"
                )
            event.stop_event()
            return
        # 只有"宿主这一轮"里心迹画像才会注入；我们自己发起的那些调用（自主决策、
        # 复盘）走的是另一条路，那边不跳，画像照旧全给。所以这里用完就复位。
        self.engine.weaver_injecting = profile_weaver_active(self.context)
        try:
            plan = await self.engine.handle_reply(
                ctx,
                history=list(getattr(req, "contexts", None) or []),
                inject_only=True,
            )
        finally:
            self.engine.weaver_injecting = False
        if isinstance(plan, ReplyOutcome):
            # 门禁 / 事件线提前认领了这一条：插件自己把该说的说完，然后不再交给宿主。
            pending = [
                str(item)
                for item in (
                    list(getattr(plan, "messages", []) or [])
                    + list(getattr(plan, "debug_messages", []) or [])
                )
                if str(item).strip()
            ]
            if pending:
                await self._send_reply(event, pending)
                try:
                    await self.engine.mark_reply_batch_replied(session_id)
                except Exception as exc:
                    self.logger.debug(f"[my_world] 推进水位线失败：{exc}")
                await self._fire_hook(event, "OnAfterMessageSentEvent")
            if self.debug:
                self.logger.info(
                    f"[my_world] 这一条不交给宿主："
                    f"{str(getattr(plan, 'error', '') or getattr(plan, 'reason', '') or '门禁拦下')}"
                )
            event.stop_event()
            return
        if not getattr(plan, "ok", False):
            event.stop_event()
            return
        # 合并了后续消息时，宿主的 req.prompt 必须换成完整 batch，不能只留第一条。
        if len(getattr(plan, "batch", None) or []) > 1 and plan.user_prompt.strip():
            req.prompt = plan.user_prompt
        # 换人设：宿主那份 persona 被摘掉，其余（工具说明、技能、安全提示、
        # 世界树 / helper_tools 之类写进 system_prompt 的内容）原样保留。
        preserved = _drop_persona_instructions(
            getattr(req, "system_prompt", "") or "", host_persona_prompt
        )
        req.system_prompt = (
            f"{plan.system_prompt}\n\n{preserved}".strip()
            if preserved
            else plan.system_prompt
        )
        _append_extra_text(req, plan.runtime_part)
        event.set_extra(
            MW_TURN_EXTRA,
            {
                "session_id": session_id,
                "user_id": ctx.user_id,
                "user_name": ctx.user_name,
                "user_text": ctx.text,
                "persona_id": ctx.persona_id,
                "started_at": time.time(),
                "model_attribution": call_context(),
                "tools": [],
            },
        )
        if self.debug:
            self.logger.info(
                f"[my_world] 注入接管 {session_id}："
                f"system={len(req.system_prompt)} 字 / 此刻={len(plan.runtime_part)} 字"
                + (f"（合并 {len(plan.texts)} 条）" if len(plan.texts) > 1 else "")
            )
        return

    async def _takeover_reply_legacy(
        self,
        event: AstrMessageEvent,
        req,
        ctx: MessageContext,
        session_id: str,
        incoming: dict | None,
    ) -> None:
        """旧版接管（已废弃）：自己调模型、执行动作、自己发送。

        ``reply_mode=takeover`` 时才走这里，只作迁移期的兜底；
        默认的注入式接管见 :meth:`on_llm_request`。
        """

        # AstrBot 会在「准备调模型」时发一次这个钩子（贴表情的小插件靠它开始处理）。
        # 我们自己调模型，这里补一遍；被拦下就整条消息都不接管。
        if await self._fire_hook(event, "OnWaitingLLMRequestEvent"):
            event.stop_event()
            return
        # 同一条会话同一时间只跑一条接管：连发的第二条会排队，
        # 等前一条说完再开口，这样它能看到对方刚说过什么。
        async with self._reply_turn(session_id), self._reply_turn_provider(
            session_id, event
        ):
            if self.engine.was_absorbed(incoming):
                if self.debug:
                    self.logger.info(
                        f"[my_world] 这条被合并进上一次回复了，不单独回 session={session_id}"
                    )
                event.stop_event()
                return
            outcome = await self.engine.handle_reply(
                ctx, history=list(getattr(req, "contexts", None) or [])
            )
            echo = list(getattr(outcome, "debug_messages", []) or [])
            # 这一轮工具 / 指令生成出来的图（生图动作走的就是这里）
            made_images = list(getattr(outcome, "images", []) or [])
            # 她这一轮要说的话：当前会话一份，另外可能还有"只说给别处"的
            routed = {
                str(key): list(value)
                for key, value in (getattr(outcome, "routed", {}) or {}).items()
                if list(value)
            }
            routed_images = {
                str(key): list(value)
                for key, value in (getattr(outcome, "routed_images", {}) or {}).items()
                if list(value)
            }
            # 慢动作之前她已经说的那几句已经当场发出去了（见 _send_live_say）：
            # 这一轮算成立，但只发"还没发出去的部分"
            live_lines = [
                str(item)
                for item in (getattr(outcome, "live_messages", []) or [])
                if str(item).strip()
            ]
            pending_lines = [
                str(item) for item in (outcome.messages or []) if str(item).strip()
            ]
            if getattr(outcome, "silenced", False):
                # 扩展把这一轮她的独立发言静默了（正文由扩展自己写、自己发）：
                # 一条都不发，但这一轮**算她回过话**——水位线必须照推，
                # 否则世界以为这批消息没被回过，下一轮会把同一批原样再喂一遍。
                targets = list(getattr(outcome, "replied_sessions", []) or [])
                for target in targets or [session_id]:
                    try:
                        await self.engine.mark_reply_batch_replied(target)
                    except Exception as exc:
                        self.logger.debug(f"[my_world] 推进水位线失败：{exc}")
                if self.debug:
                    self.logger.info(
                        f"[my_world] {session_id}：这一轮被扩展静默，什么都不发"
                    )
                event.stop_event()
                return
            if outcome.ok and (pending_lines or live_lines or routed or routed_images):
                # 只处理尚未发送的部分：钩子可能合并段落或把正文换成语音，
                # 不能再靠条数切片来猜哪些话已经发过。
                bridged, extra_chain = await self._bridge_reply_hooks(
                    event,
                    pending_lines,
                    str(getattr(outcome, "tail", "") or ""),
                )
                # 再按换行拆段：一段一条消息，别把好几句挤成一坨；
                # 调试回显按它实际发生的位置插进去（先调工具、再说话，群里也是这个顺序）
                payload = self._merge_with_echo(bridged, outcome, echo)
                sent_ok = True
                if payload or made_images or extra_chain:
                    await self._send_reply(
                        event,
                        payload,
                        # 前面已经即时说过话时，这一条是接着往下讲，不再引用触发她的那条
                        quote=not live_lines,
                        quote_mode_hint=str(getattr(outcome, "quote_hint", "") or ""),
                        images=made_images,
                        # 别的插件在回复钩子里产出的图 / 语音（表情包、TTS）跟着一起发
                        extra_chain=extra_chain,
                    )
                # 说给别处的那些：逐条投到那个会话（不带引用——那条消息不在这里）
                # 投失败的目标**不算回过话**：水位线不推进，它下一轮还会被当成新的
                failed_targets: set[str] = set()
                for target, texts in routed.items():
                    try:
                        await self.messenger.send_text(target, list(texts))
                    except Exception as exc:
                        failed_targets.add(target)
                        sent_ok = False
                        self.logger.warning(f"[my_world] 发到 {target} 失败：{exc}")
                for target, refs in routed_images.items():
                    try:
                        await self.messenger.send_images(target, list(refs))
                    except Exception as exc:
                        failed_targets.add(target)
                        sent_ok = False
                        self.logger.warning(f"[my_world] 发图到 {target} 失败：{exc}")
                # 真发出去了才算"回过这批消息"（水位线在这里推进）
                for target in [session_id, *(list(routed) + list(routed_images))]:
                    if target in failed_targets:
                        continue  # 没发出去的那边不算回应过
                    try:
                        await self.engine.mark_reply_batch_replied(target)
                    except Exception as exc:
                        self.logger.debug(f"[my_world] 推进水位线失败：{exc}")
                if not sent_ok:
                    self.logger.warning(
                        f"[my_world] {session_id}：有目标没收到，"
                        "水位线只推了真正送到的那些"
                    )
                # 「发完了」也要补一遍：靠它摘掉"处理中"标记的插件才不会一直挂着
                await self._fire_hook(event, "OnAfterMessageSentEvent")
                if self.debug:
                    self.logger.info(
                        f"[my_world] 接管回复 {session_id}："
                        f"reasoning={outcome.reasoning} "
                        f"messages={live_lines + pending_lines}"
                    )
                event.stop_event()
                return
            if str(getattr(outcome, "error", "")) == REPLY_INTERRUPTED:
                # 这一轮被新消息打断：作废，不补一句也不交给主人格——
                # 新那条自己的回复管线马上会接手（它能看到刚才这几条）。
                if echo:
                    await self._send_reply(event, echo)
                event.stop_event()
                return
            if str(getattr(outcome, "error", "")) == REPLY_MUTED:
                # 本小时被动回复触顶：接管但静默（连调试回显也不发）
                event.stop_event()
                return
            # 接管没成功：**这一条就不回了**（不交回主人格——那是另一个人设，
            # 顶下来的话用户会看到"她"突然换了个说法）。调试回显照样发，
            # 原因写进日志页，方便排查到底是模型挂了还是没给出动作。
            if echo or made_images:
                await self._send_reply(event, echo, images=made_images)
                await self._fire_hook(event, "OnAfterMessageSentEvent")
            await self._note_takeover_fallback(session_id, outcome)
            event.stop_event()
            return

    # ---------------- 注入模式的观察钩子（只记录，不干预） ----------------

    @filter.on_llm_response()
    @session_scoped
    async def on_llm_response(self, event: AstrMessageEvent, response) -> None:
        """宿主拿到最终回复：把她的正文和模型自己的思考记到这一轮上。

        这里**不改内容、不改结果**：正文由原生管线发送，工具循环也早就跑完了。
        """

        turn = event.get_extra(MW_TURN_EXTRA, None)
        await observe_native_response(self, event, response, turn)
        service = getattr(self, "profile_weaver_service", None)
        if (
            service is not None
            and not bool(getattr(event, "_profileweaver_llm_tool_used", False))
            and not bool(getattr(event, "_profileweaver_auto_extract_running", False))
        ):
            try:
                await service.maybe_extract(event, response)
            except Exception as exc:
                self.logger.debug(f"[my_world] 自动画像提取失败：{exc}")
        if not isinstance(turn, dict):
            return
        if response is not None:
            turn["reply"] = str(getattr(response, "completion_text", "") or "")
            turn["provider_reasoning"] = str(
                getattr(response, "reasoning_content", "") or ""
            )
        if self.debug:
            text = str(turn.get("reply") or "")
            if text:
                self.logger.info(
                    f"[my_world] 她这一轮说了 {len(text)} 字（原生管线发送）"
                )

    @filter.on_using_llm_tool()
    @session_scoped
    async def on_using_llm_tool(
        self, event: AstrMessageEvent, tool, tool_args: dict | None
    ) -> None:
        """原生工具开始调用：只记录，不动参数、不动工具集。"""

        turn = event.get_extra(MW_TURN_EXTRA, None)
        if not isinstance(turn, dict):
            return
        entry = {
            "name": str(getattr(tool, "name", "") or ""),
            "args": _safe_brief(tool_args),
            "at": time.time(),
            "ok": None,
            "result": "",
        }
        trace = turn.setdefault("tools", [])
        if isinstance(trace, list):
            trace.append(entry)
            if len(trace) > 20:
                del trace[:-20]
        if self.debug:
            self.logger.info(
                f"[my_world] 她调用工具 {entry['name']}（{entry['args']}）"
            )

    @filter.on_llm_tool_respond()
    @session_scoped
    async def on_llm_tool_respond(
        self, event: AstrMessageEvent, tool, tool_args, tool_result
    ) -> None:
        """原生工具返回：把结果摘要补给刚才那条记录。"""

        turn = event.get_extra(MW_TURN_EXTRA, None)
        if not isinstance(turn, dict):
            return
        trace = turn.get("tools")
        if not isinstance(trace, list):
            return
        name = str(getattr(tool, "name", "") or "")
        entry = next(
            (
                item
                for item in reversed(trace)
                if isinstance(item, dict)
                and item.get("name") == name
                and item.get("ok") is None
            ),
            None,
        )
        if entry is None:
            entry = {
                "name": name,
                "args": _safe_brief(tool_args),
                "at": time.time(),
            }
            trace.append(entry)
        entry["ok"] = (
            not bool(getattr(tool_result, "isError", False))
            if tool_result is not None
            else False
        )
        entry["result"] = str(_clip_brief(_tool_result_text(tool_result)))[:300]
        try:
            entry["ms"] = int(
                max(0.0, time.time() - float(entry.get("at") or time.time())) * 1000
            )
        except Exception:
            entry["ms"] = 0
        # 原生工具查到的天气顺手记一份：地图横幅和下一轮的提示词立刻就有真数据，
        # 本插件不用再单独查一遍。认不出是天气工具就什么都不做。
        session_id = str(turn.get("session_id") or "")
        if session_id:
            try:
                await self.engine.note_tool_weather(
                    session_id=session_id,
                    tool_name=name,
                    args=tool_args if isinstance(tool_args, dict) else {},
                    result=_tool_result_text(tool_result),
                )
            except Exception as exc:
                self.logger.debug(f"[my_world] 天气记录没更新：{exc}")

    @filter.after_message_sent()
    @session_scoped
    async def after_message_sent(self, event: AstrMessageEvent) -> None:
        """宿主发完了：推进水位线、把她的话写回世界，并在后台做一次回合复盘。"""

        turn = event.get_extra(MW_TURN_EXTRA, None)
        if not isinstance(turn, dict):
            return
        event.set_extra(MW_TURN_EXTRA, None)
        session_id = str(turn.get("session_id") or "")
        if not session_id:
            return
        try:
            await self.engine.mark_reply_batch_replied(session_id)
        except Exception as exc:
            self.logger.debug(f"[my_world] 推进水位线失败：{exc}")
        reply = str(turn.get("reply") or "").strip()
        if reply:
            try:
                await self.engine.record_self_reply(
                    session_id,
                    reply,
                    persona_id=str(turn.get("persona_id") or ""),
                )
            except Exception as exc:
                self.logger.warning(f"[my_world] 写回她说的话失败：{exc}")
        self._spawn_reflection(turn)

    def _spawn_reflection(self, turn: dict[str, Any]) -> None:
        """把回合复盘放到后台：她说完就完事，复盘慢一点不影响聊天。"""

        async def run() -> None:
            try:
                result = await self.engine.reflect_turn(
                    str(turn.get("session_id") or ""),
                    user_id=str(turn.get("user_id") or ""),
                    user_name=str(turn.get("user_name") or ""),
                    user_text=str(turn.get("user_text") or ""),
                    reply_text=str(turn.get("reply") or ""),
                    reasoning_text=str(turn.get("provider_reasoning") or ""),
                    tool_trace=list(turn.get("tools") or []),
                    persona_id=str(turn.get("persona_id") or ""),
                )
                if result and self.debug:
                    inner = str(dict(result.get("reasoning") or {}).get("inner") or "")
                    self.logger.info(
                        f"[my_world] 回合复盘："
                        f"{result.get('tone') or 'normal'}｜{inner}"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.debug(f"[my_world] 回合复盘失败：{exc}")

        task = asyncio.create_task(run())
        self._reflection_tasks.add(task)
        task.add_done_callback(self._reflection_tasks.discard)
        if len(self._reflection_tasks) > 16:
            for old in list(self._reflection_tasks)[:8]:
                if not old.done():
                    old.cancel()

    async def _send_debug_live(self, session_id: str, message: str) -> bool:
        """把调试行立刻发到群里（引擎那边一发生就调它）。"""

        try:
            return bool(await self.messenger.send_text(session_id, [str(message)]))
        except Exception as exc:
            self.logger.debug(f"[my_world] 调试回显发送失败：{exc}")
            return False

    async def _send_live_say(self, session_id: str, message: str) -> bool:
        """把她说出口的这一句立刻发出去（引擎在慢动作开始前调它）。

        检索、生图、录视频这类要花一会儿的动作排在后面时，「坐好等我两分钟～」先到群里，
        群里才是"她说了一句 → 过一会儿带着结果回来"，而不是半天不出声、然后一口气说三句。
        发失败返回 False：引擎会把这几句留到收尾按原顺序一起发，不会丢内容。
        """

        body = str(message or "").strip()
        if not body:
            return False
        # 连着的两句之间按字数停一下：和收尾发送时的节奏保持一致，
        # 免得"先说的这一句"排成一串同一瞬间冒出来
        previous = self._live_say_tail.get(session_id, "")
        if previous:
            delay = self.typing_delay_for(previous)
            if delay > 0:
                await asyncio.sleep(delay)
        try:
            sent = bool(await self.messenger.send_text(session_id, [body]))
        except Exception as exc:
            self.logger.debug(f"[my_world] 即时发言发送失败：{exc}")
            sent = False
        self._live_say_tail[session_id] = body if sent else ""
        return sent

    async def _send_reply(
        self,
        event: AstrMessageEvent,
        messages: list[str],
        *,
        quote: bool = False,
        quote_mode_hint: str = "",
        images: list[str] | None = None,
        extra_chain: list[Any] | None = None,
    ) -> None:
        """把接管产生的消息发回当前会话（逐条发送 = 天然分段回复）。

        分段之间按上一条的字数停一下：群里看起来就像她在一条条打字，
        而不是一整坨同时冒出来。停顿有上限，长句子不会等到天荒地老。
        ``quote=True`` 时第一条引用触发她的那条消息（能不能引用见 ``_quote_target``）。
        ``images`` 是这一轮工具 / 指令生成出来的图：说完再贴上去（一次一条消息，多张放一起）。
        ``extra_chain`` 是别的插件在回复钩子里产出的组件（表情包图片、语音、文件…）：
        没有文本时单独发一条，有文本时跟着最后一句一起发——和 AstrBot 原生管线一致。
        """

        session_id = event.unified_msg_origin
        pending = [str(item).strip() for item in messages if str(item).strip()]
        refs = _sendable_images(images)
        extras = [item for item in (extra_chain or []) if item is not None]
        if not pending and not refs and not extras:
            return
        quote_id = self._quote_target(event, quote_mode_hint) if quote else ""
        for index, content in enumerate(pending):
            if self.messenger.blocked(session_id):
                # 刚发送失败过：不再往下试，免得把队列越堆越长
                break
            chain: list[Any] = [Plain(text=content)]
            if quote_id and index == 0:
                # 引用段必须排在前面，平台才认得出"这条是在回哪一句"
                chain.insert(0, Reply(id=quote_id))
            if extras and index == len(pending) - 1:
                # 表情包 / 语音这类只有组件没有文本的产出，跟着最后一句一起走
                chain.extend(extras)
            try:
                await event.send(MessageChain(chain))
                self.messenger.mark_sent(session_id)
            except Exception as exc:
                # 失败就失败：以前这里会立刻换一条通道再发一次，遇到"超时但其实发出去了"
                # 的情况就会重复刷屏，所以只保留一次尝试。
                self.messenger.mark_failed(session_id, exc)
                continue
            if index == len(pending) - 1:
                break  # 最后一条不用再等
            delay = self.typing_delay_for(content)
            if delay > 0:
                await asyncio.sleep(delay)

        if extras and not pending and not self.messenger.blocked(session_id):
            # 她这一轮没有文本（例如语音插件把整句换成了语音）：单独把组件发出去
            chain = list(extras)
            if quote_id:
                chain.insert(0, Reply(id=quote_id))
            try:
                await event.send(MessageChain(chain))
                self.messenger.mark_sent(session_id)
            except Exception as exc:
                self.messenger.mark_failed(session_id, exc)

        if refs and not self.messenger.blocked(session_id):
            # 只在"她什么都没说、只发了图"时，让图片这条去引用（第一条才引用）
            await self._send_images(
                event,
                refs,
                quote=quote and not pending,
                quote_mode_hint=quote_mode_hint,
            )

    async def _send_images(
        self,
        event: AstrMessageEvent,
        images: list[str],
        *,
        quote: bool = False,
        quote_mode_hint: str = "",
    ) -> bool:
        """把结果图贴到群里（多张放同一条消息，不会拆成连发刷屏）。"""

        refs = _sendable_images(images)
        if not refs:
            return False
        session_id = event.unified_msg_origin
        if self.messenger.blocked(session_id):
            return False
        chain: list[Any] = [Image(file=ref) for ref in refs]
        quote_id = self._quote_target(event, quote_mode_hint) if quote else ""
        if quote_id:
            chain.insert(0, Reply(id=quote_id))
        try:
            await event.send(MessageChain(chain))
            self.messenger.mark_sent(session_id)
            return True
        except Exception as exc:
            # 和文字一样：失败就失败，不换通道重发（重发容易变成两条）
            self.messenger.mark_failed(session_id, exc)
            return False

    def _quote_target(self, event: AstrMessageEvent, mode_hint: str = "") -> str:
        """这条回复要引用哪条消息：触发她的那一条（用不了就返回空串）。

        档位在「说话节奏」里：关闭 / 总是 / 智能（默认）。智能档由引擎判断——
        "这一轮回的是一串消息"才引用，单独一句对答不顶引用；``mode_hint`` 就是那个判断。
        引用是 QQ / OneBot（aiocqhttp）这类平台才有的段，别的平台不认，
        硬塞过去只会让这条发失败——所以这里先按平台名挡一道。
        """

        style = getattr(self.engine.world, "reply_style", None)
        mode = str(getattr(style, "quote_mode", "smart") or "smart")
        if mode not in ("always", "smart"):
            return ""
        # 智能档由调用方判断（"这一轮回的是一串消息"才引用），这里只认 always
        if mode == "smart" and mode_hint != "always":
            return ""
        platform = ""
        getter = getattr(event, "get_platform_name", None)
        if callable(getter):
            try:
                platform = str(getter() or "").lower()
            except Exception:
                platform = ""
        if "cqhttp" not in platform and "onebot" not in platform:
            return ""
        message_obj = getattr(event, "message_obj", None)
        return str(getattr(message_obj, "message_id", "") or "").strip()

    def typing_delay_for(self, text: str) -> float:
        """按字数算这一次的「打字」停顿（秒）。0 表示不等待。"""

        style = getattr(self.engine.world, "reply_style", None)
        if style is None or not bool(getattr(style, "typing_delay_enabled", True)):
            return 0.0
        per_char = max(0.0, float(getattr(style, "typing_delay_per_char", 0.0) or 0.0))
        cap = max(0.0, float(getattr(style, "typing_delay_max", 0.0) or 0.0))
        if per_char <= 0 or cap <= 0:
            return 0.0
        return min(cap, len(str(text)) * per_char)

    @staticmethod
    def split_messages(messages: list[str], *, limit: int = 8) -> list[str]:
        """把每条消息按换行拆开：一段一条。

        模型经常把几句话、甚至「话 + 动作文案」写在同一个字符串里，
        整段发出去在群里就是一坨；拆开之后读起来才像人一句一句说的。
        """

        result: list[str] = []
        for item in messages:
            for line in str(item or "").splitlines():
                text = line.strip()
                if text:
                    result.append(text)
        return result[:limit] if result else [str(item).strip() for item in messages if str(item).strip()]

    def _merge_with_echo(
        self, bridged: list[str], outcome, echo: list[str]
    ) -> list[str]:
        """把正式回复和调试回显按"实际发生的先后"合成一串消息。

        她这一轮可能是"先调工具、拿到结果才开口"：那种情况下调试行本来就该排在
        她的话前面。没有回显时就是普通的分段回复。
        """

        if not echo:
            return self.split_messages(bridged)
        raw = list(getattr(outcome, "messages", []) or [])
        if len(bridged) != len(raw):
            # 钩子改过条数，位置对不上了：宁可把回显放在后面，也不要错位
            return self.split_messages(bridged) + list(echo)
        positions = list(getattr(outcome, "debug_positions", []) or [])
        buckets: dict[int, list[str]] = {}
        for index, line in enumerate(echo):
            slot = positions[index] if index < len(positions) else len(raw)
            buckets.setdefault(min(int(slot), len(raw)), []).append(line)
        result: list[str] = []
        for slot in range(len(raw) + 1):
            result.extend(buckets.get(slot, []))
            if slot < len(raw):
                result.extend(self.split_messages([bridged[slot]]))
        return result

    async def _bridge_reply_hooks(
        self, event: AstrMessageEvent, messages: list[str], tail: str = ""
    ) -> tuple[list[str], list[Any]]:
        """让别的插件也能"看到"这次接管的回复。

        本插件接管时是自己调模型、自己发送的，AstrBot 的回复钩子轮不到跑，
        那些靠在回复里解析标记的插件（好感度、统计、改写语气之类）就永远收不到内容。
        这里把回复补送进同一条钩子链：先过 ``on_llm_response``，
        再过 ``on_decorating_result``（发送前的最后一道），拿回它们改过的文本再发。

        返回值是 ``(要说的话, 额外消息组件)``。除了改文本的插件，还有一类插件是靠
        这两道钩子**产出别的东西**的：表情包插件往消息链里塞一张图（meme_magpie），
        语音插件把整条回复换成一段语音（genie_tts）。以前这里只把文本捞出来，
        那些图与语音就被丢在半路——群里只看到字，或者干脆什么都没有。
        现在把它们原样带回发送环节，和 AstrBot 原生的行为保持一致。

        ``tail`` 是模型写在 JSON 之后的那一小截（例如 `[好感度 持平]`）：只跟这次钩子调用走，
        给靠标记工作的插件解析用，**不会**跟着她的话发到群里。
        """

        extra: list[Any] = []
        if not self.reply_hook_bridge or not messages:
            return messages, extra
        try:
            from astrbot.core.pipeline.context_utils import call_event_hook
            from astrbot.core.provider.entities import LLMResponse
            from astrbot.core.star.star_handler import EventType
        except Exception:
            return messages, extra

        joined = "\n".join(messages)
        # 标记只加在"给钩子看的文本"里：插件解析完（通常会把标记抹掉）再返回，
        # 真正发到群里的 messages 不会带上它。
        hook_text = f"{joined}\n{tail}".strip() if tail else joined
        try:
            response = self._hook_response(event, hook_text, LLMResponse)
            await call_event_hook(event, EventType.OnLLMResponseEvent, response)
            edited = str(getattr(response, "completion_text", "") or "")
            if edited and edited != hook_text:
                lines = [line.strip() for line in edited.splitlines() if line.strip()]
                # 插件可能把标记那一行抹掉了：只用它给的内容，标记本身不发
                cleaned = [line for line in (lines or [edited]) if line != tail.strip()]
                messages = cleaned or lines or [edited]
            # 语音插件（genie_tts）会把音频放进模型响应的消息链里：
            # 文本照旧发，音频作为额外组件跟着最后一条走
            response_chain = getattr(response, "result_chain", None)
            extra = _extra_components(response_chain, skip_plain=True)
            if extra and not _chain_texts(response_chain):
                # 整条回复被换成了语音 / 文件（genie 的「仅语音」模式）：
                # 正文别再发一遍，和 AstrBot 原生管线的行为一致
                messages = []
        except Exception as exc:
            self.logger.warning(f"[my_world] 回复钩子（LLM 响应）执行出错：{exc}")

        restore_result = False
        try:
            from astrbot.core.message.message_event_result import (
                MessageEventResult,
                ResultContentType,
            )

            previous = event.get_result()
            restore_result = True
            result = MessageEventResult().message("\n".join(messages))
            for component in extra:
                result.chain.append(component)
            result.set_result_content_type(ResultContentType.LLM_RESULT)
            event.set_result(result)
            await call_event_hook(event, EventType.OnDecoratingResultEvent)
            current = event.get_result()
            joined_result = "\n".join(messages)
            chain = _chain_items(current) if current is not None else []
            texts = _chain_texts(chain)
            # 没有插件改动内容时，保持原来的分段：合并成一条会把
            # 「say 的话」和「（…抱了你一下）」这类动作文案挤进同一条消息里。
            if texts and (len(texts) > 1 or texts[0] != joined_result):
                messages = texts
            # 发送前钩子里的产出（表情包图片、语音、文件…）跟正文一起带回去发
            extra = _extra_components(chain, skip_plain=True)
            if extra and not texts:
                # 这一段整体被别的插件换成了非文本内容：正文不要了
                messages = []
        except Exception as exc:
            self.logger.warning(f"[my_world] 回复钩子（发送前）执行出错：{exc}")
        finally:
            if restore_result:
                if previous is None:
                    event.clear_result()
                else:
                    event.set_result(previous)
        return messages, extra

    def _hook_response(self, event: AstrMessageEvent, text: str, response_cls: Any) -> Any:
        """给回复钩子准备一个「像真的」的模型响应。

        别的插件可能读 ``usage`` / ``id`` / ``role`` 这些字段，自己拼一个空的响应它们
        会直接抛异常（异常被宿主吞掉，表现就是"收不到钩子"）。所以优先拿主模型
        真实响应的浅拷贝，只把文本换成最终要说的话。
        """

        session_id = str(getattr(event, "unified_msg_origin", "") or "")
        raw = self.take_llm_response(session_id) if session_id else None
        if raw is not None:
            try:
                response = copy.copy(raw)
                response.completion_text = text
                if hasattr(response, "reasoning_content"):
                    response.reasoning_content = ""
                return response
            except Exception:
                pass
        return response_cls(role="assistant", completion_text=text)

    # ---------------- 把接管的流程补进 AstrBot 的钩子链 ----------------

    @staticmethod
    def _hook_type(name: str) -> Any:
        """按名字取 AstrBot 的钩子类型（老版本没有的钩子就跳过）。"""

        try:
            from astrbot.core.star.star_handler import EventType
        except Exception:
            return None
        return getattr(EventType, name, None)

    async def _fire_hook(self, event: Any, name: str, *args: Any) -> bool:
        """补发一个 AstrBot 钩子；返回事件是否被别的插件拦下。

        本插件接管时是自己调模型、自己调工具、自己发消息的，AstrBot 管线里那些
        「等 LLM」「用完工具」「发完消息」的钩子轮不到跑——靠这些钩子工作的小插件
        （比如处理中贴表情、用完摘掉）就会漏掉一半。这里在同样的时机补一遍。
        """

        if not self.reply_hook_bridge or event is None:
            return False
        hook_type = self._hook_type(name)
        if hook_type is None:
            return False
        try:
            from astrbot.core.pipeline.context_utils import call_event_hook
        except Exception:
            return False
        try:
            return bool(await call_event_hook(event, hook_type, *args))
        except Exception as exc:
            self.logger.warning(f"[my_world] 钩子 {name} 执行出错：{exc}")
            return False

    async def _note_takeover_fallback(self, session_id: str, outcome) -> None:
        """接管没生效：这一条她不说话，但**必须留下痕迹**。

        以前只在开着调试时打一行 stdout，日志页里什么都没有——用户只看到
        "她没回"，完全猜不到是模型挂了、还是模型没给出能执行的动作。
        """

        reason = str(getattr(outcome, "error", "") or "没有对外输出")
        warnings = [str(item) for item in (getattr(outcome, "warnings", None) or [])]
        self.logger.warning(
            f"[my_world] 接管没生效（{reason}），这一条保持安静 "
            f"session={session_id}"
        )
        detail = {"reason": reason}
        if warnings:
            detail["warnings"] = warnings[:4]
        if getattr(outcome, "tail", ""):
            detail["model_tail"] = str(outcome.tail)[:200]
        try:
            async with self.engine.session_state(session_id) as state:
                await self.engine._log_event(state, "takeover_failed", detail)
        except Exception as exc:  # noqa: BLE001
            self.logger.debug(f"[my_world] 写接管失败日志失败：{exc}")

    # ================= 旁观记录（不产生 LLM 调用） =================

    @filter.event_message_type(
        filter.EventMessageType.ALL, priority=SLEEP_GUARD_PRIORITY
    )
    @session_scoped
    async def on_sleep_guard(self, event: AstrMessageEvent) -> None:
        """睡觉门禁：挡在其它插件（含意图路由）之前。

        优先级的取值比意图路由的 100 更高，所以 ``stop_event()`` 之后它不会被执行——
        既不浪费它那次判断，也不会把睡着的她拖进对话。
        """

        if not self.enabled:
            return
        if self.in_self_initiated_call():
            return
        if self.retired:
            return
        session_id = event.unified_msg_origin
        if not self.engine.is_enabled(session_id):
            return
        if event.get_sender_id() and event.get_sender_id() == event.get_self_id():
            return
        text = event.get_message_str() or ""
        if _is_command(text):
            return  # 指令照常走（/mw status、/mw 叫醒 这些要能用）
        sources = await _image_sources(event)
        if _is_forwarded(event) and self._should_expect_reply(event):
            # 这条转发是冲她来的：先把内容压成摘要，她醒来看到的才是"事情"而不是占位符
            summary = await self._forward_summary_text(event)
            if summary:
                text = _with_forward_summary(text, summary)
        if _image_components(event):
            # 她在睡觉：这里不额外调转述模型（醒来后那条消息会照常转述），
            # 但留档里要能看出"有人发了张图"，否则这条消息会是空的。
            text = f"{text}\n［对方发了一张图片］".strip()
        ctx = MessageContext(
            session_id=session_id,
            user_id=event.get_sender_id(),
            user_name=event.get_sender_name(),
            text=text,
            is_wake=bool(event.is_wake_up()),
            is_mentioned=_is_at_bot(event) or bool(event.is_private_chat()),
            is_private=bool(event.is_private_chat()),
            # 记下这条消息带的图片，等下次回复时一起给多模态主模型
            image_urls=list(sources),
            chat_images=list(sources),
            router=_router_view(event),
        )
        if not await self.engine.should_block_sleep(ctx):
            return
        # 留档已经由门禁自己记好了（她醒来还得知道群里发生过什么），这里只负责截住事件
        if self.debug:
            self.logger.info(
                f"[my_world] 她在睡觉，挡下这条消息 session={session_id}：{text[:30]}"
            )
        event.stop_event()

    @filter.event_message_type(
        filter.EventMessageType.ALL, priority=PRESENCE_PRIORITY
    )
    @session_scoped
    async def on_any_message(self, event: AstrMessageEvent) -> None:
        if not self.enabled or self.retired:
            return
        session_id = event.unified_msg_origin
        if not self.engine.is_enabled(session_id):
            return
        if event.get_sender_id() and event.get_sender_id() == event.get_self_id():
            return
        if _is_command_event(event):
            # 指令不进她的上下文：不登记待回、不留档、不进记忆，也不该由她回话
            return
        # 「连发合并」要在这里就登记：落到 on_llm_request 那一钩时，同一会话的
        # 上一条回复可能还占着管线（AstrBot 里那条管线是串行的），等她登记时
        # 早就过了安静期，两条消息就各回一次了。
        if self._should_expect_reply(event):
            try:
                raw_text = event.get_message_str() or ""
                event.set_extra(
                    MW_INCOMING_EXTRA,
                    self.engine.register_incoming(
                        session_id,
                        raw_text,
                        identity=_incoming_identity(event, raw_text),
                    ),
                )
            except Exception as exc:
                self.logger.debug(f"[my_world] 登记待回消息失败：{exc}")
        self._remember_event(event)
        sources = await _image_sources(event)
        raw_text = event.get_message_str() or ""
        text = await self._annotate_message(
            event,
            raw_text,
            sources=sources,
            summarize_forward=self._should_expect_reply(event),
        )
        identity = _incoming_identity(event, raw_text)
        await self.engine.note_presence(
            MessageContext(
                session_id=session_id,
                user_id=event.get_sender_id(),
                user_name=event.get_sender_name(),
                text=text,
                raw_text=raw_text,
                message_id=str(identity.get("message_id") or ""),
                event_id=str(identity.get("event_id") or ""),
                is_wake=bool(event.is_wake_up()),
                is_private=bool(event.is_private_chat()),
                group_name=_group_name(event),
                chat_images=list(sources),
                at_targets=_at_targets(event),
                reply_to=_reply_target(event),
                addressing=_addressing_kind(
                    _at_targets(event),
                    _reply_target(event),
                    str(event.get_self_id() or ""),
                ),
                # 意图路由放行时交过来的事前判定：记档时就按事件身份认领给对应那几条
                router=_router_view(event),
            )
        )

    # ================= 用户 / 管理员命令 =================

    @filter.command("mw", alias={"我的世界", "myworld"})
    @session_scoped
    async def cmd_hw(self, event: AstrMessageEvent):
        if self.retired:
            return
        args = _command_args(event)
        action = args[0].lower() if args else "help"
        rest = args[1:]

        if action in ("help", "帮助", "?"):
            yield event.plain_result(_help_text(self._pronoun()))
            return

        if action in ("status", "状态", "where", "在哪"):
            session_id = event.unified_msg_origin
            if not self.engine.is_enabled(session_id):
                yield event.plain_result("这个会话还没有启用她的世界。用 /mw session add 启用。")
                return
            snapshot = await self.engine.snapshot(session_id)
            yield event.plain_result(_render_status(snapshot, self._pronoun()))
            return

        if action in ("recall", "回忆"):
            if not rest:
                yield event.plain_result("用法：/mw recall <话题>")
                return
            session_id = event.unified_msg_origin
            topic = " ".join(rest)
            memories = self.engine.memory.recall(
                session_id=session_id,
                persona_id=_event_persona_id(event),
                node_id=(await self.engine.load_state(session_id, cold_start=False)).node_id,
                limit=5,
            )
            hits = [
                item
                for item in self.engine.memory.db.query_memories(session_id=session_id, limit=300)
                if topic in str(item.get("content", ""))
            ]
            lines = [f"关于「{topic}」我记得："]
            lines += [f"· {item['content']}" for item in hits[:5]]
            if not hits:
                lines.append("· 好像没什么印象……")
            if memories:
                lines.append("此刻想起的：")
                lines += [f"· {item.content}" for item in memories[:3]]
            yield event.plain_result("\n".join(lines))
            return

        if action in ("memory", "记忆"):
            session_id = event.unified_msg_origin
            user_id = event.get_sender_id()
            rows = self.engine.memory.export_user(
                session_id=session_id, persona_id=_event_persona_id(event), user_id=user_id
            )
            if not rows:
                yield event.plain_result("我暂时没有关于你的场景记忆。")
                return
            lines = [f"我记得关于你的 {len(rows)} 件事："]
            lines += [f"· {item['content']}" for item in rows[:10]]
            lines.append("想让我忘掉，可以发 /mw forget me")
            yield event.plain_result("\n".join(lines))
            return

        if action in ("profile", "画像", "通讯录"):
            yield event.plain_result(await self._cmd_profile(event, rest))
            return

        if action in ("forget", "忘记"):
            session_id = event.unified_msg_origin
            user_id = event.get_sender_id()
            topic = " ".join(rest) if rest and rest[0].lower() not in ("me", "我") else ""
            removed = self.engine.memory.forget_user(
                session_id=session_id,
                persona_id=_event_persona_id(event),
                user_id=user_id,
                topic=topic,
            )
            yield event.plain_result(f"好，我忘记了 {removed} 条相关的记忆。")
            return

        if action in ("schedule", "日程"):
            if rest and rest[0].lower() in ("run", "执行", "立即"):
                if not self._can_admin(event):
                    yield event.plain_result("只有管理员可以让日程立刻执行。")
                    return
                if len(rest) < 2:
                    yield event.plain_result("用法：/mw schedule run <日程 id>")
                    return
                result = await self.engine.run_schedule_now(
                    event.unified_msg_origin, rest[1]
                )
                yield event.plain_result(
                    result.get("note") or result.get("reason") or "已执行"
                )
                return
            lines = ["当前日程："]
            for schedule in (self.engine.schedules.schedules if self.engine.schedules else []):
                mark = "✅" if schedule.enabled else "⛔"
                lines.append(
                    f"{mark} {schedule.time} {schedule.id} → "
                    f"{'/'.join(str(getattr(step, 'type', '')) for step in schedule.action_chain)}"
                )
            yield event.plain_result("\n".join(lines))
            return

        if action in ("map", "地图"):
            world = self.engine.world
            lines = ["她的世界地图（按区域）："]
            for zone in world.zones:
                here = world.nodes_in_zone(zone.id)
                lines.append(f"【{zone.name}】{len(here)} 个地点")
                for node in here:
                    names = [
                        action.name or action.id
                        for action in world.actions_in(node.id)
                        if action.id != "walk_to"
                    ]
                    lines.append(
                        f"· {node.name}（{node.id}）：{'/'.join(names) or '只有通用动作'}"
                    )
            lines.append("区域内连线：")
            for edge in world.edges:
                lines.append(f"· {edge.from_} ↔ {edge.to}（{edge.ticks} tick）")
            if world.zone_edges:
                lines.append("跨区连线：")
                for edge in world.zone_edges:
                    lines.append(
                        f"· {node_label(world, edge.from_node)} → {node_label(world, edge.to_node)}"
                        f"（{edge.ticks} tick）"
                    )
            yield event.plain_result("\n".join(lines))
            return

        if action in ("nickname", "名片"):
            yield event.plain_result(await self._handle_nickname_command(event, rest))
            return

        if action in ("debug", "调试"):
            yield event.plain_result(await self._handle_debug_command(event, rest))
            return

        if action in ("session", "会话"):
            yield event.plain_result(await self._handle_session_command(event, rest))
            return

        if action in ("reload", "重载"):
            if not self._can_admin(event):
                yield event.plain_result("只有管理员可以重载配置。")
                return
            warnings = self.engine.reload_config()
            message = "配置已热加载。"
            if warnings:
                message += "\n提醒：" + "；".join(warnings)
            yield event.plain_result(message)
            return

        if action in ("reset", "重置"):
            if not self._can_admin(event):
                yield event.plain_result("只有管理员可以重置会话状态。")
                return
            target = rest[0] if rest else event.unified_msg_origin
            await self.db.call("delete_state", target)
            yield event.plain_result(f"已重置 {target} 的世界状态（记忆保留）。")
            return

        if action in ("restore-default", "恢复默认"):
            if not self._can_admin(event):
                yield event.plain_result("只有管理员可以恢复默认配置。")
                return
            restored = self.store.restore_default("all")
            self.engine.reload_config()
            yield event.plain_result("已恢复默认配置：" + "、".join(restored))
            return

        if action in ("event", "事件"):
            # 成功时**不回话**：她自己会在群里说这件事，指令再回一句就是噪音
            text = await self._cmd_event(event, rest)
            if text:
                yield event.plain_result(text)
            return

        if action in ("ability", "能力", "能力值"):
            # 能力值不公开：只有管理员能看（群友要看只能翻日志页）
            if not self._can_admin(event):
                yield event.plain_result("这项不公开，只有管理员可以看。")
                return
            yield event.plain_result(await self._cmd_ability(event))
            return

        if action in ("thread", "线索", "未了"):
            if not self._can_admin(event):
                yield event.plain_result("这项只有管理员可以看。")
                return
            yield event.plain_result(await self._cmd_thread(event))
            return

        if action in ("action", "actions", "动作", "动作库"):
            if not self._can_admin(event):
                yield event.plain_result("只有管理员可以清理动作库。")
                return
            yield event.plain_result(await self._cmd_actions(rest))
            return

        yield event.plain_result(f"未知指令：{action}\n\n{_help_text(self._pronoun())}")

    async def _cmd_profile(self, event: AstrMessageEvent, rest: list[str]) -> str:
        service = getattr(self, "profile_weaver_service", None)
        if service is None or not service.enabled:
            return "通讯录画像内核当前未启用，请先在插件配置中打开。"
        sub = (rest[0] if rest else "show").casefold()
        args = rest[1:]
        session_id = event.unified_msg_origin
        user_id = str(event.get_sender_id())
        actor = service.actor(event, "user_command")
        catalog = service.command_catalog()

        def help_text() -> str:
            lines = ["通讯录画像指令："]
            lines.extend(
                f"· /mw profile {item['command']} —— {item['desc']}"
                for item in catalog["user"]
            )
            if self._can_admin(event):
                lines.append("管理员：")
                lines.extend(
                    f"· /mw profile {item['command']} —— {item['desc']}"
                    for item in catalog["admin"]
                )
            lines.extend(
                [
                    "",
                    f"作用域：{'按会话隔离' if service.session_based else '跨会话共享'}",
                    f"字段锁：{service.store.field_lock_scope_label()}",
                    "WebUI：插件面板 → 我的世界编辑器 → 通讯录",
                ]
            )
            return "\n".join(lines)

        admin = self._can_admin(event)
        if sub in ("help", "帮助", "?"):
            return help_text()
        if sub in ("fields", "字段"):
            return service.field_catalog_text(user_id, session_id)
        if sub in ("stats", "统计"):
            if not admin:
                return "只有管理员可以查看全站画像统计。"
            return service.stats_text()
        if sub in ("user", "查询"):
            if not admin:
                return "只有管理员可以查询别人的画像。"
            if not args:
                return "用法：/mw profile user <用户|@某人>"
            target = service.resolve_target_user_id(event, " ".join(args))
            return service.profile_status_text(
                target,
                session_id,
                subject_label=f"用户 {target} ",
            )
        if sub in ("show", "me", "我的", "查看"):
            denied = service.group_privacy_denied(event)
            if denied:
                return denied
            return service.profile_status_text(user_id, session_id)
        if sub in ("set", "设置", "写入"):
            if len(args) < 2:
                return "用法：/mw profile set <字段> <值>"
            result = service.store.upsert_field(
                user_id=user_id,
                subject_name=str(event.get_sender_name()),
                field_name=args[0],
                value=" ".join(args[1:]),
                session_id=session_id if service.session_based else None,
                actor=actor,
                source_kind="manual_edit",
                evidence=str(event.message_str or ""),
                allow_custom_field=service.allow_user_custom_fields,
                field_description="用户通过通讯录指令创建的自定义字段",
            )
            return ("✅ " if result.ok else "❌ ") + result.message
        if sub in ("delete", "删除", "忘掉"):
            if not args:
                return "用法：/mw profile delete <字段|备注:序号>"
            result = service.store.delete_field(
                user_id=user_id,
                session_id=session_id if service.session_based else None,
                field_selector=" ".join(args),
                actor=actor,
                source_kind="manual_delete",
                evidence=str(event.message_str or ""),
            )
            return ("✅ " if result.ok else "❌ ") + result.message
        if sub in ("lock", "锁定", "unlock", "解锁"):
            if not args:
                return f"用法：/mw profile {sub} <字段>"
            return service.set_field_lock(
                user_id=user_id,
                session_id=session_id,
                field_name=" ".join(args),
                locked=sub in ("lock", "锁定"),
                actor=actor,
                subject_name=str(event.get_sender_name()),
            )
        if sub in ("history", "历史"):
            denied = service.group_privacy_denied(event)
            if denied:
                return denied
            if not args:
                return "用法：/mw profile history <字段> [条数]"
            limit = 10
            if len(args) > 1 and args[-1].isdigit():
                limit = max(1, min(int(args[-1]), 30))
                args = args[:-1]
            return service.field_history_text(
                user_id=user_id,
                session_id=session_id,
                field_name=" ".join(args),
                limit=limit,
            )
        if sub in ("clear", "清空"):
            result = service.store.clear_profile(
                user_id=user_id,
                session_id=session_id if service.session_based else None,
                actor=actor,
            )
            return ("✅ " if result.ok else "❌ ") + result.message
        if sub in ("audit", "审计"):
            if not admin:
                return "只有管理员可以查看画像审计。"
            target = ""
            limit = 10
            audit_args = list(args)
            if audit_args and audit_args[-1].isdigit():
                limit = max(1, min(int(audit_args.pop()), 50))
            if audit_args:
                target = service.resolve_target_user_id(event, " ".join(audit_args))
            page = service.audit(
                user_id=target,
                session_id=session_id if service.session_based else None,
                limit=limit,
                offset=0,
                include_rotated=True,
            )
            rows = page.get("items") or []
            if not rows:
                return "没有找到画像审计记录。"
            lines = [f"最近 {len(rows)} 条画像审计："]
            for item in rows:
                lines.append(
                    f"- [{item.get('at')}] {item.get('action') or '变更'} · "
                    f"{item.get('field_name') or '整条画像'} · "
                    f"{item.get('subject_name') or item.get('subject_user_id') or '-'}"
                )
                lines.append(
                    f"  {item.get('old_value') or '（空）'} → "
                    f"{item.get('new_value') or '（空）'}"
                )
            return "\n".join(lines)
        if sub in ("backup", "备份"):
            if not admin:
                return "只有管理员可以备份画像。"
            path = service.store.create_backup((args[0] if args else "manual")[:40])
            return (
                f"✅ 已创建备份：{path.name}"
                if path is not None
                else "❌ 备份失败，请检查数据目录写入权限。"
            )
        if sub in ("backups", "备份列表"):
            if not admin:
                return "只有管理员可以查看画像备份。"
            rows = service.backups()
            if not rows:
                return "暂无画像备份。"
            lines = [f"共 {len(rows)} 份画像备份："]
            for item in rows[:20]:
                size = float(item.get("size") or 0)
                lines.append(
                    f"- {item.get('name')}（{size / 1024:.1f} KB，"
                    f"{item.get('modified_at') or '未知'}）"
                )
            return "\n".join(lines)
        if sub in ("restore", "恢复"):
            if not admin:
                return "只有管理员可以恢复画像备份。"
            if not args:
                return "用法：/mw profile restore <备份文件名>"
            result = service.store.restore_backup(" ".join(args), actor)
            return ("✅ " if result.ok else "❌ ") + result.message
        if sub in ("merge", "合并"):
            if not admin:
                return "只有管理员可以合并画像。"
            if len(args) < 2:
                return "用法：/mw profile merge <源key> <目标key>"
            result = service.store.merge_profile_records(
                source_key=args[0],
                target_key=args[1],
                actor=actor,
            )
            return ("✅ " if result.ok else "❌ ") + result.message
        return f"未知画像指令：{sub}\n\n{help_text()}"

    async def _cmd_event(self, event: AstrMessageEvent, rest: list[str]) -> str:
        """`/mw event [要发生的事]`：用户直接给她安排一件事。"""

        if not self._can_deliver_event(event):
            return "只有管理员可以投递事件（可以在全局设置里改成所有人）。"
        session_id = event.unified_msg_origin
        sub = (rest[0] if rest else "").lower()
        if sub in ("list", "列表"):
            overview = await self.engine.event_overview(session_id)
            lines = ["最近的事件线索："]
            rows = [item for item in (overview.get("threads") or []) if item.get("steps")]
            if not rows:
                lines.append("· 还没有发生过什么")
            for item in rows:
                mark = "进行中" if str(item.get("status")) == "open" else "已结束"
                lines.append(f"· [{mark}] {item.get('title') or '一件事'}")
                for step in list(item.get("steps") or [])[:4]:
                    lines.append(f"   - {step}")
            return "\n".join(lines)
        if sub in ("help", "用法", "?"):
            return (
                "/mw event <一句话>\n"
                "　给她安排一件「会发生的事」，她会自己决定怎么处理。\n"
                "　例子：/mw event 出门忘了带伞\n"
                "　/mw event list  看最近的事件线索"
            )
        text = " ".join(rest)
        # 先问一句扩展：有没有谁认领这条（例如「/mw event h」交给扩展起它自己的一段流程）
        handled = await self.extension_host.command_event(session_id, text)
        if handled is not None:
            return handled
        note = await self.engine.submit_event(session_id, text)
        # 成功（note 为空）时不回话：她马上就会在群里说这件事
        return note

    def _can_deliver_event(self, event: AstrMessageEvent) -> bool:
        """谁能用 `/mw event` 投递事件（默认只有管理员）。"""

        if self._can_admin(event):
            return True
        return str(getattr(self.engine.world.events, "event_actor", "admin") or "admin") == "all"

    async def _cmd_ability(self, event: AstrMessageEvent) -> str:
        overview = await self.engine.event_overview(event.unified_msg_origin)
        rows = overview.get("abilities") or {}
        if not rows:
            return "能力值没有启用。"
        detail = "、".join(
            f"{item.get('label')}{item.get('hint')}" for item in rows.values()
        )
        spent = overview.get("ability_spent_today") or {}
        if spent:
            detail += "\n今天已经变化：" + "、".join(
                f"{key} {value:+.3f}" for key, value in spent.items()
            )
        return f"她现在的本事：{detail}"

    async def _cmd_thread(self, event: AstrMessageEvent) -> str:
        overview = await self.engine.event_overview(event.unified_msg_origin)
        pending = overview.get("pending_help") or {}
        lines: list[str] = []
        if str(pending.get("state") or "") == "active":
            lines.append(f"她正在等群友拿主意：{pending.get('title') or '一件事'}")
        elif str(pending.get("state") or "") == "idle":
            lines.append(f"这件事还没解决，她没再提：{pending.get('title') or '一件事'}")
        rows = [item for item in (overview.get("threads") or []) if str(item.get("status")) == "open"]
        if not rows and not lines:
            return "她手头没有没完的事。"
        for item in rows:
            lines.append(f"· {item.get('title') or '一件事'}")
            for step in list(item.get("steps") or [])[:5]:
                lines.append(f"   - {step}")
            if item.get("pending_followup"):
                lines.append(f"   → 还有下一步：{item.get('pending_followup')}")
        return "\n".join(lines)

    async def _cmd_actions(self, rest: list[str]) -> str:
        """`/mw action ...`：管理动作库（清除旧版工具 / 指令动作）。"""

        sub = rest[0].lower() if rest else ""
        if sub in ("list", "旧版", "legacy"):
            legacy = self.engine.legacy_actions()
            if not legacy:
                return "动作库里没有旧版动作。"
            lines = [
                f"旧版动作（{len(legacy)} 个，工具 / 指令型，v3 起不再执行）："
            ]
            for action in legacy[:50]:
                group = str(action.group or "").strip() or "（未分组）"
                lines.append(
                    f"· {action.id}（{action.name or action.id}）｜分组：{group}"
                )
            if len(legacy) > 50:
                lines.append(f"…还有 {len(legacy) - 50} 个")
            lines.append("全部清除：/mw action purge（先预览，再加 confirm）")
            return "\n".join(lines)
        if sub in ("purge", "clear", "清除", "清理", "删除"):
            confirm = any(
                item.lower() in ("confirm", "yes", "ok", "确认", "确定")
                for item in rest[1:]
            )
            result = await self.engine.purge_legacy_actions(confirm=confirm)
            preview = result.get("preview") or {}
            if not confirm and preview.get("count"):
                lines = [f"将清除 {preview['count']} 个旧版动作（工具 / 指令型）："]
                for item in list(preview.get("actions") or [])[:20]:
                    group = item.get("group") or "（未分组）"
                    lines.append(f"· {item.get('id')}（{item.get('name')}）｜分组：{group}")
                if preview["count"] > 20:
                    lines.append(f"…还有 {preview['count'] - 20} 个")
                if preview.get("groups"):
                    lines.append("涉及分组：" + "、".join(preview["groups"]))
                lines.append("确认无误后发：/mw action purge confirm")
                return "\n".join(lines)
            return str(result.get("note") or "已清除")
        return (
            "用法：\n"
            "· /mw action list          看动作库里还有哪些旧版动作（工具 / 指令型）\n"
            "· /mw action purge         预览要清除的旧版动作与涉及的分组\n"
            "· /mw action purge confirm 真正清除（不可撤销）"
        )

    @filter.llm_tool(name="mw_memory_lookup")
    @session_scoped
    async def tool_memory_lookup(self, event: AstrMessageEvent, topic: str = "") -> str:
        """查询角色在当前会话及共享生活组内记下的经历，核实以前发生的事。

        Args:
            topic(string): 要查的话题；留空返回最近相关的记忆。
        """
        session_id = event.unified_msg_origin
        if not self.engine.is_enabled(session_id):
            return "这个会话尚未启用我的世界。"
        with self.engine.config_scope(session_id):
            items = self.engine.memory.recall(
                session_id=session_id, persona_id=_event_persona_id(event),
                keyword=str(topic or "").strip(), strict_keyword=bool(str(topic or "").strip()),
                mode="group", limit=5,
            )
        if not items:
            return "没有找到相关的历史记忆，不要据此编造经历。"
        lines = ["历史记忆参考（以当前消息为准）："]
        used = 0
        for item in items:
            line = item.render(stamp=self.engine.prompts._memory_stamp(item.created_at, self.engine.local_now()))
            if used + len(line) > 6000:
                continue
            lines.append(line)
            used += len(line)
        return "\n".join(lines) if len(lines) > 1 else "相关记忆过长，请缩小话题后查询。"

    # ---------------- 给主人格 / 别的插件用的日程工具 ----------------

    @filter.llm_tool(name="mw_schedule_list")
    async def tool_schedule_list(self, event: AstrMessageEvent) -> str:
        """查看「我的世界」插件里 Bot 自己的日程表（什么时候会自动做什么）。

        没有参数，直接调用。
        """

        return self.engine.schedule_text()

    @filter.llm_tool(name="mw_schedule_add")
    async def tool_schedule_add(
        self,
        event: AstrMessageEvent,
        time: str,
        actions: str,
        days: str = "",
    ) -> str:
        """给「我的世界」插件里的 Bot 加一条日程：到点她会自动做一串动作。

        Args:
            time(string): 触发时间，24 小时制，例如 07:30
            actions(string): 动作链，用 > 表示先后顺序，例如 say>walk_to>search_web；需要先去某地时写成 walk_to@书房 id
            days(string): 星期，用逗号分隔，可选 mon,tue,wed,thu,fri,sat,sun；留空表示每天
        """

        chain: list[dict[str, Any]] = []
        for raw in str(actions or "").replace("，", ",").split(">"):
            part = raw.strip()
            if not part:
                continue
            node = ""
            if "@" in part:
                part, node = (piece.strip() for piece in part.split("@", 1))
            step: dict[str, Any] = {"type": part}
            if node:
                step["target_node"] = node
            chain.append(step)
        ok, note = await self.engine.schedule_add(
            {
                "time": time,
                "days": [day.strip() for day in str(days or "").replace("，", ",").split(",") if day.strip()],
                "action_chain": chain,
                "auto_travel": True,
            }
        )
        return note

    @filter.llm_tool(name="mw_schedule_remove")
    async def tool_schedule_remove(
        self,
        event: AstrMessageEvent,
        schedule_id: str = "",
        time: str = "",
        keyword: str = "",
    ) -> str:
        """删掉「我的世界」插件里 Bot 自己加的一条日程（用户手动配的日程删不掉）。

        Args:
            schedule_id(string): 日程 id，最准；不知道就留空
            time(string): 按时间匹配，例如 07:30
            keyword(string): 按里面出现的动作名匹配，例如 新闻
        """

        ok, note = await self.engine.schedule_remove(
            {"id": schedule_id, "time": time, "keyword": keyword}
        )
        return note

    async def _handle_nickname_command(self, event: AstrMessageEvent, rest: list[str]) -> str:
        sub = rest[0].lower() if rest else ""
        session_id = event.unified_msg_origin
        if not self.engine.is_enabled(session_id):
            return "这个会话还没有启用她的世界。"
        if not self._can_admin(event):
            return "只有管理员可以控制群名片。"
        text = " ".join(rest[1:]).strip()
        async with self.engine.session_state(session_id) as state:
            if sub in ("", "status", "状态"):
                node = self.engine.node(state.node_id)
                desired = compute_nickname(
                    self.engine.world, state, node, base=state.bot_base_nickname
                )
                lines = [
                    f"名片管理方：{self.messenger.group_card_owner(session_id) or '我的世界 MW'}",
                    f"原名（base）：{state.bot_base_nickname or '（空，还没抓到）'}",
                    f"当前名片：{state.bot_current_nickname or '（未知）'}",
                    f"按当前状态应该显示：{desired or '（算不出来）'}",
                    f"状态：{state.state}　地点：{state.node_id}　改名冷却：{int(self.engine.world.nickname_sync.cooldown_seconds)} 秒",
                    f"锁定：{'是' if state.bot_nickname_locked else '否'}　"
                    f"按地点/状态改名片：{'开' if self.engine.world.nickname_sync.enabled else '关'}",
                ]
                if not desired:
                    lines.append(
                        "⚠ 算不出名片：去「全局设置 → Bot 名称」填一个名字，"
                        "或者在群名片同步里给当前状态/地点配一条文案。"
                    )
                return "\n".join(lines)
            if sub in ("lock", "锁定"):
                state.bot_nickname_locked = True
                return "已锁定群名片，不再自动修改。"
            if sub in ("unlock", "解锁"):
                state.bot_nickname_locked = False
                return "已解锁群名片，会随状态自动变化。"
            if sub in ("set", "设置"):
                if not text:
                    return "用法：/mw nickname set <文本>"
                state.bot_current_nickname = text
                state.bot_base_nickname = text
                state.bot_nickname_locked = True
                changed, failed, reason = await self._apply_card_to_group(session_id, text)
                if not changed:
                    return f"已记录为「{text}」并锁定，但改名片失败：{reason}"
                note = f"已把 {changed} 个群的群名片设为「{text}」并锁定。"
                if failed:
                    note += f"（{failed} 个没改成：{reason}）"
                return note
            if sub in ("reset", "重置"):
                state.bot_nickname_locked = False
                state.bot_current_nickname = state.bot_base_nickname
                if state.bot_base_nickname:
                    changed, failed, reason = await self._apply_card_to_group(
                        session_id, state.bot_base_nickname
                    )
                    if not changed:
                        return f"已解锁，但恢复原名失败：{reason}"
                    if failed:
                        return f"已解锁并改回原名（{changed} 个群，{failed} 个没改成：{reason}）"
                return "已恢复原名并解锁。"
        return "用法：/mw nickname status|lock|unlock|set <文本>|reset"

    async def _handle_debug_command(self, event: AstrMessageEvent, rest: list[str]) -> str:
        if not self._can_admin(event):
            return "只有管理员可以使用调试命令。"
        sub = rest[0].lower() if rest else "state"
        session_id = rest[1] if len(rest) > 1 else event.unified_msg_origin
        if sub in ("on", "开"):
            self.debug = True
            self.engine.debug = True
            return "调试模式已开启（接管详情与动作校验会写入日志）。"
        if sub in ("off", "关"):
            self.debug = False
            self.engine.debug = False
            return "调试模式已关闭。"
        if sub in ("state", "状态"):
            snapshot = await self.engine.snapshot(session_id)
            return json.dumps(snapshot, ensure_ascii=False, indent=2)
        if sub in ("plan", "计划"):
            state = await self.engine.load_state(session_id, cold_start=False)
            from .core import planner as planner_module

            return planner_module.describe(state)
        if sub in ("stop", "停", "停下", "打断"):
            # 不依赖大模型的兜底：立刻停手 + 放弃剩下的安排
            stopped = await self.engine.interrupt(session_id, force=True)
            cleared = await self.engine.clear_plan(session_id)
            parts = []
            parts.append("停掉了手头的动作" if stopped else "她本来就没在做事")
            if cleared:
                parts.append("放弃了还没做的安排")
            return "，".join(parts) + "。"
        if sub in ("memories", "记忆"):
            rows = self.engine.memory.db.query_memories(session_id=session_id, limit=20)
            if not rows:
                return "（没有记忆）"
            return "\n".join(
                f"[{row['id']}] {row['scope']}/{row['node_id']} "
                f"w={row['weight']:.2f} {row['content']}"
                for row in rows
            )
        if sub in ("session", "会话"):
            ids = self.engine.enabled_session_ids()
            return "启用中的会话：\n" + "\n".join(f"· {item}" for item in ids)
        if sub in ("tools", "工具"):
            available = self.engine.available_tools()
            state = await self.engine.load_state(session_id, cold_start=False)
            allowed = self.engine.allowed_tool_names(state.node_id)
            return (
                f"当前位置 {state.node_id} 能用：{sorted(allowed)}"
                f"（= 通用工具 + 这里的动作绑定的工具）\n"
                f"AstrBot 已注册工具：{sorted(available)}"
            )
        if sub in ("prompt", "提示词"):
            return await self._prompt_report(session_id, "takeover")
        if sub in ("autoprompt", "自主提示词"):
            return await self._prompt_report(session_id, "autonomous")
        if sub in ("tick", "推进"):
            outcomes = await self.engine.tick()
            return "\n".join(
                f"{item.session_id}: {'; '.join(item.notes)} {item.messages}"
                for item in outcomes
            ) or "（无输出）"
        if sub in ("decide", "决策"):
            outcome = await self.engine.maybe_decide(session_id)
            if outcome is None:
                return "决策器跳过（未到间隔 / 正在忙）。"
            return f"{'; '.join(outcome.notes)} {outcome.messages}"
        return (
            "用法：/mw debug on|off|state|plan|stop|memories|session|tools|prompt|autoprompt|tick|decide"
        )

    async def _prompt_report(self, session_id: str, mode: str) -> str:
        """把提示词写成文件，并回一份「分段索引」。

        提示词三四千字，直接丢进聊天窗口会被平台截掉尾巴（正好是群聊上下文那几段）。
        这里只发索引 + 文件路径，原文让用户去编辑器或文件里看。
        """

        if mode == "autonomous":
            text = await self.engine.preview_autonomous_prompt(session_id)
            name = "prompt_autonomous.txt"
            label = "自主提示词"
        else:
            text = await self.engine.preview_last_reply_request(session_id)
            name = "prompt_takeover.txt"
            label = "接管请求（上一轮实际发出去的原文）"
        path = Path(self.store.data_dir) / name
        try:
            path.write_text(text, encoding="utf-8")
            written = str(path)
        except Exception as exc:
            written = f"（写文件失败：{exc}）"
        lines = [f"{label}：{len(text)} 字符，共 {len(prompt_section_index(text))} 段"]
        for item in prompt_section_index(text):
            lines.append(f"· {item['title']}：{item['chars']} 字符")
        lines.append(f"完整内容已写入：{written}")
        lines.append("（编辑器「调试 → 预览接管请求 / 预览自主提示词」里能看全文）")
        return "\n".join(lines)

    async def _handle_session_command(self, event: AstrMessageEvent, rest: list[str]) -> str:
        if not self._can_admin(event):
            return "只有管理员可以管理会话白名单。"
        sub = rest[0].lower() if rest else "list"
        if sub in ("list", "列表"):
            sessions = self.engine.sessions.sessions if self.engine.sessions else []
            if not sessions:
                return "白名单为空。"
            lines = [
                f"{'✅' if item.enabled else '⛔'} {item.session_id} ({item.type})"
                + (f" - {item.note}" if item.note else "")
                for item in sessions
            ]
            groups = list(getattr(self.engine.sessions, "groups", None) or [])
            if groups:
                lines.append("")
                lines.append("会话组（同一个她，共享状态 / 记忆 / 上下文）：")
                lines.extend(
                    f"🔗 {group.name or group.id}："
                    + "、".join(group.sessions or [])
                    + (f"（代表：{group.main_session}）" if group.main_session else "")
                    for group in groups
                )
            return "会话白名单：\n" + "\n".join(lines)
        if sub in ("add", "添加"):
            target = rest[1] if len(rest) > 1 else event.unified_msg_origin
            session_type = "private" if event.is_private_chat() and target == event.unified_msg_origin else (
                "group" if ":GroupMessage:" in target else "private"
            )
            added = self.store.add_session(target, session_type=session_type)
            self.engine.reload_config()
            return f"{'已添加' if added else '已在白名单中'}：{target}"
        if sub in ("remove", "移除", "delete"):
            if len(rest) < 2:
                return "用法：/mw session remove <会话 ID>"
            removed = self.store.remove_session(rest[1])
            self.engine.reload_config()
            return "已移除。" if removed else "没找到这个会话。"
        if sub in ("enable", "启用", "disable", "禁用"):
            if len(rest) < 2:
                return "用法：/mw session enable|disable <会话 ID>"
            enabled = sub in ("enable", "启用")
            ok = self.store.set_session_enabled(rest[1], enabled)
            self.engine.reload_config()
            return ("已更新。" if ok else "没找到这个会话。")
        return "用法：/mw session list|add [ID]|remove <ID>|enable|disable <ID>"

    # ================= Web API =================

    def sampling_params(self) -> dict[str, float]:
        """给适配器用的采样参数（关着就返回空字典＝不覆盖 Provider 自己的设置）。"""

        config = dict(getattr(self, "_sampling_config", None) or {})
        if not config.get("enabled"):
            return {}
        result: dict[str, float] = {}
        for key in ("temperature", "top_p", "frequency_penalty", "presence_penalty"):
            try:
                number = float(config.get(key))
            except (TypeError, ValueError):
                continue
            result[key] = number
        return result



# ======================================================================
# 小工具
# ======================================================================

install_profile_commands(MyWorldPlugin)


def _help_text(pronoun: str = "她") -> str:
    """指令帮助文案。pronoun 由全局设置里的性别决定（她/他/ta）。"""

    return (
        "我的世界指令：\n"
        f"· /mw status            {pronoun}现在在哪、在做什么、心情如何\n"
        f"· /mw recall <话题>     让{pronoun}回忆某个话题\n"
        f"· /mw memory            看看{pronoun}记得你什么\n"
        "· /mw profile           查看 / 管理自己的通讯录详细画像\n"
        "· /我的画像 /设置画像 /画像历史   兼容原心迹画像的顶层指令\n"
        f"· /mw forget me [话题]  让{pronoun}忘记关于你的记忆\n"
        f"· /mw event <一句话>   给她安排一件会发生的事（看她怎么处理）\n"
        f"· /mw event list       看最近的事件线索\n"
        f"· /mw ability          看{pronoun}的本事（能力值，管理员）\n"
        f"· /mw thread           看{pronoun}手头没完的事（管理员）\n"
        "· /mw schedule          查看日程\n"
        "· /mw schedule run <id> 立刻跑一遍某条日程（管理员）\n"
        "· /mw map               查看地图\n"
        "· /mw nickname lock|unlock|set <文本>|reset   群名片控制（管理员）\n"
        "· /mw session list|add [ID]|remove <ID>|enable|disable <ID>   会话白名单（管理员）\n"
        "· /mw reload            热加载配置（管理员）\n"
        "· /mw reset [会话]      重置某个会话的状态（管理员）\n"
        "· /mw restore-default   恢复默认配置（管理员）\n"
        "· /mw action list|purge  看 / 清除旧版工具与指令动作（管理员）\n"
        "· /mw debug on|off|state|plan|stop|memories|session|tools|prompt|autoprompt|tick|decide   调试（管理员）"
    )


def _cfg_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "开启", "是")
    return bool(value)


def _cfg_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _command_args(event: AstrMessageEvent) -> list[str]:
    text = (event.get_message_str() or "").strip()
    if not text:
        return []
    parts = text.split()
    if parts and parts[0].lstrip("/").lower() in ("mw", "我的世界", "myworld"):
        parts = parts[1:]
    return parts


def _event_persona_id(event: AstrMessageEvent) -> str:
    for attr in ("persona_id",):
        value = getattr(event, attr, None)
        if isinstance(value, str) and value:
            return value
    session = getattr(event, "session", None)
    value = getattr(session, "persona_id", None) if session is not None else None
    return value if isinstance(value, str) else ""


def _render_status(snapshot: dict[str, Any], pronoun: str = "她") -> str:
    values = snapshot.get("values", {})
    action = snapshot.get("current_action") or {}
    action_text = (snapshot.get("activity") or {}).get("label") or action.get("desc") or action.get("type") or "自由休息"
    travel = snapshot.get("travel") or []
    lines = [
        f"{pronoun}现在在【{snapshot.get('node_name') or snapshot.get('node_id')}】",
        f"状态：{snapshot.get('state')}　心情：{snapshot.get('mood')}",
        f"正在做：{action_text}",
        "数值："
        f"精力 {values.get('energy', 0):.2f}　孤独 {values.get('loneliness', 0):.2f}　"
        f"好奇 {values.get('curiosity', 0):.2f}　心潮 {values.get('affect', 0):.2f}　"
        f"无聊 {values.get('boredom', 0):.2f}　欲求 {values.get('desire', 0):.2f}",
        f"世界时间：{snapshot.get('world_time')} tick"
        f"（1 tick = {int(snapshot.get('tick_seconds', 60))} 秒）",
    ]
    if travel:
        lines.append(
            "从这里出发："
            + "、".join(f"{item['name']} {item['ticks']} tick" for item in travel)
        )
    if snapshot.get("unanswered_count"):
        lines.append(f"连续无人回应：{snapshot['unanswered_count']} 次")
    return "\n".join(lines)






