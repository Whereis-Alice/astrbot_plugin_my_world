"""Actual effort keeps its executed name and goal across edits."""
import copy
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from core.engine import TickOutcome
from core.engine.calendar_diary import _diary_practice_events
from core import life_matters as matters
from tests import test_action_record_boundaries as records
from tests import test_engine as fixture

S = fixture.SESSION


class ExecutionLabelTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp
    configure_action = records.ActionRecordBoundaryTests.configure_action
    execute = records.ActionRecordBoundaryTests.execute

    async def start_activity(self, *, seconds=120):
        action = self.configure_action(name="整理书签", category="continuous", llm_level="template",
            template="", duration_mode="fixed", duration=seconds, life_role="interest")
        await self.execute(action)
        return action

    async def tick(self):
        self.clock.advance(60)
        async with self.engine.session_state(S) as state:
            await self.engine._tick_continuous(state, self.engine.node(state.node_id), TickOutcome(S))
        return await self.engine.load_state(S, cold_start=False)

    async def practice(self, state, day=None):
        _, owner = await self.engine._journal_persona(S)
        day = day or self.engine._today_key(self.clock.now())
        return _diary_practice_events(state, day, owner)

    async def test_rename_before_first_tick_keeps_execution_name_in_effort_and_memory(self):
        action = await self.start_activity()
        self.configure_action(name="浇花")
        state = await self.tick()
        evidence = await self.practice(state)
        self.assertEqual("整理书签", state.current_action["action_name"])
        self.assertEqual("整理书签", state.life_interests[action]["label"])
        self.assertIn("整理书签", evidence[0]["detail"])
        self.assertNotIn("浇花", evidence[0]["detail"])
        self.assertEqual("浇花", self.engine.world.action_map()[action].name)
        state = await self.tick()
        self.assertIsNone(state.current_action)
        memories = await self.db.call("query_memories", session_id=S, limit=100)
        self.assertEqual(["在这里整理书签"], [row["content"] for row in memories if row["source"] == "runtime"])
        self.assertEqual(2, state.life_interests[action]["minutes"])
        self.assertEqual([], self.llm.calls)
        self.assertEqual([], self.messenger.sent)

    async def test_same_execution_keeps_its_name_when_a_rename_crosses_midnight(self):
        self.clock._now = datetime(2026, 11, 2, 23, 59, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        action = await self.start_activity(seconds=180)
        await self.tick()
        self.configure_action(name="浇花")
        state = await self.tick()
        for day in ("2026-11-02", "2026-11-03"):
            with self.subTest(day=day):
                evidence = await self.practice(state, day)
                self.assertEqual(1, len(evidence))
                self.assertIn("整理书签", evidence[0]["detail"])
                self.assertNotIn("浇花", evidence[0]["detail"])
        self.assertEqual(1, state.life_interests[action]["sessions"])
        self.assertEqual(2, state.life_interests[action]["minutes"])

    async def test_a_new_execution_updates_the_index_without_rewriting_old_episodes(self):
        action = await self.start_activity()
        await self.tick()
        await self.tick()
        self.configure_action(name="浇花")
        await self.execute(action)
        state = await self.tick()
        record = state.life_interests[action]
        self.assertEqual("浇花", record["label"])
        self.assertEqual(["整理书签", "浇花"], [row["label"] for row in record["episodes"]])
        self.assertEqual(2, record["sessions"])
        self.assertEqual(3, record["minutes"])

    async def test_old_running_record_without_name_uses_the_definition_fallback(self):
        action = await self.start_activity()
        async with self.engine.session_state(S) as state:
            state.current_action.pop("action_name")
            state.current_action["desc"] = "不能作为活动名称的私人描述"
        self.configure_action(name="浇花")
        state = await self.tick()
        evidence = await self.practice(state)
        self.assertEqual("浇花", state.life_interests[action]["label"])
        self.assertIn("浇花", evidence[0]["detail"])
        self.assertNotIn("私人", evidence[0]["detail"])


class GoalEditEvidenceTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp
    configure_action = records.ActionRecordBoundaryTests.configure_action

    async def prepare(self, *, explicit_goal=True):
        action = self.configure_action(action_id="chapter_read", name="读第一章", category="continuous",
            llm_level="template", template="", duration_mode="fixed", duration=300, life_role="interest")
        async with self.engine.session_state(S) as state:
            state.state, state.energy = "idle", .9
            state.life_matters = []
            _, persona = await self.engine._journal_persona(S)
            item = self.engine.add_life_matter(state, "读完第一本书", persona_key=persona)
            item["needs_replan"] = False
            item["steps"] = [{"id": "chapter", "text": "读第一章", "action": action,
                              "node": "study", "minutes": 5, "status": "pending"}]
            if explicit_goal:
                item["steps"][0]["goal"] = item["text"]
            await self.engine._start_matter_step(state, item)
            self.assertEqual(action, state.current_action["type"])
            identifier = item["id"]
        self.clock.advance(60)
        await self.engine._tick_while_thinking(S)
        return identifier

    async def test_changed_goal_keeps_last_work_bookmark_and_pause_history_with_old_goal(self):
        for explicit_goal in (True, False):
            with self.subTest(explicit_goal=explicit_goal):
                identifier = await self.prepare(explicit_goal=explicit_goal)
                async with self.engine.session_state(S) as state:
                    item = self.engine.manage_life_matter(state, identifier, "edit", text="读完第二本书")
                    step = item["steps"][0]
                    paused = [row for row in item["history"] if row["kind"] == "paused_step"]
                    self.assertEqual("读完第一本书", step["goal"])
                    self.assertEqual("读完第一本书", step["bookmark"]["goal"])
                    self.assertEqual(1, step["worked_minutes"])
                    self.assertEqual(1, len(paused))
                    self.assertEqual("读完第一本书", paused[0]["goal"])
                    self.assertEqual("读完第二本书", item["history"][-1]["goal"])
                    self.assertTrue(item["needs_replan"])
                    self.assertFalse(item["needs_review"])
                    self.assertIsNone(state.current_action)
                    self.assertIsNone(state.current_plan)
                    self.assertFalse(matters.current_bookmark(item))
                sources = await self.engine._diary_sources(S, self.engine._today_key(self.clock.now()))
                diary_pauses = [row for row in sources["matters"] if row["event"] == "paused_step"]
                self.assertTrue(diary_pauses)
                self.assertTrue(all(row["title"] == "读完第一本书" for row in diary_pauses))
                self.assertEqual([], self.llm.calls)
                self.assertEqual([], self.messenger.sent)

    async def test_unchanged_goal_or_motivation_edit_retains_existing_pause_policy(self):
        for changes in ({"text": "读完第一本书"}, {"motivation": "想慢慢读懂这个故事"}):
            with self.subTest(changes=changes):
                identifier = await self.prepare()
                async with self.engine.session_state(S) as state:
                    item = self.engine.manage_life_matter(state, identifier, "edit", **changes)
                    self.assertEqual("读完第一本书", item["text"])
                    self.assertFalse(item["needs_replan"])
                    self.assertEqual(1, item["steps"][0]["worked_minutes"])
                    self.assertEqual(1, len([row for row in item["history"] if row["kind"] == "paused_step"]))
                    self.assertEqual(1, matters.current_bookmark(item)["worked_minutes"])
                    self.assertEqual("active", item["status"])
                    self.assertIsNone(state.current_action)

    async def test_rejected_empty_goal_does_not_stop_or_rewrite_running_work(self):
        identifier = await self.prepare()
        async with self.engine.session_state(S) as state:
            before = copy.deepcopy(matters.find(state, identifier))
            action = copy.deepcopy(state.current_action)
            with self.assertRaises(ValueError):
                self.engine.manage_life_matter(state, identifier, "edit", text=" ")
            self.assertEqual(before, matters.find(state, identifier))
            self.assertEqual(action, state.current_action)
