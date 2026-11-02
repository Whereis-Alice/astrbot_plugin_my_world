"""Delayed settlement keeps only earned online minutes and their original dates."""
import asyncio
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from tests import fake_astrbot

fake_astrbot.install()

# Install the SDK fixture before imports that can resolve the installed host.
from core import life_matters as matters  # noqa: E402
from core.engine import TickOutcome  # noqa: E402
from core.engine.life_inference import LIFE_WORK, LifeWorkDiscarded, released_life_lock  # noqa: E402
from core.life import action_progress_time  # noqa: E402
from core.models import ActionDef  # noqa: E402
from tests import test_action_record_boundaries as records  # noqa: E402
from tests import test_engine as fixture  # noqa: E402

S = fixture.SESSION


class OnlineProgressTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp
    configure_action = records.ActionRecordBoundaryTests.configure_action
    execute = records.ActionRecordBoundaryTests.execute

    def set_midnight_clock(self):
        self.clock._now = datetime(2026, 11, 2, 23, 58, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()

    async def start(self, *, seconds=120):
        action = self.configure_action(name="读一会儿书", category="continuous", llm_level="template",
            template="", duration_mode="fixed", duration=seconds, life_role="interest")
        await self.execute(action)
        async with self.engine.session_state(S) as state:
            state.life_updated_at = self.clock.now()
            state.energy, state.valence, state.valence_offset = .9, .5, 0
        return action

    async def think(self, minutes):
        for _ in range(minutes):
            self.clock.advance(60)
            await self.engine._tick_while_thinking(S)

    async def test_finished_work_waiting_for_inference_is_not_advanced_or_redated(self):
        self.set_midnight_clock()
        started = self.clock.now()
        action_id = await self.start()
        await self.think(5)
        async with self.engine.session_state(S) as state:
            self.assertEqual(2, state.current_action["elapsed_ticks"])
            self.assertEqual(started + 120, state.current_action["progress_at"])
            self.assertEqual({}, state.life_interests)
        self.clock.advance(60)
        async with self.engine.session_state(S) as state:
            await self.engine._tick_continuous(state, self.engine.node(state.node_id), TickOutcome(S))
            self.assertIsNone(state.current_action)
            interest = state.life_interests[action_id]
            self.assertEqual(2, interest["minutes"])
            self.assertEqual(started + 120, interest["last_at"])
            self.assertEqual(["2026-11-02"], interest["days"])
            events = await self.db.call("query_events", session_id=S, event_type="action_done")
            self.assertEqual(2, events[0]["detail"]["elapsed_ticks"])
        self.assertEqual([], self.llm.calls)
        self.assertEqual([], self.messenger.sent)

    async def test_pending_matter_work_is_assigned_to_its_earned_day(self):
        self.set_midnight_clock()
        started = self.clock.now()
        action_id = self.configure_action(name="读第一章", category="continuous", llm_level="template",
            template="", duration_mode="fixed", duration=120, life_role="interest")
        async with self.engine.session_state(S) as state:
            state.state, state.energy, state.life_updated_at = "idle", .9, started
            _, persona = await self.engine._journal_persona(S)
            item = self.engine.add_life_matter(state, "读完第一章", persona_key=persona)
            item["needs_replan"] = False
            item["steps"] = [{"id": "chapter", "text": "读第一章", "goal": item["text"],
                "action": action_id, "node": "study", "minutes": 2, "status": "pending"}]
            await self.engine._start_matter_step(state, item)
            identifier = item["id"]
        await self.think(5)
        async with self.engine.session_state(S) as state:
            item = matters.find(state, identifier)
            self.assertEqual(2, self.engine.settle_matter_work(state, state.current_action))
            self.assertEqual("2026-11-02", item["work_day"])
            self.assertEqual(2, item["worked_minutes"])
            self.assertEqual(started + 120, item["steps"][0]["bookmark"]["at"])
            self.assertEqual(0, self.engine.settle_matter_work(state, state.current_action))
        self.assertEqual([], self.llm.calls)

    async def test_online_progress_rejoins_a_released_decision_without_discard(self):
        action_id = await self.start(seconds=600)
        token = LIFE_WORK.set(True)
        try:
            async with self.engine.session_state(S) as state:
                try:
                    async with released_life_lock(self.engine):
                        await asyncio.create_task(self.think(1))
                except LifeWorkDiscarded:
                    self.fail("online progress alone must not discard a prepared decision")
                self.assertEqual(1, state.current_action["elapsed_ticks"])
                self.assertEqual(1, state.current_action["progress_ticks"])
                self.assertEqual(self.clock.now(), state.current_action["progress_at"])
                self.engine.settle_life_practice(state, state.current_action)
                self.assertEqual(1, state.life_interests[action_id]["minutes"])
        finally:
            LIFE_WORK.reset(token)

    def test_unknown_or_mismatched_old_progress_keeps_the_observed_time(self):
        for extra in ({}, {"progress_at": 10, "progress_ticks": 1},
                      {"progress_at": 101, "progress_ticks": 2},
                      {"progress_at": "bad", "progress_ticks": 2},
                      {"progress_at": float("nan"), "progress_ticks": 2}):
            with self.subTest(extra=extra):
                self.assertEqual(100, action_progress_time({"elapsed_ticks": 2, **extra}, 100))
        self.assertEqual(10, action_progress_time({"elapsed_ticks": 2, "progress_at": 10,
                                                   "progress_ticks": 2}, 100))


class IdleProgressTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    async def start(self, *, legacy=False, seconds=60):
        self.clock._now = datetime(2026, 11, 2, 23, 58, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        for action in self.engine.world.actions:
            if action.idle:
                action.enabled = False
        self.engine.world.actions.append(ActionDef(id="quiet_clock_v", name="练习素描",
            category="continuous", idle=True, life_role="interest", duration_mode="fixed", duration=seconds))
        self.engine.world.idle_life.duration_min_minutes = seconds // 60
        self.engine.world.idle_life.duration_max_minutes = seconds // 60
        async with self.engine.session_state(S) as state:
            state.node_id, state.state, state.energy = "study", "idle", .9
            state.current_action = state.current_plan = None
            state.life_updated_at = self.clock.now()
            self.engine._tick_idle_life(state)
            if legacy:
                state.idle_activity.pop("progress_at", None)
                state.idle_activity.pop("progress_ticks", None)
        return self.clock.now()

    async def think(self, minutes):
        for _ in range(minutes):
            self.clock.advance(60)
            await self.engine._tick_while_thinking(S)

    async def test_expired_idle_keeps_its_original_day_and_old_tick_clock(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                started = await self.start(legacy=legacy)
                async with self.engine.session_state(S) as state:
                    state.life_interests = {}
                await self.think(8)
                async with self.engine.session_state(S) as state:
                    self.assertEqual({}, state.life_interests)
                    self.engine._settle_idle_practice(state)
                    interest = state.life_interests["quiet_clock_v"]
                    self.assertEqual(1, interest["minutes"])
                    self.assertEqual(started + 60, interest["last_at"])
                    self.assertEqual(["2026-11-02"], interest["days"])
        self.assertEqual([], self.llm.calls)
        self.assertEqual([], self.messenger.sent)

    async def test_later_offline_gap_does_not_move_earned_idle_into_the_new_day(self):
        started = await self.start(legacy=True)
        await self.think(8)
        self.clock.advance(3600)
        async with self.engine.session_state(S) as state:
            self.engine.reconcile_life_time(state)
            self.assertEqual(started + 60, state.life_interests["quiet_clock_v"]["last_at"])
            self.assertEqual(1, state.life_interests["quiet_clock_v"]["minutes"])
            self.assertEqual({}, state.idle_activity)

    async def test_visit_arrival_caps_an_old_idle_using_the_current_tick_clock(self):
        started = await self.start(legacy=True)
        await self.think(2)
        async with self.engine.session_state(S) as state:
            self.engine._settle_idle_practice(state, at=started + 90)
            self.assertEqual(started + 60, state.life_interests["quiet_clock_v"]["last_at"])

    async def test_reconciling_visit_gap_does_not_credit_missing_idle_ticks(self):
        previous = await self.start()
        self.clock.advance(3600)
        async with self.engine.session_state(S) as state:
            self.engine._reconcile_visit_time(state, previous, self.clock.now(),
                [(previous, self.clock.now())], visiting=False)
            self.assertEqual({}, state.life_interests)
            self.assertEqual({}, state.idle_activity)
            self.assertGreater(state.world_time, 0)

    async def test_partial_idle_keeps_current_online_time(self):
        started = await self.start(seconds=600)
        await self.think(3)
        async with self.engine.session_state(S) as state:
            self.engine._settle_idle_practice(state)
            interest = state.life_interests["quiet_clock_v"]
            self.assertEqual(3, interest["minutes"])
            self.assertEqual(started + 180, interest["last_at"])
            self.assertEqual(["2026-11-02", "2026-11-03"], interest["days"])
