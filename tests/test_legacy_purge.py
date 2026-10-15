"""旧版动作清理与群名片对齐（v3 的两个编辑器维护功能）。

- `/mw action purge` / 编辑器「清除旧版动作…」：删掉全部工具 / 指令型动作，
  连同它们留下的空分组；id 会记进 ``purged_actions``，重载也不会复活。
- 控制台的名片：平台上的真实名片优先（dynamic_card_plus 改过也跟得上），
  别人接管名片时把本地记录对齐，修掉"名字卡在第一次改过的那一版"。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config_store import ConfigStore  # noqa: E402
from core.db import AsyncDatabase  # noqa: E402
from core.engine import MyWorldEngine  # noqa: E402
from tests.stub_ports import StubClock, StubLLM, StubMessenger, StubPersona  # noqa: E402

SESSION = "aiocqhttp:GroupMessage:9002"
LIVE_CARD = "艾拉 发呆中"
STALE_CARD = "艾拉 在想浏览器"


class CardMessenger(StubMessenger):
    """带「平台真实名片」和「名片归属」的测试替身。"""

    def __init__(self, *, live: str = LIVE_CARD, owner: str = "") -> None:
        super().__init__()
        self.live = live
        self.owner = owner

    async def fetch_group_card(self, session_id: str) -> str:
        return self.live

    def group_card_owner(self, session_id: str) -> str:
        return self.owner


class EngineHarness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = ConfigStore(self._tmp.name)
        self.store.ensure_files()
        self.store.load_world()
        self.store.add_session(
            SESSION, session_type="group", platform="aiocqhttp", cold_start_node="study"
        )
        self.db = AsyncDatabase(self.store.db_path)
        self.addAsyncCleanup(self.db.close)
        self.messenger = CardMessenger()
        self.engine = MyWorldEngine(
            store=self.store,
            db=self.db,
            llm=StubLLM(['{"actions":[{"type":"say","messages":["嗯"]}]}']),
            messenger=self.messenger,
            persona=StubPersona("你是一个住在老城区的女孩。"),
            clock=StubClock(now=1_700_000_000.0),
            tick_seconds=60.0,
        )
        self.engine.reload_config()


class LegacyPurgeTests(EngineHarness):
    async def test_preview_then_confirm_removes_and_tombstones(self):
        legacy = self.engine.legacy_actions()
        self.assertTrue(legacy, "默认世界里的工具 / 指令动作应该被认成旧版")

        preview = await self.engine.purge_legacy_actions()
        self.assertEqual(preview["removed"], 0, "预览不允许动配置")
        self.assertGreaterEqual(preview["preview"]["count"], len(legacy))
        self.assertTrue(self.engine.legacy_actions(), "预览之后旧版动作还在")

        result = await self.engine.purge_legacy_actions(confirm=True)
        self.assertEqual(result["removed"], preview["preview"]["count"])
        self.assertEqual(self.engine.legacy_actions(), [])

        ids = {action.id for action in self.engine.world.actions}
        self.assertIn("say", ids, "虚拟内置动作不能被连带删掉")
        self.assertIn("walk_to", ids)
        self.assertTrue(self.store.raw_world().get("purged_actions"))

        # 再加载一遍：旧版动作不能借"缺少内置动作自动补回"复活
        self.engine.reload_config()
        self.assertEqual(self.engine.legacy_actions(), [])

    async def test_custom_group_is_cleaned_up_with_its_actions(self):
        raw = self.store.raw_world()
        raw["actions"].append(
            {
                "id": "old_web_tool",
                "name": "旧搜索",
                "group": "旧工具组",
                "llm_level": "tool",
                "tool_names": [],
            }
        )
        self.store.save_world(raw)
        self.engine.reload_config()
        self.assertIn("old_web_tool", {item.id for item in self.engine.legacy_actions()})

        result = await self.engine.purge_legacy_actions(confirm=True)

        self.assertIn("旧工具组", result["groups"])
        self.assertNotIn(
            "旧工具组",
            {str(action.group or "") for action in self.engine.world.actions},
        )
        self.assertNotIn(
            "old_web_tool", {action.id for action in self.engine.world.actions}
        )

    async def test_purge_without_legacy_actions_is_a_noop(self):
        await self.engine.purge_legacy_actions(confirm=True)
        again = await self.engine.purge_legacy_actions(confirm=True)
        self.assertEqual(again["removed"], 0)
        self.assertIn("没有旧版动作", str(again.get("note") or ""))


class NicknameLiveTests(EngineHarness):
    async def test_snapshot_prefers_live_card_and_aligns_when_owned(self):
        self.messenger.owner = "灵动名片（dynamic_card_plus）"
        self.messenger.live = LIVE_CARD
        async with self.engine.session_state(SESSION) as state:
            state.bot_current_nickname = STALE_CARD
            state.bot_base_nickname = STALE_CARD

        snapshot = await self.engine.snapshot(SESSION)

        self.assertEqual(snapshot["nickname_live"], LIVE_CARD)
        self.assertEqual(snapshot["nickname"], LIVE_CARD)
        self.assertEqual(snapshot["nickname_owner"], "灵动名片（dynamic_card_plus）")
        state = await self.engine.load_state(SESSION, cold_start=False)
        self.assertEqual(state.bot_current_nickname, LIVE_CARD)
        self.assertEqual(state.bot_base_nickname, LIVE_CARD)

    async def test_refresh_keeps_base_when_self_owned(self):
        state = await self.engine.load_state(SESSION, cold_start=False)
        state.bot_base_nickname = "艾拉"
        state.bot_current_nickname = "艾拉 在想浏览器"
        await self.engine.save_state(state)

        result = await self.engine.refresh_nickname(SESSION)

        self.assertTrue(result["ok"])
        self.assertEqual(result["card"], LIVE_CARD)
        state = await self.engine.load_state(SESSION, cold_start=False)
        self.assertEqual(state.bot_current_nickname, LIVE_CARD)
        self.assertEqual(state.bot_base_nickname, "艾拉", "自己管名片时原名不该被覆盖")


if __name__ == "__main__":
    unittest.main()
