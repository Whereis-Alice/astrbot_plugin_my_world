"""First exploration keeps its wait across room changes and old-state reloads."""
import unittest

from core.decider import Decider, ENVIRONMENT_DWELL_SECONDS, ENVIRONMENT_OUTDOOR_GAP_SECONDS
from core.state import WorldState
from tests import test_engine as engine_fixture
from tests.test_life_environment_review_s import FixedDraw
from tests.test_outdoor_hint_review_s import HintHarness


class FirstOutingOpportunityTests(unittest.TestCase):
    def setUp(self):
        self.engine = HintHarness()
        self.hour = 15
        self.decider = Decider(self.engine.world, rng=FixedDraw(), now_provider=self.engine._now,
                               hour_provider=lambda: self.hour)
        self.started = self.engine.now
        self.state = WorldState("first-outing", node_id="alice_bedroom", state="idle", energy=.9,
                                curiosity=.8, boredom=.01, created_at=self.started,
                                node_since=self.started)

    def due(self):
        return self.decider._change_environment_due(self.state)

    def test_indoor_moves_do_not_restart_the_first_eighteen_hour_wait(self):
        self.assertFalse(self.due())
        for age in (8, 16):
            self.engine.now = self.started + age * 3600
            self.state.node_since = self.engine.now  # A completed indoor move updates this field.
            self.assertFalse(self.due())
        self.engine.now = self.started + ENVIRONMENT_OUTDOOR_GAP_SECONDS
        self.assertTrue(self.due())
        self.assertEqual(self.state.first_outing_wait_at, self.started)
        self.assertEqual(self.state.last_outdoor_at, 0)
        self.assertEqual(self.decider.rule_plan(self.state, group_chatting=False,
                         interject_allowed=False)["steps"][-1]["action"], "browse_heroes")

    def test_current_room_dwell_is_still_required_after_the_first_wait(self):
        self.engine.now += ENVIRONMENT_OUTDOOR_GAP_SECONDS
        self.state.node_since = self.engine.now - ENVIRONMENT_DWELL_SECONDS + 1
        self.assertFalse(self.due())
        self.engine.now += 1
        self.assertTrue(self.due())

    def test_wait_survives_serialization_and_staying_home_remains_a_choice(self):
        self.assertFalse(self.due())
        self.engine.now += 8 * 3600
        self.state.node_since = self.engine.now
        self.state = WorldState.from_payload(self.state.to_payload(), self.state.session_id)
        self.engine.now += 10 * 3600
        self.assertTrue(self.due())
        self.assertEqual(self.state.first_outing_wait_at, self.started)
        self.decider.rng.point = .99
        self.assertIsNone(self.decider.rule_plan(self.state, group_chatting=False,
                                               interject_allowed=False))

    def test_legacy_created_time_is_used_without_inventing_an_outdoor_record(self):
        self.state.created_at = self.started - 2 * 86400
        self.state.node_since = self.started - 2 * 3600
        self.assertTrue(self.due())
        self.assertEqual(self.state.first_outing_wait_at, self.state.created_at)
        self.assertEqual(self.state.last_outdoor_at, 0)

    def test_invalid_or_future_legacy_times_use_the_known_arrival(self):
        for invalid in (None, "broken", -1, float("inf"), float("nan"), self.started + 86400):
            with self.subTest(invalid=invalid):
                payload = self.state.to_payload()
                payload.update(first_outing_wait_at=invalid, created_at=self.started + 86400,
                               node_since=self.started - 2 * 3600)
                self.state = WorldState.from_payload(payload, self.state.session_id)
                self.assertFalse(self.due())
                self.assertEqual(self.state.first_outing_wait_at, self.started - 2 * 3600)
                self.assertEqual(self.state.created_at, self.started + 86400)

    def test_missing_known_times_start_a_real_wait_that_room_changes_cannot_reset(self):
        self.state.created_at = self.state.node_since = 0
        self.assertEqual(self.state.ensure_first_outing_wait_at(self.started), self.started)
        self.assertFalse(self.due())
        self.engine.now += 16 * 3600
        self.state.node_since = self.engine.now
        self.engine.now += 2 * 3600
        self.assertTrue(self.due())
        self.assertEqual(self.state.first_outing_wait_at, self.started)

    def test_explicit_new_state_wait_uses_engine_time_even_with_an_older_created_time(self):
        self.state.created_at = self.started - 30 * 86400
        self.state.first_outing_wait_at = self.started
        self.state.node_since = self.started - 2 * 3600
        self.assertFalse(self.due())
        self.assertEqual(self.state.first_outing_wait_at, self.started)

    def test_real_outdoor_history_still_controls_later_opportunities(self):
        self.state.first_outing_wait_at = self.started - 30 * 86400
        self.state.node_since = self.started - 2 * 3600
        self.state.last_outdoor_at = self.started - ENVIRONMENT_OUTDOOR_GAP_SECONDS + 1
        self.assertFalse(self.due())
        self.engine.now += 1
        self.assertTrue(self.due())
        self.assertEqual(self.state.last_outdoor_at, self.started - ENVIRONMENT_OUTDOOR_GAP_SECONDS + 1)

    def test_first_wait_does_not_bypass_night_fatigue_sleep_or_an_existing_action(self):
        self.state.created_at = self.started - 2 * 86400
        self.state.node_since = self.started - 2 * 3600
        self.assertTrue(self.due())
        for field, value in (("state", "sleeping"), ("current_action", {"type": "browse_heroes"}),
                             ("sleep_pressure", 1), ("energy", .59), ("curiosity", .54)):
            with self.subTest(field=field):
                saved = getattr(self.state, field)
                setattr(self.state, field, value)
                self.assertFalse(self.due())
                setattr(self.state, field, saved)
        self.hour = 23
        self.assertFalse(self.due())
        self.hour = 15
        self.engine.world.state_dynamics.independent_life_enabled = False
        self.assertFalse(self.due())


class FirstOutingStateStoreTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = engine_fixture.EngineTestCase.asyncSetUp

    async def test_new_state_wait_uses_the_injected_engine_clock(self):
        state = self.engine._default_state(engine_fixture.SESSION)
        self.assertEqual(state.first_outing_wait_at, self.clock.now())
        self.assertEqual(state.first_outing_wait_at, state.node_since)
        created_at = state.created_at
        self.clock.advance(8 * 3600)
        state.node_since = self.clock.now()
        await self.engine.save_state(state)
        reloaded = await self.engine.load_state(engine_fixture.SESSION, cold_start=False)
        self.assertEqual(reloaded.first_outing_wait_at, self.clock.now() - 8 * 3600)
        self.assertEqual(reloaded.created_at, created_at)

    async def test_old_wait_is_migrated_then_saved_across_room_changes_and_reload(self):
        origin = self.clock.now() - 2 * 86400
        payload = WorldState(engine_fixture.SESSION, node_id="study", created_at=origin,
                             node_since=self.clock.now() - 2 * 3600).to_payload()
        payload.pop("first_outing_wait_at")
        await self.db.call("save_state", engine_fixture.SESSION, payload, self.clock.now())
        state = await self.engine.load_state(engine_fixture.SESSION, cold_start=False)
        self.assertEqual(state.first_outing_wait_at, origin)
        self.assertEqual(state.created_at, origin)
        self.assertEqual(state.last_outdoor_at, 0)
        self.clock.advance(8 * 3600)
        state.node_id, state.node_since = "bedroom", self.clock.now()
        await self.engine.save_state(state)
        self.clock.advance(10 * 3600)
        reloaded = await self.engine.load_state(engine_fixture.SESSION, cold_start=False)
        self.assertEqual(reloaded.first_outing_wait_at, origin)
        self.assertEqual(reloaded.node_since, self.clock.now() - 10 * 3600)
        self.assertEqual(reloaded.created_at, origin)

    async def test_old_missing_creation_field_uses_known_engine_time_and_keeps_it(self):
        payload = WorldState(engine_fixture.SESSION, node_id="study",
                             node_since=self.clock.now() - 2 * 3600).to_payload()
        for field in ("created_at", "first_outing_wait_at"):
            payload.pop(field)
        await self.db.call("save_state", engine_fixture.SESSION, payload, self.clock.now())
        state = await self.engine.load_state(engine_fixture.SESSION, cold_start=False)
        origin = self.clock.now() - 2 * 3600
        self.assertEqual(state.first_outing_wait_at, origin)
        await self.engine.save_state(state)
        self.clock.advance(8 * 3600)
        reloaded = await self.engine.load_state(engine_fixture.SESSION, cold_start=False)
        self.assertEqual(reloaded.first_outing_wait_at, origin)


if __name__ == "__main__":
    unittest.main()
