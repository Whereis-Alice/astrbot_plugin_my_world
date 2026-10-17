"""心迹画像桥：只读对方的 profiles.json，搬成这边的「事实」。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import profile_weaver  # noqa: E402
from core.db import Database  # noqa: E402
from core.defaults import default_world  # noqa: E402
from core.models import parse_world  # noqa: E402
from core.plugin_compat import profile_weaver_active  # noqa: E402
from core.profile import ProfileStore  # noqa: E402

SESSION = "aiocqhttp:GroupMessage:1001"


def sample_profiles() -> dict:
    return {
        "aiocqhttp:GroupMessage:1001_2692047521": {
            "subject_user_id": "2692047521",
            "subject_name": "阿岚",
            "fields": {
                "昵称": "阿岚",
                "爱好": "摄影、爬山",
                "忌口": "不吃香菜",
                "备注": ["上个月搬了新家", "在准备考试"],
            },
            "custom_fields": {"MBTI": {"description": "自述"}},
            "locked_fields": ["忌口"],
        },
        "42": {
            "subject_user_id": "42",
            "fields": {"昵称": "小明", "职业": "学生"},
        },
    }


class ProfileWeaverBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_profiles_path_finds_the_plugin_data(self):
        target = self.root / profile_weaver.PLUGIN_NAME / profile_weaver.PROFILE_FILE
        target.parent.mkdir(parents=True)
        target.write_text("{}", encoding="utf-8")
        self.assertEqual(profile_weaver.profiles_path(self.root), target)
        self.assertIsNone(profile_weaver.profiles_path(self.root / "nope"))

    def test_broken_file_is_treated_as_empty(self):
        target = self.root / profile_weaver.PLUGIN_NAME / profile_weaver.PROFILE_FILE
        target.parent.mkdir(parents=True)
        target.write_text("{not json", encoding="utf-8")
        self.assertEqual(profile_weaver.load_profiles(target), {})

    def test_records_become_people_and_facts(self):
        items = profile_weaver.iter_import_items(sample_profiles())
        by_id = {item["user_id"]: item for item in items}
        self.assertEqual(set(by_id), {"2692047521", "42"})
        person = by_id["2692047521"]
        self.assertEqual(person["name"], "阿岚")
        texts = [text for _kind, text in person["facts"]]
        self.assertIn("爱好：摄影、爬山", texts)
        self.assertIn("忌口：不吃香菜", texts)
        self.assertIn("上个月搬了新家", texts)
        # 昵称不进事实，它是「她怎么叫他」
        self.assertFalse(any(text.startswith("昵称：") for text in texts))
        self.assertEqual(person["source_session"], "aiocqhttp:GroupMessage:1001")
        self.assertEqual(person["locked"], ["忌口"])
        stats = profile_weaver.summarize(items)
        self.assertEqual(stats["people"], 2)
        self.assertGreaterEqual(stats["facts"], 5)


class ProfileWeaverPluginDetectTests(unittest.TestCase):
    """心迹画像插件在不在跑：在跑我们就别重复注入。"""

    class _Star:
        def __init__(self, name, activated):
            self.name = name
            self.activated = activated

    class _Context:
        def __init__(self, stars):
            self._stars = stars

        def get_all_stars(self):
            return self._stars

    def test_plugin_active_reads_star_state(self):
        active = self._Context(
            [self._Star("astrbot_plugin_profile_weaver", True)]
        )
        self.assertTrue(profile_weaver_active(active))
        idle = self._Context([self._Star("astrbot_plugin_profile_weaver", False)])
        self.assertFalse(profile_weaver_active(idle))
        missing = self._Context([self._Star("astrbot_plugin_meme_magpie", True)])
        self.assertFalse(profile_weaver_active(missing))
        self.assertFalse(profile_weaver_active(object()))


class ProfileWeaverImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(os.path.join(self._tmp.name, "state.db"))
        self.addCleanup(self.db.close)
        world, _warnings = parse_world(default_world())
        self.store = ProfileStore(self.db, world)

    def test_import_is_idempotent_and_keeps_the_evidence(self):
        items = profile_weaver.iter_import_items(sample_profiles())
        first = self.store.import_external(SESSION, items, now=1000.0)
        self.assertTrue(first["ok"])
        self.assertEqual(first["people"], 2)
        self.assertGreater(first["added"], 0)

        facts = self.store.facts(SESSION, "2692047521", limit=50)
        texts = [item["text"] for item in facts]
        self.assertIn("爱好：摄影、爬山", texts)
        self.assertTrue(all("心迹画像" in str(item.get("evidence") or "") for item in facts))
        # 人名也搬过来了
        self.assertEqual(self.store.profile(SESSION, "2692047521")["payload"]["qq_name"], "阿岚")

        again = self.store.import_external(SESSION, items, now=1100.0)
        self.assertEqual(again["added"], 0)
        self.assertGreater(again["merged"], 0)
        self.assertEqual(len(self.store.facts(SESSION, "2692047521", limit=50)), len(facts))

    def test_import_without_people_is_a_no_op(self):
        result = self.store.import_external(SESSION, [])
        self.assertTrue(result["ok"])
        self.assertEqual(result["people"], 0)
        self.assertEqual(result["added"], 0)

    def test_import_fills_call_him_but_never_overwrites_it(self):
        """它的「昵称」当称呼补进来；这边已经写过的称呼不动。"""

        items = profile_weaver.iter_import_items(sample_profiles())
        self.store.import_external(SESSION, items, now=1000.0)
        self.assertEqual(self.store.view(SESSION, "2692047521").call_him, "阿岚")

        self.store.set_call_names(SESSION, "2692047521", call_him="岚岚")
        self.store.import_external(SESSION, items, now=1100.0)
        self.assertEqual(self.store.view(SESSION, "2692047521").call_him, "岚岚")

    def test_import_respects_the_profile_switch(self):
        world, _warnings = parse_world(default_world())
        world.profile.enabled = False
        store = ProfileStore(self.db, world)
        result = store.import_external(SESSION, profile_weaver.iter_import_items(sample_profiles()))
        self.assertFalse(result["ok"])
        self.assertIn("画像功能关着", result["reason"])


if __name__ == "__main__":
    unittest.main()
