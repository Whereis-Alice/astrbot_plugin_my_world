"""Profile Weaver storage core, adapted from Whereis-Alice/astrbot_plugin_profile_weaver."""

from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from astrbot.api import logger

NOTES_FIELD_NAME = "备注"
EMPTY_PROFILE_SUMMARY = "暂无记录"
LEGACY_PLUGIN_DIR_NAMES = ("astrbot_plugin_soulmap", "SoulMap", "soulmap")
CANONICAL_FIELD_ALIASES = {
    "称呼": "昵称",
    "对用户的称呼": "昵称",
    "名字": "昵称",
    "姓名": "昵称",
}
PROFILE_NAME_FIELD_NAMES = ("昵称", "对用户的称呼", "称呼", "名字", "姓名", "别名", "网名", "用户名")
PROFILE_NAME_FIELD_NAME_SET = {re.sub(r"\s+", " ", name.strip()) for name in PROFILE_NAME_FIELD_NAMES}
PROFILE_CONFLICT_SPLIT_PATTERN = re.compile(r"[、,，/|;；\n]+")
PROFILE_CONFLICT_STRIP_CHARS = " \t\r\n\"'“”‘’`[]()（）【】<>《》:："

DEFAULT_FIELDS: list[tuple[str, str]] = [
    ("昵称", "用户希望被如何称呼"),
    ("性别", "用户明确表达的性别或性别认同"),
    ("年龄", "相对稳定的年龄信息"),
    ("所在地", "常驻城市、地区或国家"),
    ("生日", "生日或重要纪念日期"),
    ("爱吃", "用户明确表达喜欢吃的东西"),
    ("忌口", "过敏、忌口或不吃的东西"),
    ("爱好", "兴趣爱好"),
    ("职业", "职业、专业或长期身份"),
    ("重要节日", "对用户重要的节日或纪念日"),
    ("恐惧/弱点", "用户主动表达的害怕或不擅长的内容"),
    ("作息规律", "比较稳定的作息偏好"),
    ("技能水平", "用户主动表达的技能或熟练度"),
    ("健康状况", "用户主动披露且适合长期记忆的健康信息"),
    ("宠物", "宠物相关信息"),
    ("MBTI", "用户自述的 MBTI 人格类型，例如 INTP"),
    ("星座", "用户自述的星座"),
    ("时区", "用户所在时区或习惯作息时区，例如 UTC+8"),
    ("常用语言", "日常沟通使用的语言，例如中文、英文"),
    ("沟通偏好", "希望被如何对待的沟通风格，例如简洁、多解释、少表情"),
    ("禁忌话题", "用户明确要求不要提及的话题"),
    ("代词", "希望被使用的人称代词，例如 他 / 她 / TA"),
    ("喜欢的话题", "聊起来最有兴趣、最容易聊开的话题方向"),
    ("口头禅", "标志性的口头禅、常用语气词或签名式表达"),
    ("当前目标", "用户正在推进的中长期目标、计划或备考安排"),
    (NOTES_FIELD_NAME, "短期补充备注，会自动保留最近几条"),
]

DEFAULT_FIELD_DESCRIPTIONS = {name: desc for name, desc in DEFAULT_FIELDS}
CUSTOM_FIELD_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_\-/\u4e00-\u9fff ]+$")
NOTE_INDEX_PATTERN = re.compile(rf"^(?:{NOTES_FIELD_NAME}\s*[:#])?\s*(\d+)$")
FORBIDDEN_FIELD_FRAGMENTS = (
    "system",
    "prompt",
    "tool",
    "session",
    "message",
    "assistant",
    "bot",
    "系统",
    "提示词",
    "工具",
    "会话",
    "消息",
    "群友",
    "聊天记录",
    "管理员口令",
)
BLOCKED_PROFILE_NAME_VALUES = (
    "爸爸",
    "爹",
    "爹地",
    "义父",
    "主人",
    "master",
    "owner",
    "系统",
    "system",
    "assistant",
    "管理员",
    "群主",
)

AUDIT_ROTATED_NAME_PATTERN = re.compile(r"^audit_log\.jsonl\.\d{14}$")
BACKUP_NAME_PATTERN = re.compile(r"^profiles-[A-Za-z0-9_\-]+\.json$")

DEFAULT_FIELD_LOCK_SCOPE = "llm_only"
FIELD_LOCK_SCOPE_ACTORS: dict[str, frozenset[str]] = {
    "llm_only": frozenset({"llm_tool", "auto_extract"}),
    "llm_and_user": frozenset({"llm_tool", "auto_extract", "user_command"}),
    "strict": frozenset(),
}
FIELD_LOCK_SCOPE_LABELS = {
    "llm_only": "仅拦截 LLM 工具与自动抽取",
    "llm_and_user": "拦截 LLM、自动抽取与普通用户命令",
    "strict": "拦截所有来源，必须先解锁",
}


@dataclass(slots=True)
class AuditActor:
    actor_type: str
    actor_id: str
    actor_name: str


@dataclass(slots=True)
class OperationResult:
    ok: bool
    message: str
    changed: bool = False
    created_custom_field: bool = False


