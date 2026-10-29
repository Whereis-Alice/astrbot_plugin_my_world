"""Night life is timed world activity, independent of human sleep needs."""
import json
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from core.activity_time import matter_start_at, quiet_life_note, window_allows
from core.defaults import default_world
from core.context_selection import ContextRequest
from core.engine import TickOutcome
from core.json_actions import PlannedAction
from core.generator import parse_generated_actions
from core.models import ActionDef, EdgeDef, NodeDef, ZoneDef, parse_world
from core.prompt import PromptBuilder
from tests import test_engine as fixture


class NightLifeTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    get_state = fixture.EngineTestCase.get_state
    set_state = fixture.EngineTestCase.set_state

    async def asyncSetUp(self):
        await fixture.EngineTestCase.asyncSetUp(self)
        self.engine.world.sleep.mode = "activity"
        self.engine.world.night.quiet_life = True
        self.engine.world.sleep.recovery_action_id = ""
        self.engine.world.state_dynamics.energy_decay_per_min = .001
        self.clock.set_struct(datetime(2026, 10, 29, 2, 16))
        await self.set_state(state="idle", energy=.245, world_time=10, cold_start_done=True)

    def add_recovery(self, ident, *, node="study", kind="restore", seconds=300, gain="+0.04"):
        action = ActionDef(id=ident, category="continuous", scope="node", allowed_nodes=[node],
                           duration=seconds, recovery_kind=kind,
                           on_complete={"effects": {"energy": gain}})
        self.engine.world.actions.append(action)
        return action

    async def test_explicit_recovery_beats_nearby_short_relaxation(self):
        self.add_recovery("fountain", kind="auto")
        self.add_recovery("charge", node="bedroom", seconds=1800, gain="+0.65")
        plan = self.engine.decider.recovery_plan(await self.get_state())
        self.assertEqual(plan["steps"][-1]["action"], "charge")
        self.assertEqual(plan["steps"][0]["target_node"], "bedroom")

    async def test_recovery_compares_total_time_instead_of_one_round(self):
        self.add_recovery("small", seconds=60, gain="+0.01")
        self.add_recovery("large", seconds=600, gain="+0.65")
        self.assertEqual(self.engine.decider.recovery_plan(await self.get_state())["steps"][-1]["action"], "large")

    async def test_recovery_below_target_plateau_does_not_defeat_working_candidate(self):
        self.add_recovery("plateau", seconds=60, gain="=0.3")
        self.add_recovery("working", seconds=600, gain="+0.65")
        self.assertEqual(self.engine.decider.recovery_plan(await self.get_state())["steps"][-1]["action"], "working")

    async def test_explicit_choice_still_wins(self):
        self.add_recovery("selected", kind="auto")
        self.add_recovery("other", seconds=600, gain="+0.65")
        self.engine.world.sleep.recovery_action_id = "selected"
        self.assertEqual(self.engine.decider.recovery_plan(await self.get_state())["steps"][-1]["action"], "selected")

    async def test_auto_relaxation_remains_fallback_without_working_restore(self):
        self.add_recovery("unreachable", node="missing", seconds=600, gain="+0.65")
        self.add_recovery("relax", kind="auto")
        self.assertEqual(self.engine.decider.recovery_plan(await self.get_state())["steps"][-1]["action"], "relax")

    async def test_planning_sees_local_time_and_real_simulation_locations(self):
        await self.set_state(energy=.9)
        async with self.engine.session_state(fixture.SESSION) as state:
            self.engine.add_life_matter(state, "回卧室整理记录")
        self.llm.replies = [json.dumps({"steps": [{"text": "在卧室整理", "action": "life_simulation",
            "node": "bedroom", "minutes": 10, "day_offset": 0, "time_window": "daytime"}]})]
        await self.engine.advance_life_matters(fixture.SESSION)
        payload = json.loads(self.llm.calls[-1]["prompt"])
        self.assertEqual(payload["local_time"]["hour"], 2)
        self.assertTrue(payload["local_time"]["night"])
        self.assertIn(("life_simulation", "bedroom"), {(r["action"], r["node"]) for r in payload["available"]})
        state = await self.get_state()
        self.assertIsNone(state.current_action)
        self.assertEqual(state.life_matters[-1]["status"], "waiting")
        self.assertEqual(state.life_matters[-1]["steps"][0]["node"], "bedroom")

    async def test_legacy_unstarted_step_waits_until_daylight_without_replanning(self):
        await self.set_state(energy=.9)
        async with self.engine.session_state(fixture.SESSION) as state:
            item = self.engine.add_life_matter(state, "整理表格")
            item["steps"] = [{"id": "step", "text": "整理", "action": "life_simulation", "node": "study",
                              "minutes": 10, "not_before": self.clock.now(), "status": "pending"}]
        await self.engine.advance_life_matters(fixture.SESSION)
        state = await self.get_state()
        self.assertIsNone(state.current_action)
        self.assertEqual(state.life_matters[-1]["status"], "waiting")
        self.assertAlmostEqual(state.life_matters[-1]["resume_at"] - self.clock.now(), 4 * 3600 + 44 * 60)
        self.clock.advance(5 * 3600)
        self.clock.set_struct(datetime(2026, 10, 29, 7, 16))
        await self.engine.advance_life_matters(fixture.SESSION)
        self.assertEqual((await self.get_state()).current_action["type"], "life_simulation")

    async def test_explicit_night_step_can_run_at_night(self):
        await self.set_state(energy=.9)
        async with self.engine.session_state(fixture.SESSION) as state:
            item = self.engine.add_life_matter(state, "夜间观察")
            item["steps"] = [{"id": "step", "text": "观察", "action": "life_simulation", "node": "study",
                              "minutes": 10, "not_before": 0, "status": "pending", "time_window": "night"}]
        await self.engine.advance_life_matters(fixture.SESSION)
        self.assertEqual((await self.get_state()).current_action["type"], "life_simulation")

    async def test_explicit_action_window_is_enforced_on_execution(self):
        action = ActionDef(id="shop", category="continuous", duration=300,
                           preconditions={"time_window": "daytime"})
        self.engine.world.actions.append(action)
        self.assertFalse(self.engine._check_preconditions(await self.get_state(), action)[0])
        builder = PromptBuilder(self.engine.world, now_provider=self.engine.local_now)
        self.assertFalse(builder._action_usable_now(action))

    async def test_disabled_quiet_preference_preserves_unscheduled_night_work(self):
        self.engine.world.night.quiet_life = False
        await self.set_state(energy=.9)
        async with self.engine.session_state(fixture.SESSION) as state:
            item = self.engine.add_life_matter(state, "整理")
            item["steps"] = [{"id": "step", "text": "整理", "action": "life_simulation", "node": "study",
                              "minutes": 10, "status": "pending"}]
        await self.engine.advance_life_matters(fixture.SESSION)
        self.assertEqual((await self.get_state()).current_action["type"], "life_simulation")

    async def test_direct_start_cannot_bypass_action_window(self):
        action = ActionDef(id="shop", category="continuous", duration=300,
                           preconditions={"time_window": "daytime"})
        self.engine.world.actions.append(action)
        async with self.engine.session_state(fixture.SESSION) as state:
            await self.engine._start_action(state, self.engine.node("study"), TickOutcome(fixture.SESSION),
                                           action, PlannedAction(type="shop"), 0, False)
            self.assertIsNone(state.current_action)
        self.assertFalse(self.messenger.sent)

    async def test_running_daytime_activity_finishes_after_night_begins(self):
        self.clock.set_struct(datetime(2026, 10, 29, 22, 59))
        action = ActionDef(id="shop", category="continuous", duration=120,
                           preconditions={"time_window": "daytime"})
        self.engine.world.actions.append(action)
        async with self.engine.session_state(fixture.SESSION) as state:
            await self.engine._start_action(state, self.engine.node("study"), TickOutcome(fixture.SESSION),
                                           action, PlannedAction(type="shop"), 0, False)
            self.assertEqual(state.current_action["type"], "shop")
            self.clock.set_struct(datetime(2026, 10, 29, 23, 1))
            for _ in range(2):
                await self.engine._tick_continuous(state, self.engine.node("study"), TickOutcome(fixture.SESSION))
            self.assertIsNone(state.current_action)

    async def test_arrival_after_night_begins_preserves_undone_matter(self):
        self.clock.set_struct(datetime(2026, 10, 29, 22, 59))
        await self.set_state(energy=.9)
        async with self.engine.session_state(fixture.SESSION) as state:
            item = self.engine.add_life_matter(state, "去卧室整理")
            item["steps"] = [{"id": "step", "text": "整理", "action": "life_simulation", "node": "bedroom",
                              "minutes": 10, "status": "pending", "time_window": "daytime"}]
        await self.engine.advance_life_matters(fixture.SESSION)
        async with self.engine.session_state(fixture.SESSION) as state:
            self.assertEqual(state.current_action["type"], "walk_to")
            state.node_id = "bedroom"
            state.current_action = None
            state.current_plan["steps"][0]["status"] = "done"
            state.current_plan["current_step"] = 1
            self.clock.set_struct(datetime(2026, 10, 29, 23, 1))
            await self.engine._tick_plan(state, self.engine.node("bedroom"), TickOutcome(fixture.SESSION), depth=0)
            self.assertIsNone(state.current_action)
            self.assertIsNone(state.current_plan)
            item = state.life_matters[-1]
            self.assertEqual(item["steps"][0]["status"], "pending")
            self.assertEqual(item["status"], "waiting")

    async def test_recovery_uses_valid_fallback_when_restore_window_is_closed(self):
        restore = self.add_recovery("charge", seconds=600, gain="+0.65")
        restore.preconditions.time_window = "daytime"
        self.add_recovery("relax", kind="auto")
        self.assertEqual(self.engine.decider.recovery_plan(await self.get_state())["steps"][-1]["action"], "relax")

    async def test_failed_travel_cannot_simulate_work_at_wrong_location(self):
        self.clock.set_struct(datetime(2026, 10, 29, 12))
        await self.set_state(energy=.9)
        async with self.engine.session_state(fixture.SESSION) as state:
            item = self.engine.add_life_matter(state, "在卧室整理")
            item["steps"] = [{"id": "step", "text": "在卧室整理", "action": "life_simulation",
                              "node": "bedroom", "minutes": 10, "status": "pending"}]
        await self.engine.advance_life_matters(fixture.SESSION)
        async with self.engine.session_state(fixture.SESSION) as state:
            state.current_action = None
            state.current_plan["steps"][0]["status"] = "skipped"
            state.current_plan["current_step"] = 1
            await self.engine._tick_plan(state, self.engine.node("study"), TickOutcome(fixture.SESSION), depth=0)
            self.assertIsNone(state.current_action)
            self.assertIsNone(state.current_plan)
            self.assertEqual(state.life_matters[-1]["status"], "blocked")
            self.assertEqual(state.life_matters[-1]["steps"][0]["status"], "pending")

    async def test_quiet_night_rule_prefers_home_over_continuing_to_shop(self):
        from core.life import life_role
        self.engine.world.zones.append(ZoneDef(id="town", name="商业区"))
        self.engine.world.nodes.append(NodeDef(id="shop", name="商店", zone_id="town"))
        self.engine.world.edges.append(EdgeDef.model_validate({"from": "study", "to": "shop", "ticks": 1}))
        self.engine.world.actions = [a for a in self.engine.world.actions if life_role(a) not in {"interest", "explore"} or a.id == "walk_to"]
        for ident, node in (("home_read", "study"), ("shop_relax", "shop")):
            self.engine.world.actions.append(ActionDef(id=ident, category="continuous", duration=300,
                life_role="interest", scope="node", allowed_nodes=[node]))
        state = await self.get_state()
        state.node_id, state.energy, state.boredom = "shop", .9, .8
        plan = self.engine.decider._custom_life_plan(state)
        self.assertEqual(plan["steps"][-1]["action"], "home_read")
        self.engine.world.night.quiet_life = False
        self.assertEqual(self.engine.decider._custom_life_plan(state)["steps"][-1]["action"], "shop_relax")

    async def test_actual_selected_context_path_receives_night_rhythm(self):
        state = await self.get_state()
        builder = PromptBuilder(self.engine.world, now_provider=self.engine.local_now)
        for task in ("reply", "plan", "participation"):
            request = ContextRequest(task=task, session=fixture.SESSION, now=self.clock.now())
            text = builder.runtime_layer(state, node=self.engine.node("study"), context_request=request)
            self.assertIn("# 夜间生活节奏", text)
            self.assertIn("不因此安排睡觉", text)
            self.assertIn("02:16", text)


