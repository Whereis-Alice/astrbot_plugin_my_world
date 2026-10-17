"""通讯录画像内核：融合原「心迹画像」的数据、字段锁、审计与备份能力。"""

from __future__ import annotations

import json
import re
import shutil
import unicodedata
from pathlib import Path
from typing import Any

from astrbot.api import logger

from . import profile_weaver as legacy_bridge
from .constants import PLUGIN_VERSION
from .profile_weaver_store import (
    BLOCKED_PROFILE_NAME_VALUES,
    DEFAULT_FIELDS,
    EMPTY_PROFILE_SUMMARY,
    FIELD_LOCK_SCOPE_LABELS,
    NOTES_FIELD_NAME,
    AuditActor,
    ProfileStore,
)

PROFILE_TOOL_NAMES = (
    "profileweaver_view_profile",
    "profileweaver_remember_field",
    "profileweaver_forget_field",
)

LEAN_PROMPT_TEMPLATE = """<ProfileWeaver>
当前说话人：{sender_name} ({sender_id})
当前画像：暂无记录。

只有当前发送者明确、稳定地自述个人信息时，才调用画像写入工具。
提到他人、转述、玩笑、角色扮演或信息不确定时，不要写入。
</ProfileWeaver>"""

DEFAULT_PROMPT_TEMPLATE = """<ProfileWeaver>
当前说话人：{sender_name} ({sender_id})
画像作用域：{session_scope}

当前画像：
{profile_summary}

基础字段：
{field_catalog}

当前自定义字段：
{custom_field_catalog}

规则：
1. 画像只属于当前消息发送者，不属于你，也不属于其他群友。
2. 只有当前发送者明确谈论自己、表达稳定偏好或纠正自己的信息时，才允许写入。
3. 提到他人、转述、玩笑、角色扮演、引用历史或信息不确定时，不要写入。
4. 优先复用已有字段；确实不够表达时，才创建当前用户专属自定义字段。
5. 临时状态、推测、系统设定和消息过程信息不要存入画像。
6. 删除画像前，必须确认用户明确要求删除或纠正。
7. 工具返回拒绝、重复或冲突时，必须告诉用户没有写入并说明原因。
</ProfileWeaver>"""

REMEMBER_SOURCE_KINDS = {"self_report", "self_preference", "self_correction"}
FORGET_SOURCE_KINDS = {"self_request", "self_correction"}
EVIDENCE_PUNCTUATION_TABLE = str.maketrans(
    {
        "，": ",",
        "。": ".",
        "、": ",",
        "；": ";",
        "：": ":",
        "？": "?",
        "！": "!",
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "「": '"',
        "」": '"',
        "『": '"',
        "』": '"',
        "（": "(",
        "）": ")",
        "【": "[",
        "】": "]",
        "《": "<",
        "》": ">",
        "—": "-",
        "－": "-",
        "～": "~",
        "…": ".",
        "·": ".",
    }
)

_THIRD_PARTY_HE = r"(?<![其吉維维利排])他(?!妈(?!妈)|娘(?!家))"
THIRD_PARTY_PATTERN = re.compile(
    "|".join(
        (
            r"(?<![0-9A-Za-z._+\-])@",
            _THIRD_PARTY_HE,
            r"她",
            r"(?<![0-9A-Za-z])ta(?![0-9A-Za-z])",
            r"别人",
            r"群友",
            r"朋友说",
        )
    ),
    re.IGNORECASE,
)
HARD_THIRD_PARTY_PATTERN = re.compile(
    "|".join(
        (
            r"朋友说",
            r"(?<![其吉維维利排])[他她](?:们)?说",
            r"别人说",
            r"群友说",
            r"转述",
            r"引用",
            r"代发",
            r"帮我问",
            r"替[他她]说",
        )
    )
)
FIRST_PERSON_MARKERS = (
    "我",
    "我的",
    "我是",
    "我叫",
    "本人",
    "俺",
    "咱",
    "自己",
    "纠正一下",
    "更正一下",
)
STRONG_SELF_MARKERS = (
    "我是",
    "我叫",
    "我的",
    "我喜欢",
    "我爱吃",
    "我不吃",
    "我讨厌",
    "我怕",
    "我来自",
    "我住在",
    "我在",
    "我学",
    "我做",
    "我有",
    "我养",
    "我平时",
    "我通常",
    "我一般",
    "我自己",
    "叫我",
    "请叫我",
    "可以叫我",
    "本人",
)
PROFILE_SIGNAL_MARKERS = (
    "我的",
    "叫我",
    "称呼我",
    "昵称",
    "名字",
    "我是",
    "我叫",
    "喜欢",
    "爱吃",
    "不吃",
    "忌口",
    "过敏",
    "爱好",
    "职业",
    "工作",
    "专业",
    "学生",
    "学校",
    "生日",
    "纪念日",
    "所在地",
    "来自",
    "住在",
    "城市",
    "mbti",
    "宠物",
    "养了",
    "作息",
    "熬夜",
    "早睡",
    "晚睡",
    "害怕",
    "恐惧",
    "弱点",
    "擅长",
    "熟悉",
    "健康",
    "更正",
    "改成",
    "不是",
    "别记",
    "删掉",
    "删除",
    "忘掉",
)
PRIVATE_PROFILE_PREFIXES = (
    "叫我",
    "请叫我",
    "可以叫我",
    "我是",
    "我叫",
    "我喜欢",
    "我爱吃",
    "我不吃",
    "我讨厌",
    "我怕",
    "我来自",
    "我住在",
    "我在",
    "我学",
    "我做",
    "我有",
    "我养",
    "我的",
    "爱吃",
    "不吃",
    "过敏",
    "喜欢",
    "讨厌",
    "来自",
    "住在",
    "学生",
    "职业是",
    "工作是",
    "生日是",
    "mbti是",
)
AMBIGUOUS_MULTI_SUBJECT_PATTERNS = (
    re.compile(r"我和[^，。！？\s]{1,12}"),
    re.compile(r"我跟[^，。！？\s]{1,12}"),
    re.compile(r"我比(?!较)[^，。！？\s]{1,12}"),
    re.compile(r"和我[^，。！？\s]{1,12}"),
    re.compile(r"跟我[^，。！？\s]{1,12}"),
)
DIRECT_AUTO_FIELD_PATTERN = r"(?P<field>[\u4e00-\u9fffA-Za-z0-9_\-/ ]{1,16}?)"
DIRECT_AUTO_VALUE_PATTERN = r"(?P<value>[^，。！？!?；;\n]{1,120})"
DIRECT_AUTO_PROFILE_PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (
        re.compile(
            rf"^我最喜欢的{DIRECT_AUTO_FIELD_PATTERN}\s*(?:[:：=]|是|为)\s*"
            rf"{DIRECT_AUTO_VALUE_PATTERN}$"
        ),
        "最喜欢的{field}",
        "self_preference",
    ),
    (
        re.compile(
            rf"^我最爱的{DIRECT_AUTO_FIELD_PATTERN}\s*(?:[:：=]|是|为)\s*"
            rf"{DIRECT_AUTO_VALUE_PATTERN}$"
        ),
        "最爱的{field}",
        "self_preference",
    ),
    (
        re.compile(
            rf"^我喜欢的{DIRECT_AUTO_FIELD_PATTERN}\s*(?:[:：=]|是|为)\s*"
            rf"{DIRECT_AUTO_VALUE_PATTERN}$"
        ),
        "喜欢的{field}",
        "self_preference",
    ),
    (
        re.compile(
            rf"^我爱看的{DIRECT_AUTO_FIELD_PATTERN}\s*(?:[:：=]|是|为)\s*"
            rf"{DIRECT_AUTO_VALUE_PATTERN}$"
        ),
        "爱看的{field}",
        "self_preference",
    ),
    (
        re.compile(
            rf"^我常看的{DIRECT_AUTO_FIELD_PATTERN}\s*(?:[:：=]|是|为)\s*"
            rf"{DIRECT_AUTO_VALUE_PATTERN}$"
        ),
        "常看的{field}",
        "self_preference",
    ),
    (
        re.compile(
            rf"^我常玩的{DIRECT_AUTO_FIELD_PATTERN}\s*(?:[:：=]|是|为)\s*"
            rf"{DIRECT_AUTO_VALUE_PATTERN}$"
        ),
        "常玩的{field}",
        "self_preference",
    ),
    (
        re.compile(
            rf"^我常用的{DIRECT_AUTO_FIELD_PATTERN}\s*(?:[:：=]|是|为)\s*"
            rf"{DIRECT_AUTO_VALUE_PATTERN}$"
        ),
        "常用的{field}",
        "self_preference",
    ),
    (
        re.compile(
            rf"^我的{DIRECT_AUTO_FIELD_PATTERN}\s*(?:[:：=]|是|为)\s*"
            rf"{DIRECT_AUTO_VALUE_PATTERN}$"
        ),
        "{field}",
        "self_report",
    ),
)
DIRECT_AUTO_FIELD_DENY_PATTERN = re.compile(
    "|".join(
        (
            r"@",
            r"朋友",
            r"群友",
            r"别人",
            _THIRD_PARTY_HE,
            r"她",
            r"父母",
            r"妈妈",
            r"爸爸",
            r"同学",
            r"老师",
            r"系统",
            r"提示词",
            r"消息",
            r"聊天记录",
        )
    )
)
DIRECT_AUTO_VALUE_STRIP_CHARS = " \t\r\n\"'“”‘’`。！？!?；;"
AUTO_EXTRACT_SYSTEM_PROMPT = """<ProfileWeaverAutoExtract>
你是通讯录画像的后台抽取器。
只依据当前用户本轮消息，决定是否为当前消息发送者调用画像工具。

硬性规则：
1. 只记录适合长期记忆、且明确属于当前发送者的信息。
2. 提到别人、转述、玩笑、角色扮演、引用历史、猜测或临时状态时，回复 NOOP。
3. 优先更新已有字段；只有用户明确要求删除或纠正时才删除。
4. 没有明确可写入的内容时直接回复 NOOP。
5. 不能修改当前发送者之外的任何画像。

当前说话人：{sender_name} ({sender_id})
当前画像：
{profile_summary}

可用字段：
{field_catalog}

当前自定义字段：
{custom_field_catalog}
</ProfileWeaverAutoExtract>"""


