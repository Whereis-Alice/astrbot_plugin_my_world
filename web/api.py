"""编辑器 HTTP 接口。

这是一组挂在插件实例上的处理器，抽成 mixin 是为了让 main.py 只留编排。
"""

from __future__ import annotations

from ..core.life_memory import LIFE_TYPES, LIFE_DOMAIN, is_life_memory

import asyncio
import functools
import hashlib
import json
import math
import time
from typing import Any

from astrbot.api.web import error_response, json_response, request
from pydantic import ValidationError
from quart import Response

from ..adapters.llm import AstrBotLLM
from ..core.config_store import HISTORY_KEEP
from ..core.defaults import (
    CORE_BUILTIN_ACTIONS,
    DEFAULT_CAPTION_PROMPT,
    DEFAULT_CAPTION_RELATION_PROMPT,
    DEFAULT_FORWARD_PROMPT,
    DEFAULT_WORLD,
)
from ..core.generator import grid_layout_nodes, spring_layout_zones
from ..core.json_actions import extract_json_object
from ..core.llm_usage import collect_llm_usage
from ..core.model_calls import model_operation
from ..core.map_inspection import inspect_routes
from ..core.models import (
    DEFAULT_INTIMACY,
    FIELD_LABELS,
    normalize_edge_keys,
    normalize_legacy_keys,
    parse_world,
)
from ..core.profile_weaver_store import AuditActor
from ..core.prompt import prompt_section_index
from ..core.social import SocialDirectory, qq_id, rule_conflicts
from ..core.timeline import build_timeline
from ..core.tool_import import build_drafts as build_tool_action_drafts
from ..core.tool_import import merge_drafts as merge_tool_action_drafts
from .life_api import LifeApiMixin


def _generation_timeout_seconds(config: Any) -> float:
    """内容生成的超时（秒）；0 = 不超时。

    整张地图 + 每个地点的动作本来就是几分钟的大活，以前写死的 120 秒会把正常生成
    当成失败。现在默认 10 分钟，实在慢可以在插件设置里调大（填 0 就不掐表）。
    """

    try:
        raw = (config or {}).get("generator_timeout_minutes")
    except Exception:
        raw = None
    try:
        minutes = float(raw)
    except (TypeError, ValueError):
        minutes = 10.0
    if minutes <= 0:
        return 0.0
    return max(60.0, min(1800.0, minutes * 60.0))


