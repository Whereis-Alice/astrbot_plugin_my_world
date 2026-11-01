"""A returned visit cannot turn time with a friend into recovery at home."""
from __future__ import annotations

import copy
from datetime import datetime
import unittest
from unittest.mock import Mock

from core.planner import create_plan
from core.visits import VisitConfig, VisitService
from core.visits.protocol import public_actor
from tests import test_engine as fixture
from tests import test_visits_actor as actors
from tests.test_visits_protocol import Actor


class MemoryDb:
    def __init__(self):
        self.state = {}

    async def call(self, operation, key, value):
        if operation == "kv_get":
            return copy.deepcopy(self.state.get(key, value))
        self.state[key] = copy.deepcopy(value)


class VisitReturnClockTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"

    async def asyncSetUp(self):
        await actors.VisitActorTests.asyncSetUp(self)
        self.engine.world.actions.append(fixture.ActionDef(
            id="return_home_nap", name="家中的小睡", enabled=True, category="continuous",
            idle=False, recovery_kind="nap"))
        self.engine.world.actions.append(fixture.ActionDef(
            id="return_home_work", name="家中的阅读", enabled=True, category="continuous", idle=False))
        self.visit_db = MemoryDb()
        self.cfg = VisitConfig(enabled=True, auto_enabled=False, listen_port=0)
        self.service = VisitService(self.visit_db, self.cfg, self.port, clock=self.clock.now)
        self.engine.visits = self.service
        self.addAsyncCleanup(self.service.close)
        await self.service._load()
        remote = public_actor(await Actor("friend").describe("friend-life"), "friend-world")
        self.peer = self.service._add_peer(fixture.SESSION, self.local, remote,
                                           "https://unused.example", "test-key")

    def arrived(self, *, scope=fixture.SESSION, persona=None):
        index = len(self.service._state["visits"]) + 1
        visit = self.service._new_visit(scope, self.peer, self.local, f"{index:032x}", "guest", "喝茶", False)
        if persona:
            visit["local"]["persona_key"] = persona
        self.service._state["visits"][visit["id"]] = visit
        self.service._activate(visit)
        return visit

    async def home(self, *, action="return_home_nap", duration=100):
        async with self.engine.session_state(fixture.SESSION) as state:
            state.state, state.energy = ("napping" if action == "return_home_nap" else "reading"), .5
            state.current_action = {"type": action, "duration_ticks": duration, "elapsed_ticks": 2,
                                    "persona_key": self.local["persona_key"]}
            state.current_plan = create_plan(steps=[{"action": "return_home_work"}],
                world_time=state.world_time, now=self.clock.now(), valid_for=3000,
                persona_key=self.local["persona_key"])
            state.life_updated_at = self.clock.now()
            return state.world_time, state.current_plan["expires_at"]

    async def finish(self, visit):
        await self.service._finish(visit, "completed", "告别归家", notify=False)

    async def tick(self, scope=fixture.SESSION):
        await self.engine._tick_session(scope)
        return await self.engine.load_state(scope, cold_start=False)

    def record_dynamics(self):
        records = []
        original = self.engine.dynamics.tick
        def record(state, **kwargs):
            if kwargs["elapsed_seconds"] > 0:
                records.append({"state": state.state, "node": getattr(kwargs["node"], "id", None),
                                "seconds": kwargs["elapsed_seconds"], "now": kwargs["now"]})
            return original(state, **kwargs)
        self.engine.dynamics.tick = Mock(side_effect=record)
        return records

    async def test_unknown_inflight_reload_does_not_count_home_nap_while_away(self):
        visit = self.arrived()
        world_time, deadline = await self.home()
        visit["model_pending"] = "unknown-inflight"
        await self.service._save()
        self.clock.advance(600)
        reloaded = VisitService(self.visit_db, self.cfg, self.port, clock=self.clock.now)
        self.addAsyncCleanup(reloaded.close)
        self.engine.visits = reloaded
        await reloaded._load()
        state = await self.tick()
        self.assertEqual(reloaded._state["visits"][visit["id"]]["status"], "interrupted")
        self.assertEqual(state.current_action["elapsed_ticks"], 2)
        self.assertLess(state.energy, .5)
        self.assertEqual(state.world_time, world_time + 10)
        self.assertEqual(state.current_plan["expires_at"], deadline)
        self.assertEqual(state.life_recovery["paused_visit_seconds"], 600)
        self.assertNotIn("_visit_need_segments", state.to_payload())
        self.assertFalse(hasattr(state, "_visit_need_segments"))
        self.assertEqual(self.llm.calls, [])

    async def test_one_actual_home_minute_resumes_quiet_work_without_replaying_away_time(self):
        visit = self.arrived()
        _, deadline = await self.home(action="return_home_work")
        self.clock.advance(600)
        await self.finish(visit)
        self.clock.advance(60)
        records = self.record_dynamics()
        state = await self.tick()
        self.assertEqual(state.current_action["elapsed_ticks"], 3)
        self.assertEqual(state.current_plan["expires_at"], deadline)
        self.assertEqual([(row["node"], row["seconds"]) for row in records], [(None, 600), ("study", 60)])
        self.assertEqual(self.tools.calls, [])
        self.assertEqual(self.llm.calls, [])

    async def test_short_return_window_does_not_add_a_fixed_tick_of_home_work(self):
        visit = self.arrived()
        await self.home(action="return_home_work")
        self.clock.advance(60)
        await self.finish(visit)
        state = await self.tick()
        self.assertEqual(state.current_action["elapsed_ticks"], 2)
        self.assertEqual(state.world_time, 1)

    async def test_expiry_bounds_the_pause_without_guessing_later_activity(self):
        visit = self.arrived()
        await self.home()
        visit["expires_at"] = self.clock.now() + 300
        self.clock.advance(600)
        await self.finish(visit)
        records = self.record_dynamics()
        state = await self.tick()
        self.assertEqual(state.current_action["elapsed_ticks"], 7)
        self.assertEqual(state.life_recovery["paused_visit_seconds"], 300)
        self.assertEqual([(row["state"], row["node"], row["seconds"]) for row in records],
                         [("idle", None, 300), ("napping", "study", 300)])

    async def test_pending_invitation_other_life_or_other_persona_does_not_pause_home_recovery(self):
        for condition in ("pending", "scope", "persona", "trimmed"):
            with self.subTest(condition=condition):
                self.service._state["visits"].clear()
                visit = self.arrived()
                await self.home()
                if condition == "pending":
                    visit["status"] = "offered"
                    visit.pop("arrived_at")
                elif condition == "scope":
                    visit["scope"] = fixture.OTHER_SESSION
                elif condition == "persona":
                    visit["local"]["persona_key"] = "different-persona"
                self.clock.advance(600)
                await self.finish(visit)
                if condition == "trimmed":
                    self.service._state["visits"].clear()
                state = await self.tick()
                self.assertEqual(state.current_action["elapsed_ticks"], 12)
                self.assertGreater(state.energy, .5)
                self.assertNotIn("paused_visit_seconds", state.life_recovery)

    async def test_overlapping_retained_windows_are_counted_once(self):
        visit = self.arrived()
        await self.home()
        start = self.clock.now()
        self.clock.advance(400)
        await self.finish(visit)
        duplicate = copy.deepcopy(visit)
        duplicate.update(id="b" * 32, arrived_at=start + 120, ended_at=start + 600)
        self.service._state["visits"][duplicate["id"]] = duplicate
        self.clock.advance(200)
        state = await self.tick()
        self.assertEqual(state.current_action["elapsed_ticks"], 2)
        self.assertEqual(state.life_recovery["paused_visit_seconds"], 600)

    async def test_shared_member_uses_its_original_life_pause(self):
        raw = self.store.raw_world()
        raw["actions"].extend(self.engine.world.action_map()[key].model_dump(mode="json")
                              for key in ("return_home_nap", "return_home_work"))
        self.store.save_world(raw)
        fixture.EngineTestCase.add_group(self, sessions=[fixture.SESSION, fixture.PRIVATE_SESSION], main=fixture.SESSION)
        self.local = await self.port.describe(fixture.SESSION)
        visit = self.arrived()
        await self.home()
        self.clock.advance(600)
        await self.finish(visit)
        state = await self.tick(fixture.PRIVATE_SESSION)
        self.assertEqual(state.session_id, fixture.SESSION)
        self.assertEqual(state.current_action["elapsed_ticks"], 2)

    async def test_midnight_return_wakes_only_after_the_remaining_home_rest(self):
        self.clock._now = datetime(2026, 11, 1, 23, 58).timestamp()
        visit = self.arrived()
        await self.home(duration=6)
        start = self.clock.now()
        self.clock.advance(300)
        await self.finish(visit)
        self.clock.advance(300)
        records = self.record_dynamics()
        state = await self.tick()
        self.assertNotEqual((state.current_action or {}).get("type"), "return_home_nap")
        self.assertEqual(state.sleep_history[-1]["ended_at"], start + 540)
        self.assertEqual([(row["state"], row["node"], row["seconds"]) for row in records],
                         [("idle", None, 300), ("napping", "study", 240), ("idle", "study", 60)])

    async def test_finished_home_rest_does_not_apply_home_amenities_to_later_visit_time(self):
        start = self.clock.now()
        await self.home(duration=6)
        self.clock.advance(300)
        visit = self.arrived()
        self.clock.advance(600)
        await self.finish(visit)
        records = self.record_dynamics()
        state = await self.tick()
        self.assertEqual(state.sleep_history[-1]["ended_at"], start + 240)
        self.assertEqual([(row["state"], row["node"], row["seconds"]) for row in records],
                         [("napping", "study", 240), ("idle", "study", 60), ("idle", None, 600)])

    async def test_awake_needs_before_and_after_recovery_share_one_six_hour_cap(self):
        await self.home(duration=6)
        start = self.clock.now()
        for _ in range(10):
            visit = self.arrived()
            self.clock.advance(1800)
            await self.finish(visit)
        self.clock.advance(240)
        for _ in range(10):
            second = self.arrived()
            self.clock.advance(1800)
            await self.finish(second)
        records = self.record_dynamics()
        state = await self.tick()
        awake = [row for row in records if row["state"] == "idle"]
        self.assertEqual(sum(row["seconds"] for row in awake), 6 * 3600)
        self.assertEqual(state.sleep_history[-1]["ended_at"], start + 5 * 3600 + 240)
        self.assertTrue(all(row["node"] is None for row in awake))

    async def test_while_thinking_finishes_offline_rest_before_awake_home_needs(self):
        self.clock._now = datetime(2026, 11, 1, 23, 58).timestamp()
        visit = self.arrived()
        await self.home(duration=6)
        start = self.clock.now()
        self.clock.advance(300)
        await self.finish(visit)
        self.clock.advance(300)
        records = self.record_dynamics()
        await self.engine._tick_while_thinking(fixture.SESSION)
        state = await self.engine.load_state(fixture.SESSION, cold_start=False)
        self.assertIsNone(state.current_action)
        self.assertEqual(state.sleep_history[-1]["ended_at"], start + 540)
        self.assertEqual([(row["state"], row["node"], row["seconds"]) for row in records],
                         [("idle", None, 300), ("napping", "study", 240), ("idle", "study", 60)])
        self.assertEqual(self.llm.calls, [])

    async def test_expired_plan_deadline_is_not_extended_by_visiting(self):
        visit = self.arrived()
        await self.home(action="return_home_work")
        async with self.engine.session_state(fixture.SESSION) as state:
            state.current_plan["expires_at"] = self.clock.now() + 300
        self.clock.advance(600)
        await self.finish(visit)
        state = await self.tick()
        self.assertIsNone(state.current_plan)
        self.assertEqual(state.current_action["elapsed_ticks"], 2)

    async def test_invalid_retained_timestamps_cannot_create_a_pause(self):
        visit = self.arrived()
        await self.home()
        self.clock.advance(600)
        await self.finish(visit)
        for key, value in (("arrived_at", float("nan")), ("ended_at", None), ("expires_at", True)):
            with self.subTest(key=key):
                changed = copy.deepcopy(visit)
                changed[key] = value
                self.service._state["visits"] = {changed["id"]: changed}
                self.assertEqual(self.service.pause_intervals(fixture.SESSION, self.clock.now() - 600,
                    self.clock.now(), persona_key=self.local["persona_key"]), [])

    async def configure_restore(self, *, duration):
        self.engine.world.actions.append(fixture.ActionDef(
            id="return_home_restore", name="补充精力", category="continuous", recovery_kind="restore",
            on_complete={"effects_per_minute": {"energy": "+0.1"}}))
        self.engine.world.state_dynamics.energy_decay_per_min = .01
        self.engine.world.state_dynamics.atmosphere_multiplier = 0.0
        self.engine.world.state_dynamics.independent_life_enabled = False
        self.engine.world.state_dynamics.daily_mood_enabled = False
        await self.home(action="return_home_restore", duration=duration)
        async with self.engine.session_state(fixture.SESSION) as state:
            state.energy = .95
            state.current_action["effects_settled_minutes"] = 2
            state.current_plan = None

    async def alternating_rest_and_visits(self):
        self.clock.advance(60)
        first = self.arrived()
        self.clock.advance(60)
        await self.finish(first)
        self.clock.advance(60)
        second = self.arrived()
        self.clock.advance(60)
        await self.finish(second)

    async def test_incremental_home_recovery_keeps_the_online_gain_and_decay_order(self):
        # Home recovery caps at 1, then each following visit minute decays .01.
        # Prepaying both home gains before these visits would instead leave .98.
        for duration in (100, 4):
            with self.subTest(completed=duration == 4):
                self.service._state["visits"].clear()
                await self.configure_restore(duration=duration)
                await self.alternating_rest_and_visits()
                state = await self.tick()
                self.assertAlmostEqual(state.energy, .99)
                if duration == 100:
                    self.assertEqual(state.current_action["elapsed_ticks"], 4)
                    self.assertEqual(state.current_action["effects_settled_minutes"], 4)
                else:
                    self.assertNotEqual((state.current_action or {}).get("type"), "return_home_restore")
                self.assertNotIn("_visit_rest_progress", state.to_payload())
                self.assertFalse(hasattr(state, "_visit_rest_progress"))
                self.assertEqual(self.llm.calls, [])

    async def test_incremental_recovery_uses_the_actual_home_segment_date(self):
        self.clock._now = datetime(2026, 11, 1, 23, 58, 30).timestamp()
        await self.configure_restore(duration=100)
        start = self.clock.now()
        settled = []
        original = self.engine.dynamics.apply_effects
        def effects(state, value, **kwargs):
            if value.get("energy") == "+0.1":
                settled.append(kwargs["now"])
            return original(state, value, **kwargs)
        self.engine.dynamics.apply_effects = Mock(side_effect=effects)
        await self.alternating_rest_and_visits()
        state = await self.tick()
        self.assertEqual(settled, [start + 60, start + 180])
        self.assertAlmostEqual(state.energy, .99)
        self.assertEqual(state.current_action["effects_settled_minutes"], 4)

    async def test_night_stay_up_costs_only_apply_to_actual_home_time(self):
        self.clock._now = datetime(2026, 11, 1, 23, 58, 30).timestamp()
        await self.configure_restore(duration=100)
        self.engine.world.action_map()["return_home_restore"].on_complete.effects_per_minute = {"energy": "+0.02"}
        async with self.engine.session_state(fixture.SESSION) as state:
            state.energy = .5
        await self.alternating_rest_and_visits()
        state = await self.tick()
        # Each home minute pays .01 base + .005 night cost, then restores .02.
        # The two visitor minutes pay only the ordinary .01 base cost.
        self.assertAlmostEqual(state.energy, .49)

    async def test_while_thinking_return_does_not_add_home_stay_up_cost(self):
        self.clock._now = datetime(2026, 11, 1, 23, 58, 30).timestamp()
        for duration in (100, 4):
            with self.subTest(completed=duration == 4):
                self.service._state["visits"].clear()
                await self.configure_restore(duration=duration)
                self.engine.world.action_map()["return_home_restore"].on_complete.effects_per_minute = {"energy": "+0.02"}
                async with self.engine.session_state(fixture.SESSION) as state:
                    state.energy = .5
                await self.alternating_rest_and_visits()
                await self.engine._tick_while_thinking(fixture.SESSION)
                state = await self.engine.load_state(fixture.SESSION, cold_start=False)
                self.assertAlmostEqual(state.energy, .5)
