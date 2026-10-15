"""「把工具一键导入成动作」的纯逻辑测试。

这一层不碰 AstrBot、也不碰数据库：喂进去工具元信息，检查生成的草稿
能不能过世界配置的校验、重复导入会不会重复建动作、参数提示对不对。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 导入草稿要能过世界校验：显式打开旧版动作开关（生产默认已停用）。
os.environ["MY_WORLD_LEGACY_TOOL_ACTIONS"] = "1"

from core.defaults import default_world  # noqa: E402
from core.models import parse_world  # noqa: E402
from core.tool_import import (  # noqa: E402
    build_drafts,
    existing_action_index,
    guess_action_name,
    guess_group,
    humanize_tool_name,
    looks_like_self_send,
    merge_drafts,
    param_rows,
    slugify,
    unique_action_id,
)


def tool(
    name: str,
    *,
    description: str = "",
    source: str = "plugin",
    plugin: str = "demo_plugin",
    parameters: dict | None = None,
) -> dict:
    return {
        "name": name,
        "description": description,
        "parameters": parameters or {},
        "source": source,
        "plugin": plugin,
    }


SEARCH_TOOL = tool(
    "demo_search_news",
    description="搜索最近一天的新闻；只返回标题与链接。",
    parameters={
        "type": "object",
        "properties": {
            "keywords": {"type": "string", "description": "搜索关键词"},
            "limit": {"type": "number", "description": "返回条数", "default": 5},
        },
        "required": ["keywords"],
    },
)


class SlugTests(unittest.TestCase):
    def test_slugify_keeps_ascii_and_strips_namespace(self):
        self.assertEqual(slugify("demo.search_news"), "search_news")
        self.assertEqual(slugify("  搜索 工具  "), "tool")
        self.assertEqual(slugify("2fa_check"), "t2fa_check")

    def test_unique_action_id_appends_suffix(self):
        taken = {"tool_search", "tool_search_2"}
        self.assertEqual(unique_action_id("tool_search", taken), "tool_search_3")
        self.assertEqual(unique_action_id("tool_other", taken), "tool_other")

    def test_humanize_tool_name_strips_plugin_prefix(self):
        self.assertEqual(
            humanize_tool_name("astrbot_plugin_demo.search_news"),
            "search news",
        )


class NamingTests(unittest.TestCase):
    def test_name_prefers_first_clause_of_description(self):
        self.assertEqual(guess_action_name(SEARCH_TOOL), "搜索最近一天的新闻")

    def test_name_falls_back_to_tool_name(self):
        self.assertEqual(
            guess_action_name(tool("demo_search_news")),
            "search news",
        )

    def test_group_follows_source(self):
        self.assertEqual(
            guess_group(tool("x", source="official")),
            "官方工具",
        )
        self.assertEqual(
            guess_group(tool("x", source="plugin", plugin="meme_magpie")),
            "插件工具 · meme_magpie",
        )
        self.assertEqual(
            guess_group(tool("x", source="plugin", plugin=""), mode="fixed", fixed="我的工具"),
            "我的工具",
        )


class ParamTests(unittest.TestCase):
    def test_param_rows_reads_schema(self):
        rows = param_rows(SEARCH_TOOL)
        self.assertEqual([row["name"] for row in rows], ["keywords", "limit"])
        self.assertTrue(rows[0]["required"])
        self.assertFalse(rows[1]["required"])
        self.assertEqual(rows[1]["default"], 5)

    def test_param_rows_tolerates_broken_schema(self):
        self.assertEqual(param_rows({"name": "x", "parameters": "nope"}), [])
        self.assertEqual(param_rows({"name": "x"}), [])


class SelfSendTests(unittest.TestCase):
    def test_detects_direct_send_tools(self):
        self.assertTrue(
            looks_like_self_send(tool("send_message", description="直接发送消息给用户。"))
        )
        self.assertFalse(looks_like_self_send(SEARCH_TOOL))


class BuildDraftsTests(unittest.TestCase):
    def test_creates_one_action_per_tool(self):
        result = build_drafts([SEARCH_TOOL], options={}, existing_actions=[])
        drafts = result["drafts"]
        self.assertEqual(len(drafts), 1)
        draft = drafts[0]
        self.assertEqual(draft["id"], "tool_demo_search_news")
        self.assertEqual(draft["name"], "搜索最近一天的新闻")
        self.assertEqual(draft["llm_level"], "tool")
        self.assertEqual(draft["tool_names"], ["demo_search_news"])
        self.assertEqual(draft["group"], "插件工具 · demo_plugin")
        self.assertEqual(draft["params"], {})
        self.assertEqual(draft["on_complete"]["trigger"], "llm_followup")
        self.assertEqual(draft["param_summary"], "2 个参数（必填 keywords）")

    def test_drafts_pass_world_validation(self):
        world = default_world()
        result = build_drafts(
            [SEARCH_TOOL, tool("demo_make_image", description="生成一张图片。")],
            options={"scope": "node", "node_ids": ["study"]},
            existing_actions=world["actions"],
        )
        world["actions"] = [*world["actions"], *result["drafts"]]
        parsed, warnings = parse_world(world)
        self.assertEqual(warnings, [])
        ids = {action.id for action in parsed.actions}
        self.assertIn("tool_demo_search_news", ids)
        scoped = next(
            action for action in parsed.actions if action.id == "tool_demo_search_news"
        )
        self.assertEqual(scoped.scope, "node")
        self.assertEqual(scoped.allowed_nodes, ["study"])

    def test_existing_action_is_skipped_unless_update(self):
        actions = [{"id": "tool_demo_search_news", "name": "旧的", "llm_level": "tool",
                    "tool_names": ["demo_search_news"]}]
        skipped = build_drafts([SEARCH_TOOL], options={}, existing_actions=actions)
        self.assertEqual(skipped["drafts"], [])
        self.assertEqual(skipped["already"], ["demo_search_news"])

        updated = build_drafts(
            [SEARCH_TOOL], options={"duplicate": "update"}, existing_actions=actions
        )
        self.assertEqual(len(updated["drafts"]), 1)
        self.assertEqual(updated["drafts"][0]["id"], "tool_demo_search_news")
        self.assertEqual(updated["updated"], ["demo_search_news"])

    def test_direct_send_tool_is_skipped_with_warning(self):
        result = build_drafts(
            [tool("send_message", description="直接发送消息给当前会话。")],
            options={},
            existing_actions=[],
        )
        self.assertEqual(result["drafts"], [])
        self.assertEqual(result["skipped"], ["send_message"])
        self.assertTrue(any("绕过" in item for item in result["warnings"]))

    def test_param_pins_and_model_plan(self):
        result = build_drafts(
            [SEARCH_TOOL],
            options={
                "param_pins": {"demo_search_news": {"limit": 3, "empty": ""}},
                "plans": {"demo_search_news": {"name": "看看新闻", "group": "搜索"}},
            },
            existing_actions=[],
        )
        draft = result["drafts"][0]
        self.assertEqual(draft["name"], "看看新闻")
        self.assertEqual(draft["group"], "搜索")
        self.assertEqual(draft["params"], {"limit": {"type": "string", "value": "3"}})

    def test_missing_description_is_warned(self):
        result = build_drafts([tool("demo_x")], options={}, existing_actions=[])
        self.assertTrue(any("没有写说明" in item for item in result["warnings"]))

    def test_existing_action_index_reads_both_field_shapes(self):
        index = existing_action_index(
            [
                {"id": "a", "llm_level": "tool", "tool_names": ["x", "y"]},
                {"id": "b", "llm_level": "tool", "tool_name": "z"},
                {"id": "c", "llm_level": "template", "tool_names": ["ignored"]},
            ]
        )
        self.assertEqual(sorted(index), ["x", "y", "z"])


class MergeTests(unittest.TestCase):
    def test_merge_counts_and_strips_editor_fields(self):
        actions = [{"id": "keep", "name": "保留"}]
        drafts = [
            {"id": "new", "name": "新的", "param_rows": [{"name": "x"}], "param_summary": "1 个参数"},
            {"id": "keep", "name": "覆盖"},
        ]
        merged, stats = merge_drafts(actions, drafts)
        self.assertEqual(stats, {"created": 1, "updated": 1})
        self.assertEqual([item["id"] for item in merged], ["keep", "new"])
        self.assertEqual(merged[0]["name"], "覆盖")
        self.assertNotIn("param_rows", merged[1])
        self.assertNotIn("param_summary", merged[1])


if __name__ == "__main__":
    unittest.main()
