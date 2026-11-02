"""An available destination does not grant permission to walk from any room."""
from __future__ import annotations

import unittest

from tests.fake_astrbot import install

install()

from core.decider import Decider  # noqa: E402
from core.engine import TickOutcome  # noqa: E402
from core.models import ActionDef  # noqa: E402
from core.planner import create_plan  # noqa: E402
from core.state import WorldState  # noqa: E402
from tests import test_engine as fixture  # noqa: E402
from tests.test_life_environment_review_s import FixedDraw  # noqa: E402
from tests.test_outdoor_hint_review_s import HintHarness  # noqa: E402


class WalkOriginCandidateTests(unittest.TestCase):
    def setUp(self):
        self.engine = HintHarness()
        self.world = self.engine.world
        self.walking = self.world.action_map()["walk_to"]
        self.walking.scope = "node"
        self.walking.allowed_nodes = ["merch_gate"]
        self.state = WorldState("origin-review-y", node_id="alice_bedroom", state="idle",
                                energy=.9, curiosity=.8, boredom=.01, sleep_pressure=0,
                                node_since=self.engine.now - 7200,
                                last_outdoor_at=self.engine.now - 172800)
        self.decider = Decider(self.world, rng=FixedDraw(), now_provider=self.engine._now,
                               hour_provider=lambda: 15)

    def rule(self):
        return self.decider.rule_plan(self.state, group_chatting=False, interject_allowed=False)

    def candidate(self, steps):
        return self.decider._valid_candidate(self.state, {"steps": steps})

    def test_reachable_outing_is_not_offered_when_walking_cannot_start_here(self):
        self.assertTrue(self.decider._change_environment_due(self.state))
        self.assertFalse(self.walking.available_in(self.state.node_id))
        self.assertEqual(("", ""), self.decider._pick_outdoor_node(self.state, safe_only=True))
        self.assertIsNone(self.rule())

    def test_plan_validation_checks_the_origin_before_accepting_a_complete_route(self):
        self.assertFalse(self.candidate([{"action": "walk_to", "target_node": "hobby_corner"},
                                        {"action": "browse_heroes"}]))

    def test_unavailable_walking_does_not_offer_or_consume_an_outdoor_hint(self):
        self.assertEqual("", self.engine._outdoor_hint(self.state))
        self.assertEqual(0, self.state.outdoor_hint_at)

    def test_allowed_node_origin_keeps_rule_and_hint_available(self):
        self.walking.allowed_nodes = ["alice_bedroom"]
        plan = self.rule()
        self.assertEqual(["walk_to", "browse_heroes"], [step["action"] for step in plan["steps"]])
        self.assertEqual("hobby_corner", plan["steps"][0]["target_node"])
        self.assertIn("hobby_corner", self.engine._outdoor_hint(self.state))

    def test_global_walking_keeps_existing_rule_and_hint_available(self):
        self.walking.scope = "global"
        self.assertIsNotNone(self.rule())
        self.assertIn("hobby_corner", self.engine._outdoor_hint(self.state))

    def test_each_later_walk_checks_its_own_starting_place(self):
        steps = [{"action": "walk_to", "target_node": "merch_gate"},
                 {"action": "walk_to", "target_node": "hobby_corner"},
                 {"action": "browse_heroes"}]
        self.walking.allowed_nodes = ["alice_bedroom"]
        self.assertFalse(self.candidate(steps))
        self.walking.allowed_nodes.append("merch_gate")
        self.assertTrue(self.candidate(steps))
        self.assertEqual("alice_bedroom", self.state.node_id)

    def test_satisfied_location_step_keeps_the_following_onsite_activity(self):
        self.world.actions.append(ActionDef(id="onsite_y", category="continuous", scope="node",
                                            allowed_nodes=["alice_bedroom"], duration=600))
        self.assertTrue(self.candidate([{"action": "walk_to", "target_node": "alice_bedroom"},
                                       {"action": "onsite_y"}]))

    def test_satisfied_later_location_step_does_not_require_another_walk_origin(self):
        self.walking.allowed_nodes = ["alice_bedroom"]
        self.assertTrue(self.candidate([{"action": "walk_to", "target_node": "hobby_corner"},
                                       {"action": "walk_to", "target_node": "hobby_corner"},
                                       {"action": "browse_heroes"}]))

    def test_actual_activity_location_is_still_checked_after_walking(self):
        self.walking.allowed_nodes = ["alice_bedroom"]
        self.assertFalse(self.candidate([{"action": "walk_to", "target_node": "merch_gate"},
                                        {"action": "browse_heroes"}]))

    def test_unreachable_route_is_still_rejected_from_an_allowed_origin(self):
        self.walking.allowed_nodes = ["alice_bedroom"]
        self.assertFalse(self.candidate([{"action": "walk_to", "target_node": "unreachable_cafe"}]))

    def test_an_existing_stay_home_plan_still_blocks_the_extra_outing_opportunity(self):
        self.walking.allowed_nodes = ["alice_bedroom"]
        self.state.current_plan = create_plan(steps=[{"action": "walk_to", "target_node": "alice_bedroom"}],
                                              world_time=self.state.world_time, now=self.engine.now,
                                              valid_for=3600, source="llm", reason="今天想留在家里")
        self.assertFalse(self.decider._change_environment_due(self.state))
        self.assertIsNone(self.rule())


class WalkOriginRuntimeTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    def prepare_world(self):
        sample = HintHarness()
        for field in ("nodes", "zones", "edges", "zone_edges", "actions"):
            setattr(self.engine.world, field, getattr(sample.world, field))
        self.engine.world.remote_action_travel = False
        walking = self.engine.world.action_map()["walk_to"]
        walking.scope = "node"
        walking.allowed_nodes = ["alice_bedroom"]
        self.engine.decider.rng = FixedDraw()
        self.clock._now = sample.now

    def prepare_state(self, state):
        state.node_id, state.state = "alice_bedroom", "idle"
        state.energy, state.curiosity, state.boredom = .9, .8, .01
        state.node_since, state.last_outdoor_at = self.clock.now() - 7200, self.clock.now() - 172800
        state.sleep_pressure = 0
        state.current_action = state.current_plan = None

    async def test_allowed_scoped_walk_still_starts_a_real_outing_without_a_model(self):
        self.prepare_world()
        async with self.engine.session_state(fixture.SESSION) as state:
            self.prepare_state(state)
            plan = self.engine.decider.rule_plan(state, group_chatting=False, interject_allowed=False)
            self.assertIsNotNone(plan)
            outcome = TickOutcome(fixture.SESSION)
            await self.engine._apply_plan(state, self.engine.node(state.node_id), outcome, plan)
            await self.engine._tick_plan(state, self.engine.node(state.node_id), outcome, depth=0)
            self.assertEqual("walk_to", state.current_action["type"])
            self.assertEqual("hobby_corner", state.current_action["target_node"])
        self.assertEqual([], self.llm.calls)
        self.assertEqual([], self.tools.calls)

    async def test_satisfied_step_keeps_its_existing_continuation_without_a_journey(self):
        self.prepare_world()
        self.engine.world.actions.append(ActionDef(id="onsite_y", category="continuous", scope="node",
                                                  allowed_nodes=["alice_bedroom"], duration=600))
        async with self.engine.session_state(fixture.SESSION) as state:
            self.prepare_state(state)
            place_times = (state.node_since, state.last_outdoor_at)
            steps = [{"action": "walk_to", "target_node": "alice_bedroom"}, {"action": "onsite_y"}]
            self.assertTrue(self.engine.decider._valid_candidate(state, {"steps": steps}))
            state.current_plan = create_plan(steps=steps, world_time=state.world_time,
                                              now=self.clock.now(), valid_for=3600)
            await self.engine._tick_plan(state, self.engine.node(state.node_id),
                                        TickOutcome(fixture.SESSION), depth=0)
            self.assertEqual("onsite_y", state.current_action["type"])
            self.assertEqual("done", state.current_plan["steps"][0]["status"])
            self.assertEqual(place_times, (state.node_since, state.last_outdoor_at))
            self.assertNotIn("walk_to", state.action_usage)
            self.assertFalse(state.pending_arrival)
            self.assertFalse(any(row["kind"] == "move" for row in state.recent_events))
        self.assertEqual([], self.llm.calls)
        self.assertEqual([], self.tools.calls)

    async def test_satisfied_step_at_an_unavailable_walk_origin_keeps_onsite_work(self):
        self.prepare_world()
        walking = self.engine.world.action_map()["walk_to"]
        walking.allowed_nodes = ["merch_gate"]
        walking.on_complete.effects = {"energy": -.2}
        walking.on_complete.trigger = "llm_followup"
        self.engine.world.actions.append(ActionDef(id="onsite_y", category="continuous", scope="node",
                                                  allowed_nodes=["alice_bedroom"], duration=600))
        memories = await self.db.call("query_memories", session_id=fixture.SESSION, limit=100)
        async with self.engine.session_state(fixture.SESSION) as state:
            self.prepare_state(state)
            place_times = (state.node_since, state.last_outdoor_at)
            steps = [{"action": "walk_to", "target_node": "alice_bedroom"}, {"action": "onsite_y"}]
            self.assertFalse(walking.available_in(state.node_id))
            self.assertTrue(self.engine.decider._valid_candidate(state, {"steps": steps}))
            state.current_plan = create_plan(steps=steps, world_time=state.world_time,
                                              now=self.clock.now(), valid_for=3600)
            await self.engine._tick_plan(state, self.engine.node(state.node_id),
                                        TickOutcome(fixture.SESSION), depth=0)
            self.assertEqual("onsite_y", state.current_action["type"])
            self.assertEqual("done", state.current_plan["steps"][0]["status"])
            self.assertEqual("alice_bedroom", state.node_id)
            self.assertEqual(place_times, (state.node_since, state.last_outdoor_at))
            self.assertEqual(.9, state.energy)
            self.assertNotIn("walk_to", state.action_usage)
            self.assertFalse(state.pending_arrival)
            self.assertFalse(any(row["kind"] == "move" for row in state.recent_events))
        self.assertEqual(memories, await self.db.call("query_memories", session_id=fixture.SESSION, limit=100))
        done = await self.db.call("query_events", session_id=fixture.SESSION, event_type="action_done")
        self.assertFalse(any(row["detail"].get("type") == "walk_to" for row in done))
        self.assertEqual([], self.llm.calls)
        self.assertEqual([], self.tools.calls)


if __name__ == "__main__":
    unittest.main()