class ProfileStore:
    def __init__(
        self,
        data_dir: Path,
        builtin_fields: list[str],
        max_notes_count: int = 5,
        custom_field_name_max_length: int = 16,
        field_value_max_length: int = 160,
        audit_log_max_mb: float = 8.0,
        backup_retention_days: int = 14,
        field_lock_scope: str = DEFAULT_FIELD_LOCK_SCOPE,
        audit_log_keep_rotated: int = 5,
        backup_max_count: int = 60,
    ) -> None:
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.profiles_path = self.data_dir / "profiles.json"
        self.audit_log_path = self.data_dir / "audit_log.jsonl"
        self.legacy_profiles_path = self.data_dir / "user_profiles.json"
        self.migration_state_path = self.data_dir / "migration_state.json"
        self.backup_dir = self.data_dir / "backups"
        self.max_notes_count = max(1, min(int(max_notes_count), 20))
        self.custom_field_name_max_length = max(4, min(int(custom_field_name_max_length), 32))
        self.field_value_max_length = max(32, min(int(field_value_max_length), 500))
        self.audit_log_max_bytes = int(max(0.5, min(float(audit_log_max_mb), 128.0)) * 1024 * 1024)
        # 0 表示不按天数清理（与配置说明保持一致）。
        self.backup_retention_days = max(0, min(int(backup_retention_days), 180))
        self.backup_max_count = max(0, min(int(backup_max_count), 500))
        self.audit_log_keep_rotated = max(0, min(int(audit_log_keep_rotated), 200))
        self.field_lock_scope = (
            field_lock_scope if field_lock_scope in FIELD_LOCK_SCOPE_ACTORS else DEFAULT_FIELD_LOCK_SCOPE
        )
        self.builtin_field_map = self._build_builtin_field_map(builtin_fields)
        self.migration_state = self._load_migration_state()
        self.profiles = self._load_profiles()
        self._last_snapshot_day = ""

    @staticmethod
    def now_text() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def normalize_field_name(field_name: str) -> str:
        return re.sub(r"\s+", " ", str(field_name or "").strip())

    @classmethod
    def canonical_field_name(cls, field_name: str) -> str:
        normalized = cls.normalize_field_name(field_name)
        return CANONICAL_FIELD_ALIASES.get(normalized, normalized)

    @staticmethod
    def _normalize_conflict_token(value: Any) -> str:
        text = str(value or "").strip(PROFILE_CONFLICT_STRIP_CHARS)
        text = re.sub(r"\s+", "", text)
        return text.casefold()

    def _extract_conflict_tokens(self, raw_value: Any) -> list[tuple[str, str]]:
        if isinstance(raw_value, list):
            source_items = raw_value
        else:
            source_items = PROFILE_CONFLICT_SPLIT_PATTERN.split(str(raw_value or ""))

        tokens: list[tuple[str, str]] = []
        seen: set[str] = set()
        for item in source_items:
            display_text = str(item or "").strip(PROFILE_CONFLICT_STRIP_CHARS)
            token = self._normalize_conflict_token(display_text)
            if len(token) < 2 or token in seen:
                continue
            seen.add(token)
            tokens.append((token, display_text))
        return tokens

    def _is_profile_name_field(self, field_name: str) -> bool:
        normalized = self.normalize_field_name(field_name)
        return normalized in PROFILE_NAME_FIELD_NAME_SET

    def _profile_label(self, session_key: str, record: dict[str, Any]) -> str:
        subject_name = str(record.get("subject_name") or "").strip()
        subject_user_id = str(record.get("subject_user_id") or self._extract_user_id_from_key(session_key)).strip()
        if subject_name:
            return f"{subject_name}({subject_user_id})"
        return subject_user_id

    def _profile_in_session_scope(
        self,
        session_key: str,
        record: dict[str, Any],
        session_id: str | None,
    ) -> bool:
        subject_user_id = str(record.get("subject_user_id") or self._extract_user_id_from_key(session_key)).strip()
        return session_key == self._profile_key(subject_user_id, session_id)

    def _iter_profile_name_tokens(self, session_id: str | None) -> list[tuple[str, str, str, str, str]]:
        matches: list[tuple[str, str, str, str, str]] = []
        for session_key, record in self.profiles.items():
            if not isinstance(record, dict):
                continue
            if not self._profile_in_session_scope(session_key, record, session_id):
                continue
            fields = record.get("fields", {})
            if not isinstance(fields, dict):
                continue
            for field_name, value in fields.items():
                if not self._is_profile_name_field(field_name):
                    continue
                for token, display_text in self._extract_conflict_tokens(value):
                    matches.append((token, display_text, session_key, field_name, self._profile_label(session_key, record)))
        return matches

    def _iter_profile_note_tokens(self, session_id: str | None) -> list[tuple[str, str, str, str]]:
        matches: list[tuple[str, str, str, str]] = []
        for session_key, record in self.profiles.items():
            if not isinstance(record, dict):
                continue
            if not self._profile_in_session_scope(session_key, record, session_id):
                continue
            fields = record.get("fields", {})
            if not isinstance(fields, dict):
                continue
            for token, display_text in self._extract_conflict_tokens(fields.get(NOTES_FIELD_NAME, [])):
                matches.append((token, display_text, session_key, self._profile_label(session_key, record)))
        return matches

    def _validate_profile_identity_conflicts(
        self,
        *,
        session_key: str,
        session_id: str | None,
        record: dict[str, Any],
        field_name: str,
        value: str,
    ) -> OperationResult:
        candidate_tokens = self._extract_conflict_tokens(value)
        if not candidate_tokens:
            return OperationResult(True, "画像身份冲突检查通过")

        normalized_field = self.normalize_field_name(field_name)
        is_name_field = self._is_profile_name_field(normalized_field)

        if is_name_field:
            # 一次性快照，避免在候选 token 循环里反复全量扫描（O(N*T) -> O(N+T)）。
            existing_name_tokens = self._iter_profile_name_tokens(session_id)
            existing_note_tokens = self._iter_profile_note_tokens(session_id)
            for token, display_text in candidate_tokens:
                if token in BLOCKED_PROFILE_NAME_VALUES:
                    return OperationResult(
                        False,
                        f"拒绝写入：称呼/名字「{display_text}」属于容易冒犯、诱导或混淆系统身份的称呼。请向用户说明没有写入，并请用户换一个更合适的称呼。",
                    )
                for existing_token, existing_text, existing_key, existing_field, owner_label in existing_name_tokens:
                    if token != existing_token:
                        continue
                    if existing_key == session_key and self.normalize_field_name(existing_field) == normalized_field:
                        continue
                    return OperationResult(
                        False,
                        f"拒绝写入：称呼/名字「{display_text}」与 {owner_label} 的「{existing_field}：{existing_text}」重复，可能导致画像混淆。请向用户说明没有写入，并请用户换一个更明确的称呼。",
                    )
                for note_token, note_text, _, owner_label in existing_note_tokens:
                    if token != note_token:
                        continue
                    return OperationResult(
                        False,
                        f"拒绝写入：称呼/名字「{display_text}」与 {owner_label} 的备注「{note_text}」重复，名字和备注不能互相复用。请向用户说明没有写入。",
                    )
            return OperationResult(True, "画像身份冲突检查通过")

        if normalized_field == NOTES_FIELD_NAME:
            name_tokens = self._iter_profile_name_tokens(session_id)
            for token, display_text in candidate_tokens:
                for name_token, name_text, _, name_field, owner_label in name_tokens:
                    if token != name_token:
                        continue
                    return OperationResult(
                        False,
                        f"拒绝写入：备注「{display_text}」与 {owner_label} 的「{name_field}：{name_text}」重复，名字和备注不能互相复用。请向用户说明没有写入。",
                    )

        return OperationResult(True, "画像身份冲突检查通过")

    def _build_builtin_field_map(self, builtin_fields: list[str]) -> dict[str, str]:
        result: dict[str, str] = {}
        for raw_name in builtin_fields:
            field_name = self.canonical_field_name(raw_name)
            if not field_name:
                continue
            if field_name == NOTES_FIELD_NAME:
                continue
            result[field_name] = DEFAULT_FIELD_DESCRIPTIONS.get(field_name, "基础画像字段")
        result[NOTES_FIELD_NAME] = DEFAULT_FIELD_DESCRIPTIONS[NOTES_FIELD_NAME]
        return result

    def _read_json(self, path: Path) -> dict[str, Any]:
        """Read a JSON object, quarantining (never silently dropping) broken files."""
        if not path.exists():
            return {}
        try:
            raw_text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.error("[ProfileWeaver] 读取 %s 失败：%s", path, exc)
            return {}
        if not raw_text.strip():
            return {}
        try:
            payload = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            quarantine_path = path.with_name(
                f"{path.name}.corrupt-{datetime.now().strftime('%Y%m%d%H%M%S')}"
            )
            try:
                path.replace(quarantine_path)
                logger.error(
                    "[ProfileWeaver] %s 解析失败（%s），已备份为 %s，本次以空数据继续。",
                    path,
                    exc,
                    quarantine_path,
                )
            except OSError as backup_exc:
                logger.error(
                    "[ProfileWeaver] %s 解析失败且备份失败：%s / %s", path, exc, backup_exc
                )
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_json(self, path: Path, payload: Any) -> None:
        """Atomic write so a crash mid-save can never truncate the profile file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f"{path.name}.tmp")
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        try:
            with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            tmp_path.replace(path)
        except OSError as exc:
            logger.error("[ProfileWeaver] 写入 %s 失败：%s", path, exc)
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _load_migration_state(self) -> dict[str, Any]:
        raw_state = self._read_json(self.migration_state_path)
        sources = raw_state.get("sources")
        if not isinstance(sources, dict):
            sources = {}
        return {"sources": sources}

    def _save_migration_state(self) -> None:
        self._write_json(self.migration_state_path, self.migration_state)

    def _normalize_profiles_payload(self, raw_profiles: dict[str, Any]) -> dict[str, Any]:
        normalized: dict[str, Any] = {}
        for session_key, payload in raw_profiles.items():
            if not isinstance(payload, dict):
                continue
            user_id = str(payload.get("subject_user_id") or self._extract_user_id_from_key(session_key))
            subject_name = str(payload.get("subject_name") or "")
            normalized[session_key] = self._normalize_profile_record(payload, user_id, subject_name)
        return normalized

    def _load_profiles(self) -> dict[str, Any]:
        normalized = self._normalize_profiles_payload(self._read_json(self.profiles_path))
        changed = False

        for legacy_path in self._iter_legacy_profile_sources():
            normalized, imported = self._maybe_import_legacy_source(normalized, legacy_path)
            changed = changed or imported

        if changed or (normalized and not self.profiles_path.exists()):
            self._write_json(self.profiles_path, normalized)
        return normalized

    def _migrate_legacy_profiles(self, legacy_profiles: dict[str, Any]) -> dict[str, Any]:
        migrated: dict[str, Any] = {}
        for session_key, payload in legacy_profiles.items():
            if not isinstance(payload, dict):
                continue
            user_id = self._extract_user_id_from_key(session_key)
            migrated[session_key] = self._normalize_profile_record(payload, user_id, "")
        return migrated

    def _iter_legacy_profile_sources(self) -> list[Path]:
        candidates: list[Path] = [self.legacy_profiles_path]
        plugin_data_root = self.data_dir.parent
        if plugin_data_root.exists():
            for dirname in LEGACY_PLUGIN_DIR_NAMES:
                candidates.append(plugin_data_root / dirname / "user_profiles.json")
            try:
                for sibling in plugin_data_root.iterdir():
                    if not sibling.is_dir():
                        continue
                    if sibling.resolve() == self.data_dir.resolve():
                        continue
                    if "soulmap" in sibling.name.casefold():
                        candidates.append(sibling / "user_profiles.json")
            except OSError:
                pass

        unique_candidates: list[Path] = []
        seen: set[str] = set()
        for path in candidates:
            signature_key = str(path.resolve(strict=False)).casefold()
            if signature_key in seen:
                continue
            seen.add(signature_key)
            unique_candidates.append(path)
        return unique_candidates

    def _legacy_source_key(self, path: Path) -> str:
        return str(path.resolve(strict=False))

    def _legacy_source_signature(self, path: Path) -> str | None:
        if not path.exists():
            return None
        try:
            stat = path.stat()
        except OSError:
            return None
        resolved = path.resolve(strict=False)
        return f"{resolved}|{stat.st_mtime_ns}|{stat.st_size}"

    def _maybe_import_legacy_source(
        self,
        current_profiles: dict[str, Any],
        legacy_path: Path,
    ) -> tuple[dict[str, Any], bool]:
        source_signature = self._legacy_source_signature(legacy_path)
        if source_signature is None:
            return current_profiles, False

        source_key = self._legacy_source_key(legacy_path)
        previous_state = self.migration_state["sources"].get(source_key, {})
        if previous_state.get("signature") == source_signature:
            return current_profiles, False

        migrated_profiles = self._migrate_legacy_profiles(self._read_json(legacy_path))
        changed = False
        merged_profiles = current_profiles
        status = "empty"
        if migrated_profiles:
            merged_profiles, changed = self._merge_profiles(current_profiles, migrated_profiles)
            status = "merged" if changed else "already_applied"
            if changed:
                logger.info(
                    "[ProfileWeaver] Imported %s legacy profile records from %s",
                    len(migrated_profiles),
                    legacy_path,
                )
        else:
            logger.warning("[ProfileWeaver] Legacy profile source %s is empty or unreadable", legacy_path)

        self.migration_state["sources"][source_key] = {
            "signature": source_signature,
            "source_path": str(legacy_path.resolve(strict=False)),
            "record_count": len(migrated_profiles),
            "status": status,
            "imported_at": self.now_text(),
        }
        self._save_migration_state()
        return merged_profiles, changed

    @staticmethod
    def _coerce_timestamp(raw_value: Any) -> str:
        text = str(raw_value or "").strip()
        return text

    def _pick_latest_timestamp(self, *values: Any) -> str:
        timestamps = []
        for value in values:
            text = self._coerce_timestamp(value)
            if text:
                timestamps.append(text)
        return max(timestamps) if timestamps else self.now_text()

    def _pick_earliest_timestamp(self, *values: Any) -> str:
        timestamps = []
        for value in values:
            text = self._coerce_timestamp(value)
            if text:
                timestamps.append(text)
        return min(timestamps) if timestamps else self.now_text()

    def _normalize_field_meta_entry(
        self,
        raw_metadata: Any,
        fallback_updated_at: str,
    ) -> dict[str, Any]:
        if not isinstance(raw_metadata, dict):
            raw_metadata = {}
        return {
            "updated_at": str(raw_metadata.get("updated_at") or fallback_updated_at),
            "updated_by": str(raw_metadata.get("updated_by") or "unknown"),
            "actor_id": str(raw_metadata.get("actor_id") or ""),
            "actor_name": str(raw_metadata.get("actor_name") or ""),
            "source_kind": str(raw_metadata.get("source_kind") or "unknown"),
            "evidence": str(raw_metadata.get("evidence") or "").strip(),
        }

    def _merge_custom_field_meta(
        self,
        current_meta: Any,
        incoming_meta: Any,
    ) -> dict[str, Any]:
        if not isinstance(current_meta, dict):
            current_meta = {}
        if not isinstance(incoming_meta, dict):
            incoming_meta = {}

        current_created_at = self._coerce_timestamp(current_meta.get("created_at"))
        incoming_created_at = self._coerce_timestamp(incoming_meta.get("created_at"))
        prefer_incoming = bool(incoming_created_at and incoming_created_at >= current_created_at)
        description = str(current_meta.get("description") or "").strip()
        if prefer_incoming and str(incoming_meta.get("description") or "").strip():
            description = str(incoming_meta.get("description") or "").strip()
        elif not description:
            description = str(incoming_meta.get("description") or "").strip()

        created_by = str(current_meta.get("created_by") or "").strip()
        if prefer_incoming and str(incoming_meta.get("created_by") or "").strip():
            created_by = str(incoming_meta.get("created_by") or "").strip()
        elif not created_by:
            created_by = str(incoming_meta.get("created_by") or "unknown").strip()

        return {
            "description": description,
            "created_at": self._pick_earliest_timestamp(
                current_meta.get("created_at"),
                incoming_meta.get("created_at"),
            ),
            "created_by": created_by or "unknown",
        }

    def _merge_field_meta_entry(
        self,
        current_meta: Any,
        incoming_meta: Any,
        fallback_updated_at: str,
    ) -> dict[str, Any]:
        normalized_current = self._normalize_field_meta_entry(current_meta, fallback_updated_at)
        normalized_incoming = self._normalize_field_meta_entry(incoming_meta, fallback_updated_at)
        current_updated_at = normalized_current["updated_at"]
        incoming_updated_at = normalized_incoming["updated_at"]
        if incoming_updated_at >= current_updated_at:
            return normalized_incoming
        return normalized_current

    def _merge_note_values(
        self,
        current_value: Any,
        incoming_value: Any,
        *,
        prefer_incoming_tail: bool,
    ) -> list[str]:
        current_notes = self._normalize_notes(current_value)
        incoming_notes = self._normalize_notes(incoming_value)
        if prefer_incoming_tail:
            return self._normalize_notes([*current_notes, *incoming_notes])
        return self._normalize_notes([*incoming_notes, *current_notes])

    def _merge_profile_record(
        self,
        current_record: dict[str, Any],
        incoming_record: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        merged = json.loads(json.dumps(current_record, ensure_ascii=False))
        changed = False

        merged["subject_user_id"] = str(
            merged.get("subject_user_id") or incoming_record.get("subject_user_id") or ""
        )
        if not str(merged.get("subject_name") or "").strip() and str(incoming_record.get("subject_name") or "").strip():
            merged["subject_name"] = str(incoming_record.get("subject_name") or "").strip()
            changed = True

        merged_created_at = self._pick_earliest_timestamp(
            merged.get("created_at"),
            incoming_record.get("created_at"),
        )
        if merged.get("created_at") != merged_created_at:
            merged["created_at"] = merged_created_at
            changed = True

        merged_fields = merged.setdefault("fields", {})
        merged_custom_fields = merged.setdefault("custom_fields", {})
        merged_field_meta = merged.setdefault("field_meta", {})
        incoming_fields = incoming_record.get("fields", {})
        incoming_custom_fields = incoming_record.get("custom_fields", {})
        incoming_field_meta = incoming_record.get("field_meta", {})

        for field_name, incoming_value in incoming_fields.items():
            current_value = merged_fields.get(field_name)
            current_meta = merged_field_meta.get(field_name, {})
            incoming_meta = incoming_field_meta.get(field_name, {})
            current_field_updated_at = self._pick_latest_timestamp(
                (current_meta or {}).get("updated_at") if isinstance(current_meta, dict) else "",
                current_record.get("updated_at"),
            )
            incoming_field_updated_at = self._pick_latest_timestamp(
                (incoming_meta or {}).get("updated_at") if isinstance(incoming_meta, dict) else "",
                incoming_record.get("updated_at"),
            )
            normalized_meta = self._merge_field_meta_entry(
                current_meta,
                incoming_meta,
                fallback_updated_at=self._pick_latest_timestamp(current_field_updated_at, incoming_field_updated_at),
            )
            incoming_is_newer = incoming_field_updated_at >= current_field_updated_at

            if field_name == NOTES_FIELD_NAME:
                merged_value = self._merge_note_values(
                    current_value,
                    incoming_value,
                    prefer_incoming_tail=incoming_is_newer,
                )
            elif current_value is None or incoming_is_newer:
                merged_value = incoming_value
            else:
                merged_value = current_value

            if merged_value != current_value:
                merged_fields[field_name] = merged_value
                changed = True
            if merged_field_meta.get(field_name) != normalized_meta:
                merged_field_meta[field_name] = normalized_meta
                changed = True

        for field_name, incoming_meta in incoming_custom_fields.items():
            merged_meta = self._merge_custom_field_meta(merged_custom_fields.get(field_name), incoming_meta)
            if merged_custom_fields.get(field_name) != merged_meta:
                merged_custom_fields[field_name] = merged_meta
                changed = True

        # 字段锁是"粘性"的：合并时取并集，避免合并后意外解锁。
        merged_locks = self._normalize_lock_list(
            [*merged.get("locked_fields", []), *incoming_record.get("locked_fields", [])]
        )
        if merged.get("locked_fields") != merged_locks:
            merged["locked_fields"] = merged_locks
            changed = True

        merged_updated_at = self._pick_latest_timestamp(
            merged.get("updated_at"),
            incoming_record.get("updated_at"),
            *[
                metadata.get("updated_at")
                for metadata in merged_field_meta.values()
                if isinstance(metadata, dict)
            ],
        )
        if merged.get("updated_at") != merged_updated_at:
            merged["updated_at"] = merged_updated_at
            changed = True
        return merged, changed

    def _merge_profiles(
        self,
        current_profiles: dict[str, Any],
        incoming_profiles: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        merged = json.loads(json.dumps(current_profiles, ensure_ascii=False))
        changed = False
        for session_key, incoming_record in incoming_profiles.items():
            current_record = merged.get(session_key)
            if not isinstance(current_record, dict):
                merged[session_key] = incoming_record
                changed = True
                continue
            merged_record, record_changed = self._merge_profile_record(current_record, incoming_record)
            if record_changed:
                merged[session_key] = merged_record
                changed = True
        return merged, changed

    @staticmethod
    def _extract_user_id_from_key(session_key: str) -> str:
        if "_" not in session_key:
            return session_key
        return session_key.rsplit("_", 1)[-1]

    def _normalize_notes(self, raw_value: Any) -> list[str]:
        if isinstance(raw_value, list):
            source_items = raw_value
        else:
            source_items = re.split(r"[；;\n]+", str(raw_value or ""))
        notes: list[str] = []
        for item in source_items:
            text = str(item or "").strip()
            if not text:
                continue
            if text not in notes:
                notes.append(text[: self.field_value_max_length])
        return notes[-self.max_notes_count :]

    def _normalize_lock_list(self, raw_value: Any) -> list[str]:
        """Normalize locked_fields into a de-duplicated, canonical, sorted list."""
        if isinstance(raw_value, dict):
            source_items = [name for name, flag in raw_value.items() if flag]
        elif isinstance(raw_value, (list, tuple, set, frozenset)):
            source_items = list(raw_value)
        elif raw_value:
            source_items = re.split(r"[、,，/|;；\s]+", str(raw_value))
        else:
            source_items = []
        locked: list[str] = []
        for item in source_items:
            field_name = self.canonical_field_name(item)
            if field_name and field_name not in locked:
                locked.append(field_name)
        return sorted(locked)

    def _record_locked_fields(self, record: dict[str, Any]) -> list[str]:
        locked = record.get("locked_fields")
        if not isinstance(locked, list):
            locked = self._normalize_lock_list(locked)
            record["locked_fields"] = locked
        return locked

    def is_field_locked(self, record: dict[str, Any], field_name: str) -> bool:
        return self.canonical_field_name(field_name) in self._record_locked_fields(record)

    def _lock_blocks_actor(self, actor: AuditActor | None) -> bool:
        """Whether the configured lock scope should block this actor."""
        if self.field_lock_scope == "strict":
            return True
        actor_type = getattr(actor, "actor_type", "") or ""
        allowed = FIELD_LOCK_SCOPE_ACTORS.get(self.field_lock_scope, FIELD_LOCK_SCOPE_ACTORS[DEFAULT_FIELD_LOCK_SCOPE])
        return actor_type in allowed

    def field_lock_scope_label(self) -> str:
        return FIELD_LOCK_SCOPE_LABELS.get(self.field_lock_scope, FIELD_LOCK_SCOPE_LABELS[DEFAULT_FIELD_LOCK_SCOPE])

    def _normalize_profile_record(
        self,
        payload: dict[str, Any],
        subject_user_id: str,
        subject_name: str,
    ) -> dict[str, Any]:
        if "fields" not in payload:
            updated_at = str(payload.get("_last_updated") or self.now_text())
            fields: dict[str, Any] = {}
            custom_fields: dict[str, Any] = {}
            field_meta: dict[str, Any] = {}
            for raw_field_name, raw_value in payload.items():
                if str(raw_field_name).startswith("_"):
                    continue
                field_name = self.canonical_field_name(raw_field_name)
                if not field_name:
                    continue
                if field_name == NOTES_FIELD_NAME:
                    notes = self._normalize_notes(raw_value)
                    if notes:
                        fields[field_name] = notes
                else:
                    value_text = str(raw_value or "").strip()
                    if value_text:
                        fields[field_name] = value_text[: self.field_value_max_length]
                if field_name not in fields:
                    continue
                if field_name not in self.builtin_field_map:
                    custom_fields[field_name] = {
                        "description": "从上游 SoulMap 迁移的自定义字段",
                        "created_at": updated_at,
                        "created_by": "migration",
                    }
                field_meta[field_name] = {
                    "updated_at": updated_at,
                    "updated_by": "migration",
                    "source_kind": "migration",
                    "evidence": "",
                }
            return {
                "subject_user_id": subject_user_id,
                "subject_name": subject_name,
                "created_at": updated_at,
                "updated_at": updated_at,
                "fields": fields,
                "custom_fields": custom_fields,
                "field_meta": field_meta,
                "locked_fields": self._normalize_lock_list(payload.get("_locked_fields")),
            }

        created_at = str(payload.get("created_at") or self.now_text())
        updated_at = str(payload.get("updated_at") or created_at)
        fields_payload = payload.get("fields", {})
        custom_fields_payload = payload.get("custom_fields", {})
        field_meta_payload = payload.get("field_meta", {})

        fields: dict[str, Any] = {}
        for raw_field_name, raw_value in fields_payload.items():
            field_name = self.canonical_field_name(raw_field_name)
            if not field_name:
                continue
            if field_name == NOTES_FIELD_NAME:
                notes = self._normalize_notes(raw_value)
                if notes:
                    fields[field_name] = notes
            else:
                text = str(raw_value or "").strip()
                if text:
                    fields[field_name] = text[: self.field_value_max_length]

        custom_fields: dict[str, Any] = {}
        for raw_field_name, raw_metadata in custom_fields_payload.items():
            field_name = self.canonical_field_name(raw_field_name)
            if not field_name or not isinstance(raw_metadata, dict):
                continue
            if field_name in self.builtin_field_map:
                continue
            custom_fields[field_name] = {
                "description": str(raw_metadata.get("description") or "").strip(),
                "created_at": str(raw_metadata.get("created_at") or created_at),
                "created_by": str(raw_metadata.get("created_by") or "unknown"),
            }

        field_meta: dict[str, Any] = {}
        for raw_field_name, raw_metadata in field_meta_payload.items():
            field_name = self.canonical_field_name(raw_field_name)
            if not field_name or not isinstance(raw_metadata, dict):
                continue
            field_meta[field_name] = self._normalize_field_meta_entry(raw_metadata, updated_at)

        return {
            "subject_user_id": subject_user_id,
            "subject_name": subject_name,
            "created_at": created_at,
            "updated_at": updated_at,
            "fields": fields,
            "custom_fields": custom_fields,
            "field_meta": field_meta,
            "locked_fields": self._normalize_lock_list(payload.get("locked_fields")),
        }

    def _profile_key(self, user_id: str, session_id: str | None = None) -> str:
        return f"{session_id}_{user_id}" if session_id else user_id

    def profile_key(self, user_id: str, session_id: str | None = None) -> str:
        """公开的键计算入口，供主插件与 WebUI 复用，避免各处重复拼字符串。"""
        return self._profile_key(user_id, session_id)

    def _save_profiles(self) -> None:
        self._write_json(self.profiles_path, self.profiles)
        self._maybe_snapshot_backup()

    def _ensure_profile(
        self,
        user_id: str,
        subject_name: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        session_key = self._profile_key(user_id, session_id)
        if session_key not in self.profiles:
            now = self.now_text()
            self.profiles[session_key] = {
                "subject_user_id": user_id,
                "subject_name": subject_name,
                "created_at": now,
                "updated_at": now,
                "fields": {},
                "custom_fields": {},
                "field_meta": {},
                "locked_fields": [],
            }
        record = self.profiles[session_key]
        if subject_name:
            record["subject_name"] = subject_name
        self._record_locked_fields(record)
        return record

    def get_profile(self, user_id: str, session_id: str | None = None) -> dict[str, Any]:
        session_key = self._profile_key(user_id, session_id)
        record = self.profiles.get(session_key)
        if record is None:
            return {}
        return json.loads(json.dumps(record, ensure_ascii=False))

    def get_last_updated(self, user_id: str, session_id: str | None = None) -> str | None:
        profile = self.get_profile(user_id, session_id)
        return str(profile.get("updated_at")) if profile else None

    def list_field_definitions(
        self,
        user_id: str,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        profile = self.get_profile(user_id, session_id)
        custom_fields = profile.get("custom_fields", {}) if profile else {}
        locked_fields = set(self._normalize_lock_list(profile.get("locked_fields") if profile else []))
        field_defs: list[dict[str, Any]] = []
        for field_name, description in self.builtin_field_map.items():
            if field_name == NOTES_FIELD_NAME:
                continue
            field_defs.append(
                {
                    "name": field_name,
                    "description": description,
                    "is_custom": False,
                    "locked": field_name in locked_fields,
                }
            )
        for field_name, metadata in custom_fields.items():
            field_defs.append(
                {
                    "name": field_name,
                    "description": str(metadata.get("description") or "当前用户的自定义字段"),
                    "is_custom": True,
                    "locked": field_name in locked_fields,
                }
            )
        field_defs.append(
            {
                "name": NOTES_FIELD_NAME,
                "description": self.builtin_field_map[NOTES_FIELD_NAME],
                "is_custom": False,
                "locked": NOTES_FIELD_NAME in locked_fields,
            }
        )
        return field_defs

    def format_field_catalog(self, user_id: str, session_id: str | None = None) -> str:
        definitions = self.list_field_definitions(user_id, session_id)
        if not definitions:
            return "暂无字段"
        lines = []
        for item in definitions:
            field_name = item["name"]
            field_desc = item["description"]
            tags = []
            if item["is_custom"]:
                tags.append("当前用户自定义字段")
            if item.get("locked"):
                tags.append("已锁定，禁止修改")
            suffix = f"（{'；'.join(tags)}）" if tags else ""
            lines.append(f"- {field_name}：{field_desc}{suffix}")
        return "\n".join(lines)

    def format_custom_field_catalog(self, user_id: str, session_id: str | None = None) -> str:
        profile = self.get_profile(user_id, session_id)
        if not profile:
            return "无"
        custom_fields = profile.get("custom_fields", {})
        if not custom_fields:
            return "无"
        lines = []
        for field_name, metadata in custom_fields.items():
            desc = str(metadata.get("description") or "当前用户自定义字段")
            lines.append(f"- {field_name}：{desc}")
        return "\n".join(lines)

    def format_profile_summary(self, user_id: str, session_id: str | None = None) -> str:
        profile = self.get_profile(user_id, session_id)
        if not profile:
            return EMPTY_PROFILE_SUMMARY

        fields = profile.get("fields", {})
        if not fields:
            return EMPTY_PROFILE_SUMMARY

        locked_fields = set(self._normalize_lock_list(profile.get("locked_fields")))

        def _suffix(field_name: str) -> str:
            # 明确标注锁定状态，避免 LLM 反复尝试写入被锁字段。
            return "（已锁定）" if field_name in locked_fields else ""

        lines: list[str] = []
        for field_name in self.builtin_field_map:
            if field_name == NOTES_FIELD_NAME:
                continue
            if field_name not in fields:
                continue
            value = fields[field_name]
            lines.append(f"- {field_name}：{value}{_suffix(field_name)}")

        custom_fields = profile.get("custom_fields", {})
        for field_name in custom_fields:
            if field_name not in fields:
                continue
            lines.append(f"- {field_name}：{fields[field_name]}{_suffix(field_name)}")

        notes = fields.get(NOTES_FIELD_NAME)
        if isinstance(notes, list) and notes:
            notes_text = " ".join(f"{idx}.{note}" for idx, note in enumerate(notes, start=1))
            lines.append(f"- {NOTES_FIELD_NAME}：{notes_text}{_suffix(NOTES_FIELD_NAME)}")
        elif notes:
            lines.append(f"- {NOTES_FIELD_NAME}：{notes}{_suffix(NOTES_FIELD_NAME)}")

        locked_only = sorted(locked_fields - set(fields.keys()))
        if locked_only:
            lines.append(f"- 已锁定但暂无内容的字段：{'、'.join(locked_only)}")

        return "\n".join(lines) if lines else EMPTY_PROFILE_SUMMARY

    def is_known_field(self, user_id: str, field_name: str, session_id: str | None = None) -> bool:
        normalized = self.canonical_field_name(field_name)
        if normalized in self.builtin_field_map:
            return True
        profile = self.get_profile(user_id, session_id)
        custom_fields = profile.get("custom_fields", {})
        return normalized in custom_fields

    def validate_custom_field_name(self, field_name: str) -> OperationResult:
        normalized = self.normalize_field_name(field_name)
        if not normalized:
            return OperationResult(False, "字段名不能为空")
        if len(normalized) > self.custom_field_name_max_length:
            return OperationResult(
                False,
                f"字段名过长，最多 {self.custom_field_name_max_length} 个字符",
            )
        if not CUSTOM_FIELD_NAME_PATTERN.fullmatch(normalized):
            return OperationResult(False, "字段名只能包含中文、字母、数字、空格、_、-、/")
        lowered = normalized.casefold()
        if any(fragment in lowered for fragment in FORBIDDEN_FIELD_FRAGMENTS):
            return OperationResult(False, "字段名涉及系统或敏感语义，已拒绝创建")
        return OperationResult(True, "字段名可用")

    def _touch_field_meta(
        self,
        record: dict[str, Any],
        field_name: str,
        actor: AuditActor,
        source_kind: str,
        evidence: str,
    ) -> None:
        record["field_meta"][field_name] = {
            "updated_at": self.now_text(),
            "updated_by": actor.actor_type,
            "actor_id": actor.actor_id,
            "actor_name": actor.actor_name,
            "source_kind": source_kind,
            "evidence": evidence[:160],
        }

    def _append_audit(
        self,
        *,
        action: str,
        user_id: str,
        subject_name: str,
        session_id: str | None,
        actor: AuditActor,
        field_name: str,
        source_kind: str,
        evidence: str,
        old_value: Any,
        new_value: Any,
    ) -> None:
        entry = {
            "at": self.now_text(),
            "session_id": session_id or "",
            "subject_user_id": user_id,
            "subject_name": subject_name,
            "actor_type": actor.actor_type,
            "actor_id": actor.actor_id,
            "actor_name": actor.actor_name,
            "action": action,
            "field_name": field_name,
            "source_kind": source_kind,
            "evidence": evidence[:160],
            "old_value": self._compact_value(old_value),
            "new_value": self._compact_value(new_value),
        }
        self._rotate_audit_log()
        try:
            with self.audit_log_path.open("a", encoding="utf-8") as audit_file:
                audit_file.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.error("[ProfileWeaver] 写入审计日志失败：%s", exc)

    def _compact_value(self, value: Any) -> str:
        if isinstance(value, list):
            text = " | ".join(str(item) for item in value)
        else:
            text = str(value or "")
        return text[:200]

    def upsert_field(
        self,
        *,
        user_id: str,
        subject_name: str,
        field_name: str,
        value: str,
        session_id: str | None,
        actor: AuditActor,
        source_kind: str,
        evidence: str,
        allow_custom_field: bool,
        field_description: str = "",
    ) -> OperationResult:
        normalized_field = self.canonical_field_name(field_name)
        value_text = str(value or "").strip()
        if not normalized_field:
            return OperationResult(False, "字段名不能为空")
        if not value_text:
            return OperationResult(False, "字段值不能为空")
        value_text = value_text[: self.field_value_max_length]

        record = self._ensure_profile(user_id, subject_name, session_id)
        fields = record["fields"]
        custom_fields = record["custom_fields"]
        created_custom_field = False
        session_key = self._profile_key(user_id, session_id)

        if self.is_field_locked(record, normalized_field) and self._lock_blocks_actor(actor):
            self._drop_profile_if_empty(session_key, save=False)
            return OperationResult(
                False,
                f"字段「{normalized_field}」已被锁定（当前锁范围：{self.field_lock_scope_label()}），本次写入已拒绝。"
                f"如确实需要修改，请先执行「解锁画像 {normalized_field}」。",
            )

        if normalized_field not in self.builtin_field_map and normalized_field not in custom_fields:
            if not allow_custom_field:
                return OperationResult(
                    False,
                    "字段不存在。若确实需要新字段，请显式允许创建当前用户自定义字段。",
                )
            validation = self.validate_custom_field_name(normalized_field)
            if not validation.ok:
                return validation
            custom_fields[normalized_field] = {
                "description": str(field_description or "当前用户自定义字段").strip()[:80],
                "created_at": self.now_text(),
                "created_by": actor.actor_type,
            }
            created_custom_field = True

        conflict_validation = self._validate_profile_identity_conflicts(
            session_key=session_key,
            session_id=session_id,
            record=record,
            field_name=normalized_field,
            value=value_text,
        )
        if not conflict_validation.ok:
            if created_custom_field:
                custom_fields.pop(normalized_field, None)
            self._drop_profile_if_empty(session_key, save=False)
            return conflict_validation

        old_value = fields.get(normalized_field)
        if normalized_field == NOTES_FIELD_NAME:
            merged_notes = self._normalize_notes([*(old_value or []), *self._normalize_notes(value_text)])
            if merged_notes == old_value:
                return OperationResult(True, "备注没有新的变化")
            fields[normalized_field] = merged_notes
            new_value: Any = merged_notes
            audit_action = "append_note"
        else:
            if old_value == value_text:
                return OperationResult(True, f"{normalized_field} 已经是最新值")
            fields[normalized_field] = value_text
            new_value = value_text
            audit_action = "upsert_field"

        record["updated_at"] = self.now_text()
        self._touch_field_meta(record, normalized_field, actor, source_kind, evidence)
        self._save_profiles()
        self._append_audit(
            action=audit_action,
            user_id=user_id,
            subject_name=record["subject_name"],
            session_id=session_id,
            actor=actor,
            field_name=normalized_field,
            source_kind=source_kind,
            evidence=evidence,
            old_value=old_value,
            new_value=new_value,
        )
        if created_custom_field:
            return OperationResult(True, f"已写入 {normalized_field}，并创建当前用户自定义字段", True, True)
        return OperationResult(True, f"已写入 {normalized_field}", True)

    def delete_field(
        self,
        *,
        user_id: str,
        session_id: str | None,
        field_selector: str,
        actor: AuditActor,
        source_kind: str,
        evidence: str,
    ) -> OperationResult:
        session_key = self._profile_key(user_id, session_id)
        record = self.profiles.get(session_key)
        if not record:
            return OperationResult(False, "没有找到对应画像")

        normalized_selector = self.canonical_field_name(field_selector)
        notes = record["fields"].get(NOTES_FIELD_NAME, [])
        note_match = NOTE_INDEX_PATTERN.fullmatch(normalized_selector)
        if note_match and isinstance(notes, list) and notes:
            if self.is_field_locked(record, NOTES_FIELD_NAME) and self._lock_blocks_actor(actor):
                return self._locked_field_result(NOTES_FIELD_NAME)
            note_index = int(note_match.group(1)) - 1
            if note_index < 0 or note_index >= len(notes):
                return OperationResult(False, "备注序号不存在")
            old_notes = list(notes)
            deleted_note = notes.pop(note_index)
            if notes:
                record["fields"][NOTES_FIELD_NAME] = notes
            else:
                record["fields"].pop(NOTES_FIELD_NAME, None)
                record["field_meta"].pop(NOTES_FIELD_NAME, None)
            record["updated_at"] = self.now_text()
            subject_name = str(record.get("subject_name") or "")
            self._drop_profile_if_empty(session_key, save=False)
            self._save_profiles()
            self._append_audit(
                action="delete_note",
                user_id=user_id,
                subject_name=subject_name,
                session_id=session_id,
                actor=actor,
                field_name=f"{NOTES_FIELD_NAME}:{note_index + 1}",
                source_kind=source_kind,
                evidence=evidence,
                old_value=old_notes,
                new_value=notes,
            )
            return OperationResult(True, f"已删除备注 {note_index + 1}：{deleted_note}", True)

        fields = record["fields"]
        if normalized_selector not in fields:
            return OperationResult(False, f"未找到字段 {normalized_selector}")
        if self.is_field_locked(record, normalized_selector) and self._lock_blocks_actor(actor):
            return self._locked_field_result(normalized_selector)

        old_value = fields.pop(normalized_selector)
        record["field_meta"].pop(normalized_selector, None)
        record["custom_fields"].pop(normalized_selector, None)
        record["updated_at"] = self.now_text()
        subject_name = str(record.get("subject_name") or "")
        # 删空后可能整条画像作废，先判定再统一落盘，避免重复写文件。
        self._drop_profile_if_empty(session_key, save=False)
        self._save_profiles()
        self._append_audit(
            action="delete_field",
            user_id=user_id,
            subject_name=subject_name,
            session_id=session_id,
            actor=actor,
            field_name=normalized_selector,
            source_kind=source_kind,
            evidence=evidence,
            old_value=old_value,
            new_value="",
        )
        return OperationResult(True, f"已删除字段 {normalized_selector}", True)

    def _locked_field_result(self, field_name: str) -> OperationResult:
        return OperationResult(
            False,
            f"字段「{field_name}」已被锁定（当前锁范围：{self.field_lock_scope_label()}），本次操作已拒绝。"
            f"如确实需要修改，请先执行「解锁画像 {field_name}」。",
        )

    def set_field_lock(
        self,
        *,
        user_id: str,
        subject_name: str,
        session_id: str | None,
        field_name: str,
        locked: bool,
        actor: AuditActor,
    ) -> OperationResult:
        normalized_field = self.canonical_field_name(field_name)
        if not normalized_field:
            return OperationResult(False, "字段名不能为空")

        session_key = self._profile_key(user_id, session_id)
        record = self.profiles.get(session_key)
        if record is None:
            if not locked:
                return OperationResult(False, "没有找到对应画像，无需解锁")
            record = self._ensure_profile(user_id, subject_name, session_id)

        known = (
            normalized_field in self.builtin_field_map
            or normalized_field in record.get("custom_fields", {})
            or normalized_field in record.get("fields", {})
        )
        if locked and not known:
            return OperationResult(False, f"未知字段「{normalized_field}」，请先确认字段名（可用「画像字段」查看）")

        locked_fields = self._record_locked_fields(record)
        already = normalized_field in locked_fields
        if locked and already:
            self._drop_profile_if_empty(session_key, save=False)
            return OperationResult(True, f"字段「{normalized_field}」本来就是锁定状态")
        if not locked and not already:
            self._drop_profile_if_empty(session_key, save=False)
            return OperationResult(True, f"字段「{normalized_field}」当前没有被锁定")

        old_value = list(locked_fields)
        if locked:
            locked_fields.append(normalized_field)
        else:
            locked_fields.remove(normalized_field)
        record["locked_fields"] = sorted(set(locked_fields))
        record["updated_at"] = self.now_text()
        resolved_subject_name = str(record.get("subject_name") or subject_name or "")
        self._drop_profile_if_empty(session_key, save=False)
        self._save_profiles()
        self._append_audit(
            action="lock_field" if locked else "unlock_field",
            user_id=user_id,
            subject_name=resolved_subject_name,
            session_id=session_id,
            actor=actor,
            field_name=normalized_field,
            source_kind="field_lock",
            evidence=self.field_lock_scope_label(),
            old_value=old_value,
            new_value=record.get("locked_fields", []),
        )
        verb = "已锁定" if locked else "已解锁"
        return OperationResult(True, f"{verb}字段「{normalized_field}」", True)

    def _drop_profile_if_empty(self, session_key: str, *, save: bool = True) -> bool:
        record = self.profiles.get(session_key)
        if record is None:
            return False
        if record.get("fields") or record.get("locked_fields"):
            return False
        self.profiles.pop(session_key, None)
        if save:
            self._save_profiles()
        return True

    def clear_profile(
        self,
        *,
        user_id: str,
        session_id: str | None,
        actor: AuditActor,
    ) -> OperationResult:
        session_key = self._profile_key(user_id, session_id)
        record = self.profiles.get(session_key)
        if record is None:
            return OperationResult(False, "没有可清空的画像")
        old_value = record.get("fields", {})
        subject_name = str(record.get("subject_name") or "")
        self.profiles.pop(session_key, None)
        self._save_profiles()
        self._append_audit(
            action="clear_profile",
            user_id=user_id,
            subject_name=subject_name,
            session_id=session_id,
            actor=actor,
            field_name="*",
            source_kind="manual_clear",
            evidence="",
            old_value=old_value,
            new_value="",
        )
        return OperationResult(True, "已清空画像", True)

    def collect_stats(self) -> dict[str, Any]:
        field_counts: dict[str, int] = {}
        custom_field_counts: dict[str, int] = {}
        source_counts: dict[str, int] = {}
        distinct_users: set[str] = set()
        distinct_sessions: set[str] = set()
        note_count = 0
        filled_field_count = 0
        locked_field_count = 0
        locked_profile_count = 0
        completeness_total = 0.0
        latest_updated_at = ""
        active_7d = 0
        active_30d = 0
        cutoff_7d = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        cutoff_30d = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

        for session_key, record in self.profiles.items():
            if not isinstance(record, dict):
                continue
            session_id, user_id = self.split_profile_key(session_key, record)
            if user_id:
                distinct_users.add(user_id)
            if session_id:
                distinct_sessions.add(session_id)

            fields = record.get("fields", {})
            custom_fields = record.get("custom_fields", {})
            if isinstance(fields, dict):
                for field_name, value in fields.items():
                    field_counts[field_name] = field_counts.get(field_name, 0) + 1
                    filled_field_count += 1
                    if isinstance(custom_fields, dict) and field_name in custom_fields:
                        custom_field_counts[field_name] = custom_field_counts.get(field_name, 0) + 1
                    if field_name == NOTES_FIELD_NAME and isinstance(value, list):
                        note_count += len(value)

            locked_fields = record.get("locked_fields")
            if isinstance(locked_fields, list) and locked_fields:
                locked_field_count += len(locked_fields)
                locked_profile_count += 1

            completeness_total += self._record_completeness(record)

            field_meta = record.get("field_meta", {})
            if isinstance(field_meta, dict):
                for metadata in field_meta.values():
                    if not isinstance(metadata, dict):
                        continue
                    source = str(metadata.get("updated_by") or "unknown")
                    source_counts[source] = source_counts.get(source, 0) + 1

            updated_at = self._coerce_timestamp(record.get("updated_at"))
            latest_updated_at = self._pick_latest_timestamp(latest_updated_at, updated_at)
            if updated_at and updated_at >= cutoff_7d:
                active_7d += 1
            if updated_at and updated_at >= cutoff_30d:
                active_30d += 1

        profile_count = len(self.profiles)
        return {
            "user_count": len(distinct_users) or profile_count,
            "profile_count": profile_count,
            "session_count": len(distinct_sessions),
            "field_counts": field_counts,
            "custom_field_counts": custom_field_counts,
            "source_counts": source_counts,
            "note_count": note_count,
            "filled_field_count": filled_field_count,
            "locked_field_count": locked_field_count,
            "locked_profile_count": locked_profile_count,
            "avg_fields_per_profile": round(filled_field_count / profile_count, 2) if profile_count else 0.0,
            "avg_completeness": round(completeness_total / profile_count, 1) if profile_count else 0.0,
            "latest_updated_at": latest_updated_at,
            "active_profiles_7d": active_7d,
            "active_profiles_30d": active_30d,
            "audit_log_bytes": self.audit_log_path.stat().st_size if self.audit_log_path.exists() else 0,
            "audit_rotated_count": len(self._list_rotated_audit_logs()),
            "profiles_file_bytes": self.profiles_path.stat().st_size if self.profiles_path.exists() else 0,
            "backup_count": len(self.list_backups()),
            "builtin_field_count": len(self.builtin_field_map),
            "custom_field_kind_count": len(custom_field_counts),
            "field_lock_scope": self.field_lock_scope,
            "field_lock_scope_label": self.field_lock_scope_label(),
        }

    def _record_completeness(self, record: dict[str, Any]) -> float:
        """Percentage of builtin fields that already carry a value for this profile."""
        total = len(self.builtin_field_map)
        if not total:
            return 0.0
        fields = record.get("fields", {})
        if not isinstance(fields, dict):
            return 0.0
        filled = sum(1 for field_name in self.builtin_field_map if fields.get(field_name))
        return round(filled * 100 / total, 1)

    def read_recent_audit(
        self,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
        limit: int = 10,
        include_rotated: bool = False,
    ) -> list[dict[str, Any]]:
        matched: deque[dict[str, Any]] = deque(maxlen=max(1, min(int(limit), 50)))
        for entry in self._iter_audit_entries(include_rotated=include_rotated):
            if user_id and str(entry.get("subject_user_id")) != user_id:
                continue
            if session_id is not None and str(entry.get("session_id") or "") != str(session_id or ""):
                continue
            matched.append(entry)
        return list(reversed(matched))

    def read_field_history(
        self,
        *,
        user_id: str,
        session_id: str | None = None,
        field_name: str = "",
        limit: int = 20,
        include_rotated: bool = True,
    ) -> list[dict[str, Any]]:
        """Replay the audit trail for a single field so the WebUI can show its history."""
        normalized_field = self.canonical_field_name(field_name)
        if not normalized_field:
            return []
        note_prefix = f"{NOTES_FIELD_NAME}:"
        matched: deque[dict[str, Any]] = deque(maxlen=max(1, min(int(limit), 200)))
        for entry in self._iter_audit_entries(include_rotated=include_rotated):
            if user_id and str(entry.get("subject_user_id")) != str(user_id):
                continue
            if session_id is not None and str(entry.get("session_id") or "") != str(session_id or ""):
                continue
            entry_field = str(entry.get("field_name") or "")
            if entry_field == "*":
                # 清空/恢复/导入这类全量动作对任何字段都有意义，保留。
                matched.append(entry)
                continue
            if normalized_field == NOTES_FIELD_NAME:
                if entry_field != NOTES_FIELD_NAME and not entry_field.startswith(note_prefix):
                    continue
            elif self.canonical_field_name(entry_field) != normalized_field:
                continue
            matched.append(entry)
        return list(reversed(matched))

    # ------------------------------------------------------------------
    # 键解析 / 公共小工具
    # ------------------------------------------------------------------
    def split_profile_key(self, session_key: str, record: dict[str, Any] | None = None) -> tuple[str, str]:
        """Split a storage key back into (session_id, user_id)."""
        record = record if isinstance(record, dict) else (self.profiles.get(session_key) or {})
        user_id = str(record.get("subject_user_id") or self._extract_user_id_from_key(session_key)).strip()
        if not user_id:
            return "", session_key
        suffix = f"_{user_id}"
        if session_key.endswith(suffix) and len(session_key) > len(suffix):
            return session_key[: -len(suffix)], user_id
        return "", user_id

    def extract_conflict_tokens(self, raw_value: Any) -> list[tuple[str, str]]:
        """Public wrapper so callers do not need to touch a private helper."""
        return self._extract_conflict_tokens(raw_value)

    @staticmethod
    def describe_session(session_id: str) -> dict[str, str]:
        """Best-effort split of an AstrBot unified_msg_origin for display purposes."""
        raw = str(session_id or "").strip()
        if not raw:
            return {"platform": "", "chat_type": "global", "chat_id": ""}
        parts = raw.split(":")
        platform = parts[0] if parts else ""
        message_type = parts[1] if len(parts) > 1 else ""
        chat_id = parts[2] if len(parts) > 2 else ""
        lowered = message_type.casefold()
        if "group" in lowered:
            chat_type = "group"
        elif "friend" in lowered or "private" in lowered:
            chat_type = "private"
        else:
            chat_type = message_type or "unknown"
        return {"platform": platform, "chat_type": chat_type, "chat_id": chat_id}

    def primary_display_name(self, record: dict[str, Any]) -> str:
        fields = record.get("fields", {}) if isinstance(record, dict) else {}
        if isinstance(fields, dict):
            for field_name in PROFILE_NAME_FIELD_NAMES:
                value = fields.get(field_name)
                if isinstance(value, list):
                    value = value[0] if value else ""
                text = str(value or "").strip()
                if text:
                    return text
        subject_name = str(record.get("subject_name") or "").strip()
        if subject_name:
            return subject_name
        return str(record.get("subject_user_id") or "").strip()

    # ------------------------------------------------------------------
    # 备份与日志维护
    # ------------------------------------------------------------------
    def _maybe_snapshot_backup(self) -> None:
        """Keep one automatic snapshot per day so a bad import stays recoverable."""
        today = datetime.now().strftime("%Y%m%d")
        if getattr(self, "_last_snapshot_day", "") == today:
            return
        self._last_snapshot_day = today
        snapshot_path = self.backup_dir / f"profiles-auto-{today}.json"
        if snapshot_path.exists():
            self._prune_backups()
            return
        try:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            self._write_json(snapshot_path, self.profiles)
        except OSError as exc:
            logger.warning("[ProfileWeaver] 自动备份失败：%s", exc)
            return
        self._prune_backups()

    def create_backup(self, tag: str = "manual") -> Path | None:
        safe_tag = re.sub(r"[^A-Za-z0-9_\-]+", "-", str(tag or "manual")).strip("-") or "manual"
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = self.backup_dir / f"profiles-{safe_tag}-{stamp}.json"
        try:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            self._write_json(target, self.profiles)
        except OSError as exc:
            logger.warning("[ProfileWeaver] 创建备份失败：%s", exc)
            return None
        self._prune_backups()
        return target

    def _prune_backups(self) -> None:
        if not self.backup_dir.exists():
            return
        surviving: list[tuple[float, Path]] = []
        # backup_retention_days == 0 表示不按天数清理，只按个数上限收敛。
        cutoff = (
            datetime.now() - timedelta(days=self.backup_retention_days)
            if self.backup_retention_days > 0
            else None
        )
        for path in self.backup_dir.glob("profiles-*.json"):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if cutoff is not None and datetime.fromtimestamp(mtime) < cutoff:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                continue
            surviving.append((mtime, path))

        if self.backup_max_count and len(surviving) > self.backup_max_count:
            surviving.sort(key=lambda item: item[0], reverse=True)
            for _, path in surviving[self.backup_max_count :]:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    continue

    def list_backups(self) -> list[dict[str, Any]]:
        if not self.backup_dir.exists():
            return []
        items: list[dict[str, Any]] = []
        for path in sorted(self.backup_dir.glob("profiles-*.json"), reverse=True):
            try:
                stat = path.stat()
            except OSError:
                continue
            items.append(
                {
                    "name": path.name,
                    "size": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
        return items

    def read_backup_text(self, name: str) -> tuple[bool, str, str]:
        """Read one snapshot verbatim so the WebUI can offer it as a download.

        Returns ``(ok, payload_or_error, safe_name)``; the caller never sees a
        caller-supplied path, only the sanitised file name.
        """
        safe_name = Path(str(name or "")).name
        if not BACKUP_NAME_PATTERN.fullmatch(safe_name):
            return False, "备份文件名不合法", safe_name
        target = self.backup_dir / safe_name
        if not target.exists():
            return False, f"备份 {safe_name} 不存在", safe_name
        try:
            return True, target.read_text(encoding="utf-8"), safe_name
        except OSError as exc:
            return False, f"读取备份失败：{exc}", safe_name

    def delete_backup(self, name: str, actor: AuditActor) -> OperationResult:
        safe_name = Path(str(name or "")).name
        if not BACKUP_NAME_PATTERN.fullmatch(safe_name):
            return OperationResult(False, "备份文件名不合法")
        target = self.backup_dir / safe_name
        if not target.exists():
            return OperationResult(False, f"备份 {safe_name} 不存在")
        try:
            target.unlink()
        except OSError as exc:
            return OperationResult(False, f"删除备份失败：{exc}")
        self._append_audit(
            action="delete_backup",
            user_id="*",
            subject_name="",
            session_id=None,
            actor=actor,
            field_name="*",
            source_kind="backup_delete",
            evidence=safe_name,
            old_value=safe_name,
            new_value="",
        )
        return OperationResult(True, f"已删除备份 {safe_name}", True)

    def restore_backup(self, name: str, actor: AuditActor) -> OperationResult:
        safe_name = Path(str(name or "")).name
        if not BACKUP_NAME_PATTERN.fullmatch(safe_name):
            return OperationResult(False, "备份文件名不合法")
        source = self.backup_dir / safe_name
        if not source.exists():
            return OperationResult(False, f"备份 {safe_name} 不存在")
        payload = self._read_json(source)
        if not isinstance(payload, dict):
            return OperationResult(False, "备份内容无法解析")
        self.create_backup("pre-restore")
        restored = self._normalize_profiles_payload(payload)
        self.profiles = restored
        self._save_profiles()
        self._append_audit(
            action="restore_backup",
            user_id="*",
            subject_name="",
            session_id=None,
            actor=actor,
            field_name="*",
            source_kind="backup_restore",
            evidence=safe_name,
            old_value="",
            new_value=f"{len(restored)} profiles",
        )
        return OperationResult(True, f"已从 {safe_name} 恢复 {len(restored)} 条画像", True)

    def _rotate_audit_log(self) -> None:
        if not self.audit_log_path.exists():
            return
        try:
            if self.audit_log_path.stat().st_size < self.audit_log_max_bytes:
                return
        except OSError:
            return
        rotated = self.audit_log_path.with_name(
            f"{self.audit_log_path.name}.{datetime.now().strftime('%Y%m%d%H%M%S')}"
        )
        try:
            shutil.move(str(self.audit_log_path), str(rotated))
            logger.info("[ProfileWeaver] 审计日志已轮转为 %s", rotated.name)
        except OSError as exc:
            logger.warning("[ProfileWeaver] 审计日志轮转失败：%s", exc)
            return
        self._prune_rotated_audit_logs()

    def _list_rotated_audit_logs(self) -> list[Path]:
        """Rotated audit logs, newest first (names embed a sortable timestamp)."""
        parent = self.audit_log_path.parent
        if not parent.exists():
            return []
        rotated = [
            path
            for path in parent.glob(f"{self.audit_log_path.name}.*")
            if AUDIT_ROTATED_NAME_PATTERN.fullmatch(path.name)
        ]
        return sorted(rotated, key=lambda path: path.name, reverse=True)

    def _prune_rotated_audit_logs(self) -> None:
        # 0 表示全部保留。
        if not self.audit_log_keep_rotated:
            return
        for path in self._list_rotated_audit_logs()[self.audit_log_keep_rotated :]:
            try:
                path.unlink(missing_ok=True)
                logger.info("[ProfileWeaver] 已清理过期审计日志 %s", path.name)
            except OSError:
                continue

    def _audit_log_paths(self, *, include_rotated: bool) -> list[Path]:
        """Audit logs in chronological order (oldest rotated file first)."""
        paths: list[Path] = []
        if include_rotated:
            paths.extend(reversed(self._list_rotated_audit_logs()))
        if self.audit_log_path.exists():
            paths.append(self.audit_log_path)
        return paths

    def _iter_audit_entries(self, *, include_rotated: bool = False) -> Iterator[dict[str, Any]]:
        for path in self._audit_log_paths(include_rotated=include_rotated):
            try:
                with path.open("r", encoding="utf-8") as audit_file:
                    for line in audit_file:
                        try:
                            entry = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(entry, dict):
                            yield entry
            except OSError as exc:
                logger.warning("[ProfileWeaver] 读取审计日志 %s 失败：%s", path.name, exc)

    # ------------------------------------------------------------------
    # 面向 WebUI 的查询接口
    # ------------------------------------------------------------------
    def summarize_profile(self, session_key: str, record: dict[str, Any]) -> dict[str, Any]:
        session_id, user_id = self.split_profile_key(session_key, record)
        fields = record.get("fields", {}) if isinstance(record, dict) else {}
        custom_fields = record.get("custom_fields", {}) if isinstance(record, dict) else {}
        fields = fields if isinstance(fields, dict) else {}
        custom_fields = custom_fields if isinstance(custom_fields, dict) else {}
        notes = fields.get(NOTES_FIELD_NAME)
        note_count = len(notes) if isinstance(notes, list) else (1 if notes else 0)

        preview: list[dict[str, str]] = []
        for field_name in self.builtin_field_map:
            if field_name == NOTES_FIELD_NAME or field_name not in fields:
                continue
            preview.append({"name": field_name, "value": self._compact_value(fields[field_name])})
            if len(preview) >= 4:
                break
        if len(preview) < 4:
            for field_name in custom_fields:
                if field_name not in fields:
                    continue
                preview.append({"name": field_name, "value": self._compact_value(fields[field_name])})
                if len(preview) >= 4:
                    break

        session_info = self.describe_session(session_id)
        locked_fields = self._normalize_lock_list(record.get("locked_fields") if isinstance(record, dict) else [])
        return {
            "key": session_key,
            "user_id": user_id,
            "session_id": session_id,
            "subject_name": str(record.get("subject_name") or ""),
            "display_name": self.primary_display_name(record),
            "platform": session_info["platform"],
            "chat_type": session_info["chat_type"],
            "field_count": len([name for name in fields if name != NOTES_FIELD_NAME]),
            "custom_field_count": len([name for name in custom_fields if name in fields]),
            "note_count": note_count,
            "locked_count": len(locked_fields),
            "locked_fields": locked_fields,
            "completeness": self._record_completeness(record if isinstance(record, dict) else {}),
            "created_at": str(record.get("created_at") or ""),
            "updated_at": str(record.get("updated_at") or ""),
            "preview": preview,
        }

    def list_profiles(
        self,
        *,
        query: str = "",
        chat_type: str = "all",
        platform: str = "all",
        session_id: str = "",
        field: str = "",
        sort: str = "updated_desc",
        page: int = 1,
        page_size: int = 20,
        locked_only: bool = False,
    ) -> dict[str, Any]:
        keyword = str(query or "").strip().casefold()
        wanted_field = self.canonical_field_name(field) if field else ""
        rows: list[dict[str, Any]] = []

        for session_key, record in self.profiles.items():
            if not isinstance(record, dict):
                continue
            summary = self.summarize_profile(session_key, record)
            if session_id and summary["session_id"] != session_id:
                continue
            if chat_type not in ("", "all") and summary["chat_type"] != chat_type:
                continue
            if platform not in ("", "all") and summary["platform"] != platform:
                continue
            if locked_only and not summary["locked_count"]:
                continue
            fields = record.get("fields", {})
            if wanted_field and wanted_field not in (fields if isinstance(fields, dict) else {}):
                continue
            if keyword:
                haystack = " ".join(
                    [
                        session_key,
                        summary["user_id"],
                        summary["subject_name"],
                        summary["display_name"],
                        summary["session_id"],
                        json.dumps(fields, ensure_ascii=False) if isinstance(fields, dict) else "",
                    ]
                ).casefold()
                if keyword not in haystack:
                    continue
            rows.append(summary)

        reverse = not sort.endswith("_asc")
        if sort.startswith("fields"):
            rows.sort(key=lambda item: (item["field_count"], item["updated_at"]), reverse=reverse)
        elif sort.startswith("created"):
            rows.sort(key=lambda item: item["created_at"], reverse=reverse)
        elif sort.startswith("name"):
            rows.sort(key=lambda item: item["display_name"].casefold(), reverse=reverse)
        elif sort.startswith("completeness"):
            rows.sort(key=lambda item: (item["completeness"], item["updated_at"]), reverse=reverse)
        elif sort.startswith("locked"):
            rows.sort(key=lambda item: (item["locked_count"], item["updated_at"]), reverse=reverse)
        else:
            rows.sort(key=lambda item: item["updated_at"], reverse=reverse)

        total = len(rows)
        page_size = max(1, min(int(page_size or 20), 200))
        page_count = max(1, (total + page_size - 1) // page_size)
        page = max(1, min(int(page or 1), page_count))
        start = (page - 1) * page_size
        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "page_count": page_count,
            "items": rows[start : start + page_size],
        }

    def list_platforms(self) -> list[str]:
        platforms: set[str] = set()
        for session_key, record in self.profiles.items():
            if not isinstance(record, dict):
                continue
            session_id, _ = self.split_profile_key(session_key, record)
            platform = self.describe_session(session_id)["platform"]
            if platform:
                platforms.add(platform)
        return sorted(platforms)

    def get_profile_detail(self, session_key: str) -> dict[str, Any]:
        record = self.profiles.get(session_key)
        if not isinstance(record, dict):
            return {}
        session_id, user_id = self.split_profile_key(session_key, record)
        fields = record.get("fields", {}) if isinstance(record.get("fields"), dict) else {}
        custom_fields = record.get("custom_fields", {}) if isinstance(record.get("custom_fields"), dict) else {}
        field_meta = record.get("field_meta", {}) if isinstance(record.get("field_meta"), dict) else {}

        ordered_names: list[str] = [
            name for name in self.builtin_field_map if name != NOTES_FIELD_NAME and name in fields
        ]
        ordered_names.extend(name for name in custom_fields if name in fields and name not in ordered_names)
        ordered_names.extend(
            name
            for name in fields
            if name != NOTES_FIELD_NAME and name not in ordered_names
        )

        locked_fields = self._normalize_lock_list(record.get("locked_fields"))
        locked_set = set(locked_fields)

        entries: list[dict[str, Any]] = []
        for field_name in ordered_names:
            metadata = field_meta.get(field_name, {}) if isinstance(field_meta.get(field_name), dict) else {}
            entries.append(
                {
                    "name": field_name,
                    "value": fields[field_name],
                    "is_custom": field_name in custom_fields,
                    "locked": field_name in locked_set,
                    "description": str((custom_fields.get(field_name) or {}).get("description") or "")
                    if field_name in custom_fields
                    else self.builtin_field_map.get(field_name, ""),
                    "updated_at": str(metadata.get("updated_at") or ""),
                    "updated_by": str(metadata.get("updated_by") or ""),
                    "actor_name": str(metadata.get("actor_name") or ""),
                    "source_kind": str(metadata.get("source_kind") or ""),
                    "evidence": str(metadata.get("evidence") or ""),
                }
            )

        raw_notes = fields.get(NOTES_FIELD_NAME)
        notes = raw_notes if isinstance(raw_notes, list) else ([str(raw_notes)] if raw_notes else [])
        notes_meta = field_meta.get(NOTES_FIELD_NAME, {}) if isinstance(field_meta.get(NOTES_FIELD_NAME), dict) else {}
        session_info = self.describe_session(session_id)

        return {
            "key": session_key,
            "user_id": user_id,
            "session_id": session_id,
            "platform": session_info["platform"],
            "chat_type": session_info["chat_type"],
            "subject_name": str(record.get("subject_name") or ""),
            "display_name": self.primary_display_name(record),
            "created_at": str(record.get("created_at") or ""),
            "updated_at": str(record.get("updated_at") or ""),
            "fields": entries,
            "notes": notes,
            "notes_meta": {
                "updated_at": str(notes_meta.get("updated_at") or ""),
                "updated_by": str(notes_meta.get("updated_by") or ""),
                "locked": NOTES_FIELD_NAME in locked_set,
            },
            "locked_fields": locked_fields,
            "locked_count": len(locked_fields),
            "completeness": self._record_completeness(record),
            "field_lock_scope": self.field_lock_scope,
            "field_lock_scope_label": self.field_lock_scope_label(),
            "available_fields": [
                {
                    "name": name,
                    "description": description,
                    "is_custom": False,
                    "locked": name in locked_set,
                    "filled": bool(fields.get(name)),
                }
                for name, description in self.builtin_field_map.items()
            ]
            + [
                {
                    "name": name,
                    "description": str((meta or {}).get("description") or ""),
                    "is_custom": True,
                    "locked": name in locked_set,
                    "filled": bool(fields.get(name)),
                }
                for name, meta in custom_fields.items()
            ],
            "summary": self.format_profile_summary(user_id, session_id or None),
        }

    def delete_profile_by_key(self, session_key: str, actor: AuditActor) -> OperationResult:
        record = self.profiles.get(session_key)
        if not isinstance(record, dict):
            return OperationResult(False, "画像不存在")
        session_id, user_id = self.split_profile_key(session_key, record)
        old_fields = record.get("fields", {})
        subject_name = str(record.get("subject_name") or "")
        self.profiles.pop(session_key, None)
        self._save_profiles()
        self._append_audit(
            action="clear_profile",
            user_id=user_id,
            subject_name=subject_name,
            session_id=session_id or None,
            actor=actor,
            field_name="*",
            source_kind="webui_delete",
            evidence="",
            old_value=old_fields,
            new_value="",
        )
        return OperationResult(True, f"已删除画像 {session_key}", True)

    def merge_profile_records(
        self,
        *,
        source_key: str,
        target_key: str,
        actor: AuditActor,
        drop_source: bool = True,
    ) -> OperationResult:
        if source_key == target_key:
            return OperationResult(False, "源画像与目标画像相同")
        source = self.profiles.get(source_key)
        if not isinstance(source, dict):
            return OperationResult(False, f"源画像 {source_key} 不存在")
        target = self.profiles.get(target_key)
        self.create_backup("pre-merge")

        if not isinstance(target, dict):
            session_id, user_id = self.split_profile_key(target_key, {})
            merged = json.loads(json.dumps(source, ensure_ascii=False))
            merged["subject_user_id"] = user_id or merged.get("subject_user_id")
            self.profiles[target_key] = merged
        else:
            merged, _ = self._merge_profile_record(target, source)
            merged["subject_user_id"] = target.get("subject_user_id") or merged.get("subject_user_id")
            self.profiles[target_key] = merged

        if drop_source:
            self.profiles.pop(source_key, None)
        self._save_profiles()

        target_session_id, target_user_id = self.split_profile_key(target_key, self.profiles.get(target_key, {}))
        self._append_audit(
            action="merge_profile",
            user_id=target_user_id,
            subject_name=str(self.profiles.get(target_key, {}).get("subject_name") or ""),
            session_id=target_session_id or None,
            actor=actor,
            field_name="*",
            source_kind="profile_merge",
            evidence=f"{source_key} -> {target_key}",
            old_value=source_key,
            new_value=target_key,
        )
        return OperationResult(True, f"已把 {source_key} 合并进 {target_key}", True)

    def _filter_audit_entries(
        self,
        *,
        user_id: str = "",
        session_id: str | None = None,
        action: str = "",
        actor_type: str = "",
        keyword: str = "",
        include_rotated: bool = False,
    ) -> list[dict[str, Any]]:
        matched: list[dict[str, Any]] = []
        for entry in self._iter_audit_entries(include_rotated=include_rotated):
            if user_id and str(entry.get("subject_user_id") or "") != str(user_id):
                continue
            if session_id is not None and str(entry.get("session_id") or "") != str(session_id or ""):
                continue
            if action and str(entry.get("action") or "") != action:
                continue
            if actor_type and str(entry.get("actor_type") or "") != actor_type:
                continue
            if keyword and keyword not in json.dumps(entry, ensure_ascii=False).casefold():
                continue
            matched.append(entry)
        return matched

    def read_audit(
        self,
        *,
        user_id: str = "",
        session_id: str | None = None,
        action: str = "",
        actor_type: str = "",
        query: str = "",
        limit: int = 50,
        offset: int = 0,
        include_rotated: bool = False,
    ) -> dict[str, Any]:
        keyword = str(query or "").strip().casefold()
        matched = self._filter_audit_entries(
            user_id=user_id,
            session_id=session_id,
            action=action,
            actor_type=actor_type,
            keyword=keyword,
            include_rotated=include_rotated,
        )
        matched.reverse()
        total = len(matched)
        limit = max(1, min(int(limit or 50), 500))
        offset = max(0, min(int(offset or 0), total))
        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "items": matched[offset : offset + limit],
        }

    AUDIT_CSV_COLUMNS = (
        "at",
        "session_id",
        "subject_user_id",
        "subject_name",
        "actor_type",
        "actor_id",
        "actor_name",
        "action",
        "field_name",
        "source_kind",
        "evidence",
        "old_value",
        "new_value",
    )

    def export_audit_csv(
        self,
        *,
        user_id: str = "",
        session_id: str | None = None,
        action: str = "",
        actor_type: str = "",
        query: str = "",
        limit: int = 5000,
        include_rotated: bool = True,
    ) -> str:
        """Render the (filtered) audit trail as Excel-friendly CSV text."""
        matched = self._filter_audit_entries(
            user_id=user_id,
            session_id=session_id,
            action=action,
            actor_type=actor_type,
            keyword=str(query or "").strip().casefold(),
            include_rotated=include_rotated,
        )
        matched.reverse()
        capped = matched[: max(1, min(int(limit or 5000), 50000))]
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\r\n")
        writer.writerow(self.AUDIT_CSV_COLUMNS)
        for entry in capped:
            writer.writerow([str(entry.get(column) or "") for column in self.AUDIT_CSV_COLUMNS])
        return buffer.getvalue()

    ACTOR_TYPE_LABELS = {
        "user_command": "用户指令",
        "admin_command": "管理员指令",
        "llm_tool": "LLM 工具",
        "auto_extract": "自动抽取",
        "webui": "WebUI",
    }

    def actor_type_catalog(self) -> list[dict[str, str]]:
        """Authoritative actor_type list so the WebUI filter can never drift."""
        return [{"id": key, "label": label} for key, label in self.ACTOR_TYPE_LABELS.items()]

    def audit_action_catalog(self) -> list[str]:
        return [
            "upsert_field",
            "append_note",
            "delete_field",
            "delete_note",
            "lock_field",
            "unlock_field",
            "clear_profile",
            "merge_profile",
            "import_bundle",
            "restore_backup",
            "delete_backup",
        ]

    # ------------------------------------------------------------------
    # 导入 / 导出
    # ------------------------------------------------------------------
    BUNDLE_FORMAT = "profileweaver.bundle"
    BUNDLE_VERSION = 2

    @staticmethod
    def _mask_id(raw_id: str) -> str:
        text = str(raw_id or "")
        if len(text) <= 4:
            return "*" * len(text)
        return f"{text[:2]}{'*' * (len(text) - 4)}{text[-2:]}"

    def export_bundle(
        self,
        *,
        keys: list[str] | None = None,
        include_audit: bool = False,
        audit_limit: int = 500,
        mask_user_ids: bool = False,
    ) -> dict[str, Any]:
        selected_keys = [key for key in (keys or list(self.profiles)) if key in self.profiles]
        profiles: dict[str, Any] = {}
        for session_key in selected_keys:
            record = json.loads(json.dumps(self.profiles[session_key], ensure_ascii=False))
            if mask_user_ids:
                session_id, user_id = self.split_profile_key(session_key, record)
                masked_user_id = self._mask_id(user_id)
                record["subject_user_id"] = masked_user_id
                record["subject_name"] = self._mask_id(str(record.get("subject_name") or ""))
                # 字段元数据里也记着写入者身份，同样要脱敏，否则 actor_id 会把原始 ID 漏出去。
                field_meta = record.get("field_meta")
                if isinstance(field_meta, dict):
                    for meta in field_meta.values():
                        if not isinstance(meta, dict):
                            continue
                        if meta.get("actor_id"):
                            meta["actor_id"] = self._mask_id(str(meta["actor_id"]))
                        if meta.get("actor_name"):
                            meta["actor_name"] = self._mask_id(str(meta["actor_name"]))
                notes_meta = record.get("notes_meta")
                if isinstance(notes_meta, dict):
                    if notes_meta.get("actor_id"):
                        notes_meta["actor_id"] = self._mask_id(str(notes_meta["actor_id"]))
                    if notes_meta.get("actor_name"):
                        notes_meta["actor_name"] = self._mask_id(str(notes_meta["actor_name"]))
                # session_id 第 3 段是群号/私聊号，脱敏导出时同样不能原样带出去。
                masked_session_id = self._mask_session_id(session_id)
                session_key = f"{masked_session_id}_{masked_user_id}" if masked_session_id else masked_user_id
            profiles[session_key] = record

        bundle: dict[str, Any] = {
            "format": self.BUNDLE_FORMAT,
            "version": self.BUNDLE_VERSION,
            "plugin": "astrbot_plugin_profile_weaver",
            "exported_at": self.now_text(),
            "masked": bool(mask_user_ids),
            "builtin_fields": list(self.builtin_field_map.keys()),
            "field_lock_scope": self.field_lock_scope,
            "profile_count": len(profiles),
            "profiles": profiles,
        }
        if include_audit:
            entries = self.read_audit(limit=max(1, min(int(audit_limit), 500)), include_rotated=True)["items"]
            if mask_user_ids:
                for entry in entries:
                    for key_name in ("subject_user_id", "subject_name", "actor_id", "actor_name"):
                        if entry.get(key_name):
                            entry[key_name] = self._mask_id(str(entry[key_name]))
                    if entry.get("session_id"):
                        entry["session_id"] = self._mask_session_id(str(entry["session_id"]))
            bundle["audit"] = entries
        return bundle

    def _mask_session_id(self, session_id: str) -> str:
        raw = str(session_id or "")
        if not raw:
            return ""
        parts = raw.split(":")
        if len(parts) < 3:
            return self._mask_id(raw)
        parts[2] = self._mask_id(parts[2])
        return ":".join(parts)

    def import_bundle(
        self,
        payload: Any,
        *,
        mode: str = "merge",
        actor: AuditActor | None = None,
    ) -> dict[str, Any]:
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError as exc:
                return {"ok": False, "message": f"JSON 解析失败：{exc}"}
        if not isinstance(payload, dict):
            return {"ok": False, "message": "导入内容必须是 JSON 对象"}

        declared_format = str(payload.get("format") or "").strip()
        if declared_format and declared_format != self.BUNDLE_FORMAT:
            return {
                "ok": False,
                "message": f"文件格式不匹配：期望 {self.BUNDLE_FORMAT}，实际 {declared_format}。请确认导出来源。",
            }
        declared_version = payload.get("version")
        if declared_format and isinstance(declared_version, int) and declared_version > self.BUNDLE_VERSION:
            return {
                "ok": False,
                "message": f"备份版本 v{declared_version} 高于当前插件支持的 v{self.BUNDLE_VERSION}，请先升级插件。",
            }

        raw_profiles = payload.get("profiles")
        if not isinstance(raw_profiles, dict):
            candidate = {
                key: value
                for key, value in payload.items()
                if isinstance(value, dict) and not str(key).startswith("_")
            }
            raw_profiles = candidate
        if not raw_profiles:
            return {"ok": False, "message": "没有找到可导入的画像数据"}

        normalized_mode = str(mode or "merge").strip().lower()
        if normalized_mode not in {"merge", "overwrite", "replace"}:
            return {"ok": False, "message": f"未知导入模式：{mode}"}

        if payload.get("masked"):
            return {
                "ok": False,
                "message": "该备份导出时开启了脱敏，用户 ID 已不可逆，无法导入。请使用未脱敏的备份文件。",
            }

        incoming = self._normalize_profiles_payload(raw_profiles)
        if not incoming:
            return {"ok": False, "message": "导入内容无法解析为有效画像"}

        backup_path = self.create_backup(f"pre-import-{normalized_mode}")
        before_keys = set(self.profiles)

        if normalized_mode == "replace":
            self.profiles = incoming
        elif normalized_mode == "overwrite":
            merged = dict(self.profiles)
            merged.update(incoming)
            self.profiles = merged
        else:
            self.profiles, _ = self._merge_profiles(self.profiles, incoming)

        self._save_profiles()

        after_keys = set(self.profiles)
        created = len(after_keys - before_keys)
        removed = len(before_keys - after_keys)
        updated = len([key for key in incoming if key in before_keys])

        if actor is not None:
            self._append_audit(
                action="import_bundle",
                user_id="*",
                subject_name="",
                session_id=None,
                actor=actor,
                field_name="*",
                source_kind=f"import_{normalized_mode}",
                evidence=str(payload.get("exported_at") or "")[:160],
                old_value=f"{len(before_keys)} profiles",
                new_value=f"{len(after_keys)} profiles",
            )

        return {
            "ok": True,
            "message": (
                f"导入完成（{normalized_mode}）：新增 {created}，更新 {updated}，"
                f"移除 {removed}，当前共 {len(after_keys)} 条画像。"
            ),
            "mode": normalized_mode,
            "created": created,
            "updated": updated,
            "removed": removed,
            "total": len(after_keys),
            "backup": backup_path.name if backup_path else "",
        }
