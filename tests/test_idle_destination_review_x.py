"""A known disconnected place must not consume a usable quiet activity choice."""
from __future__ import annotations

import unittest

from core.decider import Decider
from core.models import WorldConfig
from core.state import WorldState


class DestinationDraw:
    def __init__(self, preferred=""):
        self.preferred = preferred
        self.pools = []

    def choice(self, values):
        self.pools.append(list(values))
        return self.preferred if self.preferred in values else values[0]

    def random(self):
        return 0


class IdleDestinationReviewTests(unittest.TestCase):
    def setUp(self):
        self.world = WorldConfig(
            zones=[{"id": "home", "is_home": True}, {"id": "closed"}],
            nodes=[{"id": "room", "zone_id": "home"},
                   {"id": "closed_corner", "zone_id": "closed"},
                   {"id": "window", "zone_id": "home"}],
            edges=[{"from": "room", "to": "window", "ticks": 1}],
            actions=[{"id": "walk_to", "scope": "global", "category": "continuous"},
                     {"id": "stare", "scope": "node", "allowed_nodes": ["closed_corner", "window"],
                      "category": "continuous", "duration": 60, "life_role": "rest"}],
        )
        self.state = WorldState("idle-choice", node_id="room", state="idle", energy=.9,
                                curiosity=.1, boredom=.9, sleep_pressure=0, world_time=100)
        self.draw = DestinationDraw("closed_corner")
        self.decider = Decider(self.world, rng=self.draw, now_provider=lambda: 1793689200,
                               hour_provider=lambda: 15)

    def plan(self):
        return self.decider.rule_plan(self.state, group_chatting=False, interject_allowed=False)

    def assert_window_activity(self, plan):
        self.assertIsNotNone(plan)
        self.assertEqual([step["action"] for step in plan["steps"]], ["walk_to", "stare"])
        self.assertEqual(plan["steps"][0]["target_node"], "window")

    def test_unreachable_first_choice_keeps_complete_rule_and_protective_plans(self):
        self.assert_window_activity(self.plan())
        self.assert_window_activity(self.decider.forced_plan(self.state, "need_change"))
        self.assertTrue(self.draw.pools)
        self.assertTrue(all("closed_corner" not in pool for pool in self.draw.pools))

    def test_only_unreachable_activities_do_not_invent_a_journey(self):
        self.world.edges = []
        self.assertEqual("", self.decider._pick_idle_node(exclude="room"))
        self.assertIsNone(self.plan())
        self.assertIsNone(self.decider.forced_plan(self.state, "need_change"))
        self.assertEqual([], self.draw.pools)

    def test_no_reachable_destination_keeps_current_quiet_activity_available(self):
        self.world.edges = []
        self.world.action_map()["stare"].allowed_nodes.append("room")
        plan = self.plan()
        self.assertEqual([step["action"] for step in plan["steps"]], ["stare"])
        self.assertEqual("room", self.state.node_id)
        self.assertEqual([], self.draw.pools)

    def test_current_place_is_excluded_when_other_places_are_available(self):
        self.world.action_map()["stare"].allowed_nodes.append("room")
        self.draw.preferred = "room"
        self.assertEqual("window", self.decider._pick_idle_node(exclude="room"))
        self.assertEqual(["window"], self.draw.pools[-1])

    def test_reachable_fallback_when_no_node_declares_stare_is_preserved(self):
        self.world.actions = [action for action in self.world.actions if action.id != "stare"]
        self.assertEqual("window", self.decider._pick_idle_node(exclude="room"))
        self.assertEqual(["window"], self.draw.pools[-1])

    def test_valid_random_choices_are_preserved(self):
        self.world.nodes.append(self.world.nodes[-1].model_copy(update={"id": "garden"}))
        self.world.edges.append(self.world.edges[-1].model_copy(update={"to": "garden"}))
        self.world.action_map()["stare"].allowed_nodes.append("garden")
        for destination in ("garden", "window"):
            with self.subTest(destination=destination):
                self.draw.preferred = destination
                self.assertEqual(destination, self.decider._pick_idle_node(exclude="room"))
                self.assertEqual(["window", "garden"], self.draw.pools[-1])

    def test_a_reverse_only_edge_does_not_prove_the_destination_reachable(self):
        self.world.edges[0] = self.world.edges[0].model_copy(
            update={"from_": "window", "to": "room", "bidirectional": False})
        self.assertEqual("", self.decider._pick_idle_node(exclude="room"))
        self.assertIsNone(self.plan())

    def test_an_unknown_current_place_cannot_invent_a_connected_route(self):
        self.assertEqual("", self.decider._pick_idle_node(exclude="deleted_room"))

    def test_legacy_call_without_a_current_place_keeps_its_candidate_semantics(self):
        self.assertEqual("closed_corner", self.decider._pick_idle_node())
        self.assertEqual(["closed_corner", "window"], self.draw.pools[-1])
