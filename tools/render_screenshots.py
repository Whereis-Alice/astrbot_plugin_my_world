"""渲染 README 用的编辑器截图（不需要真的跑 AstrBot）。

做法：起一个本地静态服务器指向 ``pages/world_editor``，用 Playwright 打开页面，
把 ``window.AstrBotPluginPage`` 换成一份桩——数据来自 ``core/defaults.py``
与 ``core/tool_import.py``，所以截出来的就是插件页真实的样子。

用法（需要 playwright + chromium，开发机才有）：::

    python tools/render_screenshots.py docs/images

生成动作管理、工具导入、分组改名截图，并验证改名保存和桌面 / 手机弹窗边界。
"""

from __future__ import annotations

import functools
import http.server
import json
import os
import socket
import sys
import threading
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

from core.defaults import (  # noqa: E402
    CORE_BUILTIN_ACTIONS,
    DEFAULT_CAPTION_PROMPT,
    DEFAULT_CAPTION_RELATION_PROMPT,
    DEFAULT_FORWARD_PROMPT,
    DEFAULT_WORLD,
    default_schedules,
    default_world,
)
from core.models import DEFAULT_INTIMACY, FIELD_LABELS, parse_world  # noqa: E402
from core.tool_import import build_drafts  # noqa: E402

PAGE_DIR = PLUGIN_DIR / "pages" / "world_editor"

WRAPPER = """<!doctype html>
<html lang="zh-CN">
  <head><meta charset="utf-8" /><title>harness</title></head>
  <body style="margin:0;background:#0b0d12">
    <iframe id="harness" src="./index.html"
      style="width:1600px;height:1080px;border:0;display:block"></iframe>
  </body>
</html>
"""


