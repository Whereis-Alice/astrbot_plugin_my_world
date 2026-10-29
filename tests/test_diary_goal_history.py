"""A completed day's journal keeps the goal that owned its actual effort."""
import asyncio
import json
import unittest
from datetime import datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from core.engine import TickOutcome
from tests import test_life_matters as fixture

S = fixture.SESSION
OLD = "读完风车的设计笔记"
NEW = "改为整理花园的栽培笔记"


class DiaryGoalHistoryTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.MattersAcceptanceTests.asyncSetUp

    async def completed_day(self):
        self.clock._now = datetime(2026, 10, 28, 10, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        self.day = self.engine.local_now().date().isoformat()
        _, persona_key = await self.engine._journal_persona(S)
        async with self.engine.session_state(S) as state:
            state.state, state.energy = "idle", .9
            item = self.engine.add_life_matter(state, OLD, persona_key=persona_key)
            self.item_id = item["id"]
            item["steps"] = [{"id": "first", "text": "读风车笔记五分钟", "goal": OLD,
                "action": "read", "node": "study", "minutes": 5, "status": "pending"}]
            await self.engine._start_matter_step(state, item)
        for _ in range(5):
            self.clock.advance(60)
            async with self.engine.session_state(S) as state:
                await self.engine._tick_continuous(state, self.engine.node(state.node_id), TickOutcome(S))
        self.clock.advance(86400)

    async def change_goal(self):
        async with self.engine.session_state(S) as state:
            self.engine.manage_life_matter(state, self.item_id, "edit", text=NEW)

    async def test_past_day_uses_the_goal_that_owned_the_work(self):
        await self.completed_day()
        await self.change_goal()
        self.engine._ask_journal = AsyncMock(return_value=json.dumps({"title": "昨天读过的笔记", "content": "在世界里读了五分钟笔记。"}))
        saved = await self.engine.ensure_diary(S, self.day)
        self.assertIsNotNone(saved)
        payload = self.engine._ask_journal.call_args.args[3]
        rows = payload["saved_life"]["matters"]
        self.assertTrue(any(row["event"] == "step_done" for row in rows))
        self.assertTrue(all(row["title"] == OLD for row in rows))
        self.assertNotIn(NEW, json.dumps(payload["saved_life"], ensure_ascii=False))

    async def test_todays_edit_does_not_invalidate_yesterdays_diary(self):
        await self.completed_day()
        entered, release = asyncio.Event(), asyncio.Event()
        async def generate(*_args):
            entered.set()
            await release.wait()
            return json.dumps({"title": "昨天的阅读", "content": "昨天在世界里读过笔记，还没有确认整个目标完成。"})
        self.engine._ask_journal = AsyncMock(side_effect=generate)
        request = asyncio.create_task(self.engine.ensure_diary(S, self.day))
        self.addAsyncCleanup(lambda: asyncio.gather(request, return_exceptions=True))
        await asyncio.wait_for(entered.wait(), 2)
        await self.change_goal()
        release.set()
        saved = await request
        self.assertIsNotNone(saved)
        self.assertEqual(saved["title"], "昨天的阅读")

    async def test_a_new_entry_in_the_target_day_still_discards_the_old_draft(self):
        await self.completed_day()
        async with self.engine.session_state(S) as state:
            self.engine.manage_life_matter(state, self.item_id, "postpone", reason="午后再接着读",
                resume_at=self.clock.now() + 3600)
        today = self.engine.local_now().date().isoformat()
        entered, release = asyncio.Event(), asyncio.Event()
        async def generate(*_args):
            entered.set()
            await release.wait()
            return json.dumps({"title": "今天的打算", "content": "今天暂时准备继续读风车笔记。"})
        self.engine._ask_journal = AsyncMock(side_effect=generate)
        request = asyncio.create_task(self.engine.ensure_diary(S, today, force=True))
        self.addAsyncCleanup(lambda: asyncio.gather(request, return_exceptions=True))
        await asyncio.wait_for(entered.wait(), 2)
        await self.change_goal()
        release.set()
        self.assertIsNone(await request)
        self.assertIsNone(await self.engine.diary_for_day(S, today))

    async def test_legacy_completed_step_keeps_its_explicit_receipt_goal(self):
        await self.completed_day()
        await self.change_goal()
        async with self.engine.session_state(S) as state:
            item = next(row for row in state.life_matters if row["id"] == self.item_id)
            for row in item["history"]:
                row.pop("goal", None)  # Pre-upgrade history has no separate goal.
        sources = await self.engine._diary_sources(S, self.day)
        row = next(row for row in sources["matters"] if row["event"] == "step_done")
        self.assertEqual(row["title"], OLD)
        anonymous = [row for row in sources["matters"] if row["event"] in {"created", "started"}]
        self.assertTrue(anonymous)
        self.assertTrue(all(row["title"] == "当时目标未记录" for row in anonymous))
        self.assertNotIn(NEW, json.dumps(sources, ensure_ascii=False))
        saved = await self.engine.load_state(S, cold_start=False)
        item = next(row for row in saved.life_matters if row["id"] == self.item_id)
        self.assertTrue(all("goal" not in row for row in item["history"]))

    async def test_interleaved_projects_keep_their_actual_history_order(self):
        self.clock._now = datetime(2026, 10, 28, 10, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        day = self.engine.local_now().date().isoformat()
        _, persona_key = await self.engine._journal_persona(S)
        async with self.engine.session_state(S) as state:
            first = self.engine.add_life_matter(state, OLD, persona_key=persona_key)
        self.clock.advance(1800)
        async with self.engine.session_state(S) as state:
            self.engine.add_life_matter(state, NEW, persona_key=persona_key)
        self.clock.advance(1800)
        async with self.engine.session_state(S) as state:
            self.engine.manage_life_matter(state, first["id"], "postpone", reason="先想清楚花园的安排，再继续风车笔记",
                resume_at=self.clock.now() + 86400)
        sources = await self.engine._diary_sources(S, day)
        self.assertEqual([row["title"] for row in sources["matters"]], [OLD, NEW, OLD])
        self.assertEqual([row["event"] for row in sources["matters"]], ["created", "created", "manual_postpone"])

    async def test_history_with_unknown_times_is_ignored_without_rewriting_it(self):
        await self.completed_day()
        bad_rows = [{"kind": "legacy", "at": "昨夜", "text": "不能确定时间的旧进度"},
                    {"kind": "legacy", "text": "没有时间的旧进度"}]
        async with self.engine.session_state(S) as state:
            item = next(row for row in state.life_matters if row["id"] == self.item_id)
            item["history"].extend(bad_rows)
        sources = await self.engine._diary_sources(S, self.day)
        self.assertTrue(any(row["event"] == "step_done" for row in sources["matters"]))
        self.assertFalse(any(row["event"] == "legacy" for row in sources["matters"]))
        saved = await self.engine.load_state(S, cold_start=False)
        item = next(row for row in saved.life_matters if row["id"] == self.item_id)
        self.assertEqual(item["history"][-2:], bad_rows)


if __name__ == "__main__":
    unittest.main()
