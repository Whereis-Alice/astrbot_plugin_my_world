"""Dense indoor action lists must leave room for actual reachable destinations."""

import re
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from core.models import ActionDef, WorldConfig
from core.prompt import PromptBuilder


class RemoteActionHintReviewTests(unittest.TestCase):
    def setUp(self):
        self.world = WorldConfig.model_validate({
            "zones": [{"id": "home", "is_home": True}, {"id": "town"},
                      {"id": "park"}, {"id": "disconnected"}],
            "nodes": [
                {"id": "bedroom", "name": "卧室", "zone_id": "home"},
                {"id": "hall", "name": "玄关", "zone_id": "home"},
                {"id": "kitchen", "name": "厨房", "zone_id": "home"},
                {"id": "shop", "name": "商店", "zone_id": "town"},
                {"id": "cafe", "name": "咖啡馆", "zone_id": "town"},
                {"id": "river", "name": "河边", "zone_id": "park"},
                {"id": "island", "name": "孤岛", "zone_id": "disconnected"},
            ],
            "edges": [{"from": "bedroom", "to": "hall"},
                      {"from": "hall", "to": "kitchen"},
                      {"from": "shop", "to": "cafe"}],
            "zone_edges": [
                {"from_zone": "home", "to_zone": "town", "from_node": "hall",
                 "to_node": "shop", "ticks": 4},
                {"from_zone": "town", "to_zone": "park", "from_node": "cafe",
                 "to_node": "river", "ticks": 2},
            ],
        })
        for place, count in (("hall", 20), ("kitchen", 3), ("shop", 15),
                             ("cafe", 3), ("river", 3), ("island", 3)):
            self.world.actions.extend(ActionDef(
                id=f"{place}_{i}", name=f"活动{i}", scope="node", allowed_nodes=[place]
            ) for i in range(count))
        self.builder = PromptBuilder(
            self.world, now_provider=lambda: datetime(2026, 11, 3, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        )

    @staticmethod
    def options(text):
        return re.findall(r"([a-z_0-9]+)（[^）]+）\[([a-z_0-9]+)\]", text)

    def test_twelve_slots_cover_zones_and_places_instead_of_dense_nearby_room(self):
        text = self.builder.remote_action_hint("bedroom")
        choices = self.options(text)
        self.assertEqual(len(choices), 12)
        self.assertEqual({node for _action, node in choices}, {"hall", "kitchen", "shop", "cafe", "river"})
        self.assertEqual(choices[:3], [("hall_0", "hall"), ("shop_0", "shop"), ("river_0", "river")])
        self.assertEqual(text, self.builder.remote_action_hint("bedroom"))
        self.assertIn("target_node", text)

    def test_hidden_disabled_idle_and_wrong_time_actions_stay_out(self):
        self.world.actions.extend([
            ActionDef(id="closed_shop", scope="node", allowed_nodes=["shop"], enabled=False),
            ActionDef(id="idle_river", scope="node", allowed_nodes=["river"], idle=True),
            ActionDef(id="night_cafe", scope="node", allowed_nodes=["cafe"], preconditions={"time_window": "night"}),
        ])
        blocked = {"shop_0", "river_0"}
        choices = self.options(self.builder.remote_action_hint("bedroom", blocked))
        self.assertEqual(len(choices), 12)
        self.assertNotIn("island", {node for _action, node in choices})
        self.assertTrue(blocked.isdisjoint({action for action, _node in choices}))
        self.assertTrue({"closed_shop", "idle_river", "night_cafe"}.isdisjoint({action for action, _node in choices}))
        self.assertIn(("shop_1", "shop"), choices)
        self.assertIn(("river_1", "river"), choices)

    def test_shared_action_keeps_nearest_reachable_place_and_no_duplicate(self):
        self.world.actions = [
            ActionDef(id="drink", scope="node", allowed_nodes=["cafe", "kitchen"]),
            ActionDef(id="onsite", scope="node", allowed_nodes=["bedroom", "river"]),
            ActionDef(id="unreachable", scope="node", allowed_nodes=["island"]),
        ]
        self.assertEqual(self.options(self.builder.remote_action_hint("bedroom")), [("drink", "kitchen")])
        self.world.remote_action_travel = False
        self.assertEqual(self.builder.remote_action_hint("bedroom"), "")

    def test_quiet_scene_still_offers_outdoor_activity_ids_and_destinations(self):
        hidden = {action.id for action in self.world.actions if action.id.endswith("_0")}
        scene = self.builder.scene_layer(self.world.node_map()["bedroom"], {}, hidden)
        choices = self.options(scene)
        self.assertEqual(len(choices), 12)
        self.assertIn(("shop_1", "shop"), choices)
        self.assertIn(("river_1", "river"), choices)
        self.assertTrue(hidden.isdisjoint({action for action, _node in choices}))