def fake_tools() -> list[dict]:
    """一批像真的注册过的工具（名字取自常见的插件）。"""

    rows = [
        ("web_search_tavily", "使用 Tavily 搜索网页，返回标题、链接与摘要。", "official", ""),
        ("web_search_bocha", "使用博查搜索网页，适合查中文资讯。", "official", ""),
        ("web_extract", "抓取网页正文并转成 markdown，适合读一篇文章。", "official", ""),
        ("kb_search", "在 AstrBot 知识库里做语义检索。", "official", ""),
        ("send_message_to_user", "直接把消息发送给当前会话，不经过回复管线。", "official", ""),
        ("magpie_search_meme", "从表情库检索候选表情包，返回编号与文字说明。", "plugin", "astrbot_plugin_meme_magpie"),
        ("magpie_send_meme", "发送编号对应的表情包。", "plugin", "astrbot_plugin_meme_magpie"),
        ("magpie_steal_meme", "把一张图片收录进表情库，并写上作品、角色与情绪。", "plugin", "astrbot_plugin_meme_magpie"),
        ("worldtree_lore_query", "按关键词检索世界树设定条目。", "plugin", "astrbot_plugin_worldtree_lore"),
        ("genie_tts_speak", "把一段文字合成为语音发到当前会话。", "plugin", "astrbot_plugin_genie_tts_llm"),
        ("genie_tts_emotion", "查看当前语音角色与可用情绪。", "plugin", "astrbot_plugin_genie_tts_llm"),
        ("bili_search", "搜索 B 站视频，返回标题、作者与链接。", "plugin", "astrbot_plugin_bilibili_pro"),
        ("bili_video_info", "查询一个 B 站视频的详细信息。", "plugin", "astrbot_plugin_bilibili_pro"),
        ("draw_image", "按提示词生成一张图片，返回图片地址。", "plugin", "astrbot_plugin_alice_image_assistant"),
        ("edit_image", "按提示词修改一张已有图片。", "plugin", "astrbot_plugin_alice_image_assistant"),
        ("get_weather", "查询某个城市的天气与温度。", "plugin", "astrbot_plugin_helper_tools"),
        ("get_time", "查询当前时间与时区。", "plugin", "astrbot_plugin_helper_tools"),
        ("translate_text", "把一段文字翻译成另一种语言。", "plugin", "astrbot_plugin_helper_tools"),
        ("music_search", "搜索歌曲并返回可播放的音频链接。", "plugin", "astrbot_plugin_music_plus"),
        ("music_send", "把点好的歌发到当前会话。", "plugin", "astrbot_plugin_music_plus"),
        ("news_digest", "抓取今日新闻热点并整理成几条要点。", "plugin", "astrbot_plugin_orbit_oracle"),
        ("repo_search", "在 GitHub 上搜索仓库。", "plugin", "astrbot_plugin_ssh_bridge"),
        ("shell_exec", "在内网机器上执行一条命令。", "plugin", "astrbot_plugin_ssh_bridge"),
        ("memorize", "把一件事写进长期记忆。", "plugin", "astrbot_plugin_anamnesis"),
        ("recall_memory", "按关键词回忆之前记下的事。", "plugin", "astrbot_plugin_anamnesis"),
        ("schedule_add", "新增一条定时任务。", "plugin", "astrbot_plugin_helper_tools"),
        ("weather_forecast", "查询未来几天的天气预报。", "plugin", "astrbot_plugin_helper_tools"),
        ("ocr_image", "识别图片里的文字。", "plugin", "astrbot_plugin_video_vision_helper"),
        ("video_summary", "把一段视频总结成几句话。", "plugin", "astrbot_plugin_video_vision_helper"),
        ("random_cat", "随机来一张猫猫图。", "plugin", "astrbot_plugin_random_cat"),
        ("meme_forge", "按文字做一张梗图。", "plugin", "astrbot_plugin_meme_forge"),
        ("math_render", "把公式渲染成图片。", "plugin", "astrbot_plugin_math_render"),
        ("xiangqi_move", "下一手中国象棋并返回棋盘图。", "plugin", "astrbot_plugin_xiangqi"),
        ("youtube_download", "下载一个 YouTube 视频。", "plugin", "astrbot_plugin_youtube_nova"),
        ("twitter_hot", "查某个关键词的推文热度。", "plugin", "astrbot_plugin_my_twitter"),
        ("profile_weave", "查看某个群友的画像。", "plugin", "astrbot_plugin_profile_weaver"),
        ("context_scene", "查询当前场景的记忆片段。", "plugin", "astrbot_plugin_context_scene_memory"),
        ("gif_frame", "把动图拆成几帧并描述画面。", "plugin", "astrbot_plugin_gif_frame_vision"),
        ("image_bed", "把一张图片传到图床并返回链接。", "plugin", "astrbot_plugin_imgbed_ferry"),
        ("log_vault", "查询历史上的日志记录。", "plugin", "astrbot_plugin_logvault"),
    ]
    tools = []
    for name, desc, source, plugin in rows:
        parameters = {"type": "object", "properties": {}, "required": []}
        if name in ("web_search_tavily", "web_search_bocha", "kb_search", "news_digest"):
            parameters = {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "查询词"},
                    "limit": {"type": "number", "description": "返回条数，默认 5", "default": 5},
                },
                "required": ["query"],
            }
        elif name in ("magpie_search_meme", "music_search", "repo_search"):
            parameters = {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键词"},
                },
                "required": ["query"],
            }
        elif name in ("genie_tts_speak", "translate_text"):
            parameters = {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要处理的文字"},
                    "emotion": {"type": "string", "description": "情绪（可选）"},
                },
                "required": ["text"],
            }
        elif name == "get_weather":
            parameters = {
                "type": "object",
                "properties": {"city": {"type": "string", "description": "城市名"}},
                "required": ["city"],
            }
        elif name == "send_message_to_user":
            parameters = {
                "type": "object",
                "properties": {"message": {"type": "string", "description": "要发送的内容"}},
                "required": ["message"],
            }
        tools.append(
            {
                "name": name,
                "description": desc,
                "parameters": parameters,
                "param_text": "、".join(parameters["properties"].keys()) or "（这个工具不需要参数）",
                "source": source,
                "plugin": plugin,
                "action_ids": [],
                "action_names": [],
            }
        )
    return tools


