"""Long-running action quotas and ownership of an in-progress quiet reading."""
import unittest
import json
from datetime import timedelta

from core.engine import TickOutcome
from core.json_actions import PlannedAction
from core.models import ActionQuota
from tests import test_chat_browse as browse
from tests import test_engine as fixture


class LongRunningQuotaTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    async def run_history(self, period):
        raw = self.store.raw_world()
        next(action for action in raw["actions"] if action["id"] == "think")["quota"] = {period: 1}
        self.store.save_world(raw)
        self.engine.reload_config()
        definition = self.engine.world.action_map()["think"]
        for month in range(14):
            self.clock.advance(31 * 86400)
            async with self.engine.session_state(fixture.SESSION) as state:
                state.current_action, state.current_plan, state.state, state.energy = None, None, "idle", .9
                await self.engine._execute_actions(state, self.engine.node("study"), TickOutcome(fixture.SESSION),
                    [PlannedAction(type="think", content=f"第{month}次安静想一想")], depth=0, autonomous=True)
        return definition

    async def test_daily_limit_still_blocks_a_second_action_after_long_running_history(self):
        definition = await self.run_history("day")
        async with self.engine.session_state(fixture.SESSION) as state:
            self.assertEqual(self.engine.action_quota_left(state, definition), 0)
            before = len(self.engine.memory.db.query_memories(session_id=fixture.SESSION, memory_type="inner"))
            outcome = TickOutcome(fixture.SESSION)
            await self.engine._execute_actions(state, self.engine.node("study"), outcome,
                [PlannedAction(type="think", content="同一天不应再次开始")], depth=0, autonomous=True)
            self.assertEqual(len(self.engine.memory.db.query_memories(session_id=fixture.SESSION, memory_type="inner")), before)

    async def test_monthly_limit_survives_history_pruning(self):
        definition = await self.run_history("month")
        async with self.engine.session_state(fixture.SESSION) as state:
            self.assertEqual(self.engine.action_quota_left(state, definition), 0)

    async def test_next_day_and_reload_restore_only_the_daily_allowance(self):
        definition = await self.run_history("day")
        self.clock.advance(86400)
        self.engine.reload_config()
        async with self.engine.session_state(fixture.SESSION) as state:
            self.assertEqual(self.engine.action_quota_left(state, definition), 1)
            await self.engine._execute_actions(state, self.engine.node("study"), TickOutcome(fixture.SESSION),
                [PlannedAction(type="think", content="新的一天继续想一想")], depth=0, autonomous=True)
            self.assertEqual(self.engine.action_quota_left(state, definition), 0)
            self.assertLessEqual(len(state.action_usage["think"]), 12)

    async def test_weekly_limit_still_applies_then_resets_next_week(self):
        definition = await self.run_history("week")
        async with self.engine.session_state(fixture.SESSION) as state:
            self.assertEqual(self.engine.action_quota_left(state, definition), 0)
        self.clock.advance(7 * 86400)
        async with self.engine.session_state(fixture.SESSION) as state:
            self.assertEqual(self.engine.action_quota_left(state, definition), 1)

    async def test_no_quota_remains_unlimited_after_record_pruning(self):
        await self.run_history("day")
        definition = self.engine.world.action_map()["think"]
        definition.quota = ActionQuota()
        async with self.engine.session_state(fixture.SESSION) as state:
            self.assertGreater(self.engine.action_quota_left(state, definition), 0)
            await self.engine._execute_actions(state, self.engine.node("study"), TickOutcome(fixture.SESSION),
                [PlannedAction(type="think", content="无配额的动作继续可用")], depth=0, autonomous=True)
            self.assertGreater(self.engine.action_quota_left(state, definition), 0)
            self.assertLessEqual(len(state.action_usage["think"]), 12)


class ReadingOwnershipTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp
    configure = browse.ChatBrowseTests.configure
    note = browse.ChatBrowseTests.note
    prepare = browse.ChatBrowseTests.prepare
    complete = browse.ChatBrowseTests.complete

    async def test_switching_character_before_inference_stops_the_previous_reading(self):
        action = await self.prepare(reply_enabled=False)
        self.persona.text = "另一位完全不同的角色，尚未开始翻消息。"
        state = await self.complete(action)
        self.assertEqual(self.llm.calls, [])
        self.assertIsNone(state.current_action)
        self.assertFalse(any(thought.get("browse_id") == action["id"] for thought in state.thoughts))
        self.assertNotIn(fixture.SESSION, state.chat_browsing.get("sessions", {}))

    async def test_same_character_can_finish_reading_after_reload(self):
        action = await self.prepare(reply_enabled=False)
        self.engine.reload_config()
        state = await self.complete(action)
        self.assertEqual(state.chat_browsing["sessions"][fixture.SESSION]["completed"], 1)
        self.assertTrue(any(thought.get("browse_id") == action["id"] for thought in state.thoughts))
        self.assertEqual(self.messenger.flat_messages, [])

    async def test_unmarked_legacy_reading_stays_compatible(self):
        action = await self.prepare(reply_enabled=False)
        async with self.engine.session_state(fixture.SESSION) as state:
            state.current_action.pop("persona_key")
        action.pop("persona_key")
        state = await self.complete(action)
        self.assertEqual(state.chat_browsing["sessions"][fixture.SESSION]["completed"], 1)

    async def test_disabled_reading_is_not_completed_or_sent(self):
        action = await self.prepare(reply_enabled=False)
        self.engine.world.chat_browse.phone = False
        state = await self.complete(action)
        self.assertEqual(self.llm.calls, [])
        self.assertIsNone(state.current_action)
        self.assertNotIn(fixture.SESSION, state.chat_browsing.get("sessions", {}))


class GenerationAndContextTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    async def test_generic_cooking_draft_uses_the_named_kitchen_instead_of_shared_room_character(self):
        raw = self.store.raw_world()
        raw["nodes"].append({"id": "review_kitchen", "name": "料理小厨房", "zone_id": raw["nodes"][0]["zone_id"]})
        self.store.save_world(raw)
        self.engine.reload_config()
        self.engine.creator_llm = self.llm
        self.llm.replies = [json.dumps({"actions": [{"id": "review_cook", "name": "做饭",
            "description": "在料理小厨房准备晚饭"}]}, ensure_ascii=False)]
        result = await self.engine.generate_generic_actions(1, False)
        self.assertEqual(len(result["actions"]), 1)
        self.assertEqual(result["actions"][0]["allowed_nodes"], ["review_kitchen"])
        self.assertNotIn("review_cook", self.engine.world.action_map())

    async def test_zero_life_context_budget_hides_an_existing_diary(self):
        _, key = await self.engine._journal_persona(fixture.SESSION)
        day = (self.engine.local_now().date() - timedelta(days=1)).isoformat()
        await self.db.call("diary_upsert", self.engine._journal_scope(), key, day,
            {"title": "昨天的回顾", "mood": "", "content": "昨天慢慢读了一会儿书。"})
        self.engine.world.life_context.max_chars = 0
        self.assertEqual(await self.engine.life_context_text(fixture.SESSION), "")

    async def test_explicit_longer_place_name_wins_over_a_shorter_contained_name(self):
        raw = self.store.raw_world()
        raw["nodes"].append({"id": "review_big_study", "name": "大书房", "zone_id": raw["nodes"][0]["zone_id"]})
        self.store.save_world(raw)
        self.engine.reload_config()
        self.engine.creator_llm = self.llm
        self.llm.replies = [json.dumps({"actions": [{"id": "review_read", "name": "读书",
            "description": "在大书房读一会儿书"}]}, ensure_ascii=False)]
        result = await self.engine.generate_generic_actions(1, False)
        self.assertEqual(result["actions"][0]["allowed_nodes"], ["review_big_study"])

    async def test_unlocated_generic_actions_remain_distributed_without_saving(self):
        self.engine.creator_llm = self.llm
        self.llm.replies = ['{"actions":[{"id":"review_stretch","name":"伸展身体"},{"id":"review_sip","name":"喝水"}]}']
        result = await self.engine.generate_generic_actions(2, False)
        self.assertEqual(len({action["node_id"] for action in result["actions"]}), 2)
        self.assertNotIn("review_stretch", self.engine.world.action_map())

    async def test_positive_life_context_budget_still_includes_a_bounded_diary(self):
        _, key = await self.engine._journal_persona(fixture.SESSION)
        day = (self.engine.local_now().date() - timedelta(days=1)).isoformat()
        await self.db.call("diary_upsert", self.engine._journal_scope(), key, day,
            {"title": "昨天的回顾", "mood": "", "content": "昨天慢慢读了一会儿书。"})
        self.engine.world.life_context.max_chars = 120
        result = await self.engine.life_context_text(fixture.SESSION)
        self.assertIn("昨天慢慢读了一会儿书", result)
        self.assertLessEqual(len(result), 120)

    async def test_zero_context_budget_does_not_read_calendar_or_diary(self):
        from unittest.mock import AsyncMock
        self.engine.world.life_context.max_chars = 0
        self.engine.calendar_for_year = AsyncMock(side_effect=AssertionError("Unexpected calendar read"))
        self.assertEqual(await self.engine.life_context_text(fixture.SESSION), "")
        self.engine.calendar_for_year.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
