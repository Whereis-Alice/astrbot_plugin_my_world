"""Outdoor prompts use the actual world and an hourly, model-free cooldown."""
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from core.engine.actions import ActionsMixin
from core.engine.constants import OUTDOOR_HINT_HOURS
from core.engine.cuddle import CuddleMixin
from core.engine.limits import LimitsMixin
from core.models import WorldConfig
from core.state import WorldState


class HintHarness(CuddleMixin, ActionsMixin, LimitsMixin):
    def __init__(self):
        self.now = datetime(2026, 11, 3, 15, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        self.world = WorldConfig(
            zones=[{"id": "alice_home", "is_home": True}, {"id": "hobby_district"},
                   {"id": "closed_island"}],
            nodes=[{"id": "alice_bedroom", "name": "卧室工作间", "zone_id": "alice_home"},
                   {"id": "merch_gate", "name": "商业街入口", "zone_id": "hobby_district"},
                   {"id": "hobby_corner", "name": "特摄周边店", "zone_id": "hobby_district"},
                   {"id": "unreachable_cafe", "name": "无人能到的咖啡屋", "zone_id": "closed_island"}],
            edges=[{"from": "merch_gate", "to": "hobby_corner", "ticks": 1}],
            zone_edges=[{"from_zone": "alice_home", "to_zone": "hobby_district",
                         "from_node": "alice_bedroom", "to_node": "merch_gate", "ticks": 2}],
            actions=[{"id": "walk_to", "scope": "global", "category": "continuous", "life_role": "explore"},
                     {"id": "browse_heroes", "name": "看看新到的英雄周边", "scope": "node",
                      "allowed_nodes": ["hobby_corner"], "category": "continuous", "life_role": "interest"}],
        )

    def _now(self):
        return self.now

    def local_now(self):
        return datetime.fromtimestamp(self.now, ZoneInfo("Asia/Shanghai"))


class OutdoorHintReviewTests(unittest.TestCase):
    def setUp(self):
        self.engine = HintHarness()
        self.state = WorldState("world-test", node_id="alice_bedroom", energy=.9)

    def test_custom_world_uses_reachable_real_places_and_their_activities(self):
        text = self.engine._outdoor_hint(self.state)
        self.assertIn("hobby_corner", text)
        self.assertIn("browse_heroes", text)
        self.assertIn("特摄周边店", text)
        self.assertNotIn("unreachable_cafe", text)
        self.assertNotIn("merch_gate", text, "a useful destination should precede its entrance")
        for old_id in ("street_gate", "mall", "snacks", "park_gate", "park_lake", "boardwalk"):
            self.assertNotIn(old_id, text)
        self.assertEqual(self.state.outdoor_hint_at, self.engine.now)

    def test_hourly_cooldown_survives_many_decisions_and_opens_at_boundary(self):
        self.assertTrue(self.engine._outdoor_hint(self.state))
        self.engine.now += 60
        self.assertEqual(self.engine._outdoor_hint(self.state), "")
        self.engine.now += OUTDOOR_HINT_HOURS * 3600 - 61
        self.assertEqual(self.engine._outdoor_hint(self.state), "")
        self.engine.now += 1
        self.assertTrue(self.engine._outdoor_hint(self.state))

    def test_recent_outing_has_the_same_hourly_boundary(self):
        self.state.last_outdoor_at = self.engine.now
        self.engine.now += OUTDOOR_HINT_HOURS * 3600 - 1
        self.assertEqual(self.engine._outdoor_hint(self.state), "")
        self.engine.now += 1
        self.assertIn("10 小时", self.engine._outdoor_hint(self.state))

    def test_missing_outing_history_does_not_invent_two_days_at_home(self):
        text = self.engine._outdoor_hint(self.state)
        self.assertIn("在家", text)
        self.assertNotIn("这两天", text)
        self.assertNotIn("小时没出过门", text)

    def test_unreachable_map_does_not_consume_the_hint_cooldown(self):
        self.engine.world.zone_edges = []
        self.assertEqual(self.engine._outdoor_hint(self.state), "")
        self.assertEqual(self.state.outdoor_hint_at, 0)

    def test_disabled_or_exhausted_walk_does_not_offer_impossible_travel(self):
        walking = self.engine.world.action_map()["walk_to"]
        walking.enabled = False
        self.assertEqual(self.engine._outdoor_hint(self.state), "")
        walking.enabled = True
        walking.quota.day = 1
        self.engine._count_action_use(self.state, walking)
        self.assertEqual(self.engine._outdoor_hint(self.state), "")
        self.assertEqual(self.state.outdoor_hint_at, 0)

    def test_currently_blocked_activity_is_not_described_as_available(self):
        action = self.engine.world.action_map()["browse_heroes"]
        action.preconditions.time_window = "night"
        text = self.engine._outdoor_hint(self.state)
        self.assertTrue(text)
        self.assertNotIn("browse_heroes", text)
        self.assertIn("merch_gate", text)

    def test_outside_state_does_not_get_a_home_outing_hint(self):
        self.state.node_id = "hobby_corner"
        self.assertEqual(self.engine._outdoor_hint(self.state), "")
        self.assertEqual(self.state.outdoor_hint_at, 0)


if __name__ == "__main__":
    unittest.main()
