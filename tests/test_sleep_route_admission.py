"""A reachable bedroom still needs an allowed departure before bedtime travel."""
from __future__ import annotations

import unittest

from core.engine import TickOutcome
from core.models import ChainStep
from tests import test_engine as fixture


class SleepRouteAdmissionTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    def prepare_world(self, *, local_sleep: bool = True) -> None:
        self.engine.world.sleep.mode = "autonomous"
        self.engine.world.sleep.node_id = "bedroom"
        self.engine.world.sleep.drowsy = False
        self.engine.world.sleep.goodnight = False
        sleep = self.engine.world.action_map()["sleep"]
        sleep.enabled = True
        sleep.scope = "global" if local_sleep else "node"
        sleep.allowed_nodes = [] if local_sleep else ["bedroom"]
        movement = self.engine.world.action_map()["walk_to"]
        movement.scope = "node"
        movement.allowed_nodes = ["bedroom"]

    def prepare_state(self, state, *, energy: float = 0.0) -> None:
        state.node_id, state.state, state.energy = "study", "idle", energy
        state.current_action = state.current_plan = None
        state.sleep_rhythm.update(
            decision="sleep", duration_seconds=8 * 3600,
            reason="该休息了", awake_since=self.clock.now() - 22 * 3600,
        )

    async def test_exhausted_character_rests_locally_when_walking_cannot_start_here(self):
        self.prepare_world()
        async with self.engine.session_state(fixture.SESSION) as state:
            self.prepare_state(state)
            await self.engine._tick_sleep_rhythm(state, TickOutcome(fixture.SESSION))
            self.assertIsNotNone(state.current_action)
            self.assertEqual("sleep", state.current_action["type"])
            self.assertEqual("study", state.node_id)
            self.assertTrue(state.current_plan["sleep_fallback"])
            self.assertNotIn("walk_to", state.action_usage)
        self.assertEqual([], self.llm.calls)

    async def test_ordinary_bedtime_uses_backoff_then_local_fallback(self):
        self.prepare_world()
        async with self.engine.session_state(fixture.SESSION) as state:
            self.prepare_state(state, energy=.3)
            await self.engine._run_rhythm_sleep(state, TickOutcome(fixture.SESSION))
            self.assertIsNone(state.current_action)
            self.assertIsNone(state.current_plan)
            self.assertEqual(1, state.sleep_rhythm["route_failures"])
            self.assertEqual(self.clock.now() + 300, state.sleep_rhythm["retry_at"])
            self.clock.advance(301)
            await self.engine._run_rhythm_sleep(state, TickOutcome(fixture.SESSION))
            self.assertEqual("sleep", state.current_action["type"])
            self.assertEqual("study", state.node_id)

    async def test_no_local_sleep_keeps_the_original_retry_without_replanning(self):
        self.prepare_world(local_sleep=False)
        async with self.engine.session_state(fixture.SESSION) as state:
            self.prepare_state(state)
            for _ in range(3):
                await self.engine._run_rhythm_sleep(state, TickOutcome(fixture.SESSION), force=True)
                self.assertIsNone(state.current_action)
                self.assertIsNone(state.current_plan)
            self.assertEqual(1, state.sleep_rhythm["route_failures"])
            self.assertEqual(self.clock.now() + 300, state.sleep_rhythm["retry_at"])
        events = await self.db.call("query_events", session_id=fixture.SESSION,
                                    event_type="skip", limit=20)
        self.assertEqual(1, len(events))

    async def test_an_allowed_departure_still_starts_bedroom_travel(self):
        self.prepare_world(local_sleep=False)
        self.engine.world.action_map()["walk_to"].allowed_nodes = ["study"]
        async with self.engine.session_state(fixture.SESSION) as state:
            self.prepare_state(state)
            await self.engine._run_rhythm_sleep(state, TickOutcome(fixture.SESSION), force=True)
            self.assertEqual("walk_to", state.current_action["type"])
            self.assertEqual("bedroom", state.current_action["target_node"])
            self.assertFalse(state.current_plan.get("sleep_fallback", False))

    async def enter_detached_drowsiness(self) -> None:
        self.prepare_world()
        self.engine.world.sleep.drowsy = True
        async with self.engine.session_state(fixture.SESSION) as state:
            self.prepare_state(state, energy=.3)
            state.node_id = "bedroom"
            await self.engine._run_chain(state, [ChainStep(type="sleep", duration=25200)],
                TickOutcome(fixture.SESSION), depth=0, plan_source="schedule")
            self.assertEqual("drowsy", state.state)
            self.assertTrue(state.drowsy_sleep_step)
            self.assertIsNone(state.current_plan)

    async def test_detached_drowsy_step_is_cancelled_after_a_character_change(self):
        await self.enter_detached_drowsiness()
        self.persona.text = "A different character with a different life."
        async with self.engine.session_state(fixture.SESSION) as state:
            self.assertEqual("idle", state.state)
            self.assertFalse(state.drowsy_sleep_step)
            self.assertEqual("continue", state.sleep_rhythm["decision"])
            self.assertEqual([], state.sleep_history)

    async def test_detached_drowsy_step_stays_pending_for_its_own_character(self):
        await self.enter_detached_drowsiness()
        async with self.engine.session_state(fixture.SESSION) as state:
            self.assertEqual("drowsy", state.state)
            self.assertTrue(state.drowsy_sleep_step)
            self.assertEqual(self.engine._matter_persona_key(state),
                             state.drowsy_sleep_step.get("persona_key"))
            self.assertEqual([], state.sleep_history)

    async def test_default_drowsiness_does_not_lose_local_fallback_authorization(self):
        self.prepare_world()
        self.engine.world.sleep.drowsy = True
        async with self.engine.session_state(fixture.SESSION) as state:
            self.prepare_state(state, energy=.3)
            await self.engine._run_rhythm_sleep(state, TickOutcome(fixture.SESSION))
            self.clock.advance(301)
            await self.engine._run_rhythm_sleep(state, TickOutcome(fixture.SESSION))
            if state.state == "drowsy":
                self.clock.advance(360)
                await self.engine._drowsy_tick(state, self.engine.node(state.node_id),
                                             TickOutcome(fixture.SESSION))
            self.assertEqual("sleep", (state.current_action or {}).get("type"))
            self.assertEqual("study", state.node_id)
            self.assertEqual({}, state.drowsy_sleep_step)


if __name__ == "__main__":
    unittest.main()
