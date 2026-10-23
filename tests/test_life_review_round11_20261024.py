"""Behavioral checks for recovery admission and idle/formal-work handoff."""
import unittest

from core.models import ActionDef
from core.planner import create_plan
from tests import test_chat_browse as browse
from tests import test_engine as fixture

S = fixture.SESSION


class RecoveryAdmissionTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp
    configure = browse.ChatBrowseTests.configure
    note = browse.ChatBrowseTests.note

    async def asyncTearDown(self):
        await self.engine.close_background()

    async def ready_matter(self, *, energy, threshold=.25, recovering=False):
        self.engine.world.sleep.mode = "activity"
        self.engine.world.sleep.recovery_threshold = threshold
        self.engine.world.sleep.recovery_target = .85
        async with self.engine.session_state(S) as state:
            state.current_action, state.current_plan, state.state = None, None, "idle"
            state.energy = energy
            state.sleep_rhythm["recovering"] = recovering
            item = self.engine.add_life_matter(state, "接着读完这一章")
            item["steps"] = [{"id": "part", "text": "读一会儿", "action": "read", "node": "study",
                              "minutes": 5, "status": "pending"}]
            return item["id"]

    async def test_ready_project_starts_above_configured_recovery_threshold(self):
        matter_id = await self.ready_matter(energy=.35)
        await self.engine.advance_life_matters(S)
        state = await self.engine.load_state(S, cold_start=False)
        self.assertEqual((state.current_action or {}).get("matter_id"), matter_id)

    async def test_higher_recovery_threshold_prevents_new_project(self):
        await self.ready_matter(energy=.6, threshold=.7)
        await self.engine.advance_life_matters(S)
        state = await self.engine.load_state(S, cold_start=False)
        self.assertIsNone(state.current_action)

    async def test_short_recovery_round_keeps_new_work_waiting_until_target(self):
        await self.ready_matter(energy=.55, recovering=True)
        await self.engine.advance_life_matters(S)
        state = await self.engine.load_state(S, cold_start=False)
        self.assertIsNone(state.current_action)

    async def test_recovery_target_releases_new_work_even_with_old_pending_flag(self):
        matter_id = await self.ready_matter(energy=.9, recovering=True)
        await self.engine.advance_life_matters(S)
        state = await self.engine.load_state(S, cold_start=False)
        self.assertEqual((state.current_action or {}).get("matter_id"), matter_id)

    async def test_below_recovery_threshold_still_blocks_new_work(self):
        await self.ready_matter(energy=.2)
        await self.engine.advance_life_matters(S)
        self.assertIsNone((await self.engine.load_state(S, cold_start=False)).current_action)

    async def test_free_reading_is_not_blocked_by_unconfigured_forty_percent_floor(self):
        self.configure()
        self.engine.world.sleep.mode = "activity"
        self.engine.world.sleep.recovery_threshold = .25
        async with self.engine.session_state(S) as state:
            state.current_action, state.current_plan, state.state = None, None, "idle"
            state.energy = .35
            self.note(state)
            self.assertTrue(self.engine.start_chat_browse(state, "browse_phone"))
            self.assertEqual(state.current_action["type"], "browse_phone")

    async def test_sleeping_characters_keep_the_existing_work_rest_floor(self):
        await self.ready_matter(energy=.35)
        self.engine.world.sleep.mode = "autonomous"
        await self.engine.advance_life_matters(S)
        self.assertIsNone((await self.engine.load_state(S, cold_start=False)).current_action)


class IdleHandoffTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    async def asyncTearDown(self):
        await self.engine.close_background()

    async def prepare(self, *, completed_idle=False):
        self.engine.world.sleep.mode = "activity"
        self.engine.world.events.auto.enabled = False
        self.engine.world.state_dynamics.energy_decay_per_min = 0
        self.engine.world.idle_life.enabled = True
        self.engine.world.idle_life.duration_min_minutes = 10
        self.engine.world.idle_life.duration_max_minutes = 10
        for candidate in self.engine.world.actions:
            if candidate.idle:
                candidate.enabled = False
        action = ActionDef(id="quiet_hum", name="轻轻哼歌", idle=True, category="continuous",
                           duration_mode="fixed", duration=600, quota={"day": 1},
                           on_complete={"effects": {"energy": "+0.1"}})
        self.engine.world.actions.append(action)
        async with self.engine.session_state(S) as state:
            state.state, state.energy, state.world_time = "idle", .6, 10
            state.current_action = None
            state.life_updated_at = self.clock.now()
            if completed_idle:
                self.engine._count_action_use(state, action)
                state.idle_activity = {"id": action.id, "node_id": "study", "start_tick": 0,
                                       "until_tick": 10, "persona_key": self.engine._matter_persona_key(state)}
            state.current_plan = create_plan(steps=[{"action": "read", "duration": 300}],
                world_time=10, now=self.clock.now(), valid_for=7200, source="schedule",
                persona_key=self.engine._matter_persona_key(state))
        return action

    async def test_queued_formal_work_does_not_spend_an_unstarted_idle_action_quota(self):
        action = await self.prepare()
        await self.engine._tick_session(S)
        state = await self.engine.load_state(S, cold_start=False)
        self.assertEqual((state.current_action or {}).get("type"), "read")
        self.assertNotIn(action.id, state.action_usage)
        self.assertNotIn(action.id, state.life_interests)

    async def test_finished_idle_keeps_earned_reward_before_formal_work_starts(self):
        action = await self.prepare(completed_idle=True)
        await self.engine._tick_session(S)
        state = await self.engine.load_state(S, cold_start=False)
        self.assertEqual((state.current_action or {}).get("type"), "read")
        self.assertAlmostEqual(state.energy, .7)
        self.assertEqual(self.engine.action_quota_left(state, action), 0)

    async def test_genuinely_free_time_still_starts_and_counts_idle_activity(self):
        action = await self.prepare()
        async with self.engine.session_state(S) as state:
            state.current_plan = None
        await self.engine._tick_session(S)
        state = await self.engine.load_state(S, cold_start=False)
        self.assertEqual(state.idle_activity.get("id"), action.id)
        self.assertEqual(self.engine.action_quota_left(state, action), 0)

    async def test_interrupted_idle_keeps_actual_effort_without_completion_reward(self):
        action = await self.prepare()
        async with self.engine.session_state(S) as state:
            self.engine._count_action_use(state, action)
            state.idle_activity = {"id": action.id, "node_id": "study", "start_tick": 10,
                                  "until_tick": 20, "persona_key": self.engine._matter_persona_key(state)}
        await self.engine._tick_session(S)
        state = await self.engine.load_state(S, cold_start=False)
        self.assertEqual((state.current_action or {}).get("type"), "read")
        self.assertAlmostEqual(state.energy, .6)
        self.assertEqual(state.life_interests[action.id]["minutes"], 1)
        self.assertEqual(self.engine.action_quota_left(state, action), 0)

