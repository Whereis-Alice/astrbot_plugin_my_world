"""入口文件的体量守卫。

``main.py`` 只该做编排：钩子、指令、生命周期与接线。真正的逻辑分别在
``core/``（不依赖 AstrBot 的领域层）、``adapters/``（宿主绑定）与 ``web/``（编辑器接口）里。
这条测试不是硬指标，而是提醒：再有大块逻辑堆进入口文件时就该拆出去。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 当前入口文件约 2.8k 行；这是「再长就该动手拆」的阈值，不是目标值。
MAIN_MAX_LINES = 3400


class EntryPointTest(unittest.TestCase):
    def test_main_stays_an_entrypoint(self) -> None:
        lines = (ROOT / "main.py").read_text(encoding="utf-8").splitlines()
        self.assertLess(
            len(lines),
            MAIN_MAX_LINES,
            "main.py 又变胖了：新逻辑请拆进 core/ 、adapters/ 或 web/ 。",
        )

    def test_layers_exist(self) -> None:
        for rel in (
            "core/engine.py",
            "core/messages.py",
            "core/message_parts.py",
            "adapters/llm.py",
            "adapters/vision.py",
            "adapters/commands.py",
            "adapters/messenger.py",
            "adapters/tools.py",
            "web/api.py",
            "web/auth.py",
        ):
            self.assertTrue((ROOT / rel).is_file(), f"缺少分层模块：{rel}")

    def test_api_handlers_live_in_the_mixin(self) -> None:
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        plugin = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "MyWorldPlugin"
        )
        bases = {base.id for base in plugin.bases if isinstance(base, ast.Name)}
        self.assertIn("WebApiMixin", bases, "插件主体应继承 WebApiMixin")
        methods = [
            node.name
            for node in plugin.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        self.assertEqual(
            [],
            [name for name in methods if name.startswith("api_")],
            "api_* 处理器应该留在 web/api.py 的 WebApiMixin 里",
        )
