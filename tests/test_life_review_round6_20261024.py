"""Running actions and queued plans obey the life that actually owns them."""
import unittest
import copy
from hashlib import sha256
from unittest.mock import AsyncMock

from core.engine import TickOutcome
from core.json_actions import PlannedAction
from core.models import ActionDef
from core.planner import create_plan
from tests import test_life_audit as audit

S = audit.S


class RunningLifeTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = audit.LifeAuditTests.asyncSetUp
    idle = audit.LifeAuditTests.idle

    async def start(self, actions):
        await self.idle()
        self.engine._llm_followup = AsyncMock(return_value=False)
        self.engine._tick_sleep_rhythm = AsyncMock()
        async with self.engine.session_state(S) as state:
            await self.engine._execute_actions(state, self.engine.node("study"), TickOutcome(session_id=S),
                actions, depth=0, autonomous=True)
            self.assertIsNotNone(state.current_action)

    async def test_changed_character_does_not_finish_or_narrate_the_old_running_action(self):
        await self.start([PlannedAction(type="read", duration=60)])
        async with self.engine.session_state(S) as state:
            state.current_action["duration_ticks"] = 1
        self.persona.text = "另一位完全不同的园艺少女。"
        outcome = await self.engine._tick_session(S)
        self.engine._llm_followup.assert_not_awaited()
        self.assertIsNotNone(outcome)
        self.assertFalse(await self.db.call("query_events", session_id=S, event_type="action_done"))

    async def test_old_queued_speech_is_not_carried_into_the_new_character(self):
        await self.start([PlannedAction(type="read", duration=60),
                          PlannedAction(type="say", messages=["旧角色计划说的话"])])
        async with self.engine.session_state(S) as state:
            self.assertIsNotNone(state.current_plan)
            state.current_action["duration_ticks"] = 1
        self.persona.text = "另一位完全不同的园艺少女。"
        outcome = await self.engine._tick_session(S)
        self.assertNotIn("旧角色计划说的话", outcome.messages)
        self.assertIsNone((await self.engine.load_state(S)).current_plan)

    async def charge(self):
        self.engine.world.sleep.mode = "activity"
        self.engine.world.actions.append(ActionDef(id="charging", name="充电", category="continuous",
            llm_level="template", recovery_kind="restore", duration_mode="llm", duration=600,
            duration_min=60, duration_max=600,
            during={"state": "charging"}, on_complete={"effects_per_minute": {"energy": "+0.02"},
                "effects": {"energy": "+0.3"}, "trigger": "none"}))
        await self.start([PlannedAction(type="charging", duration=600)])
        async with self.engine.session_state(S) as state:
            self.assertEqual(state.current_action["duration_ticks"] * self.engine.tick_seconds, 600)
            state.energy = .3
            state.life_updated_at = self.clock.now()

    async def test_disabled_recovery_stops_in_the_numerical_thinking_clock(self):
        await self.charge()
        self.engine.world.action_map()["charging"].enabled = False
        self.clock.advance(60)
        await self.engine._tick_while_thinking(S)
        state = await self.engine.load_state(S)
        self.assertIsNone(state.current_action)
        self.assertLessEqual(state.energy, .3)

    async def test_character_switch_is_checked_before_offline_completion(self):
        await self.charge()
        self.persona.text = "另一位完全不同的园艺少女。"
        self.clock.advance(1200)
        outcome = await self.engine._tick_session(S)
        self.engine._llm_followup.assert_not_awaited()
        self.assertIsNotNone(outcome)
        self.assertFalse(await self.db.call("query_events", session_id=S, event_type="action_done"))
        state = await self.engine.load_state(S)
        self.assertIsNone(state.current_action)
        self.assertLessEqual(state.energy, .3)

    async def test_same_character_finishes_and_narrates_the_action_normally(self):
        await self.start([PlannedAction(type="read", duration=60)])
        await self.engine._tick_session(S)
        self.engine._llm_followup.assert_awaited_once()
        events = await self.db.call("query_events", session_id=S, event_type="action_done")
        self.assertEqual([row["detail"]["type"] for row in events], ["read"])

    async def test_valid_recovery_keeps_progressing_during_thinking(self):
        await self.charge()
        self.clock.advance(60)
        await self.engine._tick_while_thinking(S)
        state = await self.engine.load_state(S)
        self.assertEqual(state.current_action["elapsed_ticks"], 1)
        self.assertGreater(state.energy, .3)
        self.assertLess(state.energy, .33)

    async def test_same_character_finishes_passive_recovery_offline_without_speech(self):
        await self.charge()
        self.clock.advance(1200)
        outcome = await self.engine._tick_session(S)
        self.assertEqual(outcome.messages, [])
        state = await self.engine.load_state(S)
        self.assertIsNone(state.current_action)
        self.assertGreater(state.energy, .3)
        self.assertEqual(len(await self.db.call("query_events", session_id=S, event_type="action_done")), 1)

    async def test_disabled_recovery_stops_in_the_normal_clock(self):
        await self.charge()
        self.engine.world.action_map()["charging"].enabled = False
        self.clock.advance(60)
        await self.engine._tick_session(S)
        state = await self.engine.load_state(S)
        self.assertIsNone(state.current_action)
        self.assertLessEqual(state.energy, .3)
        self.assertFalse(await self.db.call("query_events", session_id=S, event_type="action_done"))

    async def test_recovery_scope_change_stops_before_thinking_time_is_settled(self):
        await self.charge()
        definition = self.engine.world.action_map()["charging"]
        definition.scope, definition.node_ids = "node", ["bedroom"]
        self.clock.advance(60)
        await self.engine._tick_while_thinking(S)
        state = await self.engine.load_state(S)
        self.assertIsNone(state.current_action)
        self.assertLessEqual(state.energy, .3)

    async def test_removed_and_idle_actions_stop_before_thinking_time_is_settled(self):
        for change in ("removed", "idle"):
            with self.subTest(change=change):
                self.engine.world.actions = [a for a in self.engine.world.actions if a.id != "charging"]
                await self.charge()
                if change == "removed":
                    self.engine.world.actions = [a for a in self.engine.world.actions if a.id != "charging"]
                else:
                    self.engine.world.action_map()["charging"].idle = True
                self.clock.advance(60)
                await self.engine._tick_while_thinking(S)
                state = await self.engine.load_state(S)
                self.assertIsNone(state.current_action)
                self.assertLessEqual(state.energy, .3)

    async def test_foreign_plan_without_running_action_cannot_send_queued_speech(self):
        await self.idle()
        async with self.engine.session_state(S) as state:
            plan = create_plan(steps=[{"action": "say", "messages": ["旧计划的话"]}],
                world_time=state.world_time, now=self.clock.now())
            await self.engine._apply_plan(state, self.engine.node("study"), TickOutcome(S), plan)
        self.persona.text = "另一位完全不同的园艺少女。"
        outcome = await self.engine._tick_session(S)
        self.assertNotIn("旧计划的话", outcome.messages)
        self.assertIsNone((await self.engine.load_state(S)).current_plan)

    async def test_foreign_running_action_does_not_cancel_the_new_characters_plan(self):
        await self.start([PlannedAction(type="read", duration=60)])
        state = await self.engine.load_state(S)
        self.persona.text = "另一位完全不同的园艺少女。"
        state.current_plan = create_plan(steps=[{"action": "read", "duration": 300}],
            world_time=state.world_time, now=self.clock.now(),
            persona_key=sha256(self.persona.text.encode()).hexdigest()[:24])
        plan_id = state.current_plan["id"]
        await self.engine.save_state(state)
        await self.engine._tick_session(S)
        state = await self.engine.load_state(S)
        self.assertEqual(state.current_plan["id"], plan_id)
        self.assertEqual(state.current_plan["current_step"], 0)
        self.assertEqual(state.current_action["type"], "read")
        self.assertEqual(state.current_action["elapsed_ticks"], 0)
        self.engine._llm_followup.assert_not_awaited()

    async def test_disabled_action_does_not_skip_a_different_plan(self):
        await self.charge()
        state = await self.engine.load_state(S)
        state.current_action.update(from_plan=True, plan_id="previous-plan", plan_index=0)
        state.current_plan = create_plan(steps=[{"action": "read", "duration": 300}],
            world_time=state.world_time, now=self.clock.now())
        plan_id = state.current_plan["id"]
        await self.engine.save_state(state)
        self.engine.world.action_map()["charging"].enabled = False
        await self.engine._tick_session(S)
        state = await self.engine.load_state(S)
        self.assertEqual(state.current_plan["id"], plan_id)
        self.assertEqual(state.current_plan["current_step"], 0)
        self.assertEqual(state.current_action["type"], "read")

    async def test_legacy_unmarked_actions_and_plans_are_not_assigned_a_guessed_character(self):
        await self.start([PlannedAction(type="read", duration=60),
                          PlannedAction(type="say", messages=["历史计划的话"])])
        async with self.engine.session_state(S) as state:
            state.current_action.pop("persona_key")
            state.current_plan.pop("persona_key")
            state.current_action["duration_ticks"] = 1
        self.persona.text = "另一位完全不同的园艺少女。"
        outcome = await self.engine._tick_session(S)
        self.assertIn("历史计划的话", outcome.messages)
        self.engine._llm_followup.assert_awaited_once()

    async def test_explicit_completion_does_not_complete_a_disabled_action(self):
        await self.charge()
        action = copy.deepcopy((await self.engine.load_state(S)).current_action)
        action["elapsed_ticks"] = action["duration_ticks"]
        self.engine.world.action_map()["charging"].enabled = False
        async with self.engine.session_state(S) as state:
            energy = state.energy
            self.assertFalse(await self.engine._finish_action(state, self.engine.node("study"), TickOutcome(S), action))
            self.assertEqual(state.energy, energy)
        self.assertFalse(await self.db.call("query_events", session_id=S, event_type="action_done"))

    async def test_previous_plan_does_not_enter_the_new_characters_reply_context(self):
        await self.idle()
        self.engine.world.idle_life.enabled = False
        async with self.engine.session_state(S) as state:
            plan = create_plan(steps=[{"action": "read"}], world_time=state.world_time,
                now=self.clock.now(), reason="旧角色准备读蓝色风车的小说")
            await self.engine._apply_plan(state, self.engine.node("study"), TickOutcome(S), plan)
            state.current_plan = None
        self.persona.text = "另一位完全不同的园艺少女。"
        async with self.engine.session_state(S) as state:
            request = self.engine.context_request(state, S, text="现在打算做什么？", rows=[])
            await self.engine.prepare_context_memory(request)
            text = self.engine.prompts.runtime_layer(state, node=self.engine.node("study"), context_request=request)
            self.assertNotIn("旧角色准备读蓝色风车的小说", text)

    async def test_read_only_prompt_paths_hide_foreign_running_action(self):
        await self.start([PlannedAction(type="read", duration=60)])
        async with self.engine.session_state(S) as state:
            state.current_action["desc"] = "旧角色的阅读进度"
        self.persona.text = "另一位完全不同的园艺少女。"
        state = await self.engine.load_state(S)
        request = self.engine.context_request(state, S, text="你在做什么？", rows=[])
        await self.engine.prepare_context_memory(request)
        text = self.engine.prompts.runtime_layer(state, node=self.engine.node("study"), context_request=request)
        self.assertNotIn("旧角色的阅读进度", text)
        _, legacy = self.engine.prompts.build_dialogue_prompt(state=state,
            node=self.engine.node("study"), persona_text=self.persona.text)
        self.assertNotIn("旧角色的阅读进度", legacy)
        self.assertEqual(state.current_action["desc"], "旧角色的阅读进度", "rendering must not mutate history")

    async def test_unknown_character_cannot_complete_a_marked_action(self):
        await self.charge()
        state = await self.engine.load_state(S)
        action = copy.deepcopy(state.current_action)
        action["elapsed_ticks"] = action["duration_ticks"]
        energy = state.energy
        self.assertFalse(await self.engine._finish_action(state, self.engine.node("study"), TickOutcome(S), action))
        self.assertEqual(state.energy, energy)
        self.assertFalse(await self.db.call("query_events", session_id=S, event_type="action_done"))

    async def test_same_character_queued_speech_survives_config_reload(self):
        await self.start([PlannedAction(type="read", duration=60),
                          PlannedAction(type="say", messages=["已经读到这里了"])])
        old = await self.engine.load_state(S)
        self.assertEqual(old.current_plan["persona_key"], old.current_action["persona_key"])
        self.engine.reload_config()
        outcome = await self.engine._tick_session(S)
        self.assertIn("已经读到这里了", outcome.messages)
        self.engine._llm_followup.assert_awaited_once()

    async def test_invalid_action_skips_its_step_without_skipping_following_work(self):
        await self.charge()
        async with self.engine.session_state(S) as state:
            state.current_plan = create_plan(steps=[{"action": "charging"}, {"action": "read", "duration": 300}],
                world_time=state.world_time, now=self.clock.now(), persona_key=state.current_action["persona_key"])
            state.current_action.update(from_plan=True, plan_id=state.current_plan["id"], plan_index=0)
        self.engine.world.action_map()["charging"].enabled = False
        await self.engine._tick_session(S)
        state = await self.engine.load_state(S)
        self.assertEqual(state.current_plan["steps"][0]["status"], "skipped")
        self.assertEqual(state.current_plan["current_step"], 1)
        self.assertEqual(state.current_action["type"], "read")
        self.assertFalse(await self.db.call("query_events", session_id=S, event_type="action_done"))

    async def test_deleted_walking_destination_is_not_logged_as_a_completed_move(self):
        await self.start([PlannedAction(type="walk_to", target_node="bedroom")])
        async with self.engine.session_state(S) as state:
            state.current_action["duration_ticks"] = 1
        self.engine.world.nodes = [node for node in self.engine.world.nodes if node.id != "bedroom"]
        await self.engine._tick_session(S)
        self.assertFalse(await self.db.call("query_events", session_id=S, event_type="action_done"))
        state = await self.engine.load_state(S)
        self.assertEqual(state.node_id, "study")
        self.assertIsNone(state.current_action)


class InnerActivityOwnershipTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = audit.LifeAuditTests.asyncSetUp
    idle = audit.LifeAuditTests.idle

    async def runtime(self, *, task="plan"):
        async with self.engine.session_state(S) as state:
            request = self.engine.context_request(state, S, task=task, text="蓝色风车小说", rows=[])
            await self.engine.prepare_context_memory(request)
            return self.engine.prompts.runtime_layer(state, node=self.engine.node("study"), context_request=request)

    async def thought(self):
        await self.idle()
        async with self.engine.session_state(S) as state:
            await self.engine._execute_actions(state, self.engine.node("study"), TickOutcome(S),
                [PlannedAction(type="think", content="旧角色想把风车涂成蓝色")], depth=0, autonomous=True)
            self.assertTrue(any(row["content"] == "旧角色想把风车涂成蓝色" for row in state.thoughts))

    async def plan(self):
        await self.idle()
        async with self.engine.session_state(S) as state:
            plan = create_plan(steps=[{"action": "read"}], world_time=state.world_time,
                now=self.clock.now(), reason="旧角色准备读蓝色风车的小说")
            await self.engine._apply_plan(state, self.engine.node("study"), TickOutcome(S), plan)

    async def test_stored_thought_does_not_enter_another_characters_planning(self):
        await self.thought()
        self.persona.text = "另一位完全不同的园艺少女。"
        self.assertNotIn("旧角色想把风车涂成蓝色", await self.runtime())

    async def test_planning_draft_does_not_enter_another_characters_planning(self):
        await self.plan()
        async with self.engine.session_state(S) as state:
            state.current_plan, state.last_plan, state.thoughts, state.recent_events = None, {}, [], []
        self.persona.text = "另一位完全不同的园艺少女。"
        self.assertNotIn("旧角色准备读蓝色风车的小说", await self.runtime())

    async def test_recent_plan_event_does_not_enter_another_characters_planning(self):
        await self.plan()
        async with self.engine.session_state(S) as state:
            state.current_plan, state.last_plan, state.thoughts, state.last_reasoning = None, {}, [], {}
        self.persona.text = "另一位完全不同的园艺少女。"
        self.assertNotIn("旧角色准备读蓝色风车的小说", await self.runtime())

    async def test_same_character_keeps_her_thought_and_planning_draft(self):
        await self.thought()
        self.assertIn("旧角色想把风车涂成蓝色", await self.runtime())
        await self.plan()
        async with self.engine.session_state(S) as state:
            state.current_plan, state.last_plan, state.thoughts, state.recent_events = None, {}, [], []
        self.assertIn("旧角色准备读蓝色风车的小说", await self.runtime())

    async def test_unmarked_historical_thought_and_draft_remain_compatible(self):
        await self.thought()
        async with self.engine.session_state(S) as state:
            for row in state.thoughts:
                row.pop("persona_key", None)
        self.persona.text = "另一位完全不同的园艺少女。"
        self.assertIn("旧角色想把风车涂成蓝色", await self.runtime())
        async with self.engine.session_state(S) as state:
            state.note_reasoning({"intent": "历史的蓝色风车小说"}, source="plan")
            state.thoughts, state.recent_events = [], []
        self.assertIn("历史的蓝色风车小说", await self.runtime())

    async def test_foreign_draft_does_not_hide_current_characters_own_thought(self):
        await self.thought()
        old_key = sha256(self.persona.text.encode()).hexdigest()[:24]
        self.persona.text = "另一位完全不同的园艺少女。"
        async with self.engine.session_state(S) as state:
            await self.engine._remember_inner_thought(state, "新角色想种蓝色风车花", source="她想事情")
            state.note_reasoning({"intent": "旧角色准备读蓝色风车的小说"}, source="plan", persona_key=old_key)
        text = await self.runtime()
        self.assertIn("新角色想种蓝色风车花", text)
        self.assertNotIn("旧角色准备读蓝色风车的小说", text)
        self.assertNotIn("旧角色想把风车涂成蓝色", text)

    async def test_actual_reflection_draft_is_scoped_to_its_character(self):
        await self.idle()
        self.engine._ask_judge = AsyncMock(return_value='{"reasoning":{"intent":"旧角色的蓝色风车小说想法"},"inner":"蓝色风车小说让我想到以前"}')
        await self.engine.reflect_turn(S, user_text="蓝色风车小说", reply_text="嗯")
        state = await self.engine.load_state(S)
        self.assertEqual(state.last_reasoning["_persona_key"], sha256(self.persona.text.encode()).hexdigest()[:24])
        self.persona.text = "另一位完全不同的园艺少女。"
        self.assertNotIn("旧角色的蓝色风车小说想法", await self.runtime(task="reply"))