def build_payload() -> dict:
    tools = fake_tools()
    world = default_world()
    world["wizard_done"] = True
    world["global_allowed_tools"] = ["web_search_tavily", "web_extract"]
    drafts = build_drafts(
        tools,
        options={"group_mode": "source", "duplicate": "skip"},
        existing_actions=world["actions"],
    )["drafts"]
    # 只留一部分做成动作：另一部分留给「从工具导入」弹窗里演示
    world["actions"] = [*world["actions"], *drafts[:22]]
    # 真实接口返回的是"校验后的模型 + 用户自己加的键"，这里照抄一遍，
    # 不然模型补出来的默认段（例如 abilities）在编辑器里是 undefined
    parsed, _ = parse_world(world)
    world = {**world, **parsed.model_dump(mode="json", by_alias=True)}
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
    imported = {draft["tool_names"][0]: draft for draft in drafts[:22]}
    for tool in tools:
        draft = imported.get(tool["name"])
        if draft:
            tool["action_ids"] = [draft["id"]]
            tool["action_names"] = [draft["name"]]
    return {
        "config": {
            "world": world,
            "schedules": default_schedules(),
            "sessions": {"sessions": [], "groups": []},
            "warnings": [],
            "providers": {},
            "vision_cache": {},
            "weather": {},
            "extension_actions": [],
        },
        "tools": {"tools": tools, "global_allowed": world["global_allowed_tools"]},
        "defaults": {
            "core_actions": sorted(CORE_BUILTIN_ACTIONS),
            "actions": {
                str(item.get("id")): {
                    "name": item.get("name", ""),
                    "description": item.get("description", ""),
                    "nickname_text": item.get("nickname_text", ""),
                }
                for item in DEFAULT_WORLD.get("actions") or []
            },
            "field_labels": dict(FIELD_LABELS),
            "captions": {
                "look": DEFAULT_CAPTION_PROMPT,
                "relation": DEFAULT_CAPTION_RELATION_PROMPT,
                "forward": DEFAULT_FORWARD_PROMPT,
            },
            "debug_events": [],
        },
    }