def _query_int(name: str, default: int = 0) -> int:
    """Quart MultiDict and the lightweight test dict both need to work."""

    query = request.query
    try:
        raw = query.get(name, default, type=int)
    except TypeError:
        raw = query.get(name, default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


class WebApiMixin(LifeApiMixin):
    def _register_web_apis(self, plugin_id: str) -> None:
        def register(path, handler, methods, description):
            @functools.wraps(handler)
            async def scoped_handler(*args, **kwargs):
                method = str(getattr(request, "method", methods[0])).upper()
                payload = (await request.json(default={}) or {}) if method == "POST" else request.query
                if not hasattr(payload, "get"):
                    return error_response("请求参数必须是对象")
                scope = str(payload.get("config_scope") or "").strip()
                if scope and self.engine.session_config(scope) is None:
                    return error_response("配置会话不存在，请重新选择")
                endpoint = path.split(f"/{plugin_id}/", 1)[-1]
                if endpoint.startswith("relationships"):
                    scope = ""
                if scope and method == "POST" and endpoint in {
                    "config/world", "config/schedules", "config/sessions", "reset", "restore-default",
                    "actions/purge-legacy", "presets/apply", "history/restore",
                    "presets/save", "tools/import",
                }:
                    return error_response("此操作会修改全局配置，请先切换到全局；会话配置请使用顶部保存")
                if not (endpoint in {"config", "persona/apply", "persona/review", "voice-samples"} or endpoint.startswith(("config/", "generate/", "voice-samples/", "relationships"))):
                    scope = str(payload.get("session") or payload.get("session_id") or scope)
                with self.engine.config_scope(scope):
                    if self.engine.config_scope_id and method == "POST" and endpoint in {"voice-samples/save", "persona/apply"}:
                        session = self.engine.session_config(self.engine.config_scope_id)
                        if "settings" not in (session.config_overrides or {}):
                            return error_response("请先为此会话启用并保存独立设置")
                    return await handler(*args, **kwargs)
            return self.context.register_web_api(path, scoped_handler, methods, description)
        p = plugin_id
        register(f"/{p}/config/scope", self.api_put_scope, ["POST"], "保存会话独立配置")
        register(f"/{p}/config/proactive", self.api_proactive_control, ["POST"], "全局自主搭话开关")
        register(f"/{p}/auth/status", self.api_auth_status, ["GET"], "编辑器鉴权状态")
        register(f"/{p}/auth/login", self.api_auth_login, ["POST"], "编辑器登录")
        register(f"/{p}/auth/logout", self.api_auth_logout, ["POST"], "退出登录")
        register(f"/{p}/auth/password", self.api_auth_password, ["POST"], "修改访问密码")
        register(f"/{p}/config", self.api_get_config, ["GET"], "读取全部配置")
        register(f"/{p}/config/world", self.api_put_world, ["POST"], "保存世界配置")
        register(f"/{p}/config/schedules", self.api_put_schedules, ["POST"], "保存日程")
        register(f"/{p}/config/sessions", self.api_put_sessions, ["POST"], "保存会话白名单")
        register(
            f"/{p}/actions/purge-legacy",
            self.api_purge_legacy_actions,
            ["POST"],
            "预览 / 清除全部旧版工具与指令动作",
        )
        register(f"/{p}/tools", self.api_tools, ["GET"], "可用工具列表")
        register(
            f"/{p}/tools/import",
            self.api_tools_import,
            ["POST"],
            "把 AstrBot 的工具批量导入成动作",
        )
        register(f"/{p}/generate/actions", self.api_generate_actions, ["POST"], "按区域生成动作")
        register(f"/{p}/generate/idle-actions", self.api_generate_idle_actions, ["POST"], "按人设生成闲置动作")
        register(
            f"/{p}/generate/world-actions",
            self.api_generate_world_actions,
            ["POST"],
            "按人设与整张地图生成动作",
        )
        register(
            f"/{p}/generate/generic-actions",
            self.api_generate_generic_actions,
            ["POST"],
            "生成一批通用普通动作（不看人设，地图可选）",
        )
        register(f"/{p}/generate/nodes", self.api_generate_nodes, ["POST"], "按区域生成地点")
        register(
            f"/{p}/generate/zone",
            self.api_generate_zone,
            ["POST"],
            "重做一个区域的草稿（地点 + 可选地点专属动作）",
        )
        register(
            f"/{p}/generate/zone-chunk",
            self.api_generate_zone_chunk,
            ["POST"],
            "分块重建地图：一次生成一个区域（可带动作）",
        )
        register(
            f"/{p}/generate/zones",
            self.api_generate_zones,
            ["POST"],
            "按人设与已有世界生成新区域",
        )
        register(
            f"/{p}/generate/world-map",
            self.api_generate_world_map,
            ["POST"],
            "按人设一键重建整张地图（区域 / 地点 / 路线）",
        )
        register(
            f"/{p}/map/layout",
            self.api_map_layout,
            ["POST"],
            "按当前画布形状重新铺开地图（只出坐标，不落库）",
        )
        register(
            f"/{p}/generate/schedules",
            self.api_generate_schedules,
            ["POST"],
            "按人设与生活节奏生成日程",
        )
        register(
            f"/{p}/generate/events",
            self.api_generate_events,
            ["POST"],
            "按人设与最近经历生成事件",
        )
        register(
            f"/{p}/events/deliver",
            self.api_deliver_event,
            ["POST"],
            "把挑好的事件草稿投递给她",
        )
        register(f"/{p}/reload", self.api_reload, ["POST"], "热加载配置")
        register(f"/{p}/restore-default", self.api_restore, ["POST"], "恢复默认配置")
        register(f"/{p}/life", self.api_life, ["GET"], "生活事项与持续进展")
        register(f"/{p}/life/update", self.api_life_update, ["POST"], "管理生活事项")
        register(f"/{p}/memories/evidence", self.api_memory_evidence, ["GET"], "记忆来源与人工修订")
        register(f"/{p}/state", self.api_state, ["GET"], "实时状态")
        register(f"/{p}/states", self.api_states, ["GET"], "所有会话的简要状态")
        register(f"/{p}/state/action", self.api_state_action, ["POST"], "手动触发动作")
        register(f"/{p}/logs", self.api_logs, ["GET"], "事件日志")
        register(f"/{p}/model-calls", self.api_model_calls, ["GET"], "模型调用与 token 用量")
        register(f"/{p}/decision-traces", self.api_decision_traces, ["GET"], "决策与执行记录")
        register(f"/{p}/social-learning", self.api_social_learning, ["GET"], "相处习惯与会话词典")
        register(f"/{p}/social-learning/update", self.api_social_update, ["POST"], "修正相处习惯与会话词典")
        register(f"/{p}/map/inspect", self.api_map_inspect, ["POST"], "检查草稿路线、可达性与单向出口")
        register(f"/{p}/ticks", self.api_ticks, ["GET"], "运行记录：最近几拍 tick 的明细")
        register(f"/{p}/logs/export", self.api_logs_export, ["GET"], "导出事件日志")
        register(f"/{p}/memories", self.api_memories, ["GET"], "记忆列表")
        register(f"/{p}/thoughts", self.api_thoughts, ["GET"], "内心活动：她留下的心里话")
        register(
            f"/{p}/thoughts/clear",
            self.api_thoughts_clear,
            ["POST"],
            "清空某个会话留下的心里话",
        )
        register(f"/{p}/memories/create", self.api_memory_create, ["POST"], "新增记忆")
        register(f"/{p}/memories/update", self.api_memory_update, ["POST"], "修改记忆")
        register(f"/{p}/memories/delete", self.api_memory_delete, ["POST"], "删除记忆")
        register(f"/{p}/memories/delete-batch", self.api_memory_delete_batch, ["POST"], "批量删除记忆")
        register(f"/{p}/memories/clear", self.api_memory_clear, ["POST"], "清空记忆")
        register(f"/{p}/logs/delete-batch", self.api_logs_delete_batch, ["POST"], "批量删除日志")
        register(f"/{p}/logs/clear", self.api_logs_clear, ["POST"], "清空日志")
        register(f"/{p}/memories/stats", self.api_memory_stats, ["GET"], "记忆统计")
        register(f"/{p}/memories/export", self.api_memory_export, ["GET"], "导出记忆")
        register(f"/{p}/memories/import", self.api_memory_import, ["POST"], "导入记忆")
        register(f"/{p}/prompt", self.api_prompt, ["GET"], "预览提示词")
        # 注意：配置改动历史占用了 /history（还有 /history/item 等子路径），
        # 数值历史必须另起一条路径，否则两条会互相顶掉。
        register(f"/{p}/state/history", self.api_state_history, ["GET"], "数值历史")
        register(f"/{p}/defaults", self.api_defaults, ["GET"], "内置默认文案")
        register(f"/{p}/backup", self.api_backup, ["POST"], "备份配置")
        register(f"/{p}/presets", self.api_presets, ["GET"], "预设列表")
        register(f"/{p}/presets/save", self.api_preset_save, ["POST"], "把当前配置存成预设")
        register(f"/{p}/presets/new-default", self.api_preset_new_default, ["POST"], "用内置默认世界新建预设")
        register(f"/{p}/presets/apply", self.api_preset_apply, ["POST"], "应用预设")
        register(f"/{p}/presets/delete", self.api_preset_delete, ["POST"], "删除预设")
        register(f"/{p}/presets/rename", self.api_preset_rename, ["POST"], "重命名预设")
        register(f"/{p}/presets/json", self.api_preset_json, ["GET", "POST"], "读写预设 JSON")
        register(f"/{p}/history", self.api_history, ["GET"], "配置改动历史")
        register(f"/{p}/history/item", self.api_history_item, ["GET"], "看一条历史")
        register(f"/{p}/history/restore", self.api_history_restore, ["POST"], "恢复到这个版本")
        register(f"/{p}/history/delete", self.api_history_delete, ["POST"], "删除一条历史")
        register(f"/{p}/history/clear", self.api_history_clear, ["POST"], "清空历史")
        register(f"/{p}/events", self.api_events, ["GET"], "事件线索与能力值")
        register(f"/{p}/persona-brief", self.api_persona_brief, ["GET", "POST"], "简易人设")
        register(f"/{p}/voice-samples", self.api_voice_samples, ["GET"], "声音样例")
        register(
            f"/{p}/voice-samples/generate",
            self.api_voice_samples_generate,
            ["POST"],
            "生成声音样例候选",
        )
        register(
            f"/{p}/voice-samples/from-chat",
            self.api_voice_samples_from_chat,
            ["POST"],
            "从近期聊天里挑样例候选",
        )
        register(
            f"/{p}/voice-samples/save",
            self.api_voice_samples_save,
            ["POST"],
            "保存挑中的声音样例",
        )
        register(f"/{p}/persona/review", self.api_persona_review, ["POST"], "体检人设")
        register(
            f"/{p}/persona/apply", self.api_persona_apply, ["POST"], "应用挑中的改动"
        )
        register(
            f"/{p}/eval-script", self.api_eval_script, ["POST"], "生成测评剧本"
        )
        register(f"/{p}/eval-run", self.api_eval_run, ["POST"], "跑测评（并发）")
        register(f"/{p}/persona-source", self.api_persona_source, ["GET"], "读 AstrBot 当前给这个会话的人设")
        register(f"/{p}/profile/people", self.api_profile_people, ["GET"], "通讯录：认识的人")
        register(f"/{p}/profile/person", self.api_profile_person, ["GET", "POST"], "通讯录：一个人的画像")
        register(
            f"/{p}/profile/contacts",
            self.api_profile_people,
            ["GET"],
            "统一通讯录：按联系人聚合关系与画像",
        )
        register(
            f"/{p}/profile/contact",
            self.api_profile_person,
            ["GET", "POST"],
            "统一通讯录：一个联系人的完整画像",
        )
        register(
            f"/{p}/profile/contact/records",
            self.api_profile_contact_records,
            ["GET", "POST"],
            "统一通讯录：联系人的画像记录",
        )
        register(f"/{p}/profile/fact", self.api_profile_fact, ["POST"], "通讯录：改事实")
        register(f"/{p}/profile/bond", self.api_profile_bond, ["POST"], "通讯录：改关系")
        register(f"/{p}/profile/affinity", self.api_profile_affinity, ["POST"], "通讯录：改好感度")
        register(f"/{p}/relationships", self.api_relationships, ["GET"], "关系与好感：按 QQ 汇总")
        register(f"/{p}/relationships/person", self.api_relationship_person, ["GET"], "关系与好感：会话现状")
        register(f"/{p}/relationships/apply", self.api_relationship_apply, ["POST"], "设置统一起点并应用到所有会话")
        register(f"/{p}/relationships/session", self.api_relationship_session, ["POST"], "只调整一个会话的关系与好感")
        register(f"/{p}/relationships/baseline", self.api_relationship_baseline, ["POST"], "移除统一起点，保留会话现状")
        register(f"/{p}/relationships/rules", self.api_relationship_rules, ["GET", "POST"], "关系类型、相处档位与发展规则")
        register(
            f"/{p}/profile/grudge",
            self.api_profile_grudge,
            ["POST"],
            "通讯录：她记着的一笔账",
        )
        register(f"/{p}/profile/forget", self.api_profile_forget, ["POST"], "通讯录：忘掉一个人")
        register(
            f"/{p}/profile/forget-all",
            self.api_profile_forget_all,
            ["POST"],
            "通讯录：清空（忘掉全部）",
        )
        register(f"/{p}/profile/consolidate", self.api_profile_consolidate, ["POST"], "立刻整理一次（记忆 + 画像）")
        register(
            f"/{p}/profile/weaver",
            self.api_profile_weaver,
            ["GET"],
            "心迹画像桥：能不能搬、有多少人的画像",
        )
        register(
            f"/{p}/profile/weaver/import",
            self.api_profile_weaver_import,
            ["POST"],
            "把心迹画像的数据搬进本插件的通讯录",
        )
        register(f"/{p}/profile/weaver/meta", self.api_profile_weaver_meta, ["GET"], "通讯录画像：能力与字段")
        register(
            f"/{p}/profile/weaver/library",
            self.api_profile_weaver_library,
            ["GET"],
            "通讯录画像：画像库",
        )
        register(
            f"/{p}/profile/weaver/person",
            self.api_profile_weaver_person,
            ["GET"],
            "通讯录画像：完整字段",
        )
        register(
            f"/{p}/profile/weaver/key",
            self.api_profile_weaver_key,
            ["GET"],
            "通讯录画像：按记录 key 查看完整字段",
        )
        register(
            f"/{p}/profile/weaver/field",
            self.api_profile_weaver_field,
            ["POST"],
            "通讯录画像：字段写改删与锁定",
        )
        register(
            f"/{p}/profile/weaver/history",
            self.api_profile_weaver_history,
            ["GET"],
            "通讯录画像：字段历史",
        )
        register(
            f"/{p}/profile/weaver/stats",
            self.api_profile_weaver_stats,
            ["GET"],
            "通讯录画像：统计",
        )
        register(
            f"/{p}/profile/weaver/audit",
            self.api_profile_weaver_audit,
            ["GET"],
            "通讯录画像：审计",
        )
        register(
            f"/{p}/profile/weaver/audit-csv",
            self.api_profile_weaver_audit_csv,
            ["GET"],
            "通讯录画像：导出审计 CSV",
        )
        register(
            f"/{p}/profile/weaver/export",
            self.api_profile_weaver_export,
            ["GET"],
            "通讯录画像：导出",
        )
        register(
            f"/{p}/profile/weaver/import-bundle",
            self.api_profile_weaver_import_bundle,
            ["POST"],
            "通讯录画像：导入 bundle",
        )
        register(
            f"/{p}/profile/weaver/backup",
            self.api_profile_weaver_backup,
            ["GET", "POST"],
            "通讯录画像：备份管理",
        )
        register(
            f"/{p}/profile/weaver/backup-download",
            self.api_profile_weaver_backup_download,
            ["GET"],
            "通讯录画像：下载备份",
        )
        register(
            f"/{p}/profile/weaver/merge",
            self.api_profile_weaver_merge,
            ["POST"],
            "通讯录画像：合并重复画像",
        )
        register(
            f"/{p}/profile/weaver/delete",
            self.api_profile_weaver_delete,
            ["POST"],
            "通讯录画像：删除一条或多条画像",
        )
        register(f"/{p}/extensions", self.api_extensions, ["GET", "POST"], "扩展包：设置与数据")

    # ---------------- 通讯录（用户画像）----------------

    def _profile_scope(self, value: Any) -> str:
        return self._scope(value)

    def _relationship_rules(self) -> dict[str, Any]:
        config = self.engine.world.profile
        return {"bonds": [item.model_dump(mode="json") for item in config.bonds],
                "levels": [item.model_dump(mode="json") for item in config.levels],
                "affinity_min": config.affinity_min, "affinity_max": config.affinity_max,
                "default_bond": config.default_bond, "affinity_initial": config.affinity_initial}

    async def api_relationship_rules(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        profile = self.engine.world.profile.model_dump(mode="json")
        fingerprint = hashlib.sha256(json.dumps(profile, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if str(getattr(request, "method", "GET")).upper() != "POST":
            return json_response({"profile": profile, "fingerprint": fingerprint,
                "actions": [{"id": action.id, "name": action.name or action.id, "enabled": action.enabled} for action in self.engine.world.actions]})
        if payload.get("fingerprint") != fingerprint:
            return error_response("关系规则已经变更，请重新打开后再修改")
        candidate = payload.get("profile")
        if not isinstance(candidate, dict):
            return error_response("关系规则必须是对象")
        try:
            from ..core.models import ProfileConfig

            parsed = ProfileConfig.model_validate(candidate)
            names = [bond.name.strip() for bond in parsed.bonds]
            if not names or any(not name for name in names) or len(set(names)) != len(names):
                raise ValueError("关系名称不能为空或重复")
            if parsed.default_bond not in names:
                raise ValueError("默认初始关系必须保留在关系表中")
            if any(bond.name != bond.name.strip() for bond in parsed.bonds):
                raise ValueError("关系名称前后不能包含空白")
            numbers = [parsed.affinity_min, parsed.affinity_max, parsed.affinity_initial,
                       *[value for level in parsed.levels for value in (level.min_affinity, level.max_affinity)]]
            if not all(math.isfinite(value) for value in numbers):
                raise ValueError("好感与档位边界必须是有限数值")
            if not parsed.affinity_min <= parsed.affinity_initial <= parsed.affinity_max:
                raise ValueError("初始好感必须位于好感范围内")
            if not parsed.levels or parsed.affinity_min >= parsed.affinity_max:
                raise ValueError("至少保留一个档位，且好感下限需小于上限")
            level_names = [level.name.strip() for level in parsed.levels]
            if any(not name for name in level_names) or len(set(level_names)) != len(level_names):
                raise ValueError("相处档位名称不能为空或重复")
            previous_end = None
            for level in parsed.levels:
                if level.min_affinity >= level.max_affinity or previous_end is not None and level.min_affinity < previous_end:
                    raise ValueError("档位区间需从低到高排列，不能倒置或重叠")
                previous_end = level.max_affinity
            if any(bond.floor > bond.cap or bond.cap >= len(parsed.levels) or bond.floor < 0 for bond in parsed.bonds):
                raise ValueError("关系最低档和最高档需落在分级表内，且最低档不能高于最高档")
            current = self.db.raw._query("SELECT id,group_id,user_id,type FROM user_bond WHERE status='current'")
            # Shared lives use their configuration owner's rules. An explicit
            # profile snapshot in that owner's settings never follows global edits.
            inheriting = set()
            for sid in {row["group_id"] for row in current}:
                with self.engine.config_scope(sid):
                    session = self.engine.session_config(self.engine.config_scope_id)
                    settings = (getattr(session, "config_overrides", None) or {}).get("settings", {})
                    if "profile" not in settings:
                        inheriting.add(sid)
            current = [row for row in current if row["group_id"] in inheriting]
            used = {row["type"] for row in current}
            used.update(name for baseline in self.db.raw.relationship_baselines() for name in baseline.get("relations", []))
            removed = used - set(names)
            if removed:
                raise ValueError("这些关系正在使用，请先调整联系人与统一起点后再删除或改名：" + "、".join(sorted(removed)))
            unique_slots = {bond.slot for bond in parsed.bonds if bond.unique and bond.slot}
            old_identity, old_unique = rule_conflicts(self.engine.world.profile, current)
            new_identity, new_unique = rule_conflicts(parsed, current)
            if new_unique - old_unique:
                raise ValueError("此唯一槽位在现有会话中已由多人占用，请先调整关系")
            if new_identity - old_identity:
                raise ValueError("修改后，同一个人的现有关系会属于相同替代组或槽位，请先调整关系")
            baseline_slots = {}
            for baseline in self.db.raw.relationship_baselines():
                if not parsed.affinity_min <= baseline["affinity"] <= parsed.affinity_max:
                    raise ValueError("现有统一起点的好感超出新范围，请先调整起点")
                previous = []
                if parsed.default_bond in baseline.get("relations", []) and len(baseline["relations"]) > 1:
                    raise ValueError("现有统一起点将同时包含默认关系和正式身份，请先调整起点")
                for name in baseline.get("relations", []):
                    bond = parsed.bond_by_name(name)
                    if bond is not None:
                        if any(bond.group and old.group == bond.group or bond.slot and old.slot == bond.slot for old in previous):
                            raise ValueError("修改后，统一起点中的身份将属于同一替代组或槽位，请先调整起点")
                        previous.append(bond)
                    if bond is not None and bond.slot in unique_slots:
                        if bond.slot in baseline_slots and baseline_slots[bond.slot] != baseline["user_id"]:
                            raise ValueError("此唯一槽位已在多个 QQ 的统一起点中使用，请先调整起点")
                        baseline_slots[bond.slot] = baseline["user_id"]
            world = {**self.store.raw_world(), "profile": parsed.model_dump(mode="json")}
            warnings = self.store.save_world(world, reason="调整关系与好感规则")
            with self.db.raw.atomic():
                for row in current:
                    bond = parsed.bond_by_name(row["type"])
                    self.db.raw._execute("UPDATE user_bond SET slot=? WHERE id=?", (bond.slot, row["id"]))
            self.engine.reload_config()
        except (ValueError, ValidationError) as exc:
            return error_response(str(exc))
        return json_response({"ok": True, "warnings": warnings})

    async def api_relationships(self):
        guard = self._guard()
        if guard is not None:
            return guard
        data = SocialDirectory(self.engine).catalog(q=str(request.query.get("q", "")),
            page=_query_int("page", 1), size=_query_int("size", 24),
            filter_by=str(request.query.get("filter", "all")),
            sort=str(request.query.get("sort", "recent")))
        data["rules"] = self._relationship_rules()
        return json_response(data)

    async def api_relationship_person(self):
        guard = self._guard()
        if guard is not None:
            return guard
        try:
            uid = qq_id(request.query.get("user_id"))
        except ValueError as exc:
            return error_response(str(exc))
        data = SocialDirectory(self.engine).person(uid)
        data["rules"] = self._relationship_rules()
        for item in data["sessions"]:
            with self.engine.config_scope(item["session"]):
                item["rules"] = self._relationship_rules()
        return json_response(data)

    async def _relationship_apply(self, only_session: str = ""):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        try:
            return json_response(SocialDirectory(self.engine).apply(payload, only_session=only_session))
        except ValueError as exc:
            return error_response(str(exc))

    async def api_relationship_apply(self):
        return await self._relationship_apply()

    async def api_relationship_session(self):
        payload = await request.json(default={}) or {}
        session = str(payload.get("session") or "").strip()
        if not session:
            return error_response("请选择实际会话")
        return await self._relationship_apply(session)

    async def api_relationship_baseline(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        try:
            uid = qq_id(payload.get("user_id"))
            with self.db.raw.atomic():
                baseline = self.db.raw.relationship_baseline(uid)
                if baseline is None or payload.get("revision") != baseline["revision"]:
                    raise ValueError("统一起点已变更，请刷新后再操作")
                self.db.raw.delete_relationship_baseline(uid)
        except ValueError as exc:
            return error_response(str(exc))
        return json_response({"ok": True, "person": SocialDirectory(self.engine).person(uid)})

    def _weaver_service(self):
        return getattr(self.engine, "profile_weaver_service", None)

    @staticmethod
    def _weaver_actor() -> AuditActor:
        return AuditActor(
            actor_type="webui",
            actor_id="dashboard",
            actor_name="通讯录",
        )

    def _weaver_write_guard(self, service) -> Any:
        if service is None:
            return error_response("通讯录画像内核未就绪")
        if not service.webui_allow_edit:
            return error_response("通讯录画像当前是只读模式")
        return None

    async def api_profile_weaver_meta(self):
        guard = self._guard()
        if guard is not None:
            return guard
        service = self._weaver_service()
        if service is None:
            return json_response({"enabled": False, "message": "通讯录画像内核未就绪"})
        return json_response(service.meta())

    async def api_profile_weaver_library(self):
        guard = self._guard()
        if guard is not None:
            return guard
        service = self._weaver_service()
        if service is None:
            return error_response("通讯录画像内核未就绪")
        session_id = self._profile_scope(request.query.get("session", ""))
        result = service.store.list_profiles(
            query=str(request.query.get("q", "") or ""),
            chat_type=str(request.query.get("chat_type", "all") or "all"),
            platform=str(request.query.get("platform", "all") or "all"),
            session_id=session_id if service.session_based else "",
            field=str(request.query.get("field", "") or ""),
            sort=str(request.query.get("sort", "updated_desc") or "updated_desc"),
            page=_query_int("page", 1) or 1,
            page_size=_query_int("page_size", 40) or 40,
            locked_only=str(request.query.get("locked_only", "")).lower()
            in {"1", "true", "on"},
        )
        result["platforms"] = service.store.list_platforms()
        return json_response(result)

    async def api_profile_weaver_person(self):
        guard = self._guard()
        if guard is not None:
            return guard
        service = self._weaver_service()
        if service is None:
            return error_response("通讯录画像内核未就绪")
        session_id = self._profile_scope(request.query.get("session", ""))
        user_id = str(request.query.get("user_id", "") or "")
        key = str(request.query.get("key", "") or "").strip()
        if key:
            detail = service.detail_by_key(key)
            if not detail:
                return error_response("画像不存在")
            return json_response(
                {
                    "session": detail.get("session_id") or session_id,
                    "user_id": detail.get("user_id") or user_id,
                    "profile": detail,
                    "meta": service.meta(),
                }
            )
        if not session_id or not user_id:
            return error_response("缺少 session 或 user_id")
        detail = service.detail_for(session_id, user_id)
        return json_response(
            {
                "session": session_id,
                "user_id": user_id,
                "profile": detail,
                "meta": service.meta(),
            }
        )

    async def api_profile_weaver_key(self):
        guard = self._guard()
        if guard is not None:
            return guard
        service = self._weaver_service()
        if service is None:
            return error_response("通讯录画像内核未就绪")
        key = str(request.query.get("key", "") or "").strip()
        if not key:
            return error_response("缺少 key")
        detail = service.detail_by_key(key)
        if not detail:
            return error_response("画像不存在")
        return json_response({"key": key, "profile": detail, "meta": service.meta()})

    async def api_profile_weaver_field(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        service = self._weaver_service()
        write_guard = self._weaver_write_guard(service)
        if write_guard is not None:
            return write_guard
        record_key = str(payload.get("key") or "").strip()
        if record_key:
            detail = service.detail_by_key(record_key)
            session_id = str(detail.get("session_id") or "")
            user_id = str(detail.get("user_id") or "")
        else:
            session_id = self._profile_scope(payload.get("session"))
            user_id = str(payload.get("user_id") or "")
        field_name = str(payload.get("field") or "").strip()
        action = str(payload.get("action") or "upsert")
        if (not record_key and not session_id) or not user_id or not field_name:
            return error_response("缺少 session、user_id 或 field")
        actor = self._weaver_actor()
        detail = (
            service.detail_by_key(record_key)
            if record_key
            else service.detail_for(session_id, user_id)
        )
        subject_name = str(
            (detail or {}).get("subject_name")
            or (detail or {}).get("display_name")
            or user_id
        )
        session_key = service.profile_key(session_id, user_id)
        if action == "upsert":
            result = service.store.upsert_field(
                user_id=user_id,
                subject_name=subject_name,
                field_name=field_name,
                value=str(payload.get("value") or ""),
                session_id=session_id if service.session_based else None,
                actor=actor,
                source_kind="webui_edit",
                evidence="通讯录手动编辑",
                allow_custom_field=service.allow_user_custom_fields,
                field_description=str(payload.get("description") or "")
                or "通讯录中创建的自定义字段",
            )
        elif action == "delete":
            result = service.store.delete_field(
                user_id=user_id,
                session_id=session_id if service.session_based else None,
                field_selector=field_name,
                actor=actor,
                source_kind="webui_delete",
                evidence="通讯录手动删除",
            )
        elif action in {"lock", "unlock"}:
            result = service.store.set_field_lock(
                user_id=user_id,
                subject_name=subject_name,
                session_id=session_id if service.session_based else None,
                field_name=field_name,
                locked=action == "lock",
                actor=actor,
            )
        else:
            return error_response("不认识的画像字段操作")
        if not result.ok:
            return error_response(result.message)
        refreshed = (
            service.detail_by_key(record_key)
            if record_key
            else service.store.get_profile_detail(session_key)
        )
        return json_response(
            {
                "ok": True,
                "message": result.message,
                "profile": refreshed,
            }
        )

    async def api_profile_weaver_history(self):
        guard = self._guard()
        if guard is not None:
            return guard
        service = self._weaver_service()
        if service is None:
            return error_response("通讯录画像内核未就绪")
        key = str(request.query.get("key", "") or "").strip()
        detail = service.detail_by_key(key) if key else {}
        session_id = (
            str(detail.get("session_id") or "")
            if key
            else self._profile_scope(request.query.get("session", ""))
        )
        user_id = (
            str(detail.get("user_id") or "")
            if key
            else str(request.query.get("user_id", "") or "")
        )
        field_name = str(request.query.get("field", "") or "").strip()
        if not user_id or not field_name:
            return error_response("缺少 user_id 或 field")
        items = service.field_history(
            user_id=user_id,
            session_id=session_id if service.session_based else None,
            field_name=field_name,
            limit=min(100, max(1, _query_int("limit", 20) or 20)),
            include_rotated=True,
        )
        return json_response({"field": field_name, "items": items, "total": len(items)})

    async def api_profile_weaver_stats(self):
        guard = self._guard()
        if guard is not None:
            return guard
        service = self._weaver_service()
        if service is None:
            return error_response("通讯录画像内核未就绪")
        stats = service.stats()
        audit_page = service.audit(limit=1, offset=0, include_rotated=True)
        stats["audit_count"] = int(audit_page.get("total") or 0)
        custom_counts = stats.get("custom_field_counts") or {}
        stats["custom_fields"] = [
            {"name": name, "count": int(count)}
            for name, count in sorted(
                custom_counts.items(),
                key=lambda item: (-int(item[1]), str(item[0])),
            )
        ]
        coverage = [
            {
                "name": name,
                "count": int((stats.get("field_counts") or {}).get(name, 0)),
                "ratio": round(
                    int((stats.get("field_counts") or {}).get(name, 0))
                    / max(1, int(stats.get("profile_count") or 0))
                    * 100,
                    1,
                ),
            }
            for name in service.store.builtin_field_map
        ]
        coverage.sort(key=lambda item: (-item["count"], item["name"]))
        return json_response({"stats": stats, "coverage": coverage})

    async def api_profile_weaver_audit(self):
        guard = self._guard()
        if guard is not None:
            return guard
        service = self._weaver_service()
        if service is None:
            return error_response("通讯录画像内核未就绪")
        session_id = self._profile_scope(request.query.get("session", ""))
        result = service.audit(
            user_id=str(request.query.get("user_id", "") or ""),
            session_id=session_id if service.session_based else None,
            action=str(request.query.get("action", "") or ""),
            actor_type=str(request.query.get("actor_type", "") or ""),
            query=str(request.query.get("q", "") or ""),
            limit=min(500, max(1, _query_int("limit", 100) or 100)),
            offset=max(0, _query_int("offset", 0)),
            include_rotated=str(
                request.query.get("include_rotated", "true")
            ).lower()
            in {"1", "true", "on", "yes"},
        )
        return json_response(result)

    async def api_profile_weaver_audit_csv(self):
        guard = self._guard()
        if guard is not None:
            return guard
        service = self._weaver_service()
        if service is None:
            return error_response("通讯录画像内核未就绪")
        session_id = self._profile_scope(request.query.get("session", ""))
        text = service.store.export_audit_csv(
            user_id=str(request.query.get("user_id", "") or ""),
            session_id=session_id if service.session_based else None,
            action=str(request.query.get("action", "") or ""),
            actor_type=str(request.query.get("actor_type", "") or ""),
            query=str(request.query.get("q", "") or ""),
            limit=min(
                50000,
                max(1, _query_int("limit", 5000) or 5000),
            ),
            include_rotated=True,
        )
        return Response(
            "\ufeff" + text,
            status=200,
            mimetype="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="my-world-profile-audit.csv"'
            },
        )

    async def api_profile_weaver_export(self):
        guard = self._guard()
        if guard is not None:
            return guard
        service = self._weaver_service()
        if service is None:
            return error_response("通讯录画像内核未就绪")
        raw_keys = str(request.query.get("keys", "") or "").strip()
        keys = [item.strip() for item in raw_keys.split(",") if item.strip()]
        payload = service.export_bundle(
            keys=keys or None,
            include_audit=str(request.query.get("include_audit", "")).lower()
            in {"1", "true", "on"},
            mask_user_ids=(
                str(request.query.get("mask", "")).lower() in {"1", "true", "on"}
                if "mask" in request.query
                else service.export_mask_user_ids
            ),
        )
        return json_response(payload)

    async def api_profile_weaver_import_bundle(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        service = self._weaver_service()
        write_guard = self._weaver_write_guard(service)
        if write_guard is not None:
            return write_guard
        bundle = payload.get("payload")
        if not isinstance(bundle, dict):
            return error_response("导入内容必须是画像 bundle JSON")
        result = service.import_bundle(
            bundle,
            mode=str(payload.get("mode") or "merge"),
        )
        if not result.get("ok"):
            return error_response(str(result.get("message") or "导入失败"))
        return json_response(result)

    async def api_profile_weaver_backup(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload if payload else request.query)
        if guard is not None:
            return guard
        service = self._weaver_service()
        if service is None:
            return error_response("通讯录画像内核未就绪")
        method = str(getattr(request, "method", "") or "GET").upper()
        if method == "GET":
            return json_response({"items": service.backups()})
        write_guard = self._weaver_write_guard(service)
        if write_guard is not None:
            return write_guard
        action = str(payload.get("action") or "create")
        actor = self._weaver_actor()
        if action == "create":
            path = service.store.create_backup(str(payload.get("tag") or "manual"))
            if path is None:
                return error_response("备份失败，请检查数据目录权限")
            return json_response(
                {"ok": True, "message": f"已创建备份 {path.name}", "items": service.backups()}
            )
        name = str(payload.get("name") or "").strip()
        if not name:
            return error_response("缺少备份文件名")
        if action == "restore":
            result = service.store.restore_backup(name, actor)
        elif action == "delete":
            result = service.store.delete_backup(name, actor)
        else:
            return error_response("不认识的备份操作")
        return json_response(
            {
                "ok": result.ok,
                "message": result.message,
                "items": service.backups(),
            }
        )

    async def api_profile_weaver_backup_download(self):
        guard = self._guard()
        if guard is not None:
            return guard
        service = self._weaver_service()
        if service is None:
            return error_response("通讯录画像内核未就绪")
        name = str(request.query.get("name", "") or "").strip()
        ok, text, safe_name = service.store.read_backup_text(name)
        if not ok:
            return error_response(text)
        return Response(
            text,
            status=200,
            mimetype="application/json; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{safe_name}"'
            },
        )

    async def api_profile_weaver_merge(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        service = self._weaver_service()
        write_guard = self._weaver_write_guard(service)
        if write_guard is not None:
            return write_guard
        result = service.store.merge_profile_records(
            source_key=str(payload.get("source") or ""),
            target_key=str(payload.get("target") or ""),
            actor=self._weaver_actor(),
            drop_source=bool(payload.get("drop_source", True)),
        )
        return json_response({"ok": result.ok, "message": result.message})

    async def api_profile_weaver_delete(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        service = self._weaver_service()
        write_guard = self._weaver_write_guard(service)
        if write_guard is not None:
            return write_guard
        raw_keys = payload.get("keys")
        if not isinstance(raw_keys, list):
            raw_keys = [payload.get("key")]
        keys = [str(item or "").strip() for item in raw_keys if str(item or "").strip()]
        if not keys:
            return error_response("缺少要删除的画像 key")
        removed = 0
        failed: list[str] = []
        for key in keys:
            result = service.store.delete_profile_by_key(key, self._weaver_actor())
            if result.ok:
                removed += 1
            else:
                failed.append(f"{key}: {result.message}")
        return json_response(
            {
                "ok": not failed,
                "removed": removed,
                "failed": failed,
                "message": f"已删除 {removed} 条画像"
                + (f"，{len(failed)} 条失败" if failed else ""),
            }
        )

    async def _profile_person_payload(self, session_id: str, user_id: str) -> dict[str, Any]:
        """一个人的完整画像：字段 + 事实 + 关系（含历史与"他自称"）+ 好感日志。"""

        store = getattr(self.engine, "profiles", None)
        payload: dict[str, Any] = {"session": session_id, "user_id": user_id}
        if store is None:
            return payload
        weaver_service = self._weaver_service()
        weaver_detail = (
            weaver_service.detail_for(session_id, user_id)
            if weaver_service is not None
            else {}
        )
        if store.profile(session_id, user_id) is None and weaver_detail:
            created = store.touch(
                session_id,
                user_id,
                str(
                    weaver_detail.get("display_name")
                    or weaver_detail.get("subject_name")
                    or user_id
                ),
                count_message=False,
            )
            if created is not None:
                payload["created_from_weaver"] = True
        payload["person"] = self.engine.person_payload(session_id, user_id)
        if payload["person"] is None and weaver_detail:
            payload["person"] = {
                "user_id": user_id,
                "name": weaver_detail.get("display_name")
                or weaver_detail.get("subject_name")
                or user_id,
                "affinity": 0.0,
                "affinities": [],
                "claims": [],
                "past": [],
                "facts": [],
                "bonds": [],
                "digest": "",
                "note": "",
                "message_count": 0,
                "days_known": 0,
                "level": {"name": "仅有画像", "index": 0, "deny": [], "prompt": ""},
            }
        payload["weaver"] = weaver_detail
        payload["weaver_records"] = (
            weaver_service.records_for_user(
                user_id,
                session_id=session_id,
            )
            if weaver_service is not None
            else []
        )
        payload["facts"] = store.facts(session_id, user_id, statuses=["active", "candidate", "past"])
        from ..core.contact_knowledge import contact_knowledge

        payload["knowledge"] = contact_knowledge(weaver_detail, payload["facts"])
        # 称呼与备注：详细画像里的昵称/备注就是通讯录的规范字段，不再另造一套。
        person = payload.get("person")
        if isinstance(person, dict):
            weaver_fields = {
                str(item.get("name") or ""): item
                for item in weaver_detail.get("fields") or []
                if isinstance(item, dict)
            }
            nickname = next(
                (
                    weaver_fields[name]
                    for name in ("昵称", "称呼", "网名", "名字", "别名")
                    if name in weaver_fields
                ),
                None,
            )
            if nickname and not str(person.get("call_me") or "").strip():
                person["call_me"] = str(nickname.get("value") or "").strip()
                person["call_me_from"] = "详细画像"
            elif not str(person.get("call_me") or "").strip():
                subject = str(
                    weaver_detail.get("display_name")
                    or weaver_detail.get("subject_name")
                    or ""
                ).strip()
                if subject:
                    person["call_me"] = subject
                    person["call_me_from"] = "详细画像"
            if not str(person.get("note") or "").strip():
                weaver_notes = [
                    str(item).strip()
                    for item in weaver_detail.get("notes") or []
                    if str(item).strip()
                ]
                if weaver_notes:
                    person["note"] = "；".join(weaver_notes)[:200]
                    person["note_from"] = "详细画像"
            payload["identity"] = {
                "display_name": str(
                    person.get("call_me")
                    or weaver_detail.get("display_name")
                    or person.get("name")
                    or user_id
                ),
                "call_me": str(person.get("call_me") or ""),
                "call_him": str(person.get("call_him") or ""),
                "note": str(person.get("note") or ""),
                "completeness": float(weaver_detail.get("completeness") or 0.0),
                "record_count": int(
                    weaver_detail.get("record_count")
                    or (1 if weaver_detail else 0)
                ),
                "locked_count": int(weaver_detail.get("locked_count") or 0),
            }
        if isinstance(person, dict) and not str(person.get("note") or "").strip():
            notes = [
                str(item.get("text") or "").strip()
                for item in payload["facts"]
                if str(item.get("kind") or "") == "note"
                and str(item.get("text") or "").strip()
            ]
            if notes:
                person["note"] = "；".join(dict.fromkeys(notes))[:200]
                person["note_from"] = "心迹画像"
        # 垫底的"陌生人"和同类里被顶掉的旧关系都不往外露（老数据靠这一层收干净）
        bonds = store.visible_bonds(session_id, user_id)
        for item in bonds:
            item["since_text"] = self.engine._date_text(item.get("since"))
            item["until_text"] = self.engine._date_text(item.get("until"))
        payload["bonds"] = bonds
        payload["affinity_logs"] = store.affinity_logs(session_id, user_id, limit=50)
        payload["interaction"] = await self.db.call("interaction_detail", session_id, user_id)
        # 「她想不想这个人」：通讯录里一眼能看到，不用去状态页翻
        state = await self.engine.load_state(session_id, cold_start=False)
        # 「她记着他一笔账」：记的账是分人的，这里只回这个人的那条
        grudge = self.engine.grudge_for(state, user_id)
        if isinstance(payload.get("person"), dict):
            payload["person"] = self.engine._apply_grudge_coldness(state, user_id, payload["person"], session_id=session_id)
        payload["relationship"] = (payload.get("person") or {}).get("relationship", {})
        payload["grudge"] = (
            {
                **grudge,
                "at_text": self.engine._date_text(grudge.get("at")),
                "until_text": self.engine._date_text(grudge.get("until")),
                "days": max(
                    0.0,
                    (self.engine._now() - float(grudge.get("at") or 0.0)) / 86400.0,
                ),
            }
            if grudge
            else None
        )
        # 「她自己记着的事」：只列跟这个人相关的（没写对谁的就是她自己的打算）
        payload["own_topics"] = [
            dict(item)
            for item in (state.own_topics or [])
            if isinstance(item, dict)
            and str(item.get("who") or "") == user_id
        ]
        overview = self.engine.miss_overview(state, user_id=user_id, session_id=session_id)
        payload["miss"] = next(
            (item for item in overview.get("people", []) if item.get("user_id") == user_id),
            {
                "user_id": user_id,
                "value": 0.0,
                "waiting": False,
                "ready_in_minutes": 0,
                "will_reach_out": False,
            },
        )
        payload["miss_threshold"] = overview.get("threshold")
        payload["model_context"] = "\n\n".join(block for block in (
            self.engine.prompts.profile_block(payload.get("person"), []),
            weaver_service.prompt_block(session_id, user_id) if weaver_service else "",
        ) if block)
        config = self.engine.world.profile
        payload["bonds_config"] = [
            {
                "name": bond.name,
                "slot": bond.slot,
                "group": bond.group,
                "cap": bond.cap,
                "unique": bool(bond.unique),
                "negative": bool(bond.negative),
                "aliases": list(bond.aliases or []),
                "floor": bond.floor,
                "auto_form": bond.auto_form,
                "allow_boundary_crossing": bond.allow_boundary_crossing,
            }
            for bond in config.bonds
        ]
        payload["levels_config"] = [
            {
                "name": level.name,
                "min_affinity": level.min_affinity,
                "max_affinity": level.max_affinity,
                "deny": [store.action_label(item) for item in (level.deny or [])],
                "prompt": level.prompt,
            }
            for level in config.levels
        ]
        return payload

    async def api_profile_people(self):
        """通讯录列表：这个人叫啥、什么关系、好感多少、最近说过话没有。"""

        guard = self._guard()
        if guard is not None:
            return guard
        session_id = self._profile_scope(request.query.get("session", ""))
        if not session_id:
            return error_response("缺少 session 参数")
        store = getattr(self.engine, "profiles", None)
        if store is None:
            return json_response({"people": [], "enabled": False})
        people: list[dict[str, Any]] = []
        weaver_service = self._weaver_service()
        weaver_people = (
            weaver_service.people_for_session(session_id)
            if weaver_service is not None
            else {}
        )
        seen_users: set[str] = set()
        for row in store.list_people(session_id, limit=200):
            user_id = str(row.get("user_id") or "")
            view = store.view(session_id, user_id)
            if view is None:
                continue
            seen_users.add(user_id)
            weaver = weaver_people.get(user_id) or {}
            display_name = str(
                view.call_me
                or weaver.get("display_name")
                or weaver.get("subject_name")
                or view.name
                or user_id
            )
            people.append(
                {
                    "user_id": user_id,
                    "name": display_name,
                    "display_name": display_name,
                    "qq_name": view.qq_name,
                    "affinity": view.affinity,
                    "level": view.level.name,
                    "bonds": list(view.affinities),
                    "claims": [str(item.get("type") or "") for item in view.claims],
                    "digest": row.get("digest") or "",
                    "message_count": int(row.get("message_count") or 0),
                    "last_seen_at": float(row.get("last_seen_at") or 0.0),
                    "first_seen_at": float(row.get("first_seen_at") or 0.0),
                    "days": len(
                        [
                            item
                            for item in ((row.get("payload") or {}).get("days") or [])
                            if str(item)
                        ]
                    ),
                    "weaver": weaver,
                    "has_detailed_profile": bool(weaver),
                    "profile_record_count": int(weaver.get("record_count") or 0),
                    "profile_locked_count": int(weaver.get("locked_count") or 0),
                    "profile_preview": list(weaver.get("preview") or []),
                }
            )
        for user_id, summary in weaver_people.items():
            if not user_id or user_id in seen_users:
                continue
            people.append(
                {
                    "user_id": user_id,
                    "name": summary.get("display_name")
                    or summary.get("subject_name")
                    or user_id,
                    "display_name": summary.get("display_name")
                    or summary.get("subject_name")
                    or user_id,
                    "qq_name": summary.get("subject_name") or "",
                    "affinity": 0.0,
                    "level": "仅有画像",
                    "bonds": [],
                    "claims": [],
                    "digest": summary.get("preview") or "",
                    "message_count": 0,
                    "last_seen_at": 0.0,
                    "first_seen_at": 0.0,
                    "days": 0,
                    "weaver": summary,
                    "has_detailed_profile": True,
                    "profile_record_count": int(summary.get("record_count") or 0),
                    "profile_locked_count": int(summary.get("locked_count") or 0),
                    "profile_preview": summary.get("preview") or [],
                }
            )
        return json_response(
            {
                "people": people,
                "enabled": bool(self.engine.world.profile.enabled),
                "consolidate_enabled": bool(
                    self.engine.world.profile.consolidate_enabled
                ),
                "digest_limit": int(self.engine.world.profile.digest_limit),
                "weaver_enabled": bool(weaver_service and weaver_service.enabled),
                "weaver_session_based": bool(
                    weaver_service and weaver_service.session_based
                ),
            }
        )

    async def api_profile_person(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._profile_scope(
            payload.get("session") or request.query.get("session", "")
        )
        user_id = str(payload.get("user_id") or request.query.get("user_id", "") or "")
        if not session_id or not user_id:
            return error_response("缺少 session 或 user_id 参数")
        store = getattr(self.engine, "profiles", None)
        if store is None:
            return error_response("画像功能还没就绪")
        method = str(getattr(request, "method", "") or "").upper()
        if method == "POST" or str(request.query.get("_method", "")).upper() == "POST":
            weaver_service = self._weaver_service()
            sync_messages: list[str] = []
            if "note" in payload:
                note = str(payload.get("note") or "")
                store.set_note(session_id, user_id, note)
                if weaver_service is not None and weaver_service.webui_allow_edit:
                    sync_messages.extend(
                        weaver_service.sync_contact_identity(
                            session_id=session_id,
                            user_id=user_id,
                            actor=self._weaver_actor(),
                            note=note,
                        )
                    )
            call_me = str(payload.get("call_me") or "").strip()
            call_him = str(payload.get("call_him") or "").strip()
            if call_me or call_him:
                result = store.set_call_names(
                    session_id,
                    user_id,
                    call_me=call_me,
                    call_him=call_him,
                    explicit=True,
                )
                if result.get("rejected"):
                    reason = result["rejected"][0].get("reason") or "这个称呼不能用"
                    return error_response(reason)
            if call_me and weaver_service is not None and weaver_service.webui_allow_edit:
                sync_messages.extend(
                    weaver_service.sync_contact_identity(
                        session_id=session_id,
                        user_id=user_id,
                        actor=self._weaver_actor(),
                        call_me=call_me,
                    )
                )
            if "digest" in payload:
                store.set_digest(session_id, user_id, str(payload.get("digest") or ""))
            response = await self._profile_person_payload(session_id, user_id)
            response["sync_messages"] = sync_messages
            return json_response(response)
        return json_response(await self._profile_person_payload(session_id, user_id))

    async def api_profile_contact_records(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload if payload else request.query)
        if guard is not None:
            return guard
        service = self._weaver_service()
        if service is None:
            return error_response("通讯录画像内核未就绪")
        session_id = self._profile_scope(
            payload.get("session") or request.query.get("session", "")
        )
        user_id = str(
            payload.get("user_id")
            or request.query.get("user_id", "")
            or ""
        ).strip()
        if not user_id:
            return error_response("缺少 user_id")
        method = str(getattr(request, "method", "") or "GET").upper()
        records = service.record_summaries_for_user(
            user_id,
            session_id=session_id,
            include_all=False,
        )
        if method == "GET":
            return json_response(
                {
                    "user_id": user_id,
                    "preferred_key": service.preferred_record_key(
                        session_id, user_id
                    ),
                    "records": records,
                }
            )
        write_guard = self._weaver_write_guard(service)
        if write_guard is not None:
            return write_guard
        action = str(payload.get("action") or "merge_all")
        actor = self._weaver_actor()
        if action == "delete":
            removed, failures = service.delete_contact_records(
                user_id,
                actor,
                session_id=session_id,
            )
            return json_response(
                {
                    "ok": not failures,
                    "removed": removed,
                    "failed": failures,
                    "message": f"已删除 {removed} 条画像记录",
                }
            )
        if action != "merge_all":
            return error_response("不认识的联系人画像记录操作")
        target_key = str(payload.get("target_key") or "").strip()
        if not target_key:
            target_key = service.preferred_record_key(session_id, user_id)
        if not target_key or target_key not in service.store.profiles:
            return error_response("没有可合并到的目标画像记录")
        merged = 0
        for record in records:
            source_key = str(record.get("key") or "")
            if not source_key or source_key == target_key:
                continue
            result = service.store.merge_profile_records(
                source_key=source_key,
                target_key=target_key,
                actor=actor,
                drop_source=True,
            )
            if not result.ok:
                return error_response(result.message)
            merged += 1
        return json_response(
            {
                "ok": True,
                "merged": merged,
                "target_key": target_key,
                "message": f"已把 {merged} 条记录合并到当前联系人",
            }
        )

    async def api_profile_fact(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._profile_scope(payload.get("session"))
        user_id = str(payload.get("user_id") or "")
        action = str(payload.get("action") or "add")
        store = getattr(self.engine, "profiles", None)
        if store is None or not session_id or not user_id:
            return error_response("缺少参数")
        db = store.db
        group_id = store.group_key(session_id)
        if action in {"update", "delete", "resolve"}:
            try:
                fact_id = int(payload.get("id") or 0)
            except (ValueError, TypeError):
                return error_response("经历 ID 无效")
            row = db.get_user_fact(fact_id=fact_id)
            if (row is None or str(row.get("group_id")) != group_id
                    or str(row.get("user_id")) != user_id):
                return error_response("当前联系人没有这条经历")
        if action == "resolve":
            try:
                expected = float(payload.get("updated_at", -1))
            except (TypeError, ValueError):
                return error_response("资料已变化，请刷新后重试")
            if not store.resolve_fact(session_id, user_id, fact_id, expected=expected):
                return error_response("资料已变化或无需核对，请刷新后重试")
        elif action == "add":
            result = store.note_fact(
                session_id,
                user_id,
                text=str(payload.get("text") or ""),
                kind=str(payload.get("kind") or "other"),
                evidence=str(payload.get("evidence") or "主人手动加的"),
                context=str(payload.get("context") or ""),
                confidence=1.0,
                status=str(payload.get("status") or "active"),
                pinned=bool(payload.get("pinned")),
            )
            if not result.get("ok"):
                return error_response(str(result.get("reason") or "没记下来"))
        elif action == "update":
            fact_id = int(payload.get("id") or 0)
            if not fact_id:
                return error_response("缺少 id")
            db.update_user_fact(
                fact_id=fact_id,
                text=str(payload.get("text")) if "text" in payload else None,
                kind=str(payload.get("kind")) if "kind" in payload else None,
                status=str(payload.get("status")) if "status" in payload else None,
                pinned=bool(payload["pinned"]) if "pinned" in payload else None,
            )
        elif action == "delete":
            db.delete_user_fact(fact_id=int(payload.get("id") or 0))
        else:
            return error_response("不认识的操作")
        return json_response(
            {
                "ok": True,
                "facts": db.list_user_facts(group_id=group_id, user_id=user_id),
                "person": self.engine.person_payload(session_id, user_id),
            }
        )

    async def api_profile_bond(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._profile_scope(payload.get("session"))
        user_id = str(payload.get("user_id") or "")
        action = str(payload.get("action") or "note")
        store = getattr(self.engine, "profiles", None)
        if store is None or not session_id or not user_id:
            return error_response("缺少参数")
        if action in {"close", "accept", "delete"}:
            try:
                bond_id = int(payload.get("id") or 0)
            except (ValueError, TypeError):
                return error_response("关系 ID 无效")
            row = store.db.get_user_bond(bond_id=bond_id)
            if (row is None or str(row.get("group_id")) != store.group_key(session_id)
                    or str(row.get("user_id")) != user_id):
                return error_response("当前联系人没有这条关系")
        if action == "note":
            result = store.note_bond(
                session_id,
                user_id,
                type=str(payload.get("type") or ""),
                evidence=str(payload.get("evidence") or "主人手动设的"),
                confidence=1.0,
                asserted_by="他自称" if payload.get("asserted_by") == "他自称" else "管理员设置",
                policy=str(payload.get("policy") or ""),
            )
            if not result.get("ok"):
                return error_response(str(result.get("reason") or "没记下来"))
        elif action == "close":
            if not store.close_bond(session_id, user_id, bond_id=bond_id):
                return error_response("关系未解除")
        elif action == "accept":
            result = store.accept_claim(
                session_id,
                user_id,
                bond_id=bond_id,
                policy=str(payload.get("policy") or ""),
            )
            if not result.get("ok"):
                return error_response(str(result.get("reason") or "认不下来"))
        elif action == "delete":
            store.db.delete_user_bond(bond_id=bond_id)
        else:
            return error_response("不认识的操作")
        return json_response(
            {
                "ok": True,
                "person": self.engine.person_payload(session_id, user_id),
            }
        )

    async def api_profile_affinity(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._profile_scope(payload.get("session"))
        user_id = str(payload.get("user_id") or "")
        store = getattr(self.engine, "profiles", None)
        if store is None or not session_id or not user_id:
            return error_response("缺少参数")
        if "value" in payload:
            row = store.profile(session_id, user_id)
            before = float((row or {}).get("affinity") or 0.0)
            delta = float(payload.get("value") or 0.0) - before
        else:
            delta = float(payload.get("delta") or 0.0)
        result = store.adjust_affinity(
            session_id,
            user_id,
            delta,
            reason=str(payload.get("reason") or "主人调的"),
            source="manual",
        )
        if not result.get("ok"):
            return error_response(str(result.get("reason") or "没改成"))
        return json_response(
            {
                "ok": True,
                "affinity": result.get("value"),
                "person": self.engine.person_payload(session_id, user_id),
            }
        )

    async def api_profile_grudge(self):
        """通讯录里手动管她记着的账：加一笔 / 算了 / 删掉。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._profile_scope(payload.get("session"))
        user_id = str(payload.get("user_id") or "")
        action = str(payload.get("action") or "add")
        if not session_id or not user_id:
            return error_response("缺少参数")
        async with self.engine.session_state(session_id) as state:
            if action == "add":
                reason = str(payload.get("reason") or payload.get("text") or "").strip()
                if not reason:
                    return error_response("写一句她记着的事")
                ok = self.engine.note_grudge(
                    state,
                    reason,
                    user_id=user_id,
                    session_id=session_id,
                    # 手动加的不吃"每天最多一笔"那道闸（那是拦模型的）
                    force=True,
                )
                if not ok:
                    return error_response("这笔账没能记下来（记仇关着吗？）")
            elif action == "resolve":
                self.engine.resolve_grudge(state, user_id=user_id)
            elif action == "delete":
                state.grudges = [
                    item
                    for item in (state.grudges or [])
                    if not (
                        isinstance(item, dict)
                        and str(item.get("user_id") or "") == user_id
                    )
                ]
            else:
                return error_response("不认识的操作")
        return json_response(await self._profile_person_payload(session_id, user_id))

    async def api_profile_forget(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._profile_scope(payload.get("session"))
        user_id = str(payload.get("user_id") or "")
        store = getattr(self.engine, "profiles", None)
        if store is None or not session_id or not user_id:
            return error_response("缺少参数")
        removed = store.db.delete_user_profile(
            group_id=store.group_key(session_id), user_id=user_id
        )
        weaver_removed = 0
        service = self._weaver_service()
        if service is not None and service.enabled:
            weaver_removed, _failures = service.delete_contact_records(
                user_id,
                self._weaver_actor(),
                session_id=session_id,
            )
        return json_response(
            {
                "ok": True,
                "removed": removed + weaver_removed,
                "relation_removed": removed,
                "profile_removed": weaver_removed,
            }
        )

    async def api_profile_forget_all(self):
        """通讯录上方那个「清空」：把这个会话组里她认识的人全忘掉。

        只删这个 group_key 下的画像 / 事实 / 关系 / 好感日志；世界配置里的
        关系档位（profile.bonds）和会话白名单都不动。
        """

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._profile_scope(payload.get("session"))
        store = getattr(self.engine, "profiles", None)
        if store is None or not session_id:
            return error_response("缺少参数")
        group_id = store.group_key(session_id)
        people = store.list_people(session_id, limit=5000)
        removed = 0
        forgotten = 0
        for item in people:
            user_id = str(item.get("user_id") or "")
            if not user_id:
                continue
            removed += int(
                store.db.delete_user_profile(group_id=group_id, user_id=user_id) or 0
            )
            forgotten += 1
        service = self._weaver_service()
        if service is not None and service.enabled:
            for key in list(service.store.profiles):
                record = service.store.profiles.get(key) or {}
                source_session, _ = service.store.split_profile_key(key, record)
                if service.session_based and source_session != session_id:
                    continue
                service.store.delete_profile_by_key(key, self._weaver_actor())
        # 她"今天还能主动找几个人"的额度也跟着清，免得留下没人认领的计数
        store.drop_daily_people(group_id)
        return json_response({"ok": True, "people": forgotten, "removed": removed})

    async def api_profile_weaver(self):
        """心迹画像桥：看看能不能搬、有多少人的画像在那儿。"""

        query = request.query
        guard = self._guard(query if hasattr(query, "get") else {})
        if guard is not None:
            return guard
        data_root = str(self.store.data_dir.parent)
        status = self.engine.profile_weaver_status(data_root)
        status["session"] = self._profile_scope(query.get("session") or "")
        return json_response(status)

    async def api_profile_weaver_import(self):
        """把心迹画像已经记下的东西搬进本插件的通讯录（只读对方的数据）。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._profile_scope(payload.get("session"))
        if not session_id:
            return error_response("先选一个会话，再把画像搬进这个会话的通讯录")
        data_root = str(self.store.data_dir.parent)
        result = await self.engine.import_profile_weaver(session_id, data_root)
        if not result.get("ok"):
            return error_response(str(result.get("reason") or "导入失败"))
        return json_response(result)

    @model_operation("consolidate", "管理员在记忆库点击立即整理")
    async def api_profile_consolidate(self):
        """立刻整理一次（编辑器上的"现在整理一次"）：消化这段时间的经历。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._profile_scope(payload.get("session"))
        if not session_id:
            return error_response("缺少 session 参数")
        mode = str(payload.get("mode") or "full")
        dry_run = bool(payload.get("dry_run"))
        result = await self.engine.consolidate_session(session_id, mode=mode, dry_run=dry_run)
        if result is None:
            return error_response("整理没跑起来：先确认「睡眠整理模型」可用")
        if not result.get("ok"):
            return json_response({"ok": False, "note": result.get("note") or "没整理出东西"})
        return json_response(result)

    async def api_events(self):
        """事件系统概览：能力值、未了的事、最近的线索。"""

        guard = self._guard()
        if guard is not None:
            return guard
        session_id = str(request.query.get("session", "") or "")
        if not session_id:
            return error_response("缺少 session 参数")
        session_id = self._scope(session_id)
        overview = await self.engine.event_overview(session_id)
        return json_response(overview)

    async def api_persona_source(self):
        """编辑器「从 AstrBot 导入」用：把 AstrBot 给这个会话的人设读出来。

        插件默认就是用它（``world.persona.mode = astrbot``）；这里只是给个入口，
        让用户能把手上那份人格一键抄进配置里，从此跟着预设走。
        """

        guard = self._guard()
        if guard is not None:
            return guard
        session_id = self._scope(request.query.get("session", ""))
        persona = getattr(self, "persona_port", None)
        if persona is None or not hasattr(persona, "astrbot_persona_text"):
            return error_response("人设通道还没就绪")
        text = await persona.astrbot_persona_text(session_id)
        return json_response(
            {
                "session": session_id,
                "text": text,
                "chars": len(text),
                "mode": str(getattr(self.engine.world.persona, "mode", "astrbot")),
            }
        )

    async def api_persona_brief(self):
        """简易人设：按人格缓存。POST 时 ``generate=true`` 表示用模型从主人设生成。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = str(
            payload.get("session") or request.query.get("session", "") or ""
        )
        if not session_id:
            return error_response("缺少 session 参数")
        if request.method == "POST" or str(request.query.get("_method", "")).upper() == "POST":
            if payload.get("generate"):
                # 编辑器刚改过、还没保存的角色卡优先：向导里写完人设就点生成时，
                # 读配置只会拿到旧那份。
                brief = await self.engine.generate_persona_brief(
                    session_id, persona_text=str(payload.get("persona") or "")
                )
                if not brief:
                    return error_response(
                        "生成失败：先确认「生成器模型」可用，而且这个会话能读到人设"
                    )
            else:
                await self.engine.save_persona_brief(
                    session_id, str(payload.get("brief") or "")
                )
            state = await self.engine.persona_brief_state(session_id)
            return json_response({"ok": True, "persona_brief": state})
        return json_response({"persona_brief": await self.engine.persona_brief_state(session_id)})

    def _voice_sample_payload(self) -> dict[str, Any]:
        persona = self.engine.world.persona
        return {
            "samples": self.engine.persona_samples(),
            "candidates": [
                dict(item)
                for item in list(getattr(persona, "sample_candidates", None) or [])
                if isinstance(item, dict) and str(item.get("text") or "").strip()
            ],
            "scenes": [
                {"id": key, "label": label}
                for key, label in self.engine.prompts.VOICE_SCENES
            ],
            "per_turn": int(getattr(persona, "samples_per_turn", 3) or 3),
            "max": int(getattr(persona, "samples_max", 12) or 12),
            "candidates_max": int(getattr(persona, "candidates_max", 80) or 80),
        }

    async def api_voice_samples(self):
        """看样例库 + 可用的场景清单。"""

        guard = self._guard()
        if guard is not None:
            return guard
        return json_response(self._voice_sample_payload())

    async def api_voice_samples_generate(self):
        """让内容生成模型按场景出候选（草稿，不落库）。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._scope(payload.get("session"))
        if not session_id:
            return error_response("缺少 session（要拿它的角色卡去生成）")
        scenes = payload.get("scenes")
        picked = [str(item) for item in scenes] if isinstance(scenes, list) else None
        result = await self.engine.generate_voice_samples(
            session_id, picked, persona_text=str(payload.get("persona") or "")
        )
        if not result.get("ok"):
            return error_response(str(result.get("reason") or "生成失败"))
        return json_response({**result, **{"scenes_meta": self._voice_sample_payload()["scenes"]}})

    async def api_voice_samples_from_chat(self):
        """从近期聊天留档里挑她说过、而且被接住了的话，当样例候选（不落库）。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._scope(payload.get("session"))
        if not session_id:
            return error_response("缺少 session")
        try:
            limit = max(1, min(40, int(payload.get("limit") or 12)))
        except (TypeError, ValueError):
            limit = 12
        async with self.engine.session_state(session_id) as state:
            found = self.engine.voice_sample_candidates_from_chat(state, limit=limit)
        return json_response(
            {"ok": True, "candidates": found, "note": "" if found else "这段时间没挑出合适的"}
        )

    async def api_voice_samples_save(self):
        """把挑中的样例写进世界配置（落库前先留一份历史）。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        raw_samples = payload.get("samples")
        if not isinstance(raw_samples, list):
            return error_response("samples 必须是数组")
        persona = self.engine.world.persona
        limit = max(1, int(getattr(persona, "samples_max", 12) or 12))
        candidates_limit = max(1, int(getattr(persona, "candidates_max", 80) or 80))
        cleaned: list[dict[str, Any]] = []
        for index, item in enumerate(raw_samples):
            if not isinstance(item, dict):
                continue
            text = " ".join(str(item.get("text") or "").split())
            if not text:
                continue
            cleaned.append(
                {
                    "id": str(item.get("id") or f"s{index + 1}"),
                    "scene": str(item.get("scene") or ""),
                    "label": str(item.get("label") or ""),
                    "move": " ".join(str(item.get("move") or "").split())[:20],
                    "text": text[:120],
                    "source": str(item.get("source") or "model"),
                }
            )
        if len(cleaned) > limit:
            return error_response(f"最多留 {limit} 条，先删掉几条再存")
        # 候选池：可选一起存。攒着不动，只有「采用」才会挪进上面的样例库
        raw_candidates = payload.get("candidates")
        candidates: list[dict[str, Any]] | None = None
        if isinstance(raw_candidates, list):
            candidates = []
            for index, item in enumerate(raw_candidates):
                if not isinstance(item, dict):
                    continue
                text = " ".join(str(item.get("text") or "").split())
                if not text:
                    continue
                candidates.append(
                    {
                        "id": str(item.get("id") or f"c{index + 1}"),
                        "scene": str(item.get("scene") or ""),
                        "label": str(item.get("label") or ""),
                        "move": " ".join(str(item.get("move") or "").split())[:20],
                        "text": text[:120],
                        "context": " ".join(str(item.get("context") or "").split())[:60],
                        "source": str(item.get("source") or "model"),
                    }
                )
            candidates = candidates[-candidates_limit:]
        world = self.engine.editable_world()
        persona_raw = dict(world.get("persona") or {})
        persona_raw["samples"] = cleaned
        if candidates is not None:
            persona_raw["sample_candidates"] = candidates
        if payload.get("per_turn") is not None:
            try:
                persona_raw["samples_per_turn"] = max(1, min(6, int(payload["per_turn"])))
            except (TypeError, ValueError):
                pass
        world["persona"] = persona_raw
        warnings = self.engine.save_scoped_settings(world, reason="改动声音样例")
        self.engine.reload_config()
        return json_response({"ok": True, "warnings": warnings, **self._voice_sample_payload()})

    async def api_eval_run(self):
        """把剧本并发跑一遍：带着完整提示词，收集她的真实回复（不写任何状态）。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._scope(payload.get("session"))
        if not session_id:
            return error_response("缺少 session")
        rounds = payload.get("rounds")
        if not isinstance(rounds, list) or not rounds:
            return error_response("缺少 rounds（要按剧本跑的问题）")
        if len(rounds) > 30:
            return error_response("一次最多跑 30 轮")
        try:
            concurrency = int(payload.get("concurrency") or 3)
        except (TypeError, ValueError):
            concurrency = 3
        provider_id = str(payload.get("provider_id") or "").strip()
        llm = AstrBotLLM(self, provider_id or self.llm_provider_id, sampling=self.sampling_params())
        director_id = str(payload.get("director_provider_id") or "").strip()
        result = await self.engine.run_eval(
            session_id,
            [dict(item) for item in rounds if isinstance(item, dict)],
            llm=llm,
            concurrency=concurrency,
            variant=str(payload.get("variant") or "full"),
            director_llm=AstrBotLLM(
                self,
                director_id or self.judge_provider_id,
                sampling=self.sampling_params(),
            ),
        )
        if not result.get("ok"):
            return error_response(str(result.get("reason") or "跑失败"))
        return json_response(result)

    async def api_eval_script(self):
        """生成测评剧本：同一个剧本拿去跑不同模型，比"谁更像她"。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._scope(payload.get("session"))
        if not session_id:
            return error_response("缺少 session")
        try:
            rounds = int(payload.get("rounds") or 20)
        except (TypeError, ValueError):
            rounds = 20
        result = await self.engine.generate_eval_script(session_id, rounds)
        if not result.get("ok"):
            return error_response(str(result.get("reason") or "生成失败"))
        return json_response(result)

    async def api_persona_review(self):
        """体检人设：返回问题清单 + 可逐条采纳的改动（**不落库**）。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._scope(payload.get("session"))
        if not session_id:
            return error_response("缺少 session")
        result = await self.engine.review_persona(session_id)
        if not result.get("ok"):
            return error_response(str(result.get("reason") or "体检失败"))
        return json_response(result)

    async def api_persona_apply(self):
        """把挑中的改动写进角色卡（写前自动留一份历史）。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        rewrites = payload.get("rewrite") if isinstance(payload.get("rewrite"), list) else []
        adds = payload.get("add") if isinstance(payload.get("add"), list) else []
        result = self.engine.apply_persona_changes(rewrites, adds)
        if not result.get("ok"):
            return error_response(str(result.get("reason") or "应用失败"))
        return json_response(result)

    def _guard(self, payload: dict[str, Any] | None = None) -> Any:
        """统一鉴权：返回错误响应或 None。"""

        token = request.query.get("token", "") or ""
        if not token and isinstance(payload, dict):
            token = str(payload.get("token", "") or "")
        if self.auth.check(token):
            return None
        return error_response("需要先登录编辑器", status_code=401)

    def _scope(self, value: Any) -> str:
        """编辑器里选的可能是会话，也可能是会话组。

        会话组要落到它的代表会话上（她的状态、日志、日程都挂在那儿）。
        """

        text = str(value or "").strip()
        if not text:
            return ""
        engine = getattr(self, "engine", None)
        if engine is None:
            return text
        try:
            return engine.scope_session(text)
        except Exception:
            return text

    def _memory_sessions(self, value: Any) -> list[str]:
        """记忆按会话各存一份；查的时候把同组的会话一起当条件。

        编辑器里可能选的是会话组：那就展开成组里的所有会话。
        """

        engine = getattr(self, "engine", None)
        if engine is None:
            text = str(value or "").strip()
            return [text] if text else []
        try:
            return engine.group_sessions(self._scope(value))
        except Exception:
            text = str(value or "").strip()
            return [text] if text else []

    async def api_auth_status(self):
        return json_response(
            {
                "password_required": self.auth.has_password(),
                "locked_seconds": self.auth.locked(),
                "llm_provider_id": self.llm_provider_id,
                "tick_interval": self.tick_interval,
                "decider_interval": self.decider_interval,
            }
        )

    async def api_auth_login(self):
        payload = await request.json(default={}) or {}
        wait = self.auth.locked()
        if wait > 0:
            return error_response(f"失败次数过多，请 {wait} 秒后再试", status_code=429)
        if not self.auth.has_password():
            return json_response({"token": "", "password_required": False})
        if not self.auth.verify(str(payload.get("password", ""))):
            self.auth.note_failure()
            return error_response("密码不正确", status_code=401)
        return json_response({"token": self.auth.issue_token(), "password_required": True})

    async def api_auth_logout(self):
        payload = await request.json(default={}) or {}
        self.auth.revoke(str(payload.get("token", "") or ""))
        return json_response({"ok": True})

    async def api_auth_password(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        new_password = str(payload.get("new_password", "") or "")
        if self.auth.has_password() and not self.auth.verify(
            str(payload.get("old_password", ""))
        ):
            return error_response("旧密码不正确", status_code=403)
        self.auth.set_password(new_password)
        return json_response({"ok": True, "password_required": self.auth.has_password()})

    async def api_extensions(self):
        """扩展包：读设置与数据 / 保存设置。

        插件自己不认识任何扩展；这里只是把挂上来的扩展的字段定义和值原样交给设置页。
        """

        guard = self._guard()
        if guard is not None:
            return guard
        if request.method.upper() == "POST":
            payload = await request.json(default={}) or {}
            saved = self.extension_host.save_settings(dict(payload.get("values") or {}))
            return json_response({"ok": True, "saved": saved})
        session_id = self._scope(request.query.get("session", ""))
        state = None
        if session_id:
            try:
                state = await self.engine.load_state(session_id, cold_start=False)
            except Exception:
                state = None
        return json_response(self.extension_host.snapshot(state))

    async def api_get_config(self):
        guard = self._guard()
        if guard is not None:
            return guard
        # 老版本写下的配置文件里可能缺新增的键（例如心潮回落速度），
        # 直接返回原始 JSON 会让编辑器把它们显示成 0，一保存就把 0 写回磁盘。
        # 这里用校验后的配置补齐已知键，同时保留用户自己加的未知键。
        # 必须 by_alias：连线端点在模型里叫 from_（from 是 Python 关键字），
        # 但文件里和前端读的都是 from——按字段名 dump 会让地图连线整个消失。
        world = self._world_payload()
        schedules = self.store.raw_schedules()
        if self.engine.schedules is not None:
            schedules = {
                **schedules,
                **self.engine.schedules.model_dump(mode="json", by_alias=True),
            }
        sessions = self.store.raw_sessions()
        if self.engine.sessions is not None:
            sessions = {
                **sessions,
                **self.engine.sessions.model_dump(mode="json", by_alias=True),
            }
        # 动作的「亲密程度」留空 = 按内置对照表算。编辑器里要能看见**实际会用多少**，
        # 不然用户填了空白，永远不知道这一步到底会不会满足欲求。
        for action in world.get("actions") or []:
            if not isinstance(action, dict):
                continue
            explicit = action.get("intimacy")
            if explicit in (None, ""):
                action["intimacy_effective"] = round(
                    float(DEFAULT_INTIMACY.get(str(action.get("id") or ""), 0.0)), 3
                )
            else:
                try:
                    action["intimacy_effective"] = round(
                        max(0.0, min(1.0, float(explicit))), 3
                    )
                except (TypeError, ValueError):
                    action["intimacy_effective"] = 0.0
        return json_response(
            {
                "world": world,
                "schedules": schedules,
                "sessions": sessions,
                # 展示元数据独立于可保存的会话配置，避免群名往返写入 sessions.json。
                "session_names": {
                    item.session_id: name
                    for item in self.engine.sessions.sessions
                    if (name := self.engine.session_group_name(item.session_id))
                },
                "warnings": self.engine.load_warnings,
                # 模型槽位当前用的是哪个（向导里做"模型配了吗"的检查用；只有 id，没有 key）
                "providers": self._provider_slots(),
                "embedding_providers": self.embedding.choices(),
                "embedding_default": str(self.config.get("embedding_provider_id") or ""),
                # 图片转述缓存的使用情况（编辑器里显示"省了多少次识别"）
                "vision_cache": await self._vision_cache_stats(),
                # 当前天气：地图页顶部横幅直接用它
                "weather": await self.engine.weather_payload(),
                # 动作库里"哪些动作是扩展带来的"：编辑器按它把动作库分成两半
                "extension_actions": self.extension_host.action_groups(),
                "scope": self.engine.config_scope_id,
                "scope_blocks": list(getattr(self.engine.session_config(self.engine.config_scope_id), "config_overrides", {}) or {}),
                "global_config": {
                    "world": self.engine._base_runtime["world"].model_dump(mode="json", by_alias=True),
                    "schedules": self.engine._base_runtime["schedules"].model_dump(mode="json"),
                },
                "scope_owners": {
                    item.session_id: self.engine.main_session(item.session_id)
                    for item in self.engine.sessions.sessions
                },
            }
        )

    async def api_put_scope(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        scope = self.engine.config_scope_id or str(payload.get("config_scope") or "").strip()
        world, schedules, blocks = payload.get("world"), payload.get("schedules"), payload.get("blocks")
        if not scope or not isinstance(world, dict) or not isinstance(schedules, dict) or not isinstance(blocks, list):
            return error_response("需要会话、世界配置、日程配置和独立配置块列表")
        try:
            warnings = self.store.save_scope(scope, self._without_extension_actions(world), schedules, blocks)
        except (ValueError, ValidationError) as exc:
            return error_response(f"会话配置未保存：{exc}")
        self.engine.reload_config()
        return json_response({"ok": True, "warnings": warnings})

    async def api_proactive_control(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        if not isinstance(payload.get("enabled"), bool):
            return error_response("enabled 必须是布尔值")
        world = self.store.raw_world()
        world["proactive_chat_paused"] = not payload["enabled"]
        self.store.save_world(world, reason="切换全局自主搭话")
        self.engine.reload_config()
        return json_response({"ok": True, "enabled": payload["enabled"]})

    def _provider_slots(self) -> dict[str, str]:
        """六个模型槽位现在指向哪个 Provider（只给 id，不含 key）。

        向导里要能一眼看出"哪个槽位还没配"——这些槽位在 AstrBot 的插件配置里，
        编辑器本身改不了，所以只做展示 + 告诉用户去哪配。
        """

        def show(value: str) -> str:
            return str(value or "").strip()

        return {
            "llm": show(self.llm_provider_id) or "（跟会话默认）",
            "helper": show(self.helper_provider_id) or "（跟看图模型）",
            "judge": show(self.judge_provider_id) or "（跟打杂模型）",
            "vision": show(self.vision_provider_id) or "（不转述图片）",
            "creator": show(self.creator_provider_id) or "（跟插件用模型）",
            "event": show(self.event_provider_id) or "（跟打杂模型）",
            "consolidate": show(self.consolidate_provider_id) or "（跟打杂模型）",
        }

    async def _vision_cache_stats(self) -> dict[str, Any]:
        try:
            stats = await self.db.call("image_cache_stats")
        except Exception:
            stats = {"entries": 0, "hits": 0}
        vision = getattr(self, "vision", None)
        stats["session_hits"] = int(getattr(vision, "hits", 0) or 0)
        return stats

    async def api_put_world(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        world = payload.get("world")
        if not isinstance(world, dict):
            return error_response("world 必须是对象")
        world = self._without_extension_actions(world)
        # The immediate master switch is changed only through config/proactive.
        world = {**world, "proactive_chat_paused": bool(self.store.raw_world().get("proactive_chat_paused", False))}
        try:
            warnings = self.store.save_world(world)
        except ValidationError as exc:
            return self._save_error("世界配置", exc)
        self.engine.reload_config()
        return json_response({"ok": True, "warnings": warnings})

    def _without_extension_actions(self, world: dict[str, Any]) -> dict[str, Any]:
        """存世界配置前把扩展带来的动作摘掉。

        这些动作是扩展在加载时提供的，不属于用户的配置：留着的话，
        扩展一旦停用 / 卸载，它们就会变成一堆没人认领、也删不干净的动作。
        """

        owned = self.extension_host.owned_actions()
        actions = world.get("actions")
        if not owned or not isinstance(actions, list):
            return world
        kept = [
            item
            for item in actions
            if not (isinstance(item, dict) and str(item.get("id") or "") in owned)
        ]
        if len(kept) == len(actions):
            return world
        return {**world, "actions": kept}

    async def api_put_schedules(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        schedules = payload.get("schedules")
        if not isinstance(schedules, dict):
            return error_response("schedules 必须是对象")
        # 工具 / 指令型步骤缺「想干什么」时，让内容生成模型补一句写进配置：
        # 运行时就不用再猜，也不会因为缺意图被跳过。
        schedules, filled = await self.engine.fill_schedule_intents(schedules)
        # 落点归一化：旧存档里「会话组 + 具体会话」混着存的，保存前统一成一种，
        # 并把"我替你按什么存了"作为人话提示交回编辑器（不静默丢用户的选择）。
        schedules, landing_warnings = self.engine.normalize_schedule_sessions(schedules)
        try:
            warnings = self.store.save_schedules(schedules)
        except ValidationError as exc:
            return self._save_error("日程", exc)
        self.engine.reload_config()
        return json_response(
            {
                "ok": True,
                "warnings": [*landing_warnings, *warnings],
                "filled": filled,
                "schedules": schedules,
            }
        )

    async def api_put_sessions(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        sessions = payload.get("sessions")
        if not isinstance(sessions, dict):
            return error_response("sessions 必须是对象")
        try:
            warnings = self.store.save_sessions(sessions)
        except ValidationError as exc:
            return self._save_error("会话列表", exc)
        self.engine.reload_config()
        return json_response({"ok": True, "warnings": warnings})

    @staticmethod
    def _save_error(label: str, exc: Exception):
        """保存没过校验：给一句人话 + 具体是哪一项，别把英文校验串直接丢给用户。"""

        problems: list[str] = []
        try:
            for item in exc.errors():  # type: ignore[attr-defined]
                where = " / ".join(str(part) for part in (item.get("loc") or []))
                message = str(item.get("msg") or "").strip()
                problems.append(f"{where}：{message}" if where else message)
        except Exception:
            problems = []
        detail = "；".join(part for part in problems if part)[:500]
        return error_response(
            f"{label}没通过校验，这次没有保存（改动还在编辑器里，改完再存一次）。"
            + (f"\n{detail}" if detail else f"\n{exc}")
        )

    def _generator_timeout_seconds(self) -> float:
        """内容生成超时（秒）：读插件配置，默认 10 分钟，0 = 不超时。"""

        return _generation_timeout_seconds(getattr(self, "config", None))

    @model_operation("creation", "管理员在编辑器请求生成内容")
    async def _generate_drafts_response(self, generate, *args, **kwargs):
        """生成接口统一处理超时与失败，并把原因交给编辑器展示。"""

        label = getattr(generate, "__name__", "generate")
        self.logger.info(f"[my_world] 世界生成开始：{label}")
        started = time.monotonic()
        timeout = _generation_timeout_seconds(getattr(self, "config", None))
        with collect_llm_usage() as usage:
            try:
                pending = generate(*args, **kwargs)
                if timeout <= 0:
                    result = await pending
                else:
                    result = await asyncio.wait_for(pending, timeout=timeout)
            except asyncio.TimeoutError:
                minutes = max(1, int(round(timeout / 60)))
                self.logger.warning(f"[my_world] 世界生成超时：{label}（{minutes} 分钟）")
                return error_response(
                    f"生成超过 {minutes} 分钟还没返回。内容多的时候几分钟是正常的；"
                    "可以让它继续重试，或到插件设置里把「生成超时（分钟）」调大（填 0 = 不超时）"
                )
            except Exception as exc:
                detail = str(exc)[:500] or type(exc).__name__
                self.logger.warning(f"[my_world] 世界生成失败：{label}: {detail}")
                return error_response(f"生成失败：{detail}")
        elapsed = time.monotonic() - started
        token_usage = usage.payload()
        if isinstance(result, dict):
            result = {
                **result,
                "generation": {
                    "elapsed_seconds": round(elapsed, 3),
                    "usage": token_usage,
                },
            }
        token_note = (
            f"，tokens {token_usage['total']}（{token_usage['reported_responses']}/"
            f"{token_usage['responses']} 次响应有用量）"
            if token_usage["available"]
            else "，Provider 未返回 token 用量"
        )
        self.logger.info(
            f"[my_world] 世界生成完成：{label}，耗时 {elapsed:.1f} 秒{token_note}"
        )
        return json_response(result)

    async def api_generate_actions(self):
        """批量生成动作草稿（只返回草稿，用户在编辑器里勾选后才保存）。

        传 ``node`` 就是"只给这个地点生成"，否则按 ``zone`` 里的每个地点生成。
        """

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        zone_id = str(payload.get("zone", "") or "").strip()
        node_id = str(payload.get("node", "") or "").strip()
        if not zone_id and node_id:
            zone_id = self.engine.world.zone_of(node_id)
        if not zone_id:
            return error_response("缺少 zone 参数")
        return await self._generate_drafts_response(
            self.engine.generate_actions_for_zone,
            zone_id,
            payload.get("per_node"),
            node_ids=[node_id] if node_id else None,
            extra_prompt=str(payload.get("extra_prompt") or ""),
        )

    async def api_generate_world_actions(self):
        """按人设与整张地图生成动作草稿（动作库的全局生成入口）。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        return await self._generate_drafts_response(
            self.engine.generate_world_actions, payload.get("count")
        )

    async def api_generate_nodes(self):
        """按区域批量生成地点草稿（含自动摆位与自动连线）。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        zone_id = str(payload.get("zone", "") or "").strip()
        if not zone_id:
            return error_response("缺少 zone 参数")
        return await self._generate_drafts_response(
            self.engine.generate_nodes_for_zone,
            zone_id,
            payload.get("count"),
            str(payload.get("extra_prompt") or ""),
        )

    async def api_generate_zone(self):
        """重做一个区域：新地点草稿 + 可选的地点专属动作草稿（都不落库）。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        zone_id = str(payload.get("zone", "") or "").strip()
        if not zone_id:
            return error_response("缺少 zone 参数")
        return await self._generate_drafts_response(
            self.engine.generate_zone_draft,
            zone_id,
            payload.get("count"),
            str(payload.get("extra_prompt") or ""),
            payload.get("actions_per_node"),
            bool(payload.get("replace")),
        )

    async def api_generate_zone_chunk(self):
        """分块重建地图：一次只生成一个区域。

        整张地图一次生成几分钟，普通 HTTP 请求容易被 Cloudflare（100 秒）掐断；
        按区域切开之后每块都短，而且后一块能读到前面的区域，上下文不丢。
        """

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        return await self._generate_drafts_response(
            self.engine.generate_zone_chunk,
            payload.get("index"),
            payload.get("total"),
            payload.get("nodes_per_zone"),
            payload.get("actions_per_node"),
            str(payload.get("extra_prompt") or ""),
            payload.get("existing"),
            payload.get("aspect"),
        )

    async def api_purge_legacy_actions(self):
        """清除全部旧版（工具 / 指令型）动作；不带 confirm 时只返回预览。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        result = await self.engine.purge_legacy_actions(
            confirm=bool(payload.get("confirm"))
        )
        return json_response(result)

    async def api_generate_zones(self):
        """按她的人设与已有世界生成新区域草稿（不落库）。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        return await self._generate_drafts_response(
            self.engine.generate_zones,
            payload.get("count"),
            payload.get("nodes_per_zone"),
            payload.get("extra_prompt"),
            payload.get("aspect"),
            payload.get("actions_per_node"),
        )

    async def api_generate_world_map(self):
        """按她的人设一键重建整张地图草稿（不落库，确认后才替换）。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        return await self._generate_drafts_response(
            self.engine.generate_world_map,
            payload.get("count"),
            payload.get("nodes_per_zone"),
            payload.get("extra_prompt"),
            payload.get("aspect"),
            payload.get("actions_per_node"),
        )

    async def api_map_layout(self):
        """重新铺开：按当前画布形状算一组新坐标（只出坐标，不落库）。

        编辑器里的地图是「先改本地草稿、点保存才写盘」，所以这里也不碰
        世界配置：前端拿到坐标套进草稿，用户不满意直接不保存就行。
        区域用带路线的力导布局，区域内部的地点用网格——各管各的坐标空间。
        """

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        aspect = payload.get("aspect")
        level = str(payload.get("level") or "world")
        if level == "zone":
            positions = grid_layout_nodes(payload.get("count"), aspect=aspect)
            return json_response(
                {"positions": [[x, y] for x, y in positions], "level": "zone"}
            )
        zones = payload.get("zones")
        edges = payload.get("zone_edges")
        if not isinstance(zones, list):
            return error_response("参数不对：「zones」需要是数组")
        if edges is None:
            edges = []
        if not isinstance(edges, list):
            return error_response("参数不对：「zone_edges」需要是数组")
        clean_zones = [
            {
                "id": str(zone.get("id") or ""),
                "is_home": bool(zone.get("is_home")),
            }
            for zone in zones
            if isinstance(zone, dict)
        ]
        positions = spring_layout_zones(
            clean_zones,
            [edge for edge in edges if isinstance(edge, dict)],
            aspect=aspect,
        )
        return json_response(
            {
                "positions": {
                    zone_id: [x, y] for zone_id, (x, y) in positions.items()
                },
                "level": "world",
            }
        )

    async def api_map_inspect(self):
        """检查编辑器当前草稿的路线；不保存地图、不调用模型。"""
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        draft = payload.get("map")
        keys = ("nodes", "edges", "zones", "zone_edges")
        if not isinstance(draft, dict) or any(not isinstance(draft.get(key), list) for key in keys):
            return error_response("地图需要包含地点、区域与连线数组")
        if not draft["nodes"]:
            return error_response("地图还没有地点，请先新增地点")
        if len(draft["nodes"]) > 1000 or len(draft["edges"]) + len(draft["zone_edges"]) > 10000:
            return error_response("地图过大，请分区域检查")
        try:
            world, warnings = parse_world({key: draft[key] for key in keys})
            result = inspect_routes(world, str(payload.get("start") or ""), str(payload.get("target") or ""))
        except (ValidationError, ValueError) as exc:
            return error_response(f"无法检查路线：{exc}")
        return json_response({**result, "warnings": warnings, "tick_seconds": self.engine.tick_seconds})

    async def api_generate_schedules(self):
        """按她的人设与生活节奏生成日程草稿（不落库）。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        return await self._generate_drafts_response(
            self.engine.generate_schedules,
            payload.get("count"),
            payload.get("flavor"),
            payload.get("extra_prompt"),
        )

    async def api_generate_events(self):
        """按她的人设与最近经历生成事件草稿（不落库）。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        return await self._generate_drafts_response(
            self.engine.generate_events,
            payload.get("count"),
            payload.get("flavor"),
            payload.get("extra_prompt"),
        )

    async def api_generate_generic_actions(self):
        """生成一批通用普通动作（不参考人设，地图可选）。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        return await self._generate_drafts_response(
            self.engine.generate_generic_actions,
            payload.get("count"),
            payload.get("use_map", True),
        )

    async def api_generate_idle_actions(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        return await self._generate_drafts_response(self.engine.generate_idle_actions, payload.get("count", 8))

    async def api_deliver_event(self):
        """把编辑器里挑好的事件草稿投递给她（等价于 /mw event 一句话）。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = str(payload.get("session_id") or "").strip()
        if not session_id:
            sessions = self.engine.enabled_session_ids()
            session_id = sessions[0] if sessions else ""
        if not session_id:
            return error_response("还没有启用任何会话，先加一个会话再投递事件")
        text = str(payload.get("text") or "").strip()
        note = await self.engine.deliver_generated_event(session_id, text)
        return json_response(
            {"ok": not note, "note": note, "session_id": session_id}
        )

    async def api_tools(self):
        guard = self._guard()
        if guard is not None:
            return guard
        catalog = self._tool_catalog()
        used = self._tool_action_index()
        rows: list[dict[str, Any]] = []
        for name in sorted(catalog):
            item = dict(catalog[name])
            item["param_text"] = self.engine.tool_param_text(name)
            bound = used.get(name) or []
            # 哪些动作已经在用这个工具：编辑器据此标「已导入」，不用再点进去看
            item["action_ids"] = [row.get("id") for row in bound]
            item["action_names"] = [row.get("name") or row.get("id") for row in bound]
            rows.append(item)
        return json_response(
            {
                "tools": rows,
                "global_allowed": self.engine.world.global_allowed_tools,
            }
        )

    def _tool_catalog(self) -> dict[str, dict[str, Any]]:
        """工具名 -> 元信息（说明用工具作者那一份，不带"参数：…"的尾巴）。

        ``available_tools()`` 那份说明是给提示词用的（拼上了参数），
        拿来当动作说明会很难看，所以这里直接问工具端口要原始字段。
        """

        catalog: dict[str, dict[str, Any]] = {}
        port = getattr(self.engine, "tools", None)
        if port is not None:
            try:
                for info in port.list_tools():
                    name = str(getattr(info, "name", "") or "").strip()
                    if not name:
                        continue
                    catalog[name] = {
                        "name": name,
                        "description": str(getattr(info, "description", "") or "").strip(),
                        "parameters": dict(getattr(info, "parameters", {}) or {}),
                        "source": str(getattr(info, "source", "") or "plugin"),
                        "plugin": str(getattr(info, "plugin", "") or ""),
                    }
            except Exception:
                catalog = {}
        if catalog:
            return catalog
        # 拿不到端口（老版本 / 桩）时退回引擎那份，至少名字和参数是对的
        schemas = self.engine.tool_schemas()
        sources = self.engine.tool_sources()
        plugins = self.engine.tool_owners()
        for name, desc in self.engine.available_tools().items():
            catalog[name] = {
                "name": name,
                "description": str(desc or ""),
                "parameters": schemas.get(name, {}),
                "source": sources.get(name, "plugin"),
                "plugin": plugins.get(name, ""),
            }
        return catalog

    def _tool_action_index(self) -> dict[str, list[dict[str, Any]]]:
        """工具名 -> 用着它的动作（编辑器里标「已导入」用）。"""

        index: dict[str, list[dict[str, Any]]] = {}
        world = getattr(self.engine, "world", None)
        for action in list(getattr(world, "actions", []) or []):
            names = [str(item).strip() for item in (getattr(action, "tool_names", []) or [])]
            single = str(getattr(action, "tool_name", "") or "").strip()
            if single and single not in names:
                names.append(single)
            for name in names:
                if not name:
                    continue
                index.setdefault(name, []).append(
                    {
                        "id": str(getattr(action, "id", "") or ""),
                        "name": str(getattr(action, "name", "") or ""),
                    }
                )
        return index

    def _world_payload(self) -> dict[str, Any]:
        """当前世界配置（校验后的结构 + 用户自己加的未知键）。"""

        return normalize_legacy_keys(
            normalize_edge_keys(
                {
                    **self.store.raw_world(),
                    **self.engine.world.model_dump(mode="json", by_alias=True),
                }
            )
        )

    async def _plan_action_names(
        self, tools: list[dict[str, Any]]
    ) -> tuple[dict[str, dict[str, str]], str]:
        """让内容生成模型给这批工具起名、分组（可选；失败就返回空计划）。

        一次带太多工具，模型容易漏项或改名，所以按 12 个一批问。
        返回 ``({工具名: {name, description, group}}, 提示)``。
        """

        engine = getattr(self, "engine", None)
        llm = getattr(engine, "creator_llm", None)
        if engine is None or llm is None:
            return {}, "没有可用的内容生成模型，已改用按工具说明自动命名"
        session_id = engine._generator_session_id()
        persona_text = await engine._persona_text(session_id)
        plans: dict[str, dict[str, str]] = {}
        note = ""
        batch = 12
        for start in range(0, len(tools), batch):
            chunk = tools[start : start + batch]
            system_prompt, prompt = engine.prompts.build_tool_action_naming_prompt(
                persona_text=persona_text,
                tools=chunk,
            )
            reply = await engine._ask_creator(session_id, system_prompt, prompt)
            if not reply:
                note = note or "内容生成模型没有返回内容，已改用按工具说明自动命名"
                continue
            parsed = extract_json_object(reply)
            rows = parsed.get("actions") if isinstance(parsed, dict) else None
            if not isinstance(rows, list):
                note = note or "内容生成模型返回的不是约定的 JSON，已改用按工具说明自动命名"
                continue
            allowed = {str(item.get("name") or "") for item in chunk}
            for row in rows:
                if not isinstance(row, dict):
                    continue
                tool_name = str(row.get("tool") or "").strip()
                if tool_name not in allowed:
                    continue
                plan: dict[str, str] = {}
                name = str(row.get("name") or "").strip()
                group = str(row.get("group") or "").strip()
                description = str(row.get("description") or "").strip()
                if name:
                    plan["name"] = name[:24]
                if group:
                    plan["group"] = group[:24]
                if description:
                    plan["description"] = description[:200]
                if plan:
                    plans[tool_name] = plan
        return plans, note

    async def api_tools_import(self):
        """把 AstrBot 里注册的工具一次性变成动作。

        ``dry_run=true`` 时只算草稿不落库（编辑器拿它做预览表）；
        正式导入会把动作并进世界配置、留一版改动历史、然后热加载。
        ``options.naming = "llm"`` 时先让内容生成模型起名分组，失败自动退回规则命名。
        """

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        catalog = self._tool_catalog()
        if not catalog:
            return error_response(
                "AstrBot 现在没有可导入的已启用工具：可能还没加载完，"
                "也可能工具或提供工具的插件已被停用"
            )
        raw = payload.get("tools")
        if isinstance(raw, list) and raw:
            names = [str(item).strip() for item in raw if str(item).strip()]
        else:
            if isinstance(raw, str) and raw.strip() and raw.strip().lower() not in ("all", "*", "全部"):
                names = [item.strip() for item in raw.replace("，", ",").split(",") if item.strip()]
            else:
                # 不传 / 传 all：视为全选（编辑器上「全选」走的就是这条）
                names = sorted(catalog)
        missing = [name for name in names if name not in catalog]
        tools = [catalog[name] for name in names if name in catalog]
        if not tools:
            shown = "、".join(missing[:6]) + ("…" if len(missing) > 6 else "")
            return error_response(
                f"这些工具现在不可用（未注册或已停用）：{shown}"
                if missing
                else "没有可导入的工具"
            )

        options = payload.get("options") if isinstance(payload.get("options"), dict) else {}
        options = dict(options)
        options.setdefault("naming", "auto")
        options.setdefault("group_mode", "source")
        options.setdefault("scope", "global")
        options.setdefault("duplicate", "skip")
        dry_run = bool(payload.get("dry_run"))

        notes: list[str] = []
        if missing:
            shown = "、".join(missing[:6]) + ("…" if len(missing) > 6 else "")
            notes.append(f"这些工具现在不可用（没注册或已停用），已跳过：{shown}")
        want_model = bool(options.pop("use_model", False)) or str(options.get("naming")) == "llm"
        if want_model:
            plans, plan_note = await self._plan_action_names(tools)
            if plans:
                merged_plans = dict(options.get("plans") or {})
                for key, value in plans.items():
                    merged_plans.setdefault(key, value)
                options["plans"] = merged_plans
            if plan_note:
                notes.append(plan_note)
            # 名字 / 分组由计划提供，规则命名只做兜底
            options["naming"] = "auto"

        world = self._world_payload()
        actions = world.get("actions")
        if not isinstance(actions, list):
            actions = []
        result = build_tool_action_drafts(
            tools,
            options=options,
            existing_actions=actions,
        )
        drafts = list(result.get("drafts") or [])
        warnings = [*notes, *(str(item) for item in (result.get("warnings") or []))]
        if dry_run:
            return json_response(
                {
                    "ok": True,
                    "dry_run": True,
                    "drafts": drafts,
                    "warnings": warnings,
                    "already": list(result.get("already") or []),
                    "skipped": list(result.get("skipped") or []),
                    "updated": list(result.get("updated") or []),
                }
            )
        if not drafts:
            return json_response(
                {
                    "ok": True,
                    "created": 0,
                    "updated": 0,
                    "action_ids": [],
                    "warnings": warnings,
                    "already": list(result.get("already") or []),
                    "skipped": list(result.get("skipped") or []),
                }
            )

        merged, stats = merge_tool_action_drafts(actions, drafts)
        world["actions"] = merged
        world = self._without_extension_actions(world)
        try:
            save_warnings = self.store.save_world(world, reason="导入工具动作")
        except ValidationError as exc:
            return self._save_error("世界配置", exc)
        self.engine.reload_config()
        action_ids = [str(item.get("id") or "") for item in drafts if item.get("id")]
        self.logger.info(
            f"[my_world] 工具导入动作：新增 {stats.get('created', 0)} 个、"
            f"更新 {stats.get('updated', 0)} 个（共选了 {len(tools)} 个工具）"
        )
        return json_response(
            {
                "ok": True,
                "created": int(stats.get("created", 0)),
                "updated": int(stats.get("updated", 0)),
                "action_ids": action_ids,
                "warnings": [*warnings, *(str(item) for item in (save_warnings or []))],
                "already": list(result.get("already") or []),
                "skipped": list(result.get("skipped") or []),
            }
        )

    async def api_reload(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        warnings = self.engine.reload_config()
        return json_response({"ok": True, "warnings": warnings})

    async def api_restore(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        scope = str(payload.get("scope", "all"))
        restored = self.store.restore_default(scope)
        self.engine.reload_config()
        return json_response({"ok": True, "restored": restored})

    async def api_state(self):
        guard = self._guard()
        if guard is not None:
            return guard
        session_id = self._scope(request.query.get("session", ""))
        if not session_id:
            return error_response("缺少 session 参数")
        snapshot = await self.engine.snapshot(session_id)
        snapshot["nickname_owner"] = self.messenger.group_card_owner(session_id)
        return json_response(snapshot)

    @model_operation("action", "管理员在控制台触发运行操作")
    async def api_state_action(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        action = str(payload.get("action", "") or "")
        if not action:
            return error_response("缺少 action")
        if action == "refresh_weather":
            # 天气本身不分会话，但「查天气」配成指令型时要借一条真实消息当上下文，
            # 所以前端选中的那条会话优先，没带就按白名单第一条
            note = await self.engine.maybe_refresh_weather(
                force=True, session_id=self._scope(payload.get("session"))
            )
            return json_response(
                {
                    "ok": True,
                    "note": note,
                    "weather": await self.engine.weather_payload(),
                }
            )
        session_id = self._scope(payload.get("session"))
        if not session_id:
            return error_response("缺少 session")
        if action == "wake":
            try:
                was = await asyncio.wait_for(
                    self.engine.wake_up(session_id), timeout=self.state_action_timeout()
                )
            except asyncio.TimeoutError:
                return json_response(
                    {"ok": False, "note": "她正在忙，稍等一下再叫醒"}
                )
            return json_response({"ok": True, "was_sleeping": was})
        if action == "interrupt":
            try:
                done = await asyncio.wait_for(
                    self.engine.interrupt(session_id, force=True),
                    timeout=self.state_action_timeout(),
                )
            except asyncio.TimeoutError:
                return json_response(
                    {"ok": False, "note": "她正在忙，稍等一下再打断"}
                )
            return json_response({"ok": True, "interrupted": done})
        if action == "reset_tools":
            # 手动解除工具熔断（状态页上的「立即重试」）
            name = str(payload.get("tool") or "").strip()
            cleared = self.engine.reset_tool_breakers(name)
            return json_response({"ok": True, "cleared": cleared})
        if action == "event":
            # 编辑器上「给她来件事」：等于用户投递一个事件
            note = await self.engine.submit_event(
                session_id, str(payload.get("text") or "")
            )
            return json_response(
                {
                    "ok": True,
                    "note": note,
                    "events": await self.engine.event_overview(session_id),
                }
            )
        if action in ("advance_event", "close_event"):
            # 编辑器上「立即推进一幕」/「立刻完结」：手动改事件节奏
            if self.engine.is_busy(session_id):
                return json_response(
                    {"ok": False, "note": "她正在忙（动作或检索还没结束），等这一轮跑完再点"}
                )
            thread_id = str(payload.get("thread") or "")
            handler = (
                self.engine.advance_thread
                if action == "advance_event"
                else self.engine.close_thread
            )
            try:
                note = await asyncio.wait_for(
                    handler(session_id, thread_id), timeout=self.state_action_timeout()
                )
            except asyncio.TimeoutError:
                return json_response(
                    {"ok": False, "note": "这件事还在演（模型还没回），稍等一下再看"}
                )
            return json_response(
                {
                    "ok": True,
                    "note": note,
                    "events": await self.engine.event_overview(session_id),
                }
            )
        if action == "tick":
            # 连点几下不能攒成一串 tick：同一会话同一时刻只放行一次推进，
            # 后来的这几次直接忽略（前端也有同样的防护，这里是兜底）。
            # 推进是**全局**的（一次 tick 走所有会话），所以别按会话判重。
            if self._advancing:
                return json_response(
                    {"ok": False, "note": "上一次推进还在跑，这次点击已忽略"}
                )
            self._advancing.add(session_id)
            tick_task = asyncio.ensure_future(self.engine.tick())

            def _tick_done(task: asyncio.Task) -> None:
                self._advancing.discard(session_id)
                if task.cancelled():
                    return
                error = task.exception()
                if error is not None:
                    self.logger.warning(f"[my_world] 推进 tick 失败：{error}")

            tick_task.add_done_callback(_tick_done)
            try:
                # 她正在调模型（例如刚走到新地点要就地决定）时不会直接失败，
                # 而是等这一轮跑完再推进——这段时间本来就该发生一个 tick。
                # 用 shield：等不到就把它留在后台跑完，**不中途取消**
                # （取消会把这一轮砍在半路，日志和状态都会变得莫名其妙）。
                await asyncio.wait_for(
                    asyncio.shield(tick_task), timeout=self.state_tick_wait_seconds()
                )
            except asyncio.TimeoutError:
                return json_response(
                    {
                        "ok": False,
                        "note": "这一轮还在跑（她在调模型或检索），没有打断它；"
                        "过几秒刷新状态页就能看到结果",
                    }
                )
            except Exception as exc:
                return json_response({"ok": False, "note": f"推进失败：{exc}"})
            outcomes = tick_task.result()
            return json_response(
                {
                    "ok": True,
                    "outcomes": [
                        {
                            "session_id": item.session_id,
                            "messages": item.messages,
                            "notes": item.notes,
                        }
                        for item in outcomes
                    ],
                }
            )
        if action == "decide":
            if self.engine.is_busy(session_id):
                return json_response(
                    {
                        "ok": False,
                        "note": "她正在忙（动作或检索还没结束），这次决策已跳过",
                    }
                )
            # 手动触发：绕过评估间隔，但仍然尊重"正在忙 / 在睡觉"这些硬条件
            try:
                outcome = await asyncio.wait_for(
                    self.engine.maybe_decide(
                        session_id, force=bool(payload.get("force"))
                    ),
                    timeout=self.state_action_timeout(),
                )
            except asyncio.TimeoutError:
                return json_response(
                    {"ok": False, "note": "她正在忙（上一个动作还没结束），稍等一下再点"}
                )
            return json_response(
                {
                    "ok": True,
                    "notes": outcome.notes if outcome else [],
                    "messages": outcome.messages if outcome else [],
                }
            )
        if action == "set_values":
            applied = await self.engine.set_values(
                session_id, dict(payload.get("values") or {})
            )
            return json_response({"ok": True, "applied": applied})
        if action == "run_schedule":
            result = await self.engine.run_schedule_now(
                session_id,
                str(payload.get("schedule_id") or ""),
                force=bool(payload.get("force", True)),
            )
            return json_response(result)
        if action == "set_nickname":
            text = str(payload.get("text") or "").strip()
            if not text:
                return error_response("名片不能为空")
            async with self.engine.session_state(session_id) as state:
                # 只改「当前名片」，不碰「原名」：原名是她进群时的名片，用来算名片与恢复。
                # 以前这里顺手把它也写成刚保存的内容，结果控制台一直显示第一次改过的名字。
                state.bot_current_nickname = text
                state.bot_nickname_locked = True
            changed, failed, reason = await self._apply_card_to_group(session_id, text)
            if changed:
                # 刚写下去的就是当前真实名片：顺手更新缓存，编辑器不用等下一次平台读取
                self.engine.note_local_card(session_id, text)
            ok = changed > 0
            note = f"已改 {changed} 个群的群名片"
            if failed:
                note += f"，{failed} 个没改成（{reason or '没说明原因'}）"
            return json_response(
                {
                    "ok": ok,
                    "reason": "" if ok else (reason or "这个会话没有能改的群名片"),
                    "card": text,
                    "changed": changed,
                    "failed": failed,
                    "note": note,
                }
            )
        if action in ("lock_nickname", "unlock_nickname", "reset_nickname"):
            async with self.engine.session_state(session_id) as state:
                if action == "lock_nickname":
                    state.bot_nickname_locked = True
                    return json_response({"ok": True, "note": "已锁定群名片"})
                if action == "unlock_nickname":
                    state.bot_nickname_locked = False
                    state.last_nickname_update_at = 0.0
                    return json_response({"ok": True, "note": "已解锁，会随状态自动变"})
                base = state.bot_base_nickname or self.engine.world.bot_name
                if not base:
                    return error_response("还不知道她原来的名片，先在群里让她说过话")
                state.bot_nickname_locked = True
                state.bot_current_nickname = base
            changed, failed, reason = await self._apply_card_to_group(session_id, base)
            if changed:
                self.engine.note_local_card(session_id, base)
            if changed:
                note = f"已改回原名（{changed} 个群）"
                if failed:
                    note += f"，{failed} 个没改成（{reason or '没说明原因'}）"
                return json_response({"ok": True, "note": note})
            return json_response(
                {"ok": False, "note": f"改回原名失败：{reason or '这个会话没有能改的群名片'}"}
            )
        if action == "refresh_nickname":
            return json_response(await self.engine.refresh_nickname(session_id))
        if action == "reset_state":
            # 清掉这个会话的全部状态（位置、数值、计划、留档）。名字里必须带 state：
            # 以前它就叫 "reset"，和编辑器的「恢复原名」按钮撞名，一点就把状态删了。
            await self.db.call("delete_state", session_id)
            return json_response({"ok": True})
        if action == "clear_context":
            result = await self.engine.clear_chat_context(session_id)
            return json_response({"ok": True, **result})
        if action == "nickname":
            snapshot = await self.engine.load_state(session_id, cold_start=False)
            changed, failed, reason = await self._apply_card_to_group(
                session_id,
                snapshot.bot_current_nickname or snapshot.bot_base_nickname,
            )
            return json_response(
                {"ok": bool(changed), "changed": changed, "failed": failed, "reason": reason}
            )
        return error_response(f"未知动作：{action}")

    async def _apply_card_to_group(self, session_id: str, card: str) -> tuple[int, int, str]:
        """把名片写到「她这一组里的所有群」。

        返回 ``(改成功几个群, 失败几个, 最后一次失败原因)``。

        - **只在当前会话组里改**：别的会话组是另一个她，一个字都不碰
          （老版本是从哪个会话点的就只改那一个群，从私聊点还会直接报"这不是群聊"）；
        - 私聊没有群名片，跳过；当前会话自己就是群、又没进组时就改它自己。
        """

        targets = [
            str(item)
            for item in self.engine.group_sessions(session_id)
            if ":GroupMessage:" in str(item) and self.engine.is_enabled(str(item))
        ]
        if not targets:
            result = await self.messenger.set_group_card(session_id, card)
            if result.ok:
                return 1, 0, ""
            return 0, 1, str(result.reason or "")
        changed = 0
        failed = 0
        reason = ""
        for target in targets:
            result = await self.messenger.set_group_card(target, card)
            if result.ok:
                changed += 1
            else:
                failed += 1
                reason = str(result.reason or reason)
        return changed, failed, reason

    async def api_states(self):
        guard = self._guard()
        if guard is not None:
            return guard
        return json_response({"sessions": await self.engine.overview()})

    async def api_ticks(self):
        """运行记录：最近几拍 tick 的明细（tick 不再写进 AstrBot 后台日志）。"""

        guard = self._guard()
        if guard is not None:
            return guard
        return json_response(self.engine.tick_log_payload())

    async def api_logs(self):
        guard = self._guard()
        if guard is not None:
            return guard
        session_id = self._scope(request.query.get("session", ""))
        limit = min(1000, max(1, request.query.get("limit", 200, type=int) or 200))
        event_type = request.query.get("type", "") or ""
        keyword = request.query.get("q", "") or ""
        before_id = request.query.get("before", 0, type=int) or 0
        events = await self.db.call(
            "query_events",
            session_id=session_id or None,
            limit=limit,
            event_type=event_type or None,
            keyword=keyword or None,
            before_id=before_id or None,
        )
        types = await self.db.call("event_types", session_id=session_id or None)
        return json_response(
            {
                "events": build_timeline(
                    events,
                    self.engine.world,
                    ext_render=self.engine.extensions.debug_event_text,
                ),
                "types": types,
                "has_more": len(events) >= limit,
            }
        )

    async def api_model_calls(self):
        guard = self._guard()
        if guard is not None:
            return guard
        result = await self.db.call("model_calls", hours=_query_int("hours", 24),
            page=_query_int("page", 1), size=_query_int("size", 40),
            **{key: str(request.query.get(key, "")) for key in ("session", "purpose", "provider", "status", "q")})
        for item in result["items"]:
            item["session_label"] = self.engine.session_label(item["session_id"]) if item["session_id"] else "编辑器 / 后台任务"
        result["session_labels"] = {sid: self.engine.session_label(sid) or sid for sid in result["facets"]["sessions"]}
        return json_response(result)

    async def api_logs_export(self):
        guard = self._guard()
        if guard is not None:
            return guard
        session_id = self._scope(request.query.get("session", ""))
        limit = min(5000, max(1, request.query.get("limit", 2000, type=int) or 2000))
        events = await self.db.call(
            "query_events", session_id=session_id or None, limit=limit
        )
        payload = {
            "session": session_id,
            "exported_at": time.time(),
            "events": build_timeline(
                events,
                self.engine.world,
                ext_render=self.engine.extensions.debug_event_text,
            ),
        }
        return json_response(
            payload,
            headers={
                "Content-Disposition": 'attachment; filename="my-world-logs.json"'
            },
        )

    async def api_memories(self):
        guard = self._guard()
        if guard is not None:
            return guard
        # 记忆按会话各存一份，但会话组要一起看（组里几个群共用同一段经历）
        session_ids = self._memory_sessions(request.query.get("session", ""))
        node_id = request.query.get("node_id", "") or None
        memory_type = request.query.get("type", "") or None
        scope = request.query.get("scope", "") or None
        keyword = request.query.get("q", "") or ""
        rows = await self.db.call(
            "query_memories",
            session_ids=session_ids or None,
            node_id=node_id,
            memory_type=memory_type,
            scope=scope,
            limit=500,
        )
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("content", ""))]
        return json_response({"memories": rows})

    async def api_thoughts(self):
        """内心活动：她自己留下的心里话（按会话取，越新越靠前）。"""

        guard = self._guard()
        if guard is not None:
            return guard
        session_id = self._scope(request.query.get("session", ""))
        try:
            limit = int(request.query.get("limit", 120) or 120)
        except (TypeError, ValueError):
            limit = 120
        if not session_id:
            return error_response("缺少 session 参数")
        return json_response(await self.engine.thoughts_payload(session_id, limit))

    async def api_thoughts_clear(self):
        """清空某个会话留下的心里话（记忆、日志、状态都不动）。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._scope(payload.get("session"))
        if not session_id:
            return error_response("缺少 session 参数")
        removed = await self.engine.clear_thoughts(session_id)
        return json_response({"ok": True, "removed": removed})

    async def api_memory_create(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._scope(payload.get("session_id"))
        content = str(payload.get("content", "") or "").strip()
        if not session_id or not content:
            return error_response("缺少 session_id 或 content")
        if str(payload.get("type") or "scene") not in LIFE_TYPES:
            return error_response("生活记忆类型应为 scene、event、inner 或 dream；人物资料在通讯录管理")
        memory_id = self.engine.memory.remember(
            session_id=session_id,
            persona_id=str(payload.get("persona_id", "") or ""),
            node_id=str(payload.get("node_id", "") or ""),
            content=content,
            memory_type=str(payload.get("type", "scene") or "scene"),
            related_users=[str(u) for u in payload.get("related_users", []) or []],
            emotion=str(payload.get("emotion", "") or ""),
            weight=float(payload.get("weight", 0.5) or 0.5),
            scope=payload.get("scope") or None,
            source="manual",
        )
        return json_response({"ok": True, "id": memory_id})

    async def api_memory_update(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        memory_id = payload.get("id")
        if not memory_id:
            return error_response("缺少 id")
        fields = {
            key: payload[key]
            for key in ("content", "emotion", "weight", "node_id", "type", "scope")
            if key in payload
        }
        if "type" in fields and fields["type"] not in LIFE_TYPES:
            return error_response("不支持的生活记忆类型")
        # 空作用域在召回时匹配不上任何模式，等于把这条记忆"藏起来"，
        # 所以编辑器选了「用默认作用域」时不要真的写空串。
        if not str(fields.get("scope", "") or "").strip():
            fields.pop("scope", None)
        await self.db.call("revise_memory", int(memory_id), fields)
        return json_response({"ok": True})

    async def api_memory_delete(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        memory_id = payload.get("id")
        if not memory_id:
            return error_response("缺少 id")
        removed = await self.db.call("delete_memories", memory_id=int(memory_id))
        return json_response({"ok": True, "removed": removed})

    async def api_memory_delete_batch(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        ids = payload.get("ids")
        if not isinstance(ids, list) or not ids:
            return error_response("缺少 ids")
        removed = await self.db.call("delete_memories_by_ids", [int(item) for item in ids])
        return json_response({"ok": True, "removed": removed})

    async def api_memory_clear(self):
        """清空记忆。带 session 只清这个会话，不带则清全部。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        # 记忆按会话各存一份：选了会话组就把组里每个会话的都清掉
        sessions = self._memory_sessions(payload.get("session"))
        if sessions:
            removed = 0
            for item in sessions:
                removed += int(await self.db.call("clear_memories", item) or 0)
        else:
            removed = int(await self.db.call("clear_memories", None) or 0)
        return json_response({"ok": True, "removed": removed})

    async def api_logs_delete_batch(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        ids = payload.get("ids")
        if not isinstance(ids, list) or not ids:
            return error_response("缺少 ids")
        removed = await self.db.call("delete_events_by_ids", [int(item) for item in ids])
        return json_response({"ok": True, "removed": removed})

    async def api_logs_clear(self):
        """清空事件日志。带 session 只清这个会话，不带则清全部。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        session_id = self._scope(payload.get("session"))
        removed = await self.db.call("clear_events", session_id or None)
        return json_response({"ok": True, "removed": removed})

    async def api_memory_stats(self):
        guard = self._guard()
        if guard is not None:
            return guard
        session_id = request.query.get("session", "") or None
        stats = await self.db.call(
            "memory_stats",
            session_ids=self._memory_sessions(session_id) or None,
        )
        return json_response(stats)

    async def api_memory_export(self):
        guard = self._guard()
        if guard is not None:
            return guard
        data = {
            "world": self.store.raw_world(),
            "schedules": self.store.raw_schedules(),
            "sessions": self.store.raw_sessions(),
            "memory_domain": LIFE_DOMAIN,
            "memories": await self.db.call("query_memories", limit=100000),
        }
        return json_response(data)

    async def api_memory_import(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        memories = payload.get("memories")
        if not isinstance(memories, list):
            return error_response("memories 必须是数组")
        if payload.get("memory_domain") != LIFE_DOMAIN:
            return error_response("请导入生活记忆导出文件（memory_domain=my_world.life）；旧聊天备份不能直接混入生活库")
        if any(not isinstance(item, dict) or not is_life_memory(item) for item in memories):
            return error_response("文件含聊天摘要或不支持的来源，未导入任何记录")
        prepared = []
        try:
            for item in memories:
                content = item.get("content", "")
                if not isinstance(content, str):
                    raise ValueError("生活记忆正文必须是文本")
                if not content.strip():
                    continue
                weight = float(item.get("weight", 0.5))
                created_at = float(item.get("created_at", time.time()))
                next_review = float(item.get("next_review_at", 0))
                if not all(math.isfinite(value) for value in (weight, created_at, next_review)):
                    raise ValueError("日期和权重必须是有效数字")
                if not 0 <= weight <= 1 or created_at < 0 or next_review < 0:
                    raise ValueError("日期或权重超出允许范围")
                scope = item.get("scope") or self.engine.world.memory_scope_mode
                tier = item.get("tier") or "raw"
                if scope not in ("group", "persona", "group_persona", "global", "node") or tier not in ("raw", "gist"):
                    raise ValueError("记忆作用域或层级无效")
                lists = {}
                for key in ("related_users", "participants", "keywords"):
                    values = item.get(key) or []
                    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                        raise ValueError(f"{key} 必须是文本数组")
                    lists[key] = values
                prepared.append({
                    **lists, "content": content.strip(), "memory_type": item.get("type", "scene"),
                    "session_id": str(item.get("session_id") or ""), "persona_id": str(item.get("persona_id") or ""),
                    "node_id": str(item.get("node_id") or ""), "emotion": str(item.get("emotion") or ""),
                    "weight": weight, "created_at": created_at, "next_review_at": next_review,
                    "scope": scope, "source": "import", "tier": tier,
                    "context": str(item.get("context") or ""), "pinned": bool(item.get("pinned")),
                })
        except (TypeError, ValueError, OverflowError) as exc:
            return error_response(f"生活记忆格式有误，未导入任何记录：{exc}")
        # Restore historical dates and weights verbatim; don't re-score or replace
        # an existing experience through normal runtime conflict handling.
        with self.db.raw.atomic():
            for item in prepared:
                self.db.raw.add_memory(**item)
        return json_response({"ok": True, "imported": len(prepared)})

    async def api_prompt(self):
        guard = self._guard()
        if guard is not None:
            return guard
        session_id = request.query.get("session", "") or ""
        mode = request.query.get("mode", "takeover") or "takeover"
        if not session_id:
            return error_response("缺少 session 参数")
        session_id = self._scope(session_id)
        if mode == "autonomous":
            text = await self.engine.preview_autonomous_prompt(session_id)
        else:
            text = await self.engine.preview_last_reply_request(session_id)
        return json_response(
            {
                "prompt": text,
                "chars": len(text),
                # 分段索引：编辑器里直接显示，避免"某一段是不是没进去"只能靠肉眼找
                "sections": prompt_section_index(text),
                # 状态槽：调试页要能看出"槽里有东西但没进提示词"（过期了）这种情形
                "state_slots": await self.engine.preview_state_slots(session_id),
                # 这一轮抽到哪几条声音样例：提示词两万字，不点出来根本找不着
                "voice_samples": await self.engine.preview_voice_samples(session_id),
            }
        )

    # ---------------- 预设：成套的世界配置 ----------------

    async def api_state_history(self):
        """数值历史：给编辑器画「她最近过得怎么样」。"""

        guard = self._guard()
        if guard is not None:
            return guard
        session_id = request.query.get("session", "") or ""
        if not session_id:
            return error_response("缺少 session 参数")
        session_id = self._scope(session_id)
        try:
            hours = int(request.query.get("hours", 24) or 24)
        except (TypeError, ValueError):
            hours = 24
        return json_response(await self.engine.state_history(session_id, hours=hours))

    async def api_defaults(self):
        """内置默认文案：内置动作的说明/名片文案，以及两段图片转述提示词。

        编辑器里的「恢复默认」图标都从这里取值——默认文案只有 core/defaults.py 一份。
        """

        guard = self._guard()
        if guard is not None:
            return guard
        actions: dict[str, dict[str, str]] = {}
        for item in DEFAULT_WORLD.get("actions") or []:
            action_id = str(item.get("id") or "")
            if not action_id:
                continue
            actions[action_id] = {
                "name": str(item.get("name") or ""),
                "description": str(item.get("description") or ""),
                "nickname_text": str(item.get("nickname_text") or ""),
            }
        return json_response(
            {
                "actions": actions,
                # 默认动作现在是"按需启用"：编辑器用它做一键开启 / 关闭，
                # 核心动作（说话 / 移动 / 睡觉…）始终保留。
                "core_actions": sorted(CORE_BUILTIN_ACTIONS),
                # 配置字段的中文说明：滑块明细和旧默认值提醒都用它，
                # 免得同一份字段名要在前端再抄一遍。
                "field_labels": dict(FIELD_LABELS),
                "captions": {
                    "look": DEFAULT_CAPTION_PROMPT,
                    "relation": DEFAULT_CAPTION_RELATION_PROMPT,
                    "forward": DEFAULT_FORWARD_PROMPT,
                },
                # 扩展注册的事件类型：日志页的「类型」筛选和图标用它
                "debug_events": self.engine.extensions.debug_events(),
            }
        )

    async def api_presets(self):
        guard = self._guard()
        if guard is not None:
            return guard
        return json_response(
            {
                "presets": self.store.list_presets(),
                "active": self.store.active_preset(),
            }
        )

    async def api_preset_save(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        preset_id = str(payload.get("id") or "").strip()
        if not preset_id:
            return error_response("预设 id 不能为空")
        try:
            path, warnings = self.store.save_preset(
                preset_id,
                name=str(payload.get("name") or "").strip(),
                note=str(payload.get("note") or "").strip(),
            )
        except Exception as exc:
            return error_response(f"保存预设失败：{exc}")
        return json_response({"ok": True, "id": path.stem, "warnings": warnings})

    async def api_preset_new_default(self):
        """用内置的默认世界新建一份预设（不动当前配置）。"""

        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        preset_id = str(payload.get("id") or "").strip() or "default_world"
        try:
            clean, warnings = self.store.new_default_preset(
                preset_id,
                name=str(payload.get("name") or "").strip(),
                note=str(payload.get("note") or "").strip(),
            )
        except Exception as exc:
            return error_response(str(exc))
        return json_response({"ok": True, "id": clean, "warnings": warnings})

    async def api_preset_apply(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        preset_id = str(payload.get("id") or "").strip()
        if not preset_id:
            return error_response("缺少预设 id")
        blocks = payload.get("blocks")
        if blocks is not None and not isinstance(blocks, list):
            return error_response("blocks 必须是数组")
        # 默认**不清状态**：切预设本来只想换世界，上下文留着更符合直觉
        clear_state = bool(payload.get("clear_state", False))
        try:
            result = self.store.apply_preset(preset_id, blocks=blocks)
        except Exception as exc:
            return error_response(f"应用预设失败：{exc}")
        warnings = list(result.get("warnings") or [])
        warnings.extend(self.engine.reload_config())
        repairs: list[str] = []
        if clear_state:
            cleared = await self.engine.clear_all_states()
        else:
            cleared = 0
            # 不清状态就得把状态修一遍：地图/动作换过之后，旧的地点与计划可能已经不存在
            repairs = await self.engine.repair_states_after_config_change()
        return json_response(
            {
                "ok": True,
                "id": preset_id,
                "blocks": list(result.get("blocks") or []),
                "cleared_sessions": cleared,
                "repairs": repairs,
                "warnings": warnings,
            }
        )

    async def api_preset_delete(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        preset_id = str(payload.get("id") or "").strip()
        if not self.store.delete_preset(preset_id):
            return error_response("找不到这个预设")
        return json_response({"ok": True})

    async def api_preset_rename(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        preset_id = str(payload.get("id") or "").strip()
        data = self.store.read_preset(preset_id)
        if not data:
            return error_response("找不到这个预设")
        data["name"] = str(payload.get("name") or "").strip() or preset_id
        if payload.get("note") is not None:
            data["note"] = str(payload.get("note") or "")
        warnings = self.store.write_preset(preset_id, data)
        return json_response({"ok": True, "warnings": warnings})

    async def api_preset_json(self):
        """GET：读出预设原文；POST：整段写回（导入 / 直接编辑都走这里）。"""

        if request.method == "POST":
            payload = await request.json(default={}) or {}
            guard = self._guard(payload)
            if guard is not None:
                return guard
            preset_id = str(payload.get("id") or "").strip()
            if not preset_id:
                return error_response("缺少预设 id")
            raw = payload.get("payload")
            if isinstance(raw, dict):
                data = raw
            else:
                text = str(payload.get("json") or "").strip()
                try:
                    data = json.loads(text) if text else {}
                except json.JSONDecodeError as exc:
                    return error_response(f"JSON 格式不对：{exc}")
            try:
                warnings = self.store.write_preset(preset_id, data)
            except Exception as exc:
                return error_response(f"写入预设失败：{exc}")
            return json_response({"ok": True, "id": preset_id, "warnings": warnings})
        guard = self._guard()
        if guard is not None:
            return guard
        preset_id = request.query.get("id", "") or ""
        data = self.store.read_preset(preset_id)
        if not data:
            return error_response("找不到这个预设")
        return json_response({"preset": data})

    async def api_backup(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        path = self.store.backup(str(payload.get("tag", "") or ""))
        return json_response({"ok": True, "file": path.name})

    # ---------------- 改动历史 ----------------

    async def api_history(self):
        """改动历史列表（新的在前）。"""

        guard = self._guard()
        if guard is not None:
            return guard
        return json_response(
            {
                "items": self.store.list_history(),
                "keep": HISTORY_KEEP,
            }
        )

    async def api_history_item(self):
        """一条历史：快照内容 + 当前配置 + 逐块差异（前端据此画左右对比）。"""

        guard = self._guard()
        if guard is not None:
            return guard
        snapshot_id = request.query.get("id", "") or ""
        if not snapshot_id:
            return error_response("缺少 id")
        try:
            return json_response(self.store.read_history(snapshot_id))
        except Exception as exc:
            return error_response(str(exc))

    async def api_history_restore(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        snapshot_id = str(payload.get("id") or "").strip()
        if not snapshot_id:
            return error_response("缺少 id")
        blocks = payload.get("blocks")
        picked = (
            [str(item) for item in blocks] if isinstance(blocks, list) else None
        )
        try:
            result = self.store.restore_history(snapshot_id, blocks=picked)
        except Exception as exc:
            return error_response(f"恢复失败：{exc}")
        self.engine.reload_config()
        return json_response({"ok": True, **result})

    async def api_history_delete(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        snapshot_id = str(payload.get("id") or "").strip()
        if not snapshot_id:
            return error_response("缺少 id")
        return json_response({"ok": self.store.delete_history(snapshot_id)})

    async def api_history_clear(self):
        payload = await request.json(default={}) or {}
        guard = self._guard(payload)
        if guard is not None:
            return guard
        return json_response({"ok": True, "removed": self.store.clear_history()})