def _cfg_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _clean(value: Any, limit: int = 200) -> str:
    return " ".join(str(value or "").split()).strip()[:limit]


class ProfileWeaverService:
    """AstrBot 事件层与独立画像存储层之间的薄服务。"""

    def __init__(self, plugin: Any, data_root: str) -> None:
        self.plugin = plugin
        self.config = plugin.config
        self.prompt_enabled = True
        self.auto_extract_tools: Any = None
        self.legacy_source_dir = ""
        self.data_dir = Path(plugin.store.data_dir) / "profile_weaver"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.legacy_source_dir = self.migrate_from_legacy(data_root)

        fields = self._adopt_new_builtin_fields(
            self.config.get("weaver_default_fields")
            or [name for name, _description in DEFAULT_FIELDS]
        )
        self.store = ProfileStore(
            data_dir=self.data_dir,
            builtin_fields=[str(item) for item in fields if str(item).strip()],
            max_notes_count=self._safe_int(
                self.config.get("weaver_max_notes_count"), 5, 1, 20
            ),
            custom_field_name_max_length=self._safe_int(
                self.config.get("weaver_custom_field_name_max_length"), 16, 1, 40
            ),
            field_value_max_length=self._safe_int(
                self.config.get("weaver_field_value_max_length"), 160, 20, 2000
            ),
            audit_log_max_mb=self._safe_float(
                self.config.get("weaver_audit_log_max_mb"), 8.0, 0.1, 1024.0
            ),
            backup_retention_days=self._safe_int(
                self.config.get("weaver_backup_retention_days"), 14, 0, 3650
            ),
            field_lock_scope=str(
                self.config.get("weaver_field_lock_scope", "llm_only")
            ),
            audit_log_keep_rotated=self._safe_int(
                self.config.get("weaver_audit_log_keep_rotated"), 5, 0, 100
            ),
            backup_max_count=self._safe_int(
                self.config.get("weaver_backup_max_count"), 60, 0, 10000
            ),
        )
        self._sync_latest_legacy_profiles()
        self._normalize_profile_keys()

    @staticmethod
    def _safe_int(value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = default
        return max(minimum, min(number, maximum))

    @staticmethod
    def _safe_float(
        value: Any, default: float, minimum: float, maximum: float
    ) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = default
        return max(minimum, min(number, maximum))

    def _adopt_new_builtin_fields(self, configured: Any) -> list[str]:
        names = [str(item).strip() for item in (configured or []) if str(item).strip()]
        if not _cfg_bool(
            self.config.get("weaver_auto_adopt_new_builtin_fields"), True
        ):
            return names
        if str(self.config.get("weaver_builtin_fields_adopted_version") or "") == PLUGIN_VERSION:
            return names
        merged = list(names)
        seen = set(merged)
        for name, _description in DEFAULT_FIELDS:
            if name not in seen:
                merged.append(name)
                seen.add(name)
        self.config["weaver_default_fields"] = merged
        self.config["weaver_builtin_fields_adopted_version"] = PLUGIN_VERSION
        save = getattr(self.config, "save_config", None)
        if callable(save):
            try:
                save()
            except Exception as exc:
                self.plugin.logger.warning(
                    f"[my_world] 保存画像字段配置失败：{exc}"
                )
        return merged

    @property
    def enabled(self) -> bool:
        return _cfg_bool(self.config.get("weaver_enabled"), True)

    @property
    def session_based(self) -> bool:
        return _cfg_bool(self.config.get("weaver_session_based"), False)

    @property
    def allow_llm_custom_fields(self) -> bool:
        return _cfg_bool(self.config.get("weaver_allow_llm_custom_fields"), True)

    @property
    def allow_user_custom_fields(self) -> bool:
        return _cfg_bool(self.config.get("weaver_allow_user_custom_fields"), True)

    @property
    def llm_tools_enabled(self) -> bool:
        return self.enabled and _cfg_bool(
            self.config.get("weaver_llm_tools_enabled"), True
        )

    @property
    def proactive_extraction_enabled(self) -> bool:
        return self.enabled and _cfg_bool(
            self.config.get("weaver_proactive_extraction_enabled"), True
        )

    @property
    def webui_allow_edit(self) -> bool:
        return self.enabled and _cfg_bool(
            self.config.get("weaver_webui_allow_edit"), True
        )

    @property
    def strict_identity_guard(self) -> bool:
        return _cfg_bool(
            self.config.get("weaver_strict_identity_guard"), True
        )

    @property
    def evidence_match_mode(self) -> str:
        mode = str(
            self.config.get("weaver_evidence_match_mode") or "normalized"
        ).strip().lower()
        return mode if mode in {"strict", "normalized", "loose"} else "normalized"

    @property
    def allow_profile_in_group(self) -> bool:
        return _cfg_bool(
            self.config.get("weaver_allow_profile_in_group"), False
        )

    @property
    def group_profile_denied_msg(self) -> str:
        return str(
            self.config.get("weaver_group_profile_denied_msg")
            or "画像内容可能涉及隐私，请私聊查看。"
        )

    @property
    def export_mask_user_ids(self) -> bool:
        return _cfg_bool(
            self.config.get("weaver_export_mask_user_ids"), False
        )

    @property
    def debug_enabled(self) -> bool:
        return str(self.config.get("weaver_debug_log_level") or "INFO").upper() == "DEBUG"

    def debug(self, message: str) -> None:
        if self.debug_enabled:
            logger.debug("[my_world/profile] %s", message)

    def migrate_from_legacy(self, data_root: str) -> str:
        """首次启动时整目录继承原插件数据，包含画像、审计和备份。"""

        source = legacy_bridge.profiles_path(data_root)
        if source is None:
            return ""
        source_dir = source.parent
        marker = self.data_dir / ".legacy_imported"
        if marker.exists():
            return str(source_dir)
        if (self.data_dir / "profiles.json").exists():
            marker.write_text(str(source_dir), encoding="utf-8")
            return str(source_dir)
        try:
            for item in source_dir.iterdir():
                if item.name in {".git", "__pycache__"}:
                    continue
                target = self.data_dir / item.name
                if item.is_dir():
                    shutil.copytree(item, target, dirs_exist_ok=True)
                elif item.is_file():
                    shutil.copy2(item, target)
            marker.write_text(str(source_dir), encoding="utf-8")
            self.plugin.logger.info(
                f"[my_world] 已继承心迹画像数据：{source_dir} → {self.data_dir}"
            )
            return str(source_dir)
        except OSError as exc:
            self.plugin.logger.warning(f"[my_world] 继承心迹画像数据失败：{exc}")
            return ""

    def _sync_latest_legacy_profiles(self) -> None:
        """把旧插件之后新增的画像记录按字段时间合并进来，不覆盖本插件的新改动。"""

        if not self.legacy_source_dir:
            return
        source = Path(self.legacy_source_dir) / "profiles.json"
        if not source.is_file():
            return
        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.debug(f"读取旧画像增量失败：{exc}")
            return
        if not isinstance(raw, dict) or not raw:
            return
        incoming = self.store._normalize_profiles_payload(raw)
        if not incoming:
            return
        merged, changed = self.store._merge_profiles(self.store.profiles, incoming)
        if not changed:
            return
        self.store.profiles = merged
        self.store._save_profiles()
        self.plugin.logger.info(
            f"[my_world] 已增量合并心迹画像记录：{len(incoming)} 条"
        )

    def _normalize_profile_keys(self) -> None:
        """把旧插件的 key 调整成当前作用域，避免数据继承了却点不开。"""

        normalized: dict[str, dict[str, Any]] = {}
        changed = False
        for old_key, record in self.store.profiles.items():
            if not isinstance(record, dict):
                changed = True
                continue
            user_id = str(
                record.get("subject_user_id")
                or self.store._extract_user_id_from_key(old_key)
                or ""
            ).strip()
            if not user_id:
                normalized[old_key] = record
                continue
            source_session, _ = self.store.split_profile_key(old_key, record)
            desired_key = self.store.profile_key(
                user_id,
                source_session if self.session_based else None,
            )
            if desired_key != old_key:
                changed = True
            if desired_key not in normalized:
                normalized[desired_key] = record
                continue
            merged, _record_changed = self.store._merge_profile_record(
                normalized[desired_key], record
            )
            normalized[desired_key] = merged
        if changed and normalized != self.store.profiles:
            self.store.profiles = normalized
            self.store._save_profiles()
            self.plugin.logger.info(
                f"[my_world] 已按当前画像作用域整理 {len(normalized)} 条记录"
            )

    def profile_key(self, session_id: str, user_id: str) -> str:
        return self.store.profile_key(
            user_id,
            str(session_id or "") if self.session_based else None,
        )

    def preferred_record_key(self, session_id: str, user_id: str) -> str:
        exact = self.profile_key(session_id, user_id)
        if exact in self.store.profiles:
            return exact
        global_key = self.store.profile_key(user_id, None)
        if global_key in self.store.profiles:
            return global_key
        if self.session_based:
            return exact
        candidates: list[tuple[str, str]] = []
        for key, record in self.store.profiles.items():
            if not isinstance(record, dict):
                continue
            if str(record.get("subject_user_id") or "") != str(user_id or ""):
                continue
            candidates.append((str(record.get("updated_at") or ""), key))
        return max(candidates, default=("", ""))[1]

    def record_summaries_for_user(
        self,
        user_id: str,
        *,
        session_id: str | None = None,
        include_all: bool = True,
    ) -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []
        for key, record in self.store.profiles.items():
            if not isinstance(record, dict):
                continue
            if str(record.get("subject_user_id") or "") != str(user_id or ""):
                continue
            source_session = str(record.get("session_id") or "")
            if (
                self.session_based
                and not include_all
                and source_session
                and source_session != str(session_id or "")
            ):
                continue
            summaries.append(self.store.summarize_profile(key, record))
        summaries.sort(
            key=lambda item: (str(item.get("updated_at") or ""), str(item.get("key") or "")),
            reverse=True,
        )
        return summaries

    def summary_for(self, session_id: str, user_id: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        key = self.preferred_record_key(session_id, user_id)
        record = self.store.profiles.get(key)
        if not isinstance(record, dict):
            return None
        return self.store.summarize_profile(key, record)

    def detail_for(self, session_id: str, user_id: str) -> dict[str, Any]:
        if not self.enabled:
            return {}
        preferred_key = self.preferred_record_key(session_id, user_id)
        preferred = self.store.get_profile_detail(preferred_key)
        if not preferred:
            return {}
        details = [
            self.store.get_profile_detail(str(item.get("key") or ""))
            for item in self.record_summaries_for_user(
                user_id,
                session_id=session_id,
                include_all=False,
            )
        ]
        details = [item for item in details if item]
        if len(details) <= 1:
            return preferred

        fields_by_name: dict[str, dict[str, Any]] = {}
        for detail in details:
            for field in detail.get("fields") or []:
                if not isinstance(field, dict):
                    continue
                name = str(field.get("name") or "")
                if not name:
                    continue
                current = fields_by_name.get(name)
                if current is None or str(field.get("updated_at") or "") >= str(
                    current.get("updated_at") or ""
                ):
                    fields_by_name[name] = dict(field)

        locked_fields = sorted(
            {
                str(field)
                for detail in details
                for field in detail.get("locked_fields") or []
                if str(field)
            }
        )
        notes: list[str] = []
        for detail in details:
            for note in detail.get("notes") or []:
                text = str(note or "").strip()
                if text and text not in notes:
                    notes.append(text)

        available: dict[str, dict[str, Any]] = {}
        for detail in details:
            for field in detail.get("available_fields") or []:
                name = str(field.get("name") or "")
                if not name:
                    continue
                current = available.get(name, {**field})
                current["filled"] = bool(current.get("filled") or field.get("filled"))
                current["locked"] = bool(current.get("locked") or field.get("locked"))
                available[name] = current

        return {
            **preferred,
            "fields": list(fields_by_name.values()),
            "notes": notes,
            "locked_fields": locked_fields,
            "locked_count": len(locked_fields),
            "available_fields": list(available.values()),
            "completeness": max(
                float(item.get("completeness") or 0.0) for item in details
            ),
            "record_count": len(details),
            "record_keys": [str(item.get("key") or "") for item in details],
            "merged_from_records": True,
        }

    def detail_by_key(self, key: str) -> dict[str, Any]:
        if not self.enabled:
            return {}
        return self.store.get_profile_detail(str(key or ""))

    def people_for_session(self, session_id: str) -> dict[str, dict[str, Any]]:
        if not self.enabled:
            return {}
        grouped: dict[str, list[dict[str, Any]]] = {}
        for key, record in self.store.profiles.items():
            if not isinstance(record, dict):
                continue
            summary = self.store.summarize_profile(key, record)
            user_id = str(summary.get("user_id") or "")
            source_session = str(summary.get("session_id") or "")
            if not user_id:
                continue
            if (
                self.session_based
                and source_session
                and source_session != str(session_id or "")
            ):
                continue
            grouped.setdefault(user_id, []).append(summary)

        people: dict[str, dict[str, Any]] = {}
        for user_id, rows in grouped.items():
            rows.sort(
                key=lambda item: (
                    str(item.get("updated_at") or ""),
                    str(item.get("key") or ""),
                ),
                reverse=True,
            )
            preferred_key = self.preferred_record_key(session_id, user_id)
            preferred = next(
                (item for item in rows if str(item.get("key") or "") == preferred_key),
                rows[0],
            )
            locked_fields = sorted(
                {
                    str(field)
                    for item in rows
                    for field in item.get("locked_fields") or []
                    if str(field)
                }
            )
            preview: list[dict[str, str]] = []
            seen_preview: set[tuple[str, str]] = set()
            for row in rows:
                for item in row.get("preview") or []:
                    pair = (
                        str(item.get("name") or ""),
                        str(item.get("value") or ""),
                    )
                    if not pair[0] or pair in seen_preview:
                        continue
                    seen_preview.add(pair)
                    preview.append({"name": pair[0], "value": pair[1]})
                    if len(preview) >= 6:
                        break
                if len(preview) >= 6:
                    break
            people[user_id] = {
                **preferred,
                "key": preferred_key,
                "record_count": len(rows),
                "record_keys": [str(item.get("key") or "") for item in rows],
                "locked_count": len(locked_fields),
                "locked_fields": locked_fields,
                "preview": preview,
            }
        return people

    def records_for_user(self, user_id: str) -> list[dict[str, Any]]:
        return [
            self.store.get_profile_detail(str(item.get("key") or ""))
            for item in self.record_summaries_for_user(user_id)
            if str(item.get("key") or "") in self.store.profiles
        ]

    def sync_contact_identity(
        self,
        *,
        session_id: str,
        user_id: str,
        actor: AuditActor,
        call_me: str = "",
        note: str = "",
    ) -> list[str]:
        """Keep the canonical nickname and note fields in the detailed contact record."""

        if not self.enabled:
            return []
        messages: list[str] = []
        current = self.detail_for(session_id, user_id)
        subject_name = str(
            current.get("display_name")
            or current.get("subject_name")
            or user_id
        )
        nickname = str(call_me or "").strip()
        if nickname:
            result = self.store.upsert_field(
                user_id=user_id,
                subject_name=subject_name,
                field_name="昵称",
                value=nickname,
                session_id=session_id if self.session_based else None,
                actor=actor,
                source_kind="manual_edit",
                evidence="通讯录统一编辑",
                allow_custom_field=False,
            )
            if result.ok:
                messages.append(result.message)
        note_text = str(note or "").strip()
        if note_text:
            existing = {
                str(item).strip()
                for item in (current.get("notes") or [])
                if str(item).strip()
            }
            incoming_parts = [
                item.strip()
                for item in re.split(r"[；;|]+", note_text)
                if item.strip()
            ] or [note_text]
            missing = [item for item in incoming_parts if item not in existing]
            if missing:
                result = self.store.upsert_field(
                    user_id=user_id,
                    subject_name=subject_name,
                    field_name=NOTES_FIELD_NAME,
                    value="；".join(missing),
                    session_id=session_id if self.session_based else None,
                    actor=actor,
                    source_kind="manual_edit",
                    evidence="通讯录统一编辑",
                    allow_custom_field=False,
                )
                if result.ok:
                    messages.append(result.message)
        return messages

    def delete_contact_records(
        self, user_id: str, actor: AuditActor
    ) -> tuple[int, list[str]]:
        removed = 0
        failures: list[str] = []
        for summary in self.record_summaries_for_user(user_id):
            key = str(summary.get("key") or "")
            if not key:
                continue
            result = self.store.delete_profile_by_key(key, actor)
            if result.ok:
                removed += 1
            else:
                failures.append(result.message)
        return removed, failures

    def prompt_block(
        self,
        session_id: str,
        user_id: str,
        sender_name: str = "",
    ) -> str:
        if not self.enabled or not user_id:
            return ""
        summary = self.store.format_profile_summary(
            user_id,
            str(session_id or "") if self.session_based else None,
        )
        empty = summary in {"", EMPTY_PROFILE_SUMMARY}
        if empty and not _cfg_bool(
            self.config.get("weaver_inject_when_empty"), False
        ):
            if not self.llm_tools_enabled:
                return ""
            template = LEAN_PROMPT_TEMPLATE
        else:
            template = str(self.config.get("weaver_prompt_template") or "").strip()
            if not template:
                template = (
                    DEFAULT_PROMPT_TEMPLATE
                    if not empty
                    else LEAN_PROMPT_TEMPLATE
                )
        return template.format(
            sender_id=user_id,
            sender_name=sender_name or user_id,
            session_scope="当前会话隔离" if self.session_based else "全局共享",
            profile_summary=summary,
            field_catalog=self.store.format_field_catalog(
                user_id,
                str(session_id or "") if self.session_based else None,
            ),
            custom_field_catalog=self.store.format_custom_field_catalog(
                user_id,
                str(session_id or "") if self.session_based else None,
            ),
            tool_names=", ".join(PROFILE_TOOL_NAMES),
        )

    def actor(
        self,
        event: Any,
        actor_type: str,
        actor_name: str = "",
        actor_id: str = "",
    ) -> AuditActor:
        try:
            sender_id = str(event.get_sender_id())
            sender_name = str(event.get_sender_name())
        except Exception:
            sender_id = str(actor_id or "")
            sender_name = str(actor_name or "")
        return AuditActor(
            actor_type=actor_type,
            actor_id=sender_id or actor_id or actor_type,
            actor_name=sender_name or actor_name or actor_type,
        )

    @staticmethod
    def event_session(event: Any) -> str:
        return str(getattr(event, "unified_msg_origin", "") or "")

    def handle_llm_view(self, event: Any) -> str:
        try:
            setattr(event, "_profileweaver_llm_tool_used", True)
        except Exception:
            pass
        if not self.enabled:
            return "通讯录画像当前已关闭。"
        user_id = str(event.get_sender_id())
        session_id = self.event_session(event)
        return (
            "当前用户画像摘要：\n"
            f"{self.store.format_profile_summary(user_id, session_id if self.session_based else None)}\n\n"
            "可用字段：\n"
            f"{self.store.format_field_catalog(user_id, session_id if self.session_based else None)}\n\n"
            "当前自定义字段：\n"
            f"{self.store.format_custom_field_catalog(user_id, session_id if self.session_based else None)}"
        )

    def _validate_evidence(
        self,
        event: Any,
        evidence: str,
        source_kind: str,
        allowed: set[str],
        *,
        field_name: str = "",
        value: str = "",
    ) -> str:
        if source_kind not in allowed:
            return "拒绝执行：source_kind 不在允许范围内。"
        message = str(getattr(event, "message_str", "") or "").strip()
        if not message:
            return "拒绝执行：当前消息为空，无法校验画像依据。"
        evidence = str(evidence or "").strip()
        if not evidence:
            return "拒绝执行：evidence 不能为空。"
        if not self._evidence_matches_message(evidence, message):
            return "拒绝执行：evidence 必须直接来自当前用户本轮消息。"
        if source_kind in REMEMBER_SOURCE_KINDS:
            remember_error = self._validate_remember_intent(
                field_name=field_name,
                value=value,
            )
            if remember_error:
                return remember_error
        if (
            self.strict_identity_guard
            and THIRD_PARTY_PATTERN.search(f"{message}\n{evidence}")
            and not self._mentions_self(f"{message}\n{evidence}")
        ):
            return (
                "拒绝执行：当前消息同时提到其他人且缺少明确自述，"
                "容易写错对象。请先澄清，再决定是否记录。"
            )
        if (
            (
                THIRD_PARTY_PATTERN.search(message)
                or self._mentions_multi_subject_context(message)
            )
            and not self._mentions_self(evidence)
        ):
            return "拒绝执行：当前消息提到他人时，依据还必须包含明确自述。"
        return ""

    @staticmethod
    def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
        lowered = str(text or "").casefold()
        return any(marker.casefold() in lowered for marker in markers)

    def _mentions_self(self, text: str) -> bool:
        return self._contains_any(text, FIRST_PERSON_MARKERS)

    def _mentions_strong_self(self, text: str) -> bool:
        return self._contains_any(text, STRONG_SELF_MARKERS)

    @staticmethod
    def _mentions_multi_subject_context(text: str) -> bool:
        raw = str(text or "")
        return any(pattern.search(raw) for pattern in AMBIGUOUS_MULTI_SUBJECT_PATTERNS)

    @staticmethod
    def _normalize_for_match(text: str) -> str:
        folded = unicodedata.normalize("NFKC", str(text or "")).casefold()
        folded = folded.translate(EVIDENCE_PUNCTUATION_TABLE)
        return re.sub(r"[\s\u3000]+", "", folded)

    def _evidence_matches_message(self, evidence: str, message: str) -> bool:
        mode = self.evidence_match_mode
        if mode == "strict":
            return evidence in message
        if evidence in message:
            return True
        normalized_evidence = self._normalize_for_match(evidence)
        normalized_message = self._normalize_for_match(message)
        if not normalized_evidence:
            return False
        if normalized_evidence in normalized_message:
            return True
        if mode != "loose":
            return False
        window = 4
        if len(normalized_evidence) <= window:
            return False
        grams = {
            normalized_evidence[index : index + window]
            for index in range(len(normalized_evidence) - window + 1)
        }
        hits = sum(1 for gram in grams if gram in normalized_message)
        return bool(grams) and hits / len(grams) >= 0.7

    def _llm_denied_field(self, field_name: str) -> bool:
        raw = self.config.get("weaver_llm_write_denylist") or ["健康状况"]
        if isinstance(raw, str):
            raw = [part for part in re.split(r"[,，\s]+", raw) if part]
        denied = {
            self.store.canonical_field_name(item)
            for item in raw
        }
        return self.store.canonical_field_name(field_name) in denied

    def _validate_remember_intent(self, *, field_name: str, value: str) -> str:
        if self._llm_denied_field(field_name):
            return (
                f"拒绝执行：字段「{field_name}」已禁止由 LLM 写入，"
                "只能由用户命令、通讯录或管理员手动维护。"
            )
        canonical = self.store.canonical_field_name(field_name)
        if canonical == NOTES_FIELD_NAME and any(
            token in BLOCKED_PROFILE_NAME_VALUES
            for token, _kind in self.store.extract_conflict_tokens(value)
        ):
            return (
                "拒绝执行：这个备注像冒犯、诱导或冒充系统权限的称呼，"
                "容易误导后续认知。"
            )
        return ""

    def handle_llm_remember(self, event: Any, **kwargs: Any) -> str:
        try:
            setattr(event, "_profileweaver_llm_tool_used", True)
        except Exception:
            pass
        if not self.llm_tools_enabled:
            return "画像工具当前已关闭。"
        field_name = _clean(kwargs.get("field_name"), 40)
        value = _clean(kwargs.get("value"), 500)
        evidence = _clean(kwargs.get("evidence"), 500)
        source_kind = _clean(kwargs.get("source_kind"), 40)
        error = self._validate_evidence(
            event,
            evidence,
            source_kind,
            REMEMBER_SOURCE_KINDS,
            field_name=field_name,
            value=value,
        )
        if error:
            return error
        user_id = str(event.get_sender_id())
        session_id = self.event_session(event) if self.session_based else None
        known = self.store.is_known_field(user_id, field_name, session_id)
        requested_custom = bool(kwargs.get("create_custom_field", False))
        allow_custom = self.allow_llm_custom_fields and (
            known or requested_custom or self.allow_llm_custom_fields
        )
        result = self.store.upsert_field(
            user_id=user_id,
            subject_name=str(event.get_sender_name()),
            field_name=field_name,
            value=value,
            session_id=session_id,
            actor=self.actor(event, "llm_tool"),
            source_kind=source_kind,
            evidence=evidence,
            allow_custom_field=allow_custom,
            field_description=_clean(kwargs.get("field_description"), 80)
            or "由 LLM 创建的当前用户自定义字段",
        )
        return result.message

    def handle_llm_forget(self, event: Any, **kwargs: Any) -> str:
        try:
            setattr(event, "_profileweaver_llm_tool_used", True)
        except Exception:
            pass
        if not self.llm_tools_enabled:
            return "画像工具当前已关闭。"
        evidence = _clean(kwargs.get("evidence"), 500)
        source_kind = _clean(kwargs.get("source_kind"), 40)
        error = self._validate_evidence(
            event,
            evidence,
            source_kind,
            FORGET_SOURCE_KINDS,
        )
        if error:
            return error
        user_id = str(event.get_sender_id())
        result = self.store.delete_field(
            user_id=user_id,
            session_id=self.event_session(event) if self.session_based else None,
            field_selector=_clean(kwargs.get("field_selector"), 80),
            actor=self.actor(event, "llm_tool"),
            source_kind=source_kind,
            evidence=evidence,
        )
        return result.message

    def _parse_direct_auto_profile_update(
        self, message_text: str
    ) -> tuple[str, str, str] | None:
        stripped = str(message_text or "").strip()
        if (
            not stripped
            or HARD_THIRD_PARTY_PATTERN.search(stripped)
            or self._mentions_multi_subject_context(stripped)
        ):
            return None
        for pattern, field_template, source_kind in DIRECT_AUTO_PROFILE_PATTERNS:
            match = pattern.fullmatch(stripped)
            if not match:
                continue
            raw_field = str(match.group("field") or "").strip(
                DIRECT_AUTO_VALUE_STRIP_CHARS
            )
            value = str(match.group("value") or "").strip(
                DIRECT_AUTO_VALUE_STRIP_CHARS
            )
            field_name = field_template.format(field=raw_field).strip(
                DIRECT_AUTO_VALUE_STRIP_CHARS
            )
            field_name = re.sub(r"\s+", " ", field_name).strip()
            value = re.sub(r"\s+", " ", value).strip()
            if not field_name or not value:
                return None
            if DIRECT_AUTO_FIELD_DENY_PATTERN.search(field_name):
                return None
            if THIRD_PARTY_PATTERN.search(value):
                return None
            return field_name, value, source_kind
        return None

    def maybe_extract_local(self, event: Any) -> bool:
        """明确自述用零 token 本地规则落库。返回 True 表示已经处理。"""

        if not self.proactive_extraction_enabled:
            return False
        message = _clean(getattr(event, "message_str", ""), 500)
        parsed = self._parse_direct_auto_profile_update(message)
        if not parsed:
            return False
        field_name, value, source_kind = parsed
        intent_error = self._validate_remember_intent(
            field_name=field_name,
            value=value,
        )
        if intent_error:
            self.debug(f"本地画像抽取被拒绝：{intent_error}")
            return True
        user_id = str(event.get_sender_id())
        session_id = self.event_session(event) if self.session_based else None
        known = self.store.is_known_field(user_id, field_name, session_id)
        if not known and not self.allow_llm_custom_fields:
            return False
        result = self.store.upsert_field(
            user_id=user_id,
            subject_name=str(event.get_sender_name()),
            field_name=field_name,
            value=value,
            session_id=session_id,
            actor=self.actor(event, "auto_extract"),
            source_kind=source_kind,
            evidence=message,
            allow_custom_field=self.allow_llm_custom_fields,
            field_description="从当前用户明确自述中自动创建的自定义字段",
        )
        self.debug(f"本地画像抽取：{result.message}")
        return True

    def _is_group_chat(self, event: Any) -> bool:
        is_private = getattr(event, "is_private_chat", None)
        if callable(is_private):
            try:
                return not bool(is_private())
            except Exception:
                pass
        return "groupmessage" in self.event_session(event).casefold()

    def group_privacy_denied(self, event: Any) -> str:
        if not self._is_group_chat(event) or self.allow_profile_in_group:
            return ""
        return self.group_profile_denied_msg

    def is_profile_candidate_message(self, event: Any, message_text: str) -> bool:
        stripped = str(message_text or "").strip()
        if not stripped:
            return False
        if stripped[:1] in {"/", "!", "."}:
            return False
        minimum = self._safe_int(
            self.config.get("weaver_proactive_extraction_min_message_length"),
            4,
            4,
            120,
        )
        maximum = self._safe_int(
            self.config.get("weaver_proactive_extraction_max_message_length"),
            120,
            minimum,
            500,
        )
        if len(stripped) < minimum or len(stripped) > maximum:
            return False
        if stripped.count("\n") >= 3:
            return False
        if any(token in stripped for token in ("http://", "https://", "```", "[CQ:")):
            return False
        if HARD_THIRD_PARTY_PATTERN.search(stripped):
            return False
        if self._mentions_multi_subject_context(stripped):
            return False
        if not self._contains_any(stripped, PROFILE_SIGNAL_MARKERS):
            return False
        if self._mentions_self(stripped):
            if (
                THIRD_PARTY_PATTERN.search(stripped)
                and not self._mentions_strong_self(stripped)
            ):
                return False
            return True
        if self._is_group_chat(event) or THIRD_PARTY_PATTERN.search(stripped):
            return False
        return self._contains_any(stripped, PRIVATE_PROFILE_PREFIXES)

    async def _resolve_chat_provider_id(self, event: Any) -> str:
        umo = self.event_session(event)
        context = getattr(self.plugin, "context", None)
        if context is None:
            return ""
        if umo:
            try:
                return str(await context.get_current_chat_provider_id(umo))
            except Exception as exc:
                self.debug(f"读取当前 Provider 失败：{exc}")
        provider = context.get_using_provider(umo=umo)
        if provider is None:
            return ""
        try:
            return str(provider.meta().id or "")
        except Exception:
            return ""

    def _build_auto_extract_system_prompt(self, event: Any) -> str:
        user_id = str(event.get_sender_id())
        session_id = self.event_session(event) if self.session_based else None
        return AUTO_EXTRACT_SYSTEM_PROMPT.format(
            sender_id=user_id,
            sender_name=str(event.get_sender_name()),
            profile_summary=self.store.format_profile_summary(user_id, session_id),
            field_catalog=self.store.format_field_catalog(user_id, session_id),
            custom_field_catalog=self.store.format_custom_field_catalog(
                user_id, session_id
            ),
        )

    async def maybe_extract(self, event: Any, response: Any = None) -> bool:
        """低开销自动抽取：明确的简单自述走本地，其余才交给一次短工具循环。"""

        if (
            not self.proactive_extraction_enabled
            or bool(getattr(event, "_profileweaver_auto_extract_running", False))
            or bool(getattr(event, "_profileweaver_llm_tool_used", False))
        ):
            return False
        names = getattr(response, "tools_call_name", []) if response is not None else []
        if isinstance(names, str):
            names = [names]
        if set(names or []) & set(PROFILE_TOOL_NAMES):
            return False
        message = str(getattr(event, "message_str", "") or "").strip()
        if not self.is_profile_candidate_message(event, message):
            return False
        if self.maybe_extract_local(event):
            return True
        if self.auto_extract_tools is None:
            return False
        provider_id = await self._resolve_chat_provider_id(event)
        if not provider_id:
            return False
        context = getattr(self.plugin, "context", None)
        tool_loop = getattr(context, "tool_loop_agent", None)
        if not callable(tool_loop):
            return False
        try:
            setattr(event, "_profileweaver_auto_extract_running", True)
            prompt = (
                "只根据当前用户这条消息，决定是否需要调用画像工具。\n"
                f"当前消息：{message}\n"
                "没有明确、稳定、适合长期记忆的信息时，直接回复 NOOP。"
            )
            await tool_loop(
                event=event,
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=self._build_auto_extract_system_prompt(event),
                tools=self.auto_extract_tools,
                max_steps=self._safe_int(
                    self.config.get("weaver_proactive_extraction_max_steps"),
                    2,
                    1,
                    3,
                ),
                stream=False,
            )
            return True
        except Exception as exc:
            self.debug(f"LLM 自动画像抽取失败：{exc}")
            return False
        finally:
            try:
                setattr(event, "_profileweaver_auto_extract_running", False)
            except Exception:
                pass

    def meta(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "session_based": self.session_based,
            "webui_allow_edit": self.webui_allow_edit,
            "llm_tools_enabled": self.llm_tools_enabled,
            "proactive_extraction_enabled": self.proactive_extraction_enabled,
            "strict_identity_guard": self.strict_identity_guard,
            "evidence_match_mode": self.evidence_match_mode,
            "allow_profile_in_group": self.allow_profile_in_group,
            "field_lock_scope": self.store.field_lock_scope,
            "field_lock_scope_label": self.store.field_lock_scope_label(),
            "field_lock_scopes": [
                {"id": key, "label": value}
                for key, value in FIELD_LOCK_SCOPE_LABELS.items()
            ],
            "builtin_fields": [
                {"name": name, "description": description}
                for name, description in self.store.builtin_field_map.items()
            ],
            "allow_llm_custom_fields": self.allow_llm_custom_fields,
            "allow_user_custom_fields": self.allow_user_custom_fields,
            "max_notes_count": self.store.max_notes_count,
            "field_value_max_length": self.store.field_value_max_length,
            "audit_actions": self.store.audit_action_catalog(),
            "actor_types": self.store.actor_type_catalog(),
            "data_dir": str(self.data_dir),
            "legacy_source_dir": self.legacy_source_dir,
            "legacy_imported": bool(self.legacy_source_dir),
        }

    def stats(self) -> dict[str, Any]:
        return self.store.collect_stats()

    @staticmethod
    def command_catalog() -> dict[str, list[dict[str, str]]]:
        return {
            "user": [
                {"command": "show", "desc": "查看自己的画像摘要与完整度"},
                {"command": "fields", "desc": "列出基础字段和当前自定义字段"},
                {"command": "set <字段> <值>", "desc": "写入或更新自己的字段"},
                {"command": "delete <字段|备注:序号>", "desc": "删除自己的字段或备注"},
                {"command": "lock|unlock <字段>", "desc": "锁定或解锁字段"},
                {"command": "history <字段> [条数]", "desc": "查看字段变更历史"},
                {"command": "clear", "desc": "清空自己的全部画像"},
                {"command": "help", "desc": "查看通讯录画像指令"},
            ],
            "admin": [
                {"command": "user <用户>", "desc": "查看指定用户画像"},
                {"command": "audit [用户] [条数]", "desc": "查看画像审计"},
                {"command": "stats", "desc": "查看画像统计与字段覆盖率"},
                {"command": "backup [标签]", "desc": "立即创建画像备份"},
                {"command": "backups", "desc": "列出画像备份"},
                {"command": "restore <文件名>", "desc": "恢复画像备份"},
                {"command": "merge <源key> <目标key>", "desc": "合并两条重复画像"},
            ],
        }

    @staticmethod
    def resolve_target_user_id(event: Any, raw_target: str) -> str:
        get_messages = getattr(event, "get_messages", None)
        message_chain = get_messages() if callable(get_messages) else []
        if isinstance(message_chain, (list, tuple)):
            for segment in message_chain:
                if type(segment).__name__.casefold() != "at":
                    continue
                target = str(
                    getattr(segment, "qq", "")
                    or getattr(segment, "user_id", "")
                    or ""
                ).strip()
                if target and target.casefold() != "all":
                    return target
        target = str(raw_target or "").strip()
        match = re.search(r"\[CQ:at,qq=(\d+)", target)
        if match:
            return match.group(1)
        match = re.search(r"@[^()\r\n]*\((\d+)\)\s*$", target)
        if match:
            return match.group(1)
        return target

    def profile_status_text(
        self,
        user_id: str,
        session_id: str,
        *,
        subject_label: str = "",
    ) -> str:
        scoped_session = session_id if self.session_based else None
        record = self.store.get_profile(user_id, scoped_session)
        if not record:
            return f"{subject_label or '用户'}还没有画像记录。"
        summary = self.store.format_profile_summary(user_id, scoped_session)
        overview = self.store.summarize_profile(
            self.store.profile_key(user_id, scoped_session),
            record,
        )
        percent = float(overview.get("completeness") or 0.0)
        filled = int(percent / 4)
        bar = "█" * filled + "░" * (25 - filled)
        lines = [
            f"{subject_label or '当前用户'}的画像：",
            summary,
            "",
            f"完整度：{percent:.1f}%  {bar}",
            (
                f"已填字段 {overview.get('field_count', 0)} 个"
                f" · 自定义字段 {overview.get('custom_field_count', 0)} 个"
                f" · 备注 {overview.get('note_count', 0)} 条"
            ),
            f"最后更新：{self.store.get_last_updated(user_id, scoped_session) or '未知'}",
        ]
        if overview.get("locked_fields"):
            lines.append(
                "已锁定："
                + "、".join(str(item) for item in overview.get("locked_fields") or [])
            )
        return "\n".join(lines)

    def field_catalog_text(self, user_id: str, session_id: str) -> str:
        scoped_session = session_id if self.session_based else None
        return (
            "基础字段：\n"
            + self.store.format_field_catalog(user_id, scoped_session)
            + "\n\n当前自定义字段：\n"
            + self.store.format_custom_field_catalog(user_id, scoped_session)
        )

    def set_field_lock(
        self,
        *,
        user_id: str,
        session_id: str,
        field_name: str,
        locked: bool,
        actor: AuditActor,
        subject_name: str = "",
    ) -> str:
        field_name = str(field_name or "").strip()
        verb = "锁定" if locked else "解锁"
        if not field_name:
            return f"用法：/mw profile {verb} <字段>"
        scoped_session = session_id if self.session_based else None
        record = self.store.get_profile(user_id, scoped_session)
        result = self.store.set_field_lock(
            user_id=user_id,
            subject_name=str(
                (record or {}).get("subject_name")
                or subject_name
                or user_id
            ),
            session_id=scoped_session,
            field_name=field_name,
            locked=locked,
            actor=actor,
        )
        message = ("✅ " if result.ok else "❌ ") + result.message
        if result.ok and locked:
            message += f"\n当前锁范围：{self.store.field_lock_scope_label()}"
        return message

    def field_history_text(
        self,
        *,
        user_id: str,
        session_id: str,
        field_name: str,
        limit: int = 10,
        subject_label: str = "",
    ) -> str:
        canonical = self.store.canonical_field_name(field_name)
        if not canonical:
            return f"认不出字段「{field_name}」，可以先执行 /mw profile fields。"
        entries = self.store.read_field_history(
            user_id=user_id,
            session_id=session_id if self.session_based else None,
            field_name=canonical,
            limit=max(1, min(int(limit or 10), 30)),
            include_rotated=True,
        )
        if not entries:
            return f"{subject_label}字段「{canonical}」还没有变更记录。"
        lines = [f"{subject_label}字段「{canonical}」最近 {len(entries)} 条变更："]
        for entry in entries:
            lines.append(
                f"- [{entry.get('at')}] {entry.get('action') or '变更'} · "
                f"{entry.get('actor_type') or '未知'}"
                f"（{entry.get('actor_name') or entry.get('actor_id') or '-'}）"
            )
            lines.append(
                f"  {entry.get('old_value') or '（空）'} → "
                f"{entry.get('new_value') or '（空）'}"
            )
            if entry.get("evidence"):
                lines.append(f"  依据：{entry.get('evidence')}")
        return "\n".join(lines)

    def stats_text(self) -> str:
        stats = self.store.collect_stats()
        audit_count = int(
            self.store.read_audit(limit=1, include_rotated=True).get("total") or 0
        )
        profile_count = int(
            stats.get("profile_count") or stats.get("user_count") or 0
        )
        coverage = sorted(
            self.store.builtin_field_map,
            key=lambda name: (
                -int((stats.get("field_counts") or {}).get(name, 0)),
                name,
            ),
        )
        lines = [
            (
                f"画像 {profile_count} 条 · 用户 {int(stats.get('user_count') or 0)}"
                f" · 会话 {int(stats.get('session_count') or 0)}"
            ),
            (
                f"平均完整度 {float(stats.get('avg_completeness') or 0):.1f}%"
                f" · 字段锁 {int(stats.get('locked_field_count') or 0)} 个"
            ),
            (
                f"审计 {audit_count} 条"
                f" · 备份 {int(stats.get('backup_count') or 0)} 份"
            ),
            "字段覆盖率：",
        ]
        field_counts = stats.get("field_counts") or {}
        for field_name in coverage[:10]:
            count = int(field_counts.get(field_name, 0))
            ratio = count / max(1, profile_count) * 100
            lines.append(f"- {field_name}：{count}（{ratio:.1f}%）")
        custom = stats.get("custom_field_counts") or {}
        if custom:
            lines.append("自定义字段：")
            for name, count in sorted(
                custom.items(), key=lambda item: (-int(item[1]), item[0])
            )[:10]:
                lines.append(f"- {name}：{int(count)}")
        return "\n".join(lines)

    def audit(self, **kwargs: Any) -> dict[str, Any]:
        return self.store.read_audit(**kwargs)

    def field_history(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self.store.read_field_history(**kwargs)

    def export_bundle(self, **kwargs: Any) -> dict[str, Any]:
        return self.store.export_bundle(**kwargs)

    def import_bundle(self, payload: Any, mode: str = "merge") -> dict[str, Any]:
        return self.store.import_bundle(
            payload,
            mode=mode,
            actor=AuditActor("webui", "dashboard", "Dashboard"),
        )

    def backups(self) -> list[dict[str, Any]]:
        return self.store.list_backups()