BRIDGE_JS = r"""
(function () {
  const payload = __PAYLOAD__;
  try {
    window.localStorage.setItem("mw-theme", "light");
  } catch (error) {}
  const table = {
    "auth/status": { password_required: false, locked_seconds: 0, tick_interval: 60, decider_interval: 300 },
    "config": payload.config,
    "tools": payload.tools,
    "defaults": payload.defaults,
    "schedules": payload.config.schedules,
    "sessions": payload.config.sessions,
    "states": { sessions: [] },
    "state": {},
    "events": {},
    "logs": { logs: [], total: 0 },
    "memories": { memories: [], total: 0 },
    "prompt": { text: "", sections: [] },
    "persona-brief": {},
    "history": { items: [] },
    "profile/people": {
      people: [{
        user_id: "10001", name: "小鲸鱼", qq_name: "小鲸鱼",
        affinity: 72, level: "亲近", bonds: ["朋友"], claims: [],
        digest: "爱喝热茶、常熬夜，最近在准备考试", message_count: 128,
        last_seen_at: 1760000000, first_seen_at: 1750000000, days: 32,
        weaver: {
          display_name: "小鲸鱼", subject_name: "小鲸鱼",
          completeness: 68.5, locked_count: 1,
          preview: [
            { name: "爱好", value: "热茶、猫" },
            { name: "喜欢的话题", value: "安静的房间" },
          ],
        },
      }],
      enabled: true, consolidate_enabled: true, digest_limit: 5,
      weaver_enabled: true, weaver_session_based: false,
    },
    "profile/contacts": {
      people: [{
        user_id: "10001", name: "小鲸鱼", display_name: "小鲸鱼",
        qq_name: "小鲸鱼", affinity: 72, level: "亲近",
        bonds: ["朋友"], claims: [], digest: "爱喝热茶、常熬夜，最近在准备考试",
        message_count: 128, last_seen_at: 1760000000, first_seen_at: 1750000000,
        days: 32, has_detailed_profile: true, profile_record_count: 1,
        profile_locked_count: 1,
        profile_preview: [
          { name: "喜欢的话题", value: "猫、热茶、安静的房间" },
        ],
        weaver: {
          display_name: "小鲸鱼", subject_name: "小鲸鱼",
          completeness: 68.5, locked_count: 1, record_count: 1,
          preview: [
            { name: "爱好", value: "热茶、猫" },
            { name: "喜欢的话题", value: "安静的房间" },
          ],
        },
      }],
      enabled: true, consolidate_enabled: true, digest_limit: 5,
      weaver_enabled: true, weaver_session_based: false,
    },
    "profile/person": {
      session: "aiocqhttp:GroupMessage:1001", user_id: "10001",
      person: {
        user_id: "10001", name: "小鲸鱼", qq_name: "小鲸鱼", affinity: 72,
        affinities: ["朋友"], claims: [], past: [],
        facts: [{ kind: "喜好", text: "喜欢热茶", evidence: "我平时爱喝热茶" }],
        digest: "爱喝热茶、常熬夜", note: "晚上容易忘记休息",
        message_count: 128, days_known: 32, cards: {},
        level: { name: "亲近", index: 3, prompt: "", deny: [] },
      },
      facts: [{ id: 1, kind: "喜好", text: "喜欢热茶", status: "active", pinned: true }],
      bonds: [{ id: 1, type: "朋友", status: "current", since_text: "2026-01-02" }],
      affinity_logs: [],
      bonds_config: [{ name: "朋友", slot: "friend", cap: 4, unique: false, negative: false }],
      levels_config: [{ name: "亲近", min_affinity: 60, max_affinity: 79, deny: [], prompt: "" }],
      grudge: null, own_topics: [], miss: { value: 0.2, waiting: false },
      weaver: {
        key: "_10001", user_id: "10001", subject_name: "小鲸鱼",
        display_name: "小鲸鱼", completeness: 68.5, locked_count: 1,
        updated_at: "2026-10-12 20:11:00",
        fields: [
          { name: "昵称", value: "小鲸鱼", is_custom: false, locked: true,
            updated_at: "2026-10-12 20:10:00", evidence: "以后叫我小鲸鱼" },
          { name: "喜欢的话题", value: "猫、热茶、安静的房间", is_custom: false,
            locked: false, updated_at: "2026-10-12 20:11:00",
            evidence: "我平时喜欢聊猫和热茶" },
        ],
        notes: ["最近在准备考试"],
        available_fields: [
          { name: "昵称", filled: true },
          { name: "喜欢的话题", filled: true },
          { name: "生日", filled: false },
        ],
      },
    },
    "profile/contact": {
      session: "aiocqhttp:GroupMessage:1001", user_id: "10001",
      identity: {
        display_name: "小鲸鱼", call_me: "小鲸鱼", call_him: "小鲸",
        note: "最近在准备考试", completeness: 68.5,
        record_count: 1, locked_count: 1,
      },
      person: {
        user_id: "10001", name: "小鲸鱼", qq_name: "小鲸鱼", affinity: 72,
        affinities: ["朋友"], claims: [], past: [], call_me: "小鲸鱼",
        call_him: "小鲸", facts: [{ kind: "喜好", text: "喜欢热茶", evidence: "我平时爱喝热茶" }],
        digest: "爱喝热茶、常熬夜", note: "最近在准备考试",
        message_count: 128, days_known: 32, cards: {},
        level: { name: "亲近", index: 3, prompt: "", deny: [] },
      },
      facts: [{ id: 1, kind: "喜好", text: "喜欢热茶", status: "active", pinned: true }],
      bonds: [{ id: 1, type: "朋友", status: "current", since_text: "2026-01-02" }],
      affinity_logs: [],
      bonds_config: [{ name: "朋友", slot: "friend", cap: 4, unique: false, negative: false }],
      levels_config: [{ name: "亲近", min_affinity: 60, max_affinity: 79, deny: [], prompt: "" }],
      grudge: null, own_topics: [], miss: { value: 0.2, waiting: false },
      weaver: {
        key: "10001", user_id: "10001", subject_name: "小鲸鱼",
        display_name: "小鲸鱼", completeness: 68.5, locked_count: 1,
        updated_at: "2026-10-12 20:11:00", record_count: 1,
        fields: [
          { name: "昵称", value: "小鲸鱼", is_custom: false, locked: true,
            updated_at: "2026-10-12 20:10:00", evidence: "以后叫我小鲸鱼" },
          { name: "喜欢的话题", value: "猫、热茶、安静的房间", is_custom: false,
            locked: false, updated_at: "2026-10-12 20:11:00",
            evidence: "我平时喜欢聊猫和热茶" },
        ],
        notes: ["最近在准备考试"],
        available_fields: [
          { name: "昵称", filled: true },
          { name: "喜欢的话题", filled: true },
          { name: "生日", filled: false },
        ],
      },
      weaver_records: [{
        key: "10001", session_id: "", completeness: 68.5, locked_count: 1,
        updated_at: "2026-10-12 20:11:00",
        fields: [
          { name: "昵称", value: "小鲸鱼", locked: true },
          { name: "喜欢的话题", value: "猫、热茶、安静的房间", locked: false },
        ],
        notes: ["最近在准备考试"],
      }],
    },
    "profile/contact/records": {
      user_id: "10001", preferred_key: "10001",
      records: [{
        key: "10001", user_id: "10001", session_id: "",
        display_name: "小鲸鱼", completeness: 68.5, locked_count: 1,
        field_count: 2, updated_at: "2026-10-12 20:11:00",
      }],
    },
    "profile/weaver/meta": {
      enabled: true, session_based: false, webui_allow_edit: true,
      field_lock_scope: "llm_only", field_lock_scope_label: "仅拦截模型",
      data_dir: "（测试数据目录）",
      legacy_imported: true, legacy_source_dir: "astrbot_plugin_profile_weaver",
      audit_actions: ["upsert_field", "lock_field", "delete_field"],
      actor_types: [{ id: "webui", label: "通讯录" }],
      builtin_fields: [{ name: "昵称", description: "用户希望如何被称呼" }],
    },
    "profile/weaver/stats": {
      stats: {
        profile_count: 18, user_count: 16, avg_completeness: 62.4,
        locked_field_count: 7, audit_count: 143, backup_count: 3,
      },
      coverage: [
        { name: "昵称", count: 18, ratio: 100 },
        { name: "爱好", count: 12, ratio: 66.7 },
        { name: "喜欢的话题", count: 9, ratio: 50 },
      ],
      custom_fields: [{ name: "最喜欢的国漫", count: 4 }],
    },
    "profile/weaver/library": {
      total: 1, page: 1, page_size: 40, page_count: 1,
      platforms: ["aiocqhttp"],
      items: [{
        key: "_10001", user_id: "10001", session_id: "",
        subject_name: "小鲸鱼", display_name: "小鲸鱼",
        platform: "aiocqhttp", chat_type: "group",
        field_count: 2, custom_field_count: 0, note_count: 1,
        locked_count: 1, locked_fields: ["昵称"], completeness: 68.5,
        created_at: "2026-10-01 12:00:00", updated_at: "2026-10-12 20:11:00",
        preview: [
          { name: "昵称", value: "小鲸鱼" },
          { name: "喜欢的话题", value: "猫、热茶、安静的房间" },
        ],
      }],
    },
    "profile/weaver/key": {
      key: "_10001",
      profile: {
        key: "_10001", user_id: "10001", session_id: "",
        subject_name: "小鲸鱼", display_name: "小鲸鱼",
        platform: "aiocqhttp", chat_type: "group",
        completeness: 68.5, locked_count: 1,
        field_lock_scope_label: "仅拦截模型",
        created_at: "2026-10-01 12:00:00", updated_at: "2026-10-12 20:11:00",
        fields: [
          { name: "昵称", value: "小鲸鱼", is_custom: false, locked: true,
            updated_at: "2026-10-12 20:10:00", actor_name: "小鲸鱼",
            source_kind: "manual_edit", evidence: "以后叫我小鲸鱼" },
          { name: "喜欢的话题", value: "猫、热茶、安静的房间", is_custom: false,
            locked: false, updated_at: "2026-10-12 20:11:00",
            actor_name: "心迹画像", source_kind: "self_preference",
            evidence: "我平时喜欢聊猫和热茶" },
        ],
        notes: ["最近在准备考试"],
        available_fields: [
          { name: "昵称", filled: true, locked: true },
          { name: "喜欢的话题", filled: true, locked: false },
          { name: "生日", filled: false, locked: false },
        ],
      },
    },
    "profile/weaver/audit": {
      total: 1,
      items: [{
        at: "2026-10-12 20:11:00", subject_user_id: "10001",
        subject_name: "小鲸鱼", field_name: "喜欢的话题",
        action: "upsert_field", action_label: "写入字段",
        old_value: "猫", new_value: "猫、热茶、安静的房间",
        actor_type: "LLM 工具", actor_name: "心迹画像",
        evidence: "我平时喜欢聊猫和热茶",
      }],
    },
    "profile/weaver/backup": {
      items: [{ name: "profiles-manual-20261012.json", size: 8192, modified_at: "2026-10-12 20:30:00" }],
    },
    "profile/weaver/history": {
      total: 1,
      items: [{ at: "2026-10-12 20:11:00", action: "upsert_field",
        old_value: "猫", new_value: "猫、热茶", evidence: "我平时喜欢聊猫和热茶" }],
    },
  };
  window.AstrBotPluginPage = {
    ready: async () => ({
      pluginName: "astrbot_plugin_my_world",
      displayName: "小世界",
      locale: "zh-CN",
      pluginI18n: {},
    }),
    getContext: () => ({ locale: "zh-CN" }),
    getLocale: () => "zh-CN",
    getI18n: () => ({}),
    t: (key, fallback) => fallback || key,
    onContextChange: () => {},
    offContextChange: () => {},
    apiGet: async (endpoint) => {
      const key = String(endpoint || "").replace(/^\/+/, "");
      if (Object.prototype.hasOwnProperty.call(table, key)) return structuredClone(table[key]);
      return {};
    },
    apiPost: async (endpoint, data) => {
      const key = String(endpoint || "").replace(/^\/+/, "");
      if (key === "config/world") table.config.world = structuredClone(data.world);
      if (key === "config/schedules") table.config.schedules = structuredClone(data.schedules);
      if (key === "config/sessions") table.config.sessions = structuredClone(data.sessions);
      if (Object.prototype.hasOwnProperty.call(table, key)) return table[key];
      if (key === "tools/import") return { ok: true, dry_run: true, drafts: [], warnings: [] };
      return { ok: true };
    },
    upload: async () => ({}),
    download: async () => {},
    subscribeSSE: () => "sub",
    unsubscribeSSE: () => {},
  };
})();
"""


