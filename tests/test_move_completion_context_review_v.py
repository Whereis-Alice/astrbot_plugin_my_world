"""A walk's own completion sees its destination before the outer beat resumes."""
import json
import unittest

from core.engine import TickOutcome
from core.models import ActionDef
from core.planner import create_plan
from tests import test_engine as fixture


class MoveCompletionContextTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    def add_activity(self, node, *, action_id="onsite_reading", duration=600, followup=False):
        self.engine.world.actions.append(ActionDef(
            id=action_id, name="看书", category="continuous", scope="node",
            allowed_nodes=[node], duration=duration, visible=True,
            template="我正在{node}看书。",
            on_complete={"trigger": "llm_followup" if followup else "none"},
        ))
        return action_id

    async def start_plan(self, steps):
        async with self.engine.session_state(fixture.SESSION) as state:
            state.node_id, state.state, state.energy = "study", "idle", .9
            state.current_action, state.current_plan = None, None
            plan = create_plan(steps=steps, world_time=state.world_time,
                               now=self.clock.now(), valid_for=3600)
            outcome = TickOutcome(fixture.SESSION)
            await self.engine._apply_plan(state, self.engine.node(state.node_id), outcome, plan)
            await self.engine._tick_plan(state, self.engine.node(state.node_id), outcome, depth=0)
            return dict(state.current_action or {})

    async def next_beat(self):
        self.clock.advance(60)
        return await self.engine._tick_session(fixture.SESSION)

    async def test_walk_completion_prompt_and_selected_activity_use_destination(self):
        action = self.add_activity("kitchen")
        walk_definition = self.engine.world.action_map()["walk_to"]
        walk_definition.on_complete.trigger = "llm_followup"
        self.assertEqual("template", walk_definition.llm_level)
        self.assertEqual([], walk_definition.tool_list())
        self.llm.replies = [json.dumps({"actions": [{"type": action}]})]
        walk = await self.start_plan([{"action": "walk_to", "target_node": "kitchen"}])
        for _ in range(walk["duration_ticks"] - 1):
            await self.next_beat()
            self.assertEqual([], self.llm.calls)
        outcome = await self.next_beat()
        state = await self.engine.load_state(fixture.SESSION, cold_start=False)
        place = self.engine.node("kitchen").name
        self.assertEqual("kitchen", state.node_id)
        self.assertEqual(action, state.current_action["type"])
        self.assertEqual(1, len(self.llm.calls))
        self.assertIn("# 当前位置：" + place, self.llm.calls[0]["system_prompt"])
        self.assertEqual([f"我正在{place}看书。"], outcome.messages)
        self.assertFalse(state.pending_arrival)
        memories = await self.db.call("query_memories", session_id=fixture.SESSION, limit=100)
        moves = [row for row in memories if row["source"] == "runtime"
                 and row["content"] == "在这里移动"]
        self.assertEqual(["kitchen"], [row["node_id"] for row in moves])
        self.assertEqual([], self.messenger.sent)

    async def test_deleted_destination_cancels_move_without_completion_generation(self):
        self.engine.world.action_map()["walk_to"].on_complete.trigger = "llm_followup"
        await self.start_plan([{"action": "walk_to", "target_node": "kitchen"}])
        self.engine.world.nodes = [node for node in self.engine.world.nodes if node.id != "kitchen"]
        outcome = await self.next_beat()
        state = await self.engine.load_state(fixture.SESSION, cold_start=False)
        self.assertEqual("study", state.node_id)
        self.assertIsNone(state.current_action)
        self.assertIsNone(state.current_plan)
        self.assertEqual([], self.llm.calls)
        self.assertEqual([], outcome.messages)
        self.assertEqual([], self.messenger.sent)

    async def test_nonmoving_completion_keeps_current_place_for_its_followup(self):
        first = self.add_activity("study", action_id="first_read", duration=60, followup=True)
        second = self.add_activity("study", action_id="next_read")
        await self.start_plan([{"action": first}])
        self.llm.replies = [json.dumps({"actions": [{"type": second}]})]
        outcome = await self.next_beat()
        state = await self.engine.load_state(fixture.SESSION, cold_start=False)
        place = self.engine.node("study").name
        self.assertEqual("study", state.node_id)
        self.assertEqual(second, state.current_action["type"])
        self.assertIn("# 当前位置：" + place, self.llm.calls[0]["system_prompt"])
        self.assertEqual([f"我正在{place}看书。"], outcome.messages)
        self.assertEqual([], self.messenger.sent)
