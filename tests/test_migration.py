"""从原插件搬数据的测试。

规则只有三条：新目录空才搬、不覆盖已有文件、能用环境变量关掉。
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
sys.path.insert(0, PLUGIN_DIR)
sys.path.insert(0, HERE)

import fake_astrbot  # noqa: E402

fake_astrbot.install()


def load_plugin_module():
    if "mw_migration_plugin" in sys.modules:
        return sys.modules["mw_migration_plugin"]
    spec = importlib.util.spec_from_file_location(
        "mw_migration_plugin",
        os.path.join(PLUGIN_DIR, "main.py"),
        submodule_search_locations=[PLUGIN_DIR],
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["mw_migration_plugin"] = module
    spec.loader.exec_module(module)
    return module


MODULE = load_plugin_module()


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="mw-migrate-"))
        self.source = self.root / "astrbot_plugin_virtual_world"
        self.target = self.root / "astrbot_plugin_my_world"
        self.source.mkdir(parents=True)
        (self.source / "world.json").write_text(
            json.dumps({"actions": [{"id": "say"}]}, ensure_ascii=False), encoding="utf-8"
        )
        (self.source / "state.db").write_bytes(b"sqlite")
        (self.source / "state.db-wal").write_bytes(b"wal")  # 这份不该被搬
        os.environ.pop("MY_WORLD_MIGRATE_FROM", None)
        # 自动发现那条路靠 AstrBot 的宿主路径，测试里直接把它指到临时目录
        self._original_lookup = MODULE._legacy_plugin_data_dirs
        MODULE._legacy_plugin_data_dirs = lambda _plugin_dir: [str(self.source)]

    def tearDown(self) -> None:
        os.environ.pop("MY_WORLD_MIGRATE_FROM", None)
        MODULE._legacy_plugin_data_dirs = self._original_lookup

    def _migrate(self) -> list[str]:
        return MODULE.migrate_from_original(
            str(self.target), str(PLUGIN_DIR), logging.getLogger("test")
        )

    def test_copies_files_once(self):
        copied = self._migrate()
        self.assertIn("world.json", copied)
        self.assertIn("state.db", copied)
        self.assertNotIn("state.db-wal", copied)
        self.assertTrue((self.target / "world.json").exists())

    def test_does_not_overwrite_existing_data(self):
        self.target.mkdir(parents=True)
        (self.target / "world.json").write_text("{}", encoding="utf-8")
        copied = self._migrate()
        self.assertEqual(copied, [])
        self.assertEqual((self.target / "world.json").read_text(encoding="utf-8"), "{}")

    def test_can_be_disabled_with_env(self):
        os.environ["MY_WORLD_MIGRATE_FROM"] = "none"
        self.assertEqual(self._migrate(), [])
        self.assertFalse((self.target / "world.json").exists())

    def test_env_can_point_at_another_directory(self):
        other = self.root / "another"
        other.mkdir()
        (other / "world.json").write_text("{}", encoding="utf-8")
        (other / "schedules.json").write_text("{}", encoding="utf-8")
        os.environ["MY_WORLD_MIGRATE_FROM"] = str(other)
        copied = self._migrate()
        self.assertIn("schedules.json", copied)

    def test_nothing_to_migrate_is_fine(self):
        (self.source / "world.json").unlink()
        (self.source / "state.db").unlink()
        self.assertEqual(self._migrate(), [])


if __name__ == "__main__":
    unittest.main()