class Handler(http.server.SimpleHTTPRequestHandler):
    # Windows 上的 mimetypes 有时把 .js 认成 text/plain，
    # 浏览器会因此拒绝加载 ES module，这里显式钉死几个类型
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".html": "text/html",
        ".js": "text/javascript",
        ".mjs": "text/javascript",
        ".css": "text/css",
        ".json": "application/json",
        ".svg": "image/svg+xml",
        ".png": "image/png",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PAGE_DIR), **kwargs)

    def do_GET(self):  # noqa: N802 - 标准库规定的名字
        if self.path.startswith("/harness.html"):
            body = WRAPPER.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def log_message(self, *args):  # 静音：截图脚本不需要访问日志
        return


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> None:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else PLUGIN_DIR / "docs" / "images"
    out_dir.mkdir(parents=True, exist_ok=True)
    from playwright.sync_api import sync_playwright

    port = free_port()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}/harness.html"
    script = BRIDGE_JS.replace("__PAYLOAD__", json.dumps(build_payload(), ensure_ascii=False))
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            context = browser.new_context(
                viewport={"width": 1600, "height": 1080},
                device_scale_factor=1.25,
                locale="zh-CN",
            )
            context.add_init_script(script=script)
            page = context.new_page()
            page.emulate_media(reduced_motion="reduce")
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(url, wait_until="load")
            frame = page.frame_locator("#harness")
            frame.locator("#app").wait_for(state="visible", timeout=30000)
            frame.locator('button[data-tab="actions"]').click()
            frame.locator("#action-grid").wait_for(state="visible", timeout=15000)
            page.wait_for_timeout(600)
            page.screenshot(path=str(out_dir / "actions-manage.png"))
            print("wrote", out_dir / "actions-manage.png")

            rename = frame.get_by_role("button", name="重命名分组：官方工具", exact=True)
            rename.click()
            frame.locator('[data-dialog-key="name"]').fill("常用查询")
            frame.locator("#dialog-card").screenshot(path=str(out_dir / "actions-rename.png"))
            frame.locator("#dialog-ok").click()
            renamed = frame.get_by_role("button", name="重命名分组：常用查询", exact=True)
            renamed.wait_for(state="visible")
            frame.locator("#save").click()
            child = page.frames[1]
            child.wait_for_function("async () => (await window.AstrBotPluginPage.apiGet('config')).world.actions.some(a => a.group === '常用查询')")
            saved = child.evaluate("async () => (await window.AstrBotPluginPage.apiGet('config')).world")
            assert any(action.get("group") == "常用查询" for action in saved["actions"])
            assert not any(action.get("group") == "官方工具" for action in saved["actions"])
            rename_count = renamed.count()
            renamed.click()
            frame.locator('[data-dialog-key="name"]').fill("常用查询")
            frame.locator("#dialog-ok").click()
            assert renamed.count() == rename_count
            print("verified group rename and persisted save")

            for width, height in ((1280, 800), (390, 844)):
                page.set_viewport_size({"width": width, "height": height})
                page.locator("#harness").evaluate(
                    "(node, size) => {node.style.width=size.width+'px';node.style.height=size.height+'px';}",
                    {"width": width, "height": height},
                )
                frame.get_by_role("button", name="重命名分组：常用查询", exact=True).click()
                dialog = frame.locator("#dialog-card")
                box = dialog.bounding_box()
                assert box and box["x"] >= 0 and box["x"] + box["width"] <= width + 1
                page.screenshot(path=str(out_dir / f"actions-rename-{width}.png"))
                frame.locator("#dialog-cancel").click()
            print("verified desktop/mobile rename dialog bounds")
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
