"""Queued activities use the actual place at the beat that travel completes."""
import unittest

from core.engine import TickOutcome
from core.models import ActionDef
from core.planner import create_plan
from tests import test_engine as fixture


class TravelPlanHandoffTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    def add_activity(self, node, *, category="continuous", template=True):
        definition = ActionDef(
            id="onsite_reading", name="看书", category=category,
            scope="node", allowed_nodes=[node], duration=600,
            visible=True, template="我正在{node}看书。" if template else "",
        )
        self.engine.world.actions.append(definition)
        return definition.id

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
        # Use the production beat, including its local node snapshot and plan
        # continuation; every port is a fixture and delivery is not invoked.
        return await self.engine._tick_session(fixture.SESSION)

    async def assert_arrival_activity(self, *, category):
        action = self.add_activity("kitchen", category=category)
        walk = await self.start_plan([
            {"action": "walk_to", "target_node": "kitchen"}, {"action": action},
        ])
        self.assertEqual("walk_to", walk["type"])
        for _ in range(walk["duration_ticks"] - 1):
            outcome = await self.next_beat()
            state = await self.engine.load_state(fixture.SESSION, cold_start=False)
            self.assertEqual("study", state.node_id)
            self.assertEqual("walk_to", state.current_action["type"])
            self.assertEqual([], outcome.messages)
        outcome = await self.next_beat()
        state = await self.engine.load_state(fixture.SESSION, cold_start=False)
        self.assertEqual("kitchen", state.node_id)
        self.assertEqual([f"我正在{self.engine.node('kitchen').name}看书。"], outcome.messages)
        self.assertFalse(state.pending_arrival)
        if category == "continuous":
            self.assertEqual(action, state.current_action["type"])
        else:
            self.assertIsNone(state.current_action)
            self.assertIsNone(state.current_plan)
        self.assertEqual([], self.llm.calls)
        self.assertEqual([], self.messenger.sent)

    async def test_continuous_activity_uses_destination_on_the_arrival_beat(self):
        await self.assert_arrival_activity(category="continuous")

    async def test_instant_activity_uses_destination_on_the_arrival_beat(self):
        await self.assert_arrival_activity(category="instant")

    async def test_deleted_destination_stops_travel_and_does_not_claim_arrival(self):
        action = self.add_activity("kitchen")
        await self.start_plan([
            {"action": "walk_to", "target_node": "kitchen"}, {"action": action},
        ])
        self.engine.world.nodes = [node for node in self.engine.world.nodes if node.id != "kitchen"]
        outcome = await self.next_beat()
        state = await self.engine.load_state(fixture.SESSION, cold_start=False)
        self.assertEqual("study", state.node_id)
        self.assertIsNone(state.current_action)
        self.assertIsNone(state.current_plan)
        self.assertFalse(state.pending_arrival)
        self.assertEqual([], outcome.messages)
        events = await self.db.call("query_events", session_id=fixture.SESSION, event_type="skip")
        self.assertTrue(any("移动目的地已删除" in row["detail"].get("note", "") for row in events))
        self.assertEqual([], self.llm.calls)

    async def test_ordinary_completion_keeps_current_place_for_its_next_activity(self):
        self.engine.world.actions.append(ActionDef(
            id="finish_bookmark", name="整理书签", category="continuous",
            scope="node", allowed_nodes=["study"], duration=60, visible=False,
        ))
        action = self.add_activity("study")
        await self.start_plan([{"action": "finish_bookmark"}, {"action": action}])
        outcome = await self.next_beat()
        state = await self.engine.load_state(fixture.SESSION, cold_start=False)
        self.assertEqual("study", state.node_id)
        self.assertEqual(action, state.current_action["type"])
        self.assertEqual([f"我正在{self.engine.node('study').name}看书。"], outcome.messages)
        self.assertEqual([], self.llm.calls)
        self.assertEqual([], self.messenger.sent)
