"""Satisfied quiet life still has occasional, safe opportunities to explore."""
import random
import unittest
from unittest.mock import patch

from core.decider import Decider, ENVIRONMENT_OUTDOOR_GAP_SECONDS
from core.models import ActionDef
from core.planner import create_plan
from core.state import WorldState
from core.state_dynamics import StateDynamics
from core.engine import TickOutcome
from tests import test_engine as engine_fixture
from tests.test_outdoor_hint_review_s import HintHarness


class FixedDraw:
    def __init__(self, point=0):
        self.point = point

    def random(self):
        return self.point

    def choice(self, values):
        return values[0]


class EnvironmentOpportunityTests(unittest.TestCase):
    def setUp(self):
        self.engine = HintHarness()
        self.hour = 15
        self.decider = Decider(self.engine.world, rng=FixedDraw(), now_provider=self.engine._now,
                               hour_provider=lambda: self.hour)
        self.state = WorldState("world-test", node_id="alice_bedroom", state="idle", energy=.9,
                                curiosity=.8, boredom=.01, node_since=self.engine.now - 2 * 3600,
                                last_outdoor_at=self.engine.now - 2 * 86400)

    def plan(self):
        return self.decider.rule_plan(self.state, group_chatting=False, interject_allowed=False)

    def test_low_boredom_can_choose_a_complete_real_outing(self):
        plan = self.plan()
        self.assertIsNotNone(plan)
        self.assertEqual([step["action"] for step in plan["steps"]], ["walk_to", "browse_heroes"])
        self.assertEqual(plan["steps"][0]["target_node"], "hobby_corner")
        self.assertEqual(plan["source"], "rule")

    def test_staying_home_remains_a_valid_choice_without_creating_a_plan(self):
        self.decider.rng.point = .99
        self.assertTrue(self.decider._change_environment_due(self.state))
        self.assertIsNone(self.plan())

    def test_new_opportunity_respects_energy_curiosity_and_independent_life(self):
        for field, value in (("energy", .59), ("curiosity", .54)):
            with self.subTest(field=field):
                saved = getattr(self.state, field)
                setattr(self.state, field, value)
                self.assertIsNone(self.plan())
                setattr(self.state, field, saved)
        self.engine.world.state_dynamics.independent_life_enabled = False
        self.assertIsNone(self.plan())

    def test_night_recent_outing_and_recent_arrival_keep_existing_quiet_life(self):
        self.hour = 23
        self.assertIsNone(self.plan())
        self.hour = 15
        self.state.last_outdoor_at = self.engine.now - ENVIRONMENT_OUTDOOR_GAP_SECONDS + 1
        self.assertIsNone(self.plan())
        self.state.last_outdoor_at -= 1
        self.state.node_since = self.engine.now - 90 * 60 + 1
        self.assertIsNone(self.plan())
        self.state.node_since -= 1
        self.assertIsNotNone(self.plan())

    def test_first_outing_uses_real_dwell_age_instead_of_invented_history(self):
        self.state.last_outdoor_at = 0
        self.state.node_since = self.engine.now - ENVIRONMENT_OUTDOOR_GAP_SECONDS + 1
        self.assertIsNone(self.plan())
        self.state.node_since -= 1
        self.assertIsNotNone(self.plan())
        self.state.node_since = 0
        self.assertIsNone(self.plan())

    def test_work_sleep_and_existing_plan_are_not_replaced(self):
        self.state.current_action = {"type": "browse_heroes"}
        self.assertIsNone(self.plan())
        self.state.current_action = None
        for status in ("sleeping", "napping", "drowsy", "walking"):
            self.state.state = status
            self.assertIsNone(self.plan())
        self.state.state = "idle"
        self.state.sleep_pressure = 1
        self.assertIsNone(self.plan())
        self.state.sleep_pressure = 0
        self.state.current_plan = create_plan(steps=[{"action": "walk_to", "target_node": "hobby_corner"}],
                                              world_time=0, now=self.engine.now, valid_for=3600)
        self.assertIsNone(self.plan())

    def test_unreachable_or_disabled_travel_does_not_make_a_partial_plan(self):
        self.engine.world.zone_edges = []
        self.assertIsNone(self.plan())
        self.engine.world.zone_edges = HintHarness().world.zone_edges
        self.engine.world.action_map()["walk_to"].enabled = False
        self.assertIsNone(self.plan())

    def test_each_activity_is_checked_before_its_destination_is_chosen(self):
        first = self.engine.world.action_map()["browse_heroes"]
        first.preconditions.min_energy = 1
        self.engine.world.actions.append(ActionDef(id="browse_safe", name="慢慢看看周边", category="continuous",
                                                  scope="node", allowed_nodes=["hobby_corner"], life_role="interest"))
        self.assertEqual(self.plan()["steps"][-1]["action"], "browse_safe")
        self.engine.world.action_map()["browse_safe"].preconditions.time_window = "night"
        self.assertIsNone(self.plan())

    def test_new_outing_excludes_tools_commands_social_targets_and_rest(self):
        safe = self.engine.world.action_map()["browse_heroes"]
        for modification in ({"llm_level": "tool"}, {"target_type": "user"},
                             {"tool_names": ["search"]}, {"trigger_command": "/test"},
                             {"on_complete": {"trigger": "schedule"}}, {"life_role": "rest"}):
            with self.subTest(modification=modification):
                raw = safe.model_dump(mode="json")
                raw.update(modification)
                self.engine.world.actions = [action for action in self.engine.world.actions if action.id != safe.id]
                self.engine.world.actions.append(ActionDef.model_validate(raw))
                self.assertIsNone(self.plan())
        self.engine.world.actions[-1] = safe
        self.assertIsNotNone(self.plan())

    def test_existing_single_activity_and_its_followup_remain_available(self):
        activity = self.engine.world.action_map()["browse_heroes"]
        activity.llm_level = "single"
        activity.on_complete.trigger = "llm_followup"
        self.assertEqual(self.plan()["steps"][-1]["action"], "browse_heroes")
        self.assertEqual(activity.llm_level, "single")
        self.assertEqual(activity.on_complete.trigger, "llm_followup")

    def test_learned_indoor_habit_does_not_permanently_win_the_first_branch(self):
        self.engine.world.actions.append(ActionDef(id="indoor_read", name="读熟悉的书", category="continuous",
                                                  scope="node", allowed_nodes=["alice_bedroom"], life_role="interest"))
        self.state.boredom = .6
        self.decider.rng.point = .5
        with patch("core.life_matters.routine_hints", return_value=[{"action": "indoor_read", "period": "白天",
                    "weight": 1, "explanation": "几个不同日期曾在白天读书"}]), \
                patch("core.life_matters.practice_records", return_value={}):
            self.assertEqual(self.plan()["steps"][-1]["action"], "browse_heroes")
            self.decider.rng.point = .1
            self.assertEqual(self.plan()["steps"][-1]["action"], "indoor_read")

    def test_day_mood_still_shapes_the_strength_of_the_opportunity(self):
        self.state.day_mood = "sluggish"
        slow = self.decider.branch_weights(self.state)["outdoor"]
        self.state.day_mood = "lively"
        lively = self.decider.branch_weights(self.state)["outdoor"]
        self.assertLess(slow, lively)
        self.assertGreater(slow, 1)

    def test_72_hours_of_satisfying_idle_life_has_spaced_outings(self):
        world = self.engine.world
        world.state_dynamics.energy_decay_per_min = 0  # Isolate travel motivation from rest.
        world.sleep.pressure_enabled = False
        world.actions.append(ActionDef(id="quiet_interest", name="摆弄喜欢的小东西", idle=True,
                                       category="continuous", life_role="interest"))
        world.action_map()["browse_heroes"].duration = 1200
        self.decider.rng = random.Random(31)
        self.decider._hour_provider = lambda: self.engine.local_now().hour
        dynamics = StateDynamics(world.state_dynamics, now_provider=self.engine._now,
                                 hour_provider=self.decider.local_hour)
        at_home = self.state.node_id
        outings, visiting_until = [], 0
        for _ in range(72 * 12):
            self.engine.now += 300
            self.state.world_time += 5
            if visiting_until and self.engine.now >= visiting_until:
                self.state.node_id = at_home
                self.state.node_since = self.engine.now
                self.state.current_action = None
                self.state.state = "idle"
                visiting_until = 0
            self.state.idle_activity = {"id": "quiet_interest", "node_id": self.state.node_id,
                                        "start_tick": self.state.world_time, "until_tick": self.state.world_time + 10}
            dynamics.tick(self.state, node=world.node_map()[self.state.node_id], elapsed_seconds=300,
                          world=world, now=self.engine.now)
            if visiting_until:
                self.state.last_outdoor_at = self.engine.now
                continue
            plan = self.plan()
            if plan:
                self.assertEqual(plan["steps"][-1]["action"], "browse_heroes")
                outings.append(self.engine.now)
                self.state.node_id = plan["steps"][0]["target_node"]
                self.state.node_since = self.state.last_outdoor_at = self.engine.now
                self.state.current_action = {"type": "browse_heroes"}
                visiting_until = self.engine.now + 1200
        self.assertGreaterEqual(len(outings), 2)
        self.assertLessEqual(len(outings), 5)
        self.assertTrue(all(later - earlier >= ENVIRONMENT_OUTDOOR_GAP_SECONDS
                            for earlier, later in zip(outings, outings[1:])))
        self.assertLess(self.state.boredom, .45)


class EnvironmentRuntimeTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = engine_fixture.EngineTestCase.asyncSetUp

    async def complete_outing(self, *, single=False):
        sample = HintHarness()
        sample.world.action_map()["browse_heroes"].duration = 1200
        if single:
            activity = sample.world.action_map()["browse_heroes"]
            activity.llm_level = "single"
            activity.on_complete.trigger = "llm_followup"
        raw = self.store.raw_world()
        raw.update(sample.world.model_dump(mode="json"))
        raw["proactive_session_whitelist"] = [engine_fixture.SESSION]
        raw["life_share_session_whitelist"] = [engine_fixture.SESSION]
        self.store.save_world(raw)
        self.engine.reload_config()
        self.clock._now = sample.now
        self.engine.decider.rng = FixedDraw()
        async with self.engine.session_state(engine_fixture.SESSION) as state:
            state.node_id, state.state = "alice_bedroom", "idle"
            state.energy, state.curiosity, state.boredom = .9, .8, .01
            state.node_since = self.clock.now() - 2 * 3600
            state.last_outdoor_at = self.clock.now() - 2 * 86400
            state.sleep_pressure = 0
            state.current_action, state.current_plan = None, None
            plan = self.engine.decider.rule_plan(state, group_chatting=False, interject_allowed=False)
            self.assertIsNotNone(plan)
            outcome = TickOutcome(engine_fixture.SESSION)
            await self.engine._apply_plan(state, self.engine.node(state.node_id), outcome, plan)
            await self.engine._tick_plan(state, self.engine.node(state.node_id), outcome, depth=0)
            self.assertEqual(state.node_id, "alice_bedroom")
            self.assertEqual(state.current_action["type"], "walk_to")
            for _ in range(30):
                self.clock.advance(60)
                state.world_time += 1
                await self.engine._tick_continuous(state, self.engine.node(state.node_id), outcome)
                await self.engine._tick_plan(state, self.engine.node(state.node_id), outcome, depth=0)
                if state.current_action is None and state.current_plan is None:
                    break
            self.assertEqual(state.node_id, "hobby_corner")
            self.assertIsNone(state.current_action)
            self.assertIsNone(state.current_plan)
            self.assertFalse(state.pending_arrival)
            self.assertGreaterEqual(state.last_outdoor_at, sample.now + 3 * 60)
        return len(self.llm.calls)

    async def test_complete_template_outing_uses_actual_walk_without_a_model_call(self):
        self.assertEqual(await self.complete_outing(), 0)

    async def test_configured_single_followup_runs_once_without_extra_arrival_generation(self):
        self.assertEqual(await self.complete_outing(single=True), 1)


if __name__ == "__main__":
    unittest.main()
