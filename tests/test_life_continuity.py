"""Regression checks for interruptions, fair scheduling and truthful plan identity."""
import copy
import unittest
from datetime import datetime
from unittest.mock import AsyncMock

from core.engine import TickOutcome
from core.json_actions import PlannedAction
from core.planner import create_plan, same_plan
from tests import test_engine as fixture
from tests import test_life_matters as fixtures

SESSION = fixture.SESSION


class LifeContinuityTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixtures.MattersAcceptanceTests.asyncSetUp
    ctx = fixture.EngineTestCase.ctx
    add_group = fixture.EngineTestCase.add_group

    async def prepare(self, *, running=True):
        state = await self.engine.load_state(SESSION, cold_start=False)
        state.state, state.energy = "idle", .9
        state.world_time = 10
        item = self.engine.add_life_matter(state, "慢慢读完这本书")
        item["steps"] = [{"id": "s1", "text": "安静阅读", "action": "read", "node": "study",
                          "minutes": 20, "status": "pending"}]
        if running:
            await self.engine._start_matter_step(state, item)
            state.current_action["elapsed_ticks"] = 2
        return state, item

    async def test_exhausted_project_does_not_starve_other_projects(self):
        async with self.engine.session_state(SESSION) as state:
            state.state, state.energy = "idle", .9
            first = self.engine.add_life_matter(state, "昨天的项目")
            first.update(work_day=self.engine._today_key(self.clock.now()),
                         worked_minutes=self.engine.world.limits.life_matter_daily_minutes)
            other = self.engine.add_life_matter(state, "今天接着读")
            other["steps"] = [{"id": "s2", "text": "读一会儿", "action": "read", "node": "study",
                               "minutes": 5, "status": "pending"}]
        await self.engine.advance_life_matters(SESSION)
        state = await self.engine.load_state(SESSION, cold_start=False)
        self.assertEqual((state.current_action or {}).get("matter_id"), other["id"])

    async def test_planning_limit_does_not_starve_other_projects(self):
        async with self.engine.session_state(SESSION) as state:
            state.state, state.energy = "idle", .9
            first = self.engine.add_life_matter(state, "暂时没有能力的项目")
            first.update(work_day=self.engine._today_key(self.clock.now()), plan_attempts=3)
            other = self.engine.add_life_matter(state, "读一下")
            other["steps"] = [{"id": "s2", "text": "读一会儿", "action": "read", "node": "study",
                               "minutes": 5, "status": "pending"}]
        await self.engine.advance_life_matters(SESSION)
        state = await self.engine.load_state(SESSION, cold_start=False)
        self.assertEqual((state.current_action or {}).get("matter_id"), other["id"])

    async def test_immediate_cancel_retains_partial_work(self):
        state, item = await self.prepare()
        await self.engine.apply_cancel(state, "now", "先放下", TickOutcome(SESSION))
        self.assertEqual(item["steps"][0].get("worked_minutes"), 2)
        self.assertIsNone(state.current_action)

    async def test_pausing_same_action_twice_never_double_counts(self):
        state, item = await self.prepare()
        action = state.current_action
        self.engine.pause_matter_action(state, action)
        self.engine.pause_matter_action(state, action)
        self.assertEqual(item["steps"][0].get("worked_minutes"), 2)
        action["elapsed_ticks"] += 1
        self.engine.pause_matter_action(state, action)
        self.assertEqual(item["steps"][0].get("worked_minutes"), 3)

    async def test_resumed_step_receipt_contains_all_actual_work(self):
        state, item = await self.prepare()
        self.engine.pause_matter_action(state, state.current_action)
        state.current_action, state.current_plan, state.state = None, None, "idle"
        await self.engine._start_matter_step(state, item)
        self.assertEqual(state.current_action["duration_ticks"], 18)
        state.current_action["elapsed_ticks"] = 18
        await self.engine._finish_action(state, self.engine.node("study"), TickOutcome(SESSION), state.current_action, depth=0)
        self.assertEqual(item["steps"][0]["result"]["minutes"], 20)
        self.assertEqual(item["worked_minutes"], 20)
        self.assertTrue(item["needs_review"])

    async def test_force_sleep_retains_partial_work(self):
        state, item = await self.prepare()
        state.sleep_rhythm.update(duration_seconds=25200, reason="累了")
        await self.engine._run_rhythm_sleep(state, TickOutcome(SESSION), force=True)
        self.assertEqual(item["steps"][0].get("worked_minutes"), 2)
        self.assertEqual(state.current_action["type"], "walk_to")

    async def test_unreachable_bed_never_discards_current_work(self):
        state, _ = await self.prepare()
        self.engine.world.action_map()["walk_to"].enabled = False
        before = copy.deepcopy(state.current_action)
        state.sleep_rhythm.update(duration_seconds=25200, reason="累了")
        await self.engine._run_rhythm_sleep(state, TickOutcome(SESSION), force=True)
        self.assertEqual(state.current_action, before)
        self.assertGreater(state.sleep_rhythm["retry_at"], self.clock.now())

    async def test_drowsiness_retains_partial_work(self):
        state, item = await self.prepare()
        await self.engine._enter_drowsy(state, self.engine.node("study"), TickOutcome(SESSION),
                                      self.engine.world.action_map()["sleep"], PlannedAction(type="sleep"), depth=0)
        self.assertEqual(item["steps"][0].get("worked_minutes"), 2)

    async def test_edited_goal_cannot_review_old_stage_as_new_success(self):
        state, item = await self.prepare(running=False)
        item.update(needs_review=True, resume_at=self.clock.now() + 86400)
        self.engine.manage_life_matter(state, item["id"], "edit", text="读完另一本书")
        self.assertFalse(item.get("needs_review"))
        self.assertTrue(item.get("needs_replan"))
        self.assertEqual(item.get("resume_at"), 0)

    async def test_same_tick_plan_replacement_never_finishes_new_step(self):
        state, _ = await self.prepare()
        old_action = state.current_action
        new_plan = create_plan(steps=[{"action":"read"}], world_time=state.world_time)
        state.current_plan = new_plan
        self.assertIsNone(self.engine._running_plan_index(state, new_plan))
        await self.engine._finish_action(state, self.engine.node("study"), TickOutcome(SESSION), old_action, depth=0)
        self.assertIs(state.current_plan, new_plan)
        self.assertEqual(new_plan["steps"][0]["status"], "pending")

    async def test_cancelled_plan_is_never_reported_as_completed(self):
        state, _ = await self.prepare()
        await self.engine.apply_cancel(state, "now", "别做了", TickOutcome(SESSION))
        state.life_idle_action = ""
        for action in self.engine.world.actions:
            if action.idle:
                action.enabled = False
        text = self.engine.prompts._in_progress_block(state)
        self.assertNotIn("已经做完了", text)
        self.assertIn("实际执行记录", text)

    async def test_deleted_or_disabled_action_stops_and_preserves_work(self):
        for deleted in (False, True):
            with self.subTest(deleted=deleted):
                async with self.engine.session_state(SESSION) as state:
                    state.state, state.energy = "idle", .9
                    item = self.engine.add_life_matter(state, f"阅读项目{deleted}")
                    item["steps"] = [{"id": "s1", "text": "阅读", "action": "read", "node": "study",
                                      "minutes": 20, "status": "pending"}]
                    await self.engine._start_matter_step(state, item)
                    state.current_action["elapsed_ticks"] = 2
                original = list(self.engine.world.actions)
                read = self.engine.world.action_map()["read"]
                if deleted:
                    self.engine.world.actions = [a for a in original if a.id != "read"]
                else:
                    read.enabled = False
                self.engine._maybe_decide_locked = AsyncMock()
                await self.engine._tick_session(SESSION)
                state = await self.engine.load_state(SESSION, cold_start=False)
                current = state.current_action or {}
                self.assertNotEqual(current.get("type"), "read")
                saved = next(m for m in state.life_matters if m["id"] == item["id"])
                self.assertEqual(saved["steps"][0]["worked_minutes"], 2)
                self.assertEqual(saved["progress"], 0)
                self.engine.world.actions = original
                read.enabled = True

    async def test_skipped_plan_step_is_never_a_completed_experience(self):
        state, _ = await self.prepare(running=False)
        plan = create_plan(steps=[{"action":"missing_action"}, {"action":"read"}], world_time=10)
        state.current_plan = plan
        await self.engine._tick_plan(state, self.engine.node("study"), TickOutcome(SESSION), depth=0)
        self.assertEqual(plan["steps"][0]["status"], "skipped")
        self.assertNotIn("已经做完的", self.engine.prompts._in_progress_block(state))

    async def test_legacy_action_never_advances_a_new_identified_plan(self):
        state, _ = await self.prepare()
        action = state.current_action
        action.pop("plan_id", None)
        new_plan = create_plan(steps=[{"action":"read"}], world_time=state.world_time)
        state.current_plan = new_plan
        await self.engine._finish_action(state, self.engine.node("study"), TickOutcome(SESSION), action, depth=0)
        self.assertIs(state.current_plan, new_plan)
        legacy = {"created_at":10}
        self.assertTrue(same_plan({"plan_created_at":10}, legacy))
        self.assertFalse(same_plan({"plan_created_at":11}, legacy))

    async def test_offline_settlement_uses_last_online_day(self):
        self.clock._now = datetime(2026, 10, 18, 23, 58).timestamp()
        state, item = await self.prepare()
        state.life_updated_at = self.clock.now()
        self.clock.advance(3 * 3600)
        self.engine.reconcile_life_time(state)
        self.assertEqual(item["work_day"], "2026-10-18")
        self.assertEqual(item["worked_minutes"], 2)
        self.assertEqual(item["progress"], 0)

    async def test_midnight_retains_today_work_without_recounting_yesterday(self):
        self.clock._now = datetime(2026, 10, 18, 23, 59).timestamp()
        state, item = await self.prepare()
        state.current_action["elapsed_ticks"] = 0
        self.clock.advance(60)
        await self.engine._tick_continuous(state, self.engine.node("study"), TickOutcome(SESSION))
        self.clock.advance(60)
        await self.engine._tick_continuous(state, self.engine.node("study"), TickOutcome(SESSION))
        self.assertEqual(item["work_day"], "2026-10-19")
        self.assertEqual(item["worked_minutes"], 1)
        self.assertEqual(item["steps"][0]["worked_minutes"], 2)
        self.engine.pause_matter_action(state, state.current_action)
        self.assertEqual(item["worked_minutes"], 1)

    async def test_resuming_paused_project_respects_active_limit(self):
        state, item = await self.prepare(running=False)
        self.engine.manage_life_matter(state, item["id"], "pause")
        self.engine.world.limits.life_matter_active_max = 1
        self.engine.add_life_matter(state, "占用唯一空档")
        with self.assertRaisesRegex(ValueError, "上限"):
            self.engine.manage_life_matter(state, item["id"], "resume")
        self.assertEqual(item["status"], "paused")
