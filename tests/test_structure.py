"""入口与引擎的体量守卫。

``main.py`` 只该做编排：钩子、指令、生命周期与接线。真正的逻辑分别在
``core/``（不依赖 AstrBot 的领域层）、``adapters/``（宿主绑定）与 ``web/``（编辑器接口）里。
这条测试不是硬指标，而是提醒：再有大块逻辑堆进入口文件时就该拆出去。

``core/engine/`` 曾经是一个两万行的类，现在按职责拆成一组 mixin；
下面的阈值是「再长就该动手拆」的提醒，不是目标。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 当前入口文件约 2.8k 行；这是「再长就该动手拆」的阈值，不是目标值。
MAIN_MAX_LINES = 3400
# 引擎里单个模块目前最长约 1.6k 行（weather.py）
ENGINE_MODULE_MAX_LINES = 2200


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
            "core/engine/__init__.py",
            "core/engine/constants.py",
            "core/engine/models.py",
            "core/engine/util.py",
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

    def test_engine_modules_stay_readable(self) -> None:
        """引擎拆成 mixin 之后，别再往单个文件里堆回两万行。"""

        oversized: list[str] = []
        for path in sorted((ROOT / "core" / "engine").glob("*.py")):
            count = len(path.read_text(encoding="utf-8").splitlines())
            if count >= ENGINE_MODULE_MAX_LINES:
                oversized.append(f"{path.name}: {count} 行")
        self.assertEqual(
            [],
            oversized,
            "core/engine/ 里有文件又长回去了，把新的职责拆到相邻模块：\n"
            + "\n".join(oversized),
        )

    def test_engine_is_composed_of_mixins(self) -> None:
        """MyWorldEngine 自己只负责拼装，方法都在 mixin 里。"""

        tree = ast.parse((ROOT / "core" / "engine" / "__init__.py").read_text(encoding="utf-8"))
        engine = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "MyWorldEngine"
        )
        bases = [base.id for base in engine.bases if isinstance(base, ast.Name)]
        self.assertGreaterEqual(len(bases), 20, f"mixin 数量太少：{bases}")
        methods = [
            node.name
            for node in engine.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        self.assertLessEqual(
            len(methods),
            6,
            f"MyWorldEngine 本体只该留 __init__ 与几个入口方法，现在有：{methods}",
        )

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
