"""Need edits, quiet activities and life experience regressions."""

import asyncio
import copy
import json
import tempfile
import unittest
from unittest.mock import AsyncMock

from core.config_store import ConfigStore
from core.db import AsyncDatabase
from core.defaults import default_world
from core.engine import MyWorldEngine, TickOutcome
from core.engagement import EngagementTracker
from core.events import EventPackage
from core.json_actions import PlannedAction
from core.life import completion_relief, event_relief
from core.models import ActionDef, WorldConfig
from core.state import WorldState
from core.state_dynamics import StateDynamics
from tests.stub_ports import StubClock, StubLLM, StubMessenger, StubPersona

SESSION = "test:GroupMessage:100"


def quiet_action(identifier="idle_hum", **extra):
    return ActionDef(
        **{
            "id": identifier,
            "name": "轻轻哼歌",
            "description": "哼一段熟悉的旋律。",
            "idle": True,
            "category": "continuous",
            "llm_level": "template",
            "life_role": "interest",
            **extra,
        }
    )


class IndependentDynamicsTests(unittest.TestCase):
    def test_a_quiet_day_is_normal_and_interesting_life_helps(self):
        world = WorldConfig()
        dynamics = StateDynamics(world.state_dynamics, hour_provider=lambda: 12)
        alone = WorldState(SESSION, loneliness=0.3, boredom=0.3, node_since=1000)
        occupied = copy.deepcopy(alone)
        world.actions = [quiet_action()]
        for minute in range(16 * 60):
            occupied.idle_activity = {"id": "idle_hum", "node_id": "", "until_tick": 9999}
            dynamics.tick(alone, node=None, elapsed_seconds=60, world=world, now=1000 + minute * 60)
            dynamics.tick(
                occupied, node=None, elapsed_seconds=60, world=world, now=1000 + minute * 60
            )
        self.assertLess(alone.loneliness, 0.36)
        self.assertLess(alone.boredom, 0.54)
        self.assertLess(occupied.loneliness, alone.loneliness)
        self.assertLess(occupied.boredom, 0.3)

    def test_fulfilling_activity_recovers_saturated_needs_and_speech_earns_nothing(self):
        world = WorldConfig(actions=[quiet_action("read", idle=False)])
        state = WorldState(SESSION, loneliness=1, boredom=1, current_action={"type": "read"})
        dynamics = StateDynamics(world.state_dynamics, hour_provider=lambda: 12)
        dynamics.tick(state, node=None, elapsed_seconds=3600, world=world, now=1000)
        self.assertLess(state.loneliness, 1)
        self.assertLess(state.boredom, 0.98)
        self.assertEqual(completion_relief(ActionDef(id="say", target_type="group")), {})
        self.assertEqual(completion_relief(ActionDef(id="think")), {})

    def test_explicit_effects_win_and_failure_is_not_company(self):
        action = quiet_action(on_complete={"effects": {"boredom": "+0.1"}})
        self.assertNotIn("boredom", completion_relief(action, elapsed_minutes=30))
        self.assertLess(event_relief("fail", {})["boredom"], 0)
        self.assertNotIn("loneliness", event_relief("fail", {}))
        self.assertEqual(event_relief("good", {"loneliness": 0.1, "boredom": 0.1}), {})

    def test_disabling_independent_life_keeps_old_sleep_behavior(self):
        world = WorldConfig(state_dynamics={"independent_life_enabled": False})
        state = WorldState(SESSION, state="sleeping", loneliness=0.5, boredom=0.5)
        StateDynamics(world.state_dynamics).tick(
            state, node=None, elapsed_seconds=3600, world=world
        )
        self.assertEqual(state.loneliness, 0.5)
        self.assertEqual(state.boredom, 0.5)

    def test_migration_is_once_only_and_preserves_custom_rates(self):
        with tempfile.TemporaryDirectory() as folder:
            store = ConfigStore(folder)
            store.ensure_files()
            raw = default_world()
            raw["state_dynamics"].pop("cadence_version")
            raw["state_dynamics"].update(
                loneliness_growth_per_min=0.0008, boredom_growth_per_min=0.0003
            )
            store.save_world(raw)
            world = store.load_world()
            self.assertEqual(world.state_dynamics.loneliness_growth_per_min, 0.00005)
            self.assertEqual(world.state_dynamics.boredom_growth_per_min, 0.0003)
            raw = store.raw_world()
            raw["state_dynamics"]["loneliness_growth_per_min"] = 0.0008
            store.save_world(raw)
            self.assertEqual(store.load_world().state_dynamics.loneliness_growth_per_min, 0.0008)
            self.assertTrue(list(store.history_dir.glob("*")))

    def test_one_unanswered_message_is_counted_only_once(self):
        world = WorldConfig()
        tracker = EngagementTracker(world)
        state = WorldState(SESSION)
        tracker.on_bot_spoke(state)
        for hour in range(1, 25):
            state.world_time = hour * 60
            tracker.evaluate(state, tick_seconds=60)
        self.assertEqual(state.unanswered_count, 1)
        self.assertFalse(state.awaiting_reply)
        tracker.on_bot_spoke(state)
        state.world_time += 60
        tracker.evaluate(state, tick_seconds=60)
        self.assertEqual(state.unanswered_count, 2)


class IndependentEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = ConfigStore(tmp.name)
        self.store.ensure_files()
        self.store.add_session(SESSION)
        self.db = AsyncDatabase(self.store.db_path)
        self.addAsyncCleanup(self.db.close)
        self.clock = StubClock()
        self.messenger = StubMessenger()
        self.engine = MyWorldEngine(
            store=self.store,
            db=self.db,
            messenger=self.messenger,
            clock=self.clock,
            llm=StubLLM(),
            persona=StubPersona("喜欢哼歌的安静少女"),
            tick_seconds=60,
        )

    def add_idle(self, **extra):
        raw = self.store.raw_world()
        raw["actions"].append(quiet_action(**extra).model_dump(mode="json"))
        self.store.save_world(raw)
        self.engine.reload_config()

    async def test_edit_waits_for_tick_lock_and_survives_reload(self):
        async with self.engine.session_state(SESSION) as state:
            state.affect_synced_at = self.clock.now() - 3600
            edit = asyncio.create_task(
                self.engine.set_values(SESSION, {"affect": 0.8, "loneliness": 0.2})
            )
            await asyncio.sleep(0)
            self.assertFalse(edit.done())
            state.loneliness = 0.9
        applied = await edit
        state = await self.engine.load_state(SESSION)
        self.assertEqual(state.affect, 0.8)
        self.assertEqual(state.loneliness, 0.2)
        self.assertEqual(applied["affect"], 0.8)
        self.assertEqual(await self.engine.set_values(SESSION, {"energy": float("nan")}), {})

    async def test_combined_edit_applies_valence_after_needs_in_any_order(self):
        values = {"valence": 0.68, "energy": 0.9, "loneliness": 0.15, "boredom": 0.1}
        await self.engine.set_values(SESSION, values)
        self.assertAlmostEqual((await self.engine.load_state(SESSION)).valence, 0.68)
        await self.engine.set_values(SESSION, dict(reversed(list(values.items()))))
        self.assertAlmostEqual((await self.engine.load_state(SESSION)).valence, 0.68)

    async def test_manual_valence_can_reach_both_slider_endpoints(self):
        for value in (0.0, 1.0):
            result = await self.engine.set_values(SESSION, {"valence": value})
            state = await self.engine.load_state(SESSION)
            self.engine.dynamics.refresh(state, now=self.clock.now())
            self.assertAlmostEqual(state.valence, value)
            self.assertAlmostEqual(result["valence"], value)

    async def test_idle_is_stable_silent_and_formal_work_interrupts_without_reward(self):
        self.add_idle(on_complete={"effects": {"energy": "+0.1"}})
        async with self.engine.session_state(SESSION) as state:
            state.state = "idle"
            state.energy = 0.5
            self.engine._tick_idle_life(state)
            first = dict(state.idle_activity)
            state.world_time += 1
            self.engine._tick_idle_life(state)
            self.assertEqual(state.idle_activity, first)
            self.assertEqual(self.engine.activity_view(state)["label"], "轻轻哼歌")
            state.current_action = {"type": "walk_to", "target_node": "study", "desc": "走路"}
            self.engine._tick_idle_life(state)
            self.assertFalse(state.idle_activity)
            self.assertEqual(state.energy, 0.5)
            self.assertIn("书房", self.engine.activity_view(state)["label"])
        self.assertEqual(self.messenger.sent, [])

    async def test_natural_idle_completion_applies_once_and_disabling_takes_effect(self):
        self.add_idle(on_complete={"effects": {"energy": "+0.1"}})
        async with self.engine.session_state(SESSION) as state:
            state.state = "idle"
            state.energy = 0.5
            self.engine._tick_idle_life(state)
            state.world_time = state.idle_activity["until_tick"]
            self.engine._tick_idle_life(state)
            self.assertAlmostEqual(state.energy, 0.6)
            self.engine._tick_idle_life(state)
            self.assertAlmostEqual(state.energy, 0.6)
            self.engine.world.actions[-1].enabled = False
            self.engine._tick_idle_life(state)
            self.assertFalse(state.idle_activity)

    async def test_idle_drafts_use_persona_and_cannot_invoke_tools(self):
        self.engine._ask_creator = AsyncMock(
            return_value=json.dumps(
                {
                    "actions": [
                        {
                            "id": "idle_test",
                            "name": "哼歌",
                            "description": "哼一段旋律",
                            "llm_level": "single",
                            "on_complete": {"trigger": "llm_followup", "effects": {"energy": "=1"}},
                        }
                    ]
                }
            )
        )
        result = await self.engine.generate_idle_actions(2)
        args = self.engine._ask_creator.call_args.args
        self.assertIn("喜欢哼歌", args[2])
        draft = result["actions"][0]
        self.assertTrue(draft["idle"])
        self.assertEqual(draft["llm_level"], "template")
        self.assertEqual(draft["on_complete"]["trigger"], "none")
        self.assertEqual(draft["on_complete"]["effects"], {})
        self.assertEqual(draft["scope"], "global")
        self.assertFalse(any(action.id == draft["id"] for action in self.engine.world.actions))

    async def test_idle_cannot_be_executed_as_a_formal_action(self):
        self.add_idle()
        async with self.engine.session_state(SESSION) as state:
            outcome = TickOutcome(SESSION)
            state.state = "idle"
            await self.engine._start_action(
                state,
                self.engine.node(state.node_id),
                outcome,
                self.engine.world.action_map()["idle_hum"],
                PlannedAction(type="idle_hum"),
                0,
                True,
            )
            self.assertFalse(state.current_action)
            self.assertFalse(outcome.messages)
            self.assertNotIn("idle_hum", self.engine._parseable_action_ids(state.node_id))

    async def test_micro_experience_reduces_boredom_without_charging_twice(self):
        async with self.engine.session_state(SESSION) as state:
            state.boredom = 0.5
            state.loneliness = 0.5
            thread = {"id": "micro", "title": "发现一朵花", "steps": [], "tone": "good"}
            state.event_threads = [thread]
            package = EventPackage(title="发现一朵花", tier="micro", outcome="在路边发现一朵花。")
            await self.engine._settle_micro(state, TickOutcome(SESSION), thread, package)
            self.assertAlmostEqual(state.boredom, 0.485)
            self.assertLess(state.loneliness, 0.5)
            self.assertEqual(thread["status"], "closed")

    async def test_finished_schedule_activity_gets_life_relief(self):
        definition = ActionDef(
            id="read_for_schedule", name="看书", category="continuous", life_role="interest"
        )
        self.engine.world.actions.append(definition)
        self.engine._remember_action = lambda *args: None
        self.engine._satisfy_desire_from_action = AsyncMock()
        self.engine._maybe_soothe_from_action = AsyncMock()
        async with self.engine.session_state(SESSION) as state:
            state.boredom = 0.6
            state.loneliness = 0.5
            action = {
                "type": definition.id,
                "elapsed_ticks": 20,
                "duration_ticks": 20,
                "speech_kind": "schedule",
            }
            state.current_action = action
            await self.engine._finish_action(
                state, self.engine.node(state.node_id), TickOutcome(SESSION), action
            )
            self.assertAlmostEqual(state.boredom, 0.565)
            self.assertLess(state.loneliness, 0.5)

    async def test_overview_uses_each_sessions_own_action_library(self):
        self.add_idle()
        local = self.engine.world.model_dump(mode="json", by_alias=True)
        local["actions"][-1]["name"] = "独立人设的轻声哼歌"
        self.store.save_scope(SESSION, local, {"schedules": []}, ["actions"])
        self.engine.reload_config()
        async with self.engine.session_state(SESSION) as state:
            state.state = "idle"
            self.engine._tick_idle_life(state)
        rows = await self.engine.overview()
        self.assertEqual(rows[0]["activity"]["label"], "独立人设的轻声哼歌")
        snapshot = await self.engine.snapshot(SESSION)
        self.assertIn("独立人设的轻声哼歌", snapshot["activity"]["label"])

    async def test_idle_policy_belongs_to_action_override_and_migration_covers_local_settings(self):
        local = self.engine.world.model_dump(mode="json", by_alias=True)
        local["idle_life"]["enabled"] = False
        self.store.save_scope(SESSION, local, {"schedules": []}, ["actions"])
        self.engine.reload_config()
        with self.engine.config_scope(SESSION):
            self.assertFalse(self.engine.world.idle_life.enabled)
        self.assertTrue(self.engine.world.idle_life.enabled)
        raw = self.store.raw_sessions()
        raw["sessions"][0]["config_overrides"]["settings"] = {
            "state_dynamics": {"loneliness_growth_per_min": 0.0008, "boredom_growth_per_min": 0.0012}}
        self.store.save_sessions(raw)
        self.engine.reload_config()
        with self.engine.config_scope(SESSION):
            self.assertEqual(self.engine.world.state_dynamics.loneliness_growth_per_min, 0.00005)
            self.assertEqual(self.engine.world.state_dynamics.boredom_growth_per_min, 0.00012)
