"""Recovery actions execute physiology, duration and interruption semantics."""
import unittest
import json

from core.engine import TickOutcome
from core.json_actions import PlannedAction
from core.models import ActionDef
from core.recovery import recovery_seconds, sleep_action_kind
from core.generator import parse_generated_actions
from tests import test_engine as fixture


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    set_state = fixture.EngineTestCase.set_state
    get_state = fixture.EngineTestCase.get_state

    async def asyncSetUp(self):
        await fixture.EngineTestCase.asyncSetUp(self)
        self.engine.world.sleep.mode = "activity"
        self.engine.world.sleep.recovery_action_id = "charge"
        self.engine.world.sleep.goodnight = False
        self.engine.world.sleep.goodmorning = False
        self.engine.world.sleep.drowsy = False
        self.engine.world.state_dynamics.energy_decay_per_min = 0
        self.engine.world.state_dynamics.independent_life_enabled = False
        self.action = ActionDef(id="charge", name="充电", category="continuous", recovery_kind="restore",
            scope="node", allowed_nodes=["study"], duration_mode="llm", duration_min=60, duration_max=3600,
            on_complete={"effects_per_minute": {"energy": "+0.01"}})
        self.engine.world.actions.append(self.action)
        await self.set_state(node_id="study", state="idle", energy=.2, world_time=10, cold_start_done=True)

    def outcome(self):
        return TickOutcome(session_id=fixture.SESSION)

    async def start(self, state, definition=None, seconds=600):
        definition = definition or self.action
        await self.engine._start_action(state, self.engine.node(state.node_id), self.outcome(), definition,
            PlannedAction(type=definition.id, duration=seconds), 0, False)

    async def test_low_energy_selects_recovery_with_real_bounds(self):
        state = await self.get_state()
        plan = self.engine.decider.recovery_plan(state)
        self.assertEqual(plan["steps"][-1]["action"], "charge")
        self.assertEqual(plan["steps"][-1]["duration"], 3600)
        self.action.on_complete.effects_per_minute["energy"] = "+0.1"
        self.assertEqual(recovery_seconds(self.action, .2, self.engine.world), 390)

    async def test_recovery_duration_accounts_for_completion_penalty(self):
        self.action.on_complete.effects_per_minute["energy"] = "+0.1"
        self.action.on_complete.effects["energy"] = "-0.1"
        self.assertEqual(recovery_seconds(self.action, .2, self.engine.world), 450)

    async def test_recovery_duration_accounts_for_completion_multiplier(self):
        self.action.on_complete.effects_per_minute["energy"] = "+0.1"
        self.action.on_complete.effects["energy"] = "*2"
        self.assertEqual(recovery_seconds(self.action, .2, self.engine.world), 135)

    async def test_automatic_recovery_reaches_target_after_completion_penalty(self):
        self.action.on_complete.effects_per_minute["energy"] = "+0.1"
        self.action.on_complete.effects["energy"] = "-0.1"
        async with self.engine.session_state(fixture.SESSION) as state:
            await self.engine.tick_activity_recovery(state, self.outcome())
            for _ in range(7):
                await self.engine._tick_continuous(state, self.engine.node("study"), self.outcome())
            self.assertIsNotNone(state.current_action)
            await self.engine._tick_continuous(state, self.engine.node("study"), self.outcome())
            self.assertIsNone(state.current_action)
            self.assertGreaterEqual(state.energy, self.engine.world.sleep.recovery_target)

    async def test_automatic_recovery_stops_when_completion_multiplier_reaches_target(self):
        self.action.on_complete.effects_per_minute["energy"] = "+0.1"
        self.action.on_complete.effects["energy"] = "*2"
        async with self.engine.session_state(fixture.SESSION) as state:
            await self.engine.tick_activity_recovery(state, self.outcome())
            state.current_action["duration_ticks"] = 60
            for _ in range(3):
                await self.engine._tick_continuous(state, self.engine.node("study"), self.outcome())
            self.assertIsNone(state.current_action)
            self.assertGreaterEqual(state.energy, self.engine.world.sleep.recovery_target)

    async def test_quota_and_preconditions_prevent_unrunnable_recovery(self):
        state = await self.get_state()
        self.action.preconditions.min_energy = .5
        self.assertIsNone(self.engine.decider.recovery_plan(state))
        self.action.preconditions.min_energy = None
        self.action.quota.day = 1
        self.engine._count_action_use(state, self.action)
        self.assertIsNone(self.engine.decider.recovery_plan(state))

    async def test_no_recovery_plan_for_external_tool_or_unreachable_location(self):
        state = await self.get_state()
        self.action.allowed_nodes = ["missing"]
        self.assertIsNone(self.engine.decider.recovery_plan(state))
        self.action.allowed_nodes = ["study"]
        self.action.tool_names = ["web_search"]
        self.assertIsNone(self.engine.decider.recovery_plan(state))

    async def test_existing_set_value_below_current_energy_is_not_recovery(self):
        self.action.on_complete.effects_per_minute = {}
        self.action.on_complete.effects = {"energy": "=0.1"}
        self.assertIsNone(self.engine.decider.recovery_plan(await self.get_state()))

    async def test_running_recovery_is_incremental_and_completion_does_not_double_count(self):
        async with self.engine.session_state(fixture.SESSION) as state:
            await self.start(state, seconds=120)
            await self.engine._tick_continuous(state, self.engine.node("study"), self.outcome())
            self.assertAlmostEqual(state.energy, .21)
            await self.engine._tick_continuous(state, self.engine.node("study"), self.outcome())
            self.assertAlmostEqual(state.energy, .22)
            self.assertIsNone(state.current_action)

    async def test_interrupt_settles_elapsed_only_without_completion_bonus(self):
        self.action.on_complete.effects = {"energy": "+0.5"}
        async with self.engine.session_state(fixture.SESSION) as state:
            await self.start(state)
            state.current_action["elapsed_ticks"] = 3
            self.engine.suspend_life(state, reason="临时有事", source="event")
            self.assertAlmostEqual(state.energy, .23)
            self.assertFalse(state.life_resume)

    async def test_location_change_stops_without_bonus(self):
        self.action.on_complete.effects = {"energy": "+0.5"}
        async with self.engine.session_state(fixture.SESSION) as state:
            await self.start(state)
            state.current_action["elapsed_ticks"] = 2
            self.action.allowed_nodes = ["bedroom"]
            await self.engine._tick_continuous(state, self.engine.node("study"), self.outcome())
            self.assertAlmostEqual(state.energy, .22)
            self.assertIsNone(state.current_action)

    async def test_offline_passive_charging_restores_only_elapsed_duration(self):
        async with self.engine.session_state(fixture.SESSION) as state:
            await self.start(state)
            state.life_updated_at = self.clock.now()
            self.clock.advance(300)
            self.engine.reconcile_life_time(state)
            self.assertAlmostEqual(state.energy, .25)
            await self.engine._tick_continuous(state, self.engine.node("study"), self.outcome())
            self.assertAlmostEqual(state.energy, .25)
            self.clock.advance(600)
            self.engine.reconcile_life_time(state)
            await self.engine._tick_continuous(state, self.engine.node("study"), self.outcome())
            self.assertAlmostEqual(state.energy, .30)
            self.assertFalse(state.sleep_history)

    async def test_activity_mode_cancels_sleep_without_full_reward_or_sleep_pressure(self):
        await self.set_state(state="sleeping", sleep_pressure=.9, current_action={"type":"sleep", "elapsed_ticks":0})
        state = await self.get_state()
        self.engine._sync_sleep_rhythm_mode(state)
        self.assertEqual(state.state, "idle")
        self.assertIsNone(state.current_action)
        self.assertAlmostEqual(state.energy, .2)
        self.assertEqual(state.sleep_pressure, 0)
        self.assertFalse(self.engine.world.action_map()["sleep"].enabled)
        self.assertFalse(self.engine.decider.sleep_pressure_high(state))

    async def test_generated_sleep_respects_its_duration_and_records_real_sleep(self):
        self.engine.world.sleep.mode = "autonomous"
        self.action.recovery_kind = "sleep"
        # Validation also assigns the correct sleeping state for generated actions.
        self.action = ActionDef.model_validate(self.action.model_dump())
        self.engine.world.actions[-1] = self.action
        async with self.engine.session_state(fixture.SESSION) as state:
            await self.start(state, seconds=120)
            self.assertEqual(state.state, "sleeping")
            self.assertEqual(state.current_action["duration_ticks"], 2)
            self.engine.dynamics.tick(state, node=None, elapsed_seconds=60, world=self.engine.world)
            await self.engine._tick_continuous(state, self.engine.node("study"), self.outcome())
            self.assertAlmostEqual(state.energy, .21)  # No extra built-in sleep rate.
            await self.engine._tick_continuous(state, self.engine.node("study"), self.outcome())
            self.assertEqual(state.sleep_history[-1]["kind"], "sleep")

    async def test_generated_nap_is_selected_when_builtin_disabled(self):
        self.engine.world.sleep.mode = "fixed"
        self.engine.world.action_map()["nap"].enabled = False
        self.action.recovery_kind = "nap"
        state = await self.get_state()
        self.assertTrue(self.engine.decider.can_nap())
        self.assertEqual(self.engine.decider._rest_steps(state, "nap")[-1]["action"], "charge")

    async def test_sleep_name_does_not_override_explicit_non_sleep_type(self):
        self.action.name = "睡觉"
        self.assertEqual(sleep_action_kind(self.action), "")

    async def test_recovery_intention_survives_short_round_until_target(self):
        self.action.duration_mode = "fixed"
        self.action.duration = 60
        async with self.engine.session_state(fixture.SESSION) as state:
            await self.engine.tick_activity_recovery(state, self.outcome())
            self.assertEqual(state.current_action["type"], "charge")
            state.energy = .5
            await self.engine._tick_continuous(state, self.engine.node("study"), self.outcome())
            await self.engine.tick_activity_recovery(state, self.outcome())
            self.assertEqual(state.current_action["type"], "charge")
            state.energy = .9
            await self.engine.tick_activity_recovery(state, self.outcome())
            self.assertFalse(state.sleep_rhythm.get("recovering"))

    async def test_recovery_multiplier_applies_once_on_completion(self):
        self.action.on_complete.effects_per_minute = {"energy": "*2", "boredom": "-0.01"}
        async with self.engine.session_state(fixture.SESSION) as state:
            await self.start(state, seconds=120)
            await self.engine._tick_continuous(state, self.engine.node("study"), self.outcome())
            self.assertAlmostEqual(state.energy, .2)
            await self.engine._tick_continuous(state, self.engine.node("study"), self.outcome())
            self.assertAlmostEqual(state.energy, .4)

    async def test_no_net_gain_or_explicit_opt_out_is_not_automatic_recovery(self):
        state = await self.get_state()
        self.action.recovery_kind = "none"
        self.assertIsNone(self.engine.decider.recovery_plan(state))
        self.action.recovery_kind = "restore"
        self.engine.world.state_dynamics.energy_decay_per_min = .02
        self.assertIsNone(self.engine.decider.recovery_plan(state))

    async def test_automatic_recovery_finishes_at_target_after_minimum_duration(self):
        self.action.on_complete.effects_per_minute["energy"] = "+0.4"
        async with self.engine.session_state(fixture.SESSION) as state:
            await self.engine.tick_activity_recovery(state, self.outcome())
            state.current_action["duration_ticks"] = 60
            await self.engine._tick_continuous(state, self.engine.node("study"), self.outcome())
            self.assertIsNotNone(state.current_action)
            await self.engine._tick_continuous(state, self.engine.node("study"), self.outcome())
            self.assertIsNone(state.current_action)
            self.assertGreaterEqual(state.energy, .85)

    async def test_critical_energy_interrupts_ordinary_activity_but_not_normal_low_energy(self):
        from unittest.mock import AsyncMock
        activity = ActionDef(id="reading", category="continuous", duration=3600, duration_mode="fixed")
        self.engine.world.actions.append(activity)
        self.engine._defer_rhythm_for_event = AsyncMock(return_value=False)
        async with self.engine.session_state(fixture.SESSION) as state:
            await self.start(state, activity)
            await self.engine.tick_activity_recovery(state, self.outcome())
            self.assertEqual(state.current_action["type"], "reading")
            state.energy = .05
            await self.engine.tick_activity_recovery(state, self.outcome())
            self.assertEqual(state.current_action["type"], "charge")

    async def test_critical_event_keeps_priority_over_charging(self):
        from unittest.mock import Mock
        self.engine._active_thread = Mock(return_value={"id": "urgent"})
        self.engine._thread_critical = Mock(return_value=True)
        state = await self.get_state()
        state.energy = .03
        await self.engine.tick_activity_recovery(state, self.outcome())
        self.assertIsNone(state.current_action)
        self.assertIsNone(state.current_plan)

    async def test_low_energy_planning_cannot_replace_charging_with_social_impulses(self):
        from unittest.mock import AsyncMock
        self.engine._maybe_miss_push = AsyncMock(side_effect=AssertionError('must recover first'))
        self.engine._maybe_desire_push = AsyncMock(side_effect=AssertionError('must recover first'))
        before = len(self.llm.calls)
        await self.engine.maybe_decide(fixture.SESSION, force=True)
        state = await self.get_state()
        self.assertEqual(state.current_action['type'], 'charge')
        self.assertEqual(len(self.llm.calls), before)

    async def test_negative_nap_rate_does_not_become_positive_and_quota_has_fallback(self):
        self.engine.world.sleep.mode = "fixed"
        nap = self.engine.world.action_map()["nap"]
        nap.enabled = True
        nap.quota.day = 1
        state = await self.get_state()
        self.engine._count_action_use(state, nap)
        self.action.recovery_kind = "nap"
        self.action.on_complete.effects_per_minute["energy"] = "-0.01"
        self.assertFalse(self.engine.decider.can_nap(state))
        self.action.on_complete.effects_per_minute["energy"] = "+0.01"
        self.assertEqual(self.engine.decider._rest_steps(state, "nap")[-1]["action"], "charge")

    async def test_rebuild_reuses_names_but_never_reuses_old_action_identity(self):
        from unittest.mock import AsyncMock
        self.engine._ask_creator = AsyncMock(return_value=json.dumps({"actions": [{
            "id": "charge", "name": "新充电", "allowed_nodes": ["study"], "recovery_kind": "restore",
            "duration_min": 120, "duration_max": 600, "on_complete": {"effects_per_minute": {"energy": "+0.02"}}}]}))
        result = await self.engine.generate_actions_for_zone("home", 1, node_ids=["study"], replacing=True)
        self.assertEqual(len(result["actions"]), 1, result["problems"])
        self.assertNotEqual(result["actions"][0]["id"], "charge")
        self.assertEqual(self.engine.world.actions[-1].name, "充电")

    def test_generated_recovery_defaults_are_continuous_and_passive(self):
        for kind in ("sleep", "nap", "restore"):
            actions, problems = parse_generated_actions(json.dumps({"actions": [{"id": "rest", "name": "恢复",
                "allowed_nodes": ["study"], "recovery_kind": kind, "duration_min": 120, "duration_max": 600}]}), node_ids=["study"], tool_names=set())
            self.assertTrue(actions, problems)
            self.assertEqual(actions[0]["category"], "continuous")
            self.assertEqual(actions[0]["llm_level"], "template")
            self.assertEqual(actions[0]["duration_min"], 120)
            self.assertEqual(actions[0]["on_complete"]["trigger"], "none")

    async def test_sleep_free_daily_review_runs_once_and_retries_without_requiring_sleep(self):
        state = await self.get_state()
        self.engine.world.memory.sleep_review = True
        self.assertIsNone(self.engine.sleep_review_mode(state))
        self.clock.advance(self.engine.world.profile.sleep_consolidate_minutes * 60 + 1)
        self.assertEqual(self.engine.sleep_review_mode(state), 'full')
        self.engine._record_sleep_review_attempt(state, {'ok': False})
        self.assertIsNone(self.engine.sleep_review_mode(state))
        self.clock.advance(301)
        self.assertEqual(self.engine.sleep_review_mode(state), 'full')
        self.engine._record_sleep_review_attempt(state, {'ok': True})
        self.assertIsNone(self.engine.sleep_review_mode(state))
        self.clock.advance(86400)
        self.assertIsNone(self.engine.sleep_review_mode(state))
        self.clock.advance(self.engine.world.profile.sleep_consolidate_minutes * 60 + 1)
        self.assertEqual(self.engine.sleep_review_mode(state), 'full')