class ActivityTimeTests(unittest.TestCase):
    def setUp(self):
        self.world, warnings = parse_world(default_world())
        self.assertFalse(warnings)

    def start_at(self, local, *, window="auto", action_window="any", days=0):
        now = local.timestamp()
        action = ActionDef(id="work", preconditions={"time_window": action_window})
        return matter_start_at({"time_window": window, "not_before": now + days * 86400}, action,
                               self.world, now, lambda at: datetime.fromtimestamp(at, local.tzinfo))

    def test_cross_midnight_and_boundaries(self):
        self.assertEqual([window_allows("night", self.world, hour) for hour in (22, 23, 0, 6, 7)],
                         [False, True, True, True, False])

    def test_non_crossing_night_and_disabled_night(self):
        self.world.night.start_hour, self.world.night.end_hour = 8, 17
        local = datetime(2026, 10, 29, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(datetime.fromtimestamp(self.start_at(local), local.tzinfo).hour, 17)
        self.world.night.end_hour = 8
        self.assertEqual(self.start_at(local), local.timestamp())

    def test_day_offset_is_retained_before_applying_hour_window(self):
        local = datetime(2026, 10, 29, 2, 16, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(datetime.fromtimestamp(self.start_at(local, days=1), local.tzinfo),
                         datetime(2026, 10, 30, 7, tzinfo=local.tzinfo))

    def test_conflicting_explicit_windows_need_replan(self):
        local = datetime(2026, 10, 29, 2, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertIsNone(self.start_at(local, window="night", action_window="daytime"))
        self.assertEqual(self.start_at(local, window="auto", action_window="night"), local.timestamp())

    def test_dst_wait_uses_world_local_boundary(self):
        zone = ZoneInfo("America/New_York")
        local = datetime(2026, 11, 1, 0, 30, tzinfo=zone)
        later = self.start_at(local)
        self.assertEqual(datetime.fromtimestamp(later, zone), datetime(2026, 11, 1, 7, tzinfo=zone))
        self.assertAlmostEqual(later - local.timestamp(), 7.5 * 3600)

    def test_quiet_note_is_context_not_sleep_command(self):
        note = quiet_life_note(self.world, datetime(2026, 10, 29, 2))
        self.assertIn("不因此安排睡觉", note)
        self.world.night.quiet_life = False
        self.assertEqual(quiet_life_note(self.world, datetime(2026, 10, 29, 2)), "")

    def test_nonexistent_dst_hour_is_not_mistaken_for_night(self):
        self.world.night.start_hour, self.world.night.end_hour = 2, 3
        zone = ZoneInfo("America/New_York")
        local = datetime(2026, 3, 8, 1, 30, tzinfo=zone)
        later = self.start_at(local, window="night")
        self.assertEqual(datetime.fromtimestamp(later, zone), datetime(2026, 3, 9, 2, tzinfo=zone))

    def test_generated_action_retains_optional_start_window(self):
        for window in ("any", "daytime", "night"):
            with self.subTest(window=window):
                rows, problems = parse_generated_actions(json.dumps({"actions": [{"id": "scheduled_work",
                    "name": "整理", "category": "continuous", "allowed_nodes": ["study"],
                    "preconditions": {"time_window": window}}]}), node_ids=["study"], tool_names=set())
                self.assertFalse(problems)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["preconditions"]["time_window"], window)
