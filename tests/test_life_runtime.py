"""Runtime acceptance: fair work, reloads, sleep accounting and event isolation."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.engine import TickOutcome
from core.engine.background import BackgroundMixin
from core.events import Decision, EventPackage
from core.planner import create_plan
from core.state import WorldState
from tests import test_engine as fixture
from tests import test_life_continuity as continuity
from tests import test_life_matters as matters_fixture

SESSION = fixture.SESSION


class BackgroundFairnessTests(unittest.IsolatedAsyncioTestCase):
    def queue(self, characters=20):
        engine = BackgroundMixin()
        engine.world = SimpleNamespace(limits=SimpleNamespace(background_concurrency=2))
        engine.tick_session_ids = lambda: list(range(characters))
        engine._log = lambda *args: None
        self.addAsyncCleanup(engine.close_background)
        return engine

    async def test_every_character_and_job_family_gets_a_turn_under_pressure(self):
        engine = self.queue()
        release, started = asyncio.Event(), asyncio.Event()
        seen, active, peak = [], 0, 0

        async def work(key):
            nonlocal active, peak
            active += 1
            peak = max(active, peak)
            started.set()
            try:
                await release.wait()
                seen.append(key)
            finally:
                active -= 1

        keys = ["weather", "daily_schedule", "profile_sync", "calendar_diary"] + [
            f"{kind}:{i}" for kind in (
                "life_seed", "matter", "memory", "decision", "social", "chat_browse", "participation"
            ) for i in range(20)]
        tasks = [engine.queue_background(key, lambda k=key: work(k)) for key in keys]
        self.assertTrue(all(task is not None for task in tasks), "fixed admission order must not discard the tail")
        await started.wait()
        self.assertIs(engine.queue_background(keys[0], lambda: work("duplicate")), tasks[0])
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 3)
        self.assertEqual(seen, keys)
        self.assertLessEqual(peak, 2, "bound running work as well as provider calls")
        self.assertEqual(engine._background_jobs, {})

    async def test_shutdown_cancels_waiting_work_without_starting_it(self):
        engine = self.queue(characters=1)
        release = asyncio.Event()
        work = AsyncMock(side_effect=release.wait)
        tasks = [engine.queue_background(str(i), work) for i in range(8)]
        await asyncio.sleep(0)
        await engine.close_background()
        self.assertTrue(all(task.done() for task in tasks))
        self.assertLessEqual(work.await_count, 2)
        self.assertEqual(engine._background_jobs, {})
        self.assertIsNone(engine.queue_background("late", work))

    async def test_waiting_jobs_honor_a_lowered_concurrency_limit(self):
        engine = self.queue(characters=1)
        started = [asyncio.Event() for _ in range(3)]
        release = [asyncio.Event() for _ in range(3)]
        async def work(index):
            started[index].set()
            await release[index].wait()
        tasks = [engine.queue_background(str(i), lambda i=i: work(i)) for i in range(3)]
        await asyncio.wait_for(started[1].wait(), 1)
        engine.world.limits.background_concurrency = 1
        release[0].set()
        await tasks[0]
        self.assertFalse(started[2].is_set())
        release[1].set()
        await asyncio.wait_for(started[2].wait(), 1)
        release[2].set()
        await asyncio.gather(*tasks)

    async def test_cancelling_the_first_waiter_does_not_block_later_jobs(self):
        engine = self.queue(characters=1)
        engine.world.limits.background_concurrency = 1
        release, entered = asyncio.Event(), asyncio.Event()
        async def block():
            entered.set()
            await release.wait()
        first = engine.queue_background("first", block)
        await entered.wait()
        cancelled = engine.queue_background("cancelled", AsyncMock())
        last_work = AsyncMock()
        last = engine.queue_background("last", last_work)
        await asyncio.sleep(0)
        cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)
        release.set()
        await asyncio.wait_for(asyncio.gather(first, last), 1)
        last_work.assert_awaited_once()


class LifeRuntimeTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = matters_fixture.MattersAcceptanceTests.asyncSetUp
    ctx = fixture.EngineTestCase.ctx
    add_group = fixture.EngineTestCase.add_group
    prepare = continuity.LifeContinuityTests.prepare

    async def test_self_start_retries_transient_failure_after_reload(self):
        async with self.engine.session_state(SESSION) as state:
            state.world_time, state.state, state.energy = 600, "idle", .9
        self.engine._ask_judge = AsyncMock(side_effect=[None, '{"text":"慢慢读完一本书","motivation":"想知道结局"}'])
        await self.engine.seed_life_matter(SESSION)
        self.engine.reload_config()
        await self.engine.seed_life_matter(SESSION)
        self.assertEqual(self.engine._ask_judge.await_count, 1)
        self.clock.advance(max(15, self.engine.world.limits.life_matter_interval_minutes) * 60)
        await self.engine.seed_life_matter(SESSION)
        saved = await self.engine.load_state(SESSION, cold_start=False)
        self.assertEqual(len(saved.life_matters), 1)
        self.assertEqual(saved.life_matters[0]["text"], "慢慢读完一本书")
        self.assertEqual(self.engine._ask_judge.await_count, 2)

    async def test_self_start_caps_failures_but_accepts_no_new_idea(self):
        async with self.engine.session_state(SESSION) as state:
            state.world_time, state.state, state.energy = 600, "idle", .9
        self.engine._ask_judge = AsyncMock(return_value="invalid")
        for _ in range(6):
            await self.engine.seed_life_matter(SESSION)
            self.clock.advance(max(15, self.engine.world.limits.life_matter_interval_minutes) * 60)
        self.assertEqual(self.engine._ask_judge.await_count, 3)
        self.clock.advance(86400)
        self.engine._ask_judge = AsyncMock(return_value='{"text":""}')
        for _ in range(2):
            await self.engine.seed_life_matter(SESSION)
            self.clock.advance(3600)
        self.engine._ask_judge.assert_awaited_once()

    async def test_waking_an_awake_person_never_cancels_her_work(self):
        state, _ = await self.prepare()
        await self.engine.save_state(state)
        before = await self.engine.load_state(SESSION, cold_start=False)
        self.assertFalse(await self.engine.wake_up(SESSION))
        after = await self.engine.load_state(SESSION, cold_start=False)
        self.assertEqual(after.current_action, before.current_action)
        self.assertEqual(after.current_plan, before.current_plan)
        self.assertEqual(after.no_sleep_until, before.no_sleep_until)

    async def test_confirmed_promise_stops_its_own_preparation_and_keeps_effort(self):
        state, item = await self.prepare()
        item.update(kind="promise", who="42")
        done = self.engine.mark_own_topic_done(state, user_id="42", matter_id=item["id"],
                                               evidence="真实工具已发送", confirmed=True)
        self.assertEqual(done, 1)
        self.assertEqual(item["steps"][0].get("worked_minutes"), 2)
        self.assertIsNone(state.current_action)
        self.assertIsNone(state.current_plan)
        self.assertEqual(item["status"], "completed")

    async def test_confirming_another_promise_does_not_stop_current_work(self):
        state, item = await self.prepare()
        other = self.engine.add_life_matter(state, "给42的照片", who="42", kind="promise")
        before = state.current_action
        self.engine.mark_own_topic_done(state, user_id="42", matter_id=other["id"],
                                       evidence="真实工具已发送", confirmed=True)
        self.assertIs(state.current_action, before)
        self.assertEqual(state.current_action["matter_id"], item["id"])

    async def test_late_partial_receipt_cannot_reopen_a_completed_matter(self):
        state, item = await self.prepare()
        action = state.current_action
        item.update(status="completed", progress=100)
        self.engine._record_matter_action(state, action)
        self.assertEqual(item["status"], "completed")
        self.assertEqual(item["progress"], 100)

    async def test_world_reload_keeps_partial_project_work(self):
        state, item = await self.prepare()
        await self.engine.save_state(state)
        self.engine.world.actions = [a for a in self.engine.world.actions if a.id != "read"]
        await self.engine.repair_states_after_config_change()
        saved = await self.engine.load_state(SESSION, cold_start=False)
        item = next(m for m in saved.life_matters if m["id"] == item["id"])
        self.assertEqual(item["steps"][0].get("worked_minutes"), 2)
        self.assertIsNone(saved.current_action)

    async def test_repair_remaps_running_step_after_removing_an_earlier_step(self):
        state, _ = await self.prepare(running=False)
        plan = create_plan(steps=[{"action": "obsolete"}, {"action": "read"}, {"action": "think"}], world_time=10)
        plan["steps"][0]["status"] = "done"
        plan["current_step"] = 1
        state.current_plan = plan
        await self.engine._tick_plan(state, self.engine.node("study"), TickOutcome(SESSION), depth=0)
        await self.engine.save_state(state)
        await self.engine.repair_states_after_config_change()
        saved = await self.engine.load_state(SESSION, cold_start=False)
        self.assertEqual(saved.current_plan["current_step"], 0)
        self.assertEqual(saved.current_action["plan_index"], 0)
        action = saved.current_action
        action["elapsed_ticks"] = action["duration_ticks"]
        await self.engine._finish_action(saved, self.engine.node("study"), TickOutcome(SESSION), action)
        self.assertEqual(saved.current_plan["current_step"], 1)
        self.assertEqual(saved.current_plan["steps"][1]["status"], "pending")

    async def test_offline_sleep_recovers_energy_and_cools_curiosity(self):
        state = WorldState(SESSION, node_id="study", state="sleeping", energy=.05, curiosity=.9)
        state.life_updated_at = self.clock.now()
        state.current_action = {"type": "sleep", "elapsed_ticks": 0, "duration_ticks": 480}
        self.clock.advance(3 * 3600)
        elapsed = self.engine.reconcile_life_time(state)
        await self.engine._tick_continuous(state, self.engine.node("study"), TickOutcome(SESSION))
        self.engine.dynamics.tick(state, node=self.engine.node("study"), elapsed_seconds=elapsed,
                                  world=self.engine.world, now=self.clock.now())
        self.assertGreater(state.energy, .2)
        self.assertLess(state.curiosity, .8)
        self.assertEqual(state.current_action["elapsed_ticks"], 180)
        self.assertEqual(self.messenger.sent, [])

    async def test_offline_wake_separates_rest_from_awake_time(self):
        state = WorldState(SESSION, node_id="study", state="sleeping", energy=.05, curiosity=.9)
        state.life_updated_at = self.clock.now()
        state.current_action = {"type": "sleep", "elapsed_ticks": 0, "duration_ticks": 60}
        self.clock.advance(3 * 3600)
        elapsed = self.engine.reconcile_life_time(state)
        self.assertEqual(elapsed, 2 * 3600)
        action = state.current_action
        await self.engine._tick_continuous(state, self.engine.node("study"), TickOutcome(SESSION))
        self.assertEqual(action["elapsed_ticks"], 60)
        self.assertIsNone(state.current_action)
        self.assertEqual(state.sleep_history[-1]["ended_at"], self.clock.now() - 2 * 3600)
        self.assertEqual(state.sleep_rhythm["last_wake_at"], self.clock.now() - 2 * 3600)
        self.assertEqual(self.messenger.sent, [])

    async def test_offline_sleep_does_not_apply_minute_effects_twice(self):
        definition = self.engine.world.action_map()["sleep"]
        definition.on_complete.effects = {}
        definition.on_complete.effects_per_minute = {"energy": "+0.001"}
        self.engine.dynamics.config.sleep_energy_recovery_per_min = 0
        state = WorldState(SESSION, node_id="study", state="sleeping", energy=.1)
        state.life_updated_at = self.clock.now()
        state.current_action = {"type": "sleep", "elapsed_ticks": 0, "duration_ticks": 5}
        self.clock.advance(600)
        self.engine.reconcile_life_time(state)
        self.assertAlmostEqual(state.energy, .105)
        await self.engine._tick_continuous(state, self.engine.node("study"), TickOutcome(SESSION))
        self.assertAlmostEqual(state.energy, .105)

    async def test_event_silence_cannot_mute_another_concurrent_character(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(*args, **kwargs):
            entered.set()
            await release.wait()
        self.engine._execute_actions = slow
        event_state = WorldState(SESSION, node_id="study")
        decision = Decision(actions=[{"type": "read"}])
        task = asyncio.create_task(self.engine._run_event_action(event_state, self.engine.node("study"),
            TickOutcome(SESSION), {}, EventPackage(title="查阅资料"), decision))
        self.addAsyncCleanup(lambda: asyncio.gather(task, return_exceptions=True))
        try:
            await entered.wait()
            other = WorldState("other", node_id="study", state="reading")
            action = {"type": "read", "elapsed_ticks": 5, "duration_ticks": 5}
            other.current_action = action
            self.engine.world.action_map()["read"].on_complete.trigger = "llm_followup"
            self.engine._llm_followup = AsyncMock(return_value=False)
            await self.engine._finish_action(other, self.engine.node("study"), TickOutcome(SESSION), action)
            self.engine._llm_followup.assert_awaited_once()
        finally:
            release.set()
            await task

    async def test_event_continuous_activity_is_not_reported_as_finished(self):
        state, _ = await self.prepare(running=False)
        self.engine.world.action_map()["read"].on_complete.trigger = "llm_followup"
        thread = {}
        await self.engine._run_event_action(state, self.engine.node("study"), TickOutcome(SESSION),
            thread, EventPackage(title="查阅资料"), Decision(actions=[{"type": "read"}]))
        self.assertIsNotNone(state.current_action)
        self.assertIn("进行中", " ".join(thread["observations"]))
        self.engine._llm_followup = AsyncMock(return_value=False)
        action = state.current_action
        action["elapsed_ticks"] = action["duration_ticks"]
        await self.engine._finish_action(state, self.engine.node("study"), TickOutcome(SESSION), action)
        self.engine._llm_followup.assert_not_awaited()
