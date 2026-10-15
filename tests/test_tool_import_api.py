"""「从工具导入」这条 Web API 的端到端测试（用 AstrBot 桩）。

覆盖：dry_run 只算草稿、正式导入落盘并热加载、重复导入默认跳过、
工具列表接口会标出"哪个工具已经被做成动作"。
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
sys.path.insert(0, PLUGIN_DIR)
sys.path.insert(0, HERE)
# 这条 API 只服务旧版「工具导入」迁移路径：显式打开旧版动作开关（生产默认已停用）。
os.environ["MY_WORLD_LEGACY_TOOL_ACTIONS"] = "1"

import fake_astrbot  # noqa: E402

fake_astrbot.install()


def load_plugin_module():
    if "mw_import_plugin" in sys.modules:
        return sys.modules["mw_import_plugin"]
    spec = importlib.util.spec_from_file_location(
        "mw_import_plugin",
        os.path.join(PLUGIN_DIR, "main.py"),
        submodule_search_locations=[PLUGIN_DIR],
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["mw_import_plugin"] = module
    spec.loader.exec_module(module)
    return module


MODULE = load_plugin_module()


def fake_tool(name: str, description: str, parameters: dict | None = None):
    return types.SimpleNamespace(
        name=name,
        description=description,
        parameters=parameters or {},
        handler_module_path="",
    )


class ToolImportApiTests(unittest.TestCase):
    def setUp(self) -> None:
        # 每个用例一份干净的数据目录：不然上一个用例写下的动作会带进来
        os.environ["MY_WORLD_DATA_DIR"] = tempfile.mkdtemp(prefix="mw-import-")
        os.environ["MY_WORLD_MIGRATE_FROM"] = "none"
        self.plugin = MODULE.MyWorldPlugin(
            MODULE.Context(),
            MODULE.AstrBotConfig({"enabled": True, "tick_interval": 60}),
        )
        manager = self.plugin.context.get_llm_tool_manager()
        manager.func_list = [
            fake_tool(
                "demo_search_news",
                "搜索最近一天的新闻；只返回标题与链接。",
                {
                    "type": "object",
                    "properties": {"keywords": {"type": "string", "description": "关键词"}},
                    "required": ["keywords"],
                },
            ),
            fake_tool("demo_send_message", "直接发送消息给当前会话。"),
        ]

    def tearDown(self) -> None:
        try:
            self.plugin.db.raw.close()
        except Exception:
            pass

    def _call(self, payload: dict) -> dict:
        MODULE.request._json = payload
        try:
            return asyncio.run(self.plugin.api_tools_import())
        finally:
            MODULE.request._json = {}

    def test_dry_run_only_returns_drafts(self):
        result = self._call({"tools": ["demo_search_news"], "dry_run": True})
        self.assertEqual(result["status_code"], 200)
        data = result["data"]
        self.assertTrue(data["dry_run"])
        self.assertEqual([item["id"] for item in data["drafts"]], ["tool_demo_search_news"])
        self.assertEqual(data["drafts"][0]["tool_names"], ["demo_search_news"])
        # 只算草稿：世界里一个都没多
        self.assertEqual(
            [action.id for action in self.plugin.engine.world.actions if action.id.startswith("tool_")],
            [],
        )

    def test_commit_writes_world_and_reloads(self):
        result = self._call(
            {
                "tools": ["demo_search_news"],
                "options": {"scope": "global"},
                "dry_run": False,
            }
        )
        data = result["data"]
        self.assertEqual((data["created"], data["updated"]), (1, 0))
        # 引擎热加载之后就能看到她多会了这件事
        ids = [action.id for action in self.plugin.engine.world.actions]
        self.assertIn("tool_demo_search_news", ids)
        # 落盘了：重新读一份配置也能读到
        raw = self.plugin.store.raw_world()
        self.assertIn(
            "tool_demo_search_news",
            [item.get("id") for item in raw.get("actions") or []],
        )

    def test_second_import_skips_existing_action(self):
        self._call({"tools": ["demo_search_news"], "dry_run": False})
        again = self._call({"tools": ["demo_search_news"], "dry_run": False})
        data = again["data"]
        self.assertEqual(data["created"], 0)
        self.assertEqual(data["already"], ["demo_search_news"])
        self.assertEqual(
            [
                action.id
                for action in self.plugin.engine.world.actions
                if action.id == "tool_demo_search_news"
            ],
            ["tool_demo_search_news"],
        )

    def test_direct_send_tool_is_skipped(self):
        result = self._call({"tools": ["demo_send_message"], "dry_run": True})
        data = result["data"]
        self.assertEqual(data["drafts"], [])
        self.assertEqual(data["skipped"], ["demo_send_message"])
        self.assertTrue(any("绕过" in item for item in data["warnings"]))

    def test_tools_endpoint_marks_imported_tools(self):
        self._call({"tools": ["demo_search_news"], "dry_run": False})
        listing = asyncio.run(self.plugin.api_tools())
        rows = {item["name"]: item for item in listing["data"]["tools"]}
        self.assertEqual(rows["demo_search_news"]["action_ids"], ["tool_demo_search_news"])
        self.assertEqual(rows["demo_send_message"]["action_ids"], [])

    def test_unknown_tool_is_reported(self):
        result = self._call({"tools": ["nope"], "dry_run": True})
        self.assertEqual(result["status_code"], 400)
        self.assertIn("nope", result["message"])

    def test_inactive_tool_is_hidden_and_reported(self):
        """在 AstrBot 面板上关掉的工具（例如 helper_tools 的模块开关）不该被列出来。"""

        manager = self.plugin.context.get_llm_tool_manager()
        manager.func_list[0].active = False
        listing = asyncio.run(self.plugin.api_tools())
        names = [item["name"] for item in listing["data"]["tools"]]
        self.assertNotIn("demo_search_news", names)
        # 直接用名字调它：要说"被停用"，而不是含糊的"没有注册"
        result = asyncio.run(
            self.plugin.engine.tools.call_tool("demo_search_news", {})
        )
        self.assertFalse(result.ok)
        self.assertIn("停用", result.error)

    def test_import_all_excludes_inactive_tools(self):
        manager = self.plugin.context.get_llm_tool_manager()
        manager.func_list[0].active = False
        manager.func_list.append(fake_tool("demo_weather", "查询天气。"))
        result = self._call({"tools": "all", "dry_run": False})
        self.assertEqual(result["data"]["created"], 1)
        self.assertEqual(
            [
                item["id"]
                for item in self.plugin.store.raw_world().get("actions", [])
                if item["id"].startswith("tool_")
            ],
            ["tool_demo_weather"],
        )

    def test_explicit_selection_cannot_import_inactive_tool(self):
        manager = self.plugin.context.get_llm_tool_manager()
        manager.func_list[0].active = False
        result = self._call({"tools": ["demo_search_news"], "dry_run": False})
        self.assertEqual(result["status_code"], 400)
        self.assertIn("demo_search_news", result["message"])
        self.assertNotIn(
            "tool_demo_search_news",
            [item["id"] for item in self.plugin.store.raw_world().get("actions", [])],
        )

    def test_get_func_cannot_bypass_inactive_check(self):
        manager = self.plugin.context.get_llm_tool_manager()
        tool = manager.func_list[0]
        tool.active = False
        tool.handler = Mock()
        manager.get_func = lambda name: tool
        result = asyncio.run(self.plugin.engine.tools.call_tool(tool.name, {}))
        self.assertFalse(result.ok)
        self.assertIn("停用", result.error)
        tool.handler.assert_not_called()

    def test_inactive_builtin_from_get_func_is_rejected(self):
        manager = self.plugin.context.get_llm_tool_manager()
        tool = fake_tool("builtin_search", "搜索。")
        tool.active = False
        manager.get_func = lambda name: tool
        manager.iter_builtin_tools = lambda: [tool]
        listing = asyncio.run(self.plugin.api_tools())
        self.assertNotIn(tool.name, [item["name"] for item in listing["data"]["tools"]])
        result = asyncio.run(self.plugin.engine.tools.call_tool(tool.name, {}))
        self.assertFalse(result.ok)
        self.assertIn("停用", result.error)

    def test_tools_of_unloaded_plugin_are_gone(self):
        """模拟 AstrBot 卸载插件时从管理器移除工具。"""

        manager = self.plugin.context.get_llm_tool_manager()
        # AstrBot 卸载插件时做的事：把它注册的工具从 func_list 里移掉
        manager.func_list = []
        listing = asyncio.run(self.plugin.api_tools())
        self.assertEqual(listing["data"]["tools"], [])
        result = self._call({"tools": ["demo_search_news"], "dry_run": True})
        self.assertEqual(result["status_code"], 400)
        self.assertIn("没有可导入的已启用工具", result["message"])

    def test_unload_preserves_existing_action_but_tool_is_unavailable(self):
        self._call({"tools": ["demo_search_news"], "dry_run": False})
        self.plugin.context.get_llm_tool_manager().func_list = []
        self.assertIn(
            "tool_demo_search_news",
            [item["id"] for item in self.plugin.store.raw_world().get("actions", [])],
        )
        result = asyncio.run(self.plugin.engine.tools.call_tool("demo_search_news", {}))
        self.assertFalse(result.ok)
        self.assertIn("没有注册", result.error)

    def test_partly_unloaded_selection_still_imports_the_rest(self):
        """选了一批工具，其中一部分的插件刚被停用：能导的照常导，并说明漏了谁。"""

        manager = self.plugin.context.get_llm_tool_manager()
        manager.func_list = [
            candidate
            for candidate in manager.func_list
            if candidate.name != "demo_send_message"
        ]
        result = self._call(
            {"tools": ["demo_search_news", "demo_send_message"], "dry_run": True}
        )
        data = result["data"]
        self.assertEqual([item["id"] for item in data["drafts"]], ["tool_demo_search_news"])
        self.assertTrue(any("没注册" in item for item in data["warnings"]))


if __name__ == "__main__":
    unittest.main()
