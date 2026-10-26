"""Real lifecycle regressions: slow reviews, late plans and interrupted practice."""
import asyncio
import json
import unittest
from datetime import datetime
from unittest.mock import AsyncMock

from core import life_matters as matters
from core.engine import TickOutcome
from core.engine.background import CRITICAL_TICK
from core.ports import LLMReply
from tests import test_engine as fixture

SESSION = fixture.SESSION


class AutonomousLifeIntegrityTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp
    add_group = fixture.EngineTestCase.add_group

    async def state(self):
        return await self.engine.load_state(SESSION, cold_start=False)

    async def sleeping(self):
        async with self.engine.session_state(SESSION) as state:
            state.state, state.world_time, state.sleep_started_at = "sleeping", 100, 70
            state.current_action = {"type": "sleep", "duration_ticks": 480, "elapsed_ticks": 30}
            self.engine._start_sleep_rhythm(state, "sleep", 480)
            await self.engine.maybe_consolidate(state)
        self.clock.advance(21 * 60)
        async with self.engine.session_state(SESSION) as state:
            state.life_updated_at = self.clock.now()
        self.engine.memory.remember(session_id=SESSION, persona_id="", node_id="study", content="读完了风车的维修说明")

    async def test_slow_sleep_review_does_not_block_the_clock_or_state_edits(self):
        await self.sleeping()
        entered, release = asyncio.Event(), asyncio.Event()

        async def model(**kwargs):
            entered.set()
            await release.wait()
            return LLMReply(ok=True, text='{"dream":"风车在云间转动"}')

        self.engine.consolidate_llm.generate = AsyncMock(side_effect=model)
        for name in ("maybe_refresh_weather", "auto_generate_schedule_scopes", "maybe_sync_profile_weaver", "maintain_social_learning"):
            setattr(self.engine, name, AsyncMock())
        self.addAsyncCleanup(self.engine.close_background)
        task = asyncio.create_task(self.engine.tick())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await asyncio.wait_for(asyncio.shield(task), .5)
            async with asyncio.timeout(.5):
                async with self.engine.session_state(SESSION) as state:
                    state.world_time += 7
                    state.energy = .37
            expected = (await self.state()).world_time
        finally:
            release.set()
            await task
            jobs = list(getattr(self.engine, "_background_jobs", {}).values())
            if jobs:
                await asyncio.gather(*jobs)
        after = await self.state()
        self.assertEqual(after.world_time, expected)
        self.assertEqual(after.energy, .37)
        self.assertEqual(after.last_dream, "风车在云间转动")
        self.assertGreater(after.consolidate_cursor, 0)

    async def test_failed_review_does_not_consume_the_first_sleep_pass(self):
        await self.sleeping()
        self.engine.consolidate_now = AsyncMock(side_effect=[{"ok": False}, {"ok": True}])
        async with self.engine.session_state(SESSION) as state:
            await self.engine.maybe_consolidate(state)
        self.clock.advance(6 * 60)
        async with self.engine.session_state(SESSION) as state:
            await self.engine.maybe_consolidate(state)
        self.assertEqual(self.engine.consolidate_now.await_count, 2)

    async def pending_matter(self, *, review=False, change=None):
        async with self.engine.session_state(SESSION) as state:
            state.state, state.energy = "idle", .9
            item = self.engine.add_life_matter(state, "慢慢读完风车的维修说明")
            if review:
                item.update(needs_review=True, steps=[{"id": "done", "text": "读完说明", "status": "done",
                    "action": "read", "node": "study", "minutes": 5, "result": {"result": "读了五分钟说明"}}])
            mid = item["id"]
        entered, release = asyncio.Event(), asyncio.Event()

        async def judge(*args):
            entered.set()
            await release.wait()
            return json.dumps({"outcome": "completed", "reason": "已阅读", "steps": [
                {"text": "读第一部分", "action": "read", "node": "study", "minutes": 5, "day_offset": 0}]})

        self.engine._ask_judge = AsyncMock(side_effect=judge)
        task = asyncio.create_task(self.engine.advance_life_matters(SESSION))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            if change:
                await change()
            else:
                self.persona.text = "你是一位严谨的学者。"
        finally:
            release.set()
            await task
        return matters.find(await self.state(), mid)

    async def test_late_matter_plan_respects_the_current_persona(self):
        item = await self.pending_matter()
        self.assertFalse(item["steps"])
        self.assertIsNone((await self.state()).current_action)

    async def test_late_matter_stage_cannot_complete_another_personas_project(self):
        item = await self.pending_matter(review=True)
        self.assertNotEqual(item["status"], "completed")
        self.assertTrue(item["needs_review"])

    async def test_interrupted_reading_keeps_practice_without_completing_a_project(self):
        state = await self.state()
        action = {"type": "read", "elapsed_ticks": 20, "duration_ticks": 60}
        self.engine.pause_matter_action(state, action)
        interest = state.life_interests.get("read", {})
        self.assertEqual(interest.get("minutes"), 20)
        self.assertEqual(interest.get("sessions"), 1)
        self.engine.pause_matter_action(state, action)
        self.assertEqual(interest["minutes"], 20, "repeated interruption must not recount work")
        self.assertFalse(state.life_artifacts)

    async def test_auto_schedule_cannot_overwrite_edits_made_during_generation(self):
        raw = self.store.raw_schedules()
        raw["auto"] = {"enabled": True, "hour": 0, "per_day": 1}
        self.store.save_schedules(raw)
        self.engine.reload_config()
        entered, release = asyncio.Event(), asyncio.Event()
        today = self.engine._today_key(self.clock.now())

        async def generate(*args, **kwargs):
            entered.set()
            await release.wait()
            return {"schedules": [{"id": "draft", "date": today, "time": "23:59", "once": True,
                                   "action_chain": [{"type": "think", "content": "看看书"}]}]}

        self.engine.generate_schedules = AsyncMock(side_effect=generate)
        task = asyncio.create_task(self.engine.maybe_auto_generate_schedules())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            updated = self.store.raw_schedules()
            updated["schedules"].append({"id": "manual", "time": "18:00", "action_chain": [{"type": "read"}]})
            self.store.save_schedules(updated)
        finally:
            release.set()
            await task
        ids = {row["id"] for row in self.store.raw_schedules()["schedules"]}
        self.assertIn("manual", ids)
        self.assertNotIn("draft", ids)

    async def test_background_job_has_its_full_timeout_even_when_queued_from_a_tick(self):
        self.engine.world.limits.background_llm_timeout_seconds = 4
        self.addAsyncCleanup(self.engine.close_background)
        seen = []

        async def model(**kwargs):
            seen.append(CRITICAL_TICK.get())
            await asyncio.sleep(2.1)
            return LLMReply(ok=True, text="{}")

        self.engine.consolidate_llm.generate = AsyncMock(side_effect=model)
        token = CRITICAL_TICK.set(True)
        try:
            task = self.engine.queue_background("slow-review", lambda: self.engine._ask_consolidate(SESSION, "整理", "生活"))
        finally:
            CRITICAL_TICK.reset(token)
        self.assertEqual(await asyncio.wait_for(task, 4), "{}")
        self.assertEqual(seen, [False])
        self.assertFalse(CRITICAL_TICK.get())

    async def test_successful_sleep_pass_and_retry_time_survive_reload(self):
        await self.sleeping()
        self.engine.consolidate_now = AsyncMock(return_value={"ok": True})
        async with self.engine.session_state(SESSION) as state:
            await self.engine.maybe_consolidate(state)
        self.engine.reload_config()
        self.clock.advance(6 * 60)
        async with self.engine.session_state(SESSION) as state:
            await self.engine.maybe_consolidate(state)
        self.assertEqual(self.engine.consolidate_now.await_count, 1)
        self.clock.advance(5 * 3600)
        async with self.engine.session_state(SESSION) as state:
            await self.engine.maybe_consolidate(state)
        self.engine.reload_config()
        self.clock.advance(3600)
        async with self.engine.session_state(SESSION) as state:
            await self.engine.maybe_consolidate(state)
        self.assertEqual(self.engine.consolidate_now.await_count, 2)

    async def test_failure_retries_without_advancing_the_success_cursor(self):
        await self.sleeping()
        self.engine._ask_consolidate = AsyncMock(side_effect=[None, '{"dream":"云间的风车"}'])
        result = await self.engine.consolidate_session(SESSION, automatic=True)
        self.assertFalse(result["ok"])
        after = await self.state()
        self.assertEqual(after.consolidate_cursor, 0)
        self.assertEqual(after.sleep_rhythm["review"]["passes"], 0)
        self.engine.reload_config()
        self.assertIsNone(await self.engine.consolidate_session(SESSION, automatic=True))
        self.clock.advance(301)
        self.assertTrue((await self.engine.consolidate_session(SESSION, automatic=True))["ok"])
        self.assertEqual(self.engine._ask_consolidate.await_count, 2)
        self.assertEqual((await self.state()).sleep_rhythm["review"]["passes"], 1)

    async def pending_review(self, *, change=None, automatic=True, dry_run=False):
        await self.sleeping()
        entered, release = asyncio.Event(), asyncio.Event()

        async def model(*args):
            entered.set()
            await release.wait()
            return '{"dream":"梦中的风车飞上了天"}'

        self.engine._ask_consolidate = AsyncMock(side_effect=model)
        task = asyncio.create_task(self.engine.consolidate_session(SESSION, automatic=automatic, dry_run=dry_run))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            if change:
                await change()
        finally:
            release.set()
            result = await asyncio.wait_for(task, 2)
        return result, await self.state()

    async def test_sleep_review_cannot_commit_after_waking(self):
        async def change():
            async with self.engine.session_state(SESSION) as state:
                state.state, state.current_action = "idle", None
        result, state = await self.pending_review(change=change)
        self.assertIsNone(result)
        self.assertEqual(state.consolidate_cursor, 0)
        self.assertFalse(state.last_dream)
        self.assertFalse(self.db.raw.query_memories(memory_type="dream"))

    async def test_sleep_review_cannot_commit_into_the_next_sleep(self):
        async def change():
            self.clock.advance(1)
            async with self.engine.session_state(SESSION) as state:
                state.sleep_started_at += 10
                self.engine._start_sleep_rhythm(state, "sleep", 480)
        result, state = await self.pending_review(change=change)
        self.assertIsNone(result)
        self.assertFalse(state.last_dream)
        self.assertFalse(self.db.raw.query_memories(memory_type="dream"))

    async def test_manual_review_merges_only_its_fields(self):
        async def change():
            async with self.engine.session_state(SESSION) as state:
                state.state, state.current_action, state.world_time = "idle", None, 700
                state.energy, state.pending_arrival = .42, True
        result, state = await self.pending_review(change=change, automatic=False)
        self.assertTrue(result["ok"])
        self.assertEqual((state.state, state.current_action, state.world_time, state.energy, state.pending_arrival),
                         ("idle", None, 700, .42, True))
        self.assertEqual(state.last_dream, "梦中的风车飞上了天")

    async def test_manual_preview_does_not_consume_the_automatic_review(self):
        result, state = await self.pending_review(automatic=False, dry_run=True)
        self.assertTrue(result["ok"])
        self.assertTrue(result["dry_run"])
        self.assertEqual(state.sleep_rhythm["review"]["passes"], 0)
        self.assertEqual(state.consolidate_cursor, 0)
        self.assertFalse(state.last_dream)
        self.assertFalse(self.db.raw.query_memories(memory_type="dream"))

    async def test_overlapping_manual_and_automatic_review_share_one_model_call(self):
        await self.sleeping()
        entered, release = asyncio.Event(), asyncio.Event()

        async def model(*args):
            entered.set()
            await release.wait()
            return '{"dream":"风车"}'

        self.engine._ask_consolidate = AsyncMock(side_effect=model)
        manual = asyncio.create_task(self.engine.consolidate_session(SESSION))
        await asyncio.wait_for(entered.wait(), 2)
        automatic = asyncio.create_task(self.engine.consolidate_session(SESSION, automatic=True))
        release.set()
        results = await asyncio.gather(manual, automatic)
        self.assertTrue(results[0]["ok"])
        self.assertIsNone(results[1])
        self.assertEqual(self.engine._ask_consolidate.await_count, 1)

    async def test_late_plan_and_stage_are_discarded_after_world_reload(self):
        for review in (False, True):
            with self.subTest(review=review):
                async def change():
                    self.engine.reload_config()
                item = await self.pending_matter(review=review, change=change)
                self.assertNotEqual(item["status"], "completed")
                if review:
                    self.assertTrue(item["needs_review"])
                else:
                    self.assertFalse(item["steps"])
                async with self.engine.session_state(SESSION) as state:
                    state.life_matters.clear()

    async def test_unchanged_stage_still_completes_and_uses_the_base_persona(self):
        async def unchanged():
            pass
        item = await self.pending_matter(review=True, change=unchanged)
        self.assertEqual(item["status"], "completed")
        prompt = json.loads(self.engine._ask_judge.call_args.args[2])
        self.assertIn(self.persona.text, prompt["persona"])

    async def test_late_plan_is_not_started_when_rest_begins(self):
        async def change():
            async with self.engine.session_state(SESSION) as state:
                state.state = "drowsy"
        item = await self.pending_matter(change=change)
        self.assertTrue(item["steps"])
        self.assertIsNone((await self.state()).current_action)

    async def test_practice_is_split_at_midnight_and_not_recounted_on_completion(self):
        self.clock._now = datetime(2026, 10, 20, 0, 10).timestamp()
        state = await self.state()
        action = {"type": "read", "elapsed_ticks": 20, "duration_ticks": 60}
        self.engine.pause_matter_action(state, action)
        interest = state.life_interests["read"]
        self.assertEqual(interest["days"], ["2026-10-19", "2026-10-20"])
        self.assertEqual([row["minutes"] for row in interest["episodes"]], [10, 10])
        self.clock.advance(300)
        action["elapsed_ticks"] = 25
        self.engine._record_matter_action(state, action)
        self.assertEqual(interest["minutes"], 25)
        self.assertEqual(interest["sessions"], 1)
        self.assertEqual([row["minutes"] for row in interest["episodes"]], [10, 15])
        self.assertFalse(matters.stable_interests(state))

    async def test_practice_is_split_across_the_actual_time_periods(self):
        self.clock._now = datetime(2026, 10, 20, 11, 10).timestamp()
        state = await self.state()
        action = {"type": "read", "elapsed_ticks": 20, "duration_ticks": 60}
        self.engine.pause_matter_action(state, action)
        self.assertEqual([(row["period"], row["minutes"]) for row in state.life_interests["read"]["episodes"]],
                         [("早晨", 10), ("白天", 10)])

    async def test_repeated_ticks_form_one_practice_session_and_one_episode(self):
        self.clock._now = datetime(2026, 10, 20, 12).timestamp()
        state = await self.state()
        state.current_action = {"type": "read", "elapsed_ticks": 0, "duration_ticks": 60}
        for _ in range(20):
            self.clock.advance(60)
            await self.engine._tick_continuous(state, self.engine.node("study"), TickOutcome(SESSION))
        interest = state.life_interests["read"]
        self.assertEqual((interest["minutes"], interest["sessions"], len(interest["episodes"])), (20, 1, 1))
        self.engine.pause_matter_action(state, state.current_action)
        self.assertEqual(interest["minutes"], 20)

    async def test_offline_gap_does_not_invent_practice(self):
        state = await self.state()
        state.current_action = {"type": "read", "elapsed_ticks": 20, "duration_ticks": 60}
        state.life_updated_at = self.clock.now()
        self.clock.advance(3 * 3600)
        self.engine.reconcile_life_time(state)
        self.assertIsNone(state.current_action)
        self.assertEqual(state.life_interests["read"]["minutes"], 20)
        self.assertEqual(state.life_interests["read"]["last_at"], self.clock.now() - 3 * 3600)

    async def test_sleep_and_external_actions_do_not_create_personal_practice(self):
        state = await self.state()
        for kind in ("sleep", "nap", "walk_to", "search_web", "take_photo"):
            self.engine.pause_matter_action(state, {"type": kind, "elapsed_ticks": 20})
        self.assertFalse(state.life_interests)

    async def test_quiet_idle_life_also_accumulates_actual_experience(self):
        state = await self.state()
        idle = next(action for action in self.engine.world.actions if action.idle and action.enabled)
        state.state, state.world_time = "idle", 100
        state.idle_activity = {"id": idle.id, "node_id": state.node_id, "start_tick": 80, "until_tick": 120}
        self.engine._tick_idle_life(state)
        self.assertEqual(state.life_interests[idle.id]["minutes"], 20)
        self.engine._tick_idle_life(state)
        self.assertEqual(state.life_interests[idle.id]["minutes"], 20)

    async def test_offline_gap_does_not_accumulate_quiet_idle_experience(self):
        state = await self.state()
        idle = next(action for action in self.engine.world.actions if action.idle and action.enabled)
        state.state, state.world_time = "idle", 100
        state.idle_activity = {"id": idle.id, "node_id": state.node_id, "start_tick": 80, "until_tick": 120}
        state.life_updated_at = self.clock.now()
        self.clock.advance(3 * 3600)
        self.engine.reconcile_life_time(state)
        self.assertEqual(state.life_interests[idle.id]["minutes"], 20)
        self.assertFalse(state.idle_activity)

    async def pending_schedule(self, change):
        raw = self.store.raw_schedules()
        raw["auto"] = {"enabled": True, "hour": 0, "per_day": 1}
        self.store.save_schedules(raw)
        self.engine.reload_config()
        entered, release = asyncio.Event(), asyncio.Event()
        today = self.engine._today_key(self.clock.now())

        async def generate(*args, **kwargs):
            entered.set()
            await release.wait()
            return {"schedules": [{"id": "old_draft", "date": today, "time": "23:59",
                                   "action_chain": [{"type": "think", "content": "看看书"}]}]}

        self.engine.generate_schedules = AsyncMock(side_effect=generate)
        task = asyncio.create_task(self.engine.maybe_auto_generate_schedules())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await change()
        finally:
            release.set()
            result = await task
        return result, today

    async def test_auto_schedule_discards_drafts_after_persona_changes(self):
        async def change():
            self.persona.text = "你是一位严谨的学者。"
        result, today = await self.pending_schedule(change)
        self.assertEqual(result, "")
        self.assertNotEqual(self.db.raw.kv_get("schedule.auto_day:global"), today)
        self.assertNotIn("old_draft", {row["id"] for row in self.store.raw_schedules()["schedules"]})

    async def test_auto_schedule_uses_the_initial_persona_across_an_aba_switch(self):
        raw = self.store.raw_schedules()
        raw["auto"] = {"enabled": True, "hour": 0, "per_day": 1}
        self.store.save_schedules(raw)
        self.engine.reload_config()
        entered, release = asyncio.Event(), asyncio.Event()
        today = self.engine._today_key(self.clock.now())
        original = self.persona.text
        snapshots = []

        async def generate(*args, **kwargs):
            snapshots.append(kwargs.get("persona_snapshot"))
            entered.set()
            await release.wait()
            return {
                "schedules": [
                    {
                        "id": "snapshot_draft",
                        "date": today,
                        "time": "23:59",
                        "action_chain": [{"type": "think", "content": "看看书"}],
                    }
                ]
            }

        self.engine.generate_schedules = AsyncMock(side_effect=generate)
        task = asyncio.create_task(self.engine.maybe_auto_generate_schedules())
        await asyncio.wait_for(entered.wait(), 2)
        self.persona.text = "临时切换后的人设"
        self.persona.text = original
        release.set()
        result = await task
        self.assertIn("1 条", result)
        self.assertEqual(snapshots, [original])
        self.assertIn("snapshot_draft", {row["id"] for row in self.store.raw_schedules()["schedules"]})

    async def test_auto_schedule_discards_drafts_when_the_world_reloads(self):
        async def change():
            self.engine.reload_config()
        result, _ = await self.pending_schedule(change)
        self.assertEqual(result, "")
        self.assertNotIn("old_draft", {row["id"] for row in self.store.raw_schedules()["schedules"]})

    async def test_auto_schedule_discards_drafts_when_generation_crosses_midnight(self):
        async def change():
            self.clock.advance(24 * 3600)
        result, _ = await self.pending_schedule(change)
        self.assertEqual(result, "")
        self.assertNotIn("old_draft", {row["id"] for row in self.store.raw_schedules()["schedules"]})

    async def test_disabling_auto_schedule_while_it_generates_preserves_the_setting(self):
        async def change():
            raw = self.store.raw_schedules()
            raw["auto"]["enabled"] = False
            self.store.save_schedules(raw)
        result, _ = await self.pending_schedule(change)
        self.assertEqual(result, "")
        self.assertFalse(self.store.raw_schedules()["auto"]["enabled"])

    async def test_unchanged_auto_schedule_still_commits_and_consumes_the_day(self):
        async def unchanged():
            pass
        result, today = await self.pending_schedule(unchanged)
        self.assertIn("1 条", result)
        self.assertEqual(self.db.raw.kv_get("schedule.auto_day:global"), today)
        self.assertIn("old_draft", {row["id"] for row in self.store.raw_schedules()["schedules"]})

    async def test_late_matter_plan_cannot_enter_a_different_shared_life(self):
        async def change():
            self.add_group(main=fixture.OTHER_SESSION)
        item = await self.pending_matter(change=change)
        if item:
            self.assertFalse(item["steps"])
        self.assertIsNone((await self.state()).current_action)

    async def test_late_matter_stage_cannot_complete_while_the_session_is_disabled(self):
        async def change():
            self.engine.session_config(SESSION).enabled = False
        item = await self.pending_matter(review=True, change=change)
        self.assertNotEqual(item["status"], "completed")
        self.assertTrue(item["needs_review"])

    async def test_interrupted_half_minute_ticks_are_only_counted_after_real_practice(self):
        self.engine.tick_seconds = 30
        state = await self.state()
        action = {"type": "read", "elapsed_ticks": 1, "duration_ticks": 60}
        self.engine.pause_matter_action(state, action)
        self.assertFalse(state.life_interests)
        action["elapsed_ticks"] = 2
        self.engine.pause_matter_action(state, action)
        self.assertEqual(state.life_interests["read"]["minutes"], 1)
        action["elapsed_ticks"] = 3
        self.engine.pause_matter_action(state, action)
        self.assertEqual(state.life_interests["read"]["minutes"], 1.5)

    async def test_sleep_validation_and_commit_are_coherent_with_a_concurrent_wake(self):
        await self.sleeping()
        validation, release = asyncio.Event(), asyncio.Event()
        model_returned, paused = False, False
        load_state = self.engine.load_state
        apply_full = self.engine.consolidator._apply_full
        states_at_commit = []

        async def model(*args):
            nonlocal model_returned
            model_returned = True
            return '{"dream":"天空中的风车"}'

        async def delayed_read(*args, **kwargs):
            nonlocal paused
            state = await load_state(*args, **kwargs)
            if model_returned and not paused:
                paused = True
                validation.set()
                await release.wait()
            return state

        def commit(*args, **kwargs):
            states_at_commit.append(self.db.raw.get_state(SESSION)["state"])
            return apply_full(*args, **kwargs)

        async def wake():
            async with self.engine.session_state(SESSION) as state:
                state.state, state.current_action = "idle", None

        self.engine._ask_consolidate = AsyncMock(side_effect=model)
        self.engine.load_state = delayed_read
        self.engine.consolidator._apply_full = commit
        review = asyncio.create_task(self.engine.consolidate_session(SESSION, automatic=True))
        wake_task = None
        try:
            await asyncio.wait_for(validation.wait(), 2)
            wake_task = asyncio.create_task(wake())
            await asyncio.sleep(.1)
        finally:
            release.set()
            await asyncio.wait_for(review, 2)
            if wake_task:
                await asyncio.wait_for(wake_task, 2)
        self.assertTrue(states_at_commit)
        self.assertEqual(states_at_commit, ["sleeping"], "a stale read must not validate a result after she wakes")
        self.assertEqual((await self.state()).state, "idle")

    async def test_auto_schedule_uses_a_shared_life_when_its_config_owner_is_disabled(self):
        self.add_group(main=SESSION)
        raw = self.store.raw_schedules()
        raw["auto"] = {"enabled": True, "hour": 0, "per_day": 1}
        self.store.save_scope(SESSION, self.store.raw_world(), raw, ["schedules"])
        sessions = self.store.raw_sessions()
        next(row for row in sessions["sessions"] if row["session_id"] == SESSION)["enabled"] = False
        self.store.save_sessions(sessions)
        self.engine.reload_config()
        self.engine.generate_schedules = AsyncMock(return_value={"schedules": [{
            "id": "shared_draft", "time": "23:59", "action_chain": [{"type": "think", "content": "看看书"}]}]})
        with self.engine.config_scope(fixture.OTHER_SESSION):
            self.assertEqual(self.engine.config_scope_id, SESSION)
            self.assertEqual(self.engine.state_key(SESSION), fixture.OTHER_SESSION)
            result = await self.engine.maybe_auto_generate_schedules()
            self.assertIn("1 条", result)
            self.assertIn("shared_draft", {row["id"] for row in self.engine.scoped_schedules()["schedules"]})

    async def test_partial_review_retries_only_the_failed_stage_without_consuming_a_pass(self):
        await self.sleeping()
        self.engine.profiles.touch(SESSION, "42", "小明")
        async with self.engine.session_state(SESSION) as state:
            state.recent_chat = [{"user_id": "42", "text": "我喜欢猫", "origin": SESSION}]
        self.engine._ask_consolidate = AsyncMock(side_effect=[None, '{"dream":"云间的风车"}',
            '{"facts":[{"user_id":"42","text":"喜欢猫","evidence":"我喜欢猫"}]}'])
        first = await self.engine.consolidate_session(SESSION, automatic=True)
        self.assertTrue(first["ok"], "the successful life stage is retained")
        self.assertTrue(first.get("retry_needed"), "partial failure must retry without waiting until late sleep")
        self.assertEqual((await self.state()).sleep_rhythm["review"]["passes"], 0)
        self.clock.advance(301)
        second = await self.engine.consolidate_session(SESSION, automatic=True)
        self.assertTrue(second["ok"])
        self.assertFalse(second.get("retry_needed"))
        self.assertEqual((await self.state()).sleep_rhythm["review"]["passes"], 1)
        self.assertEqual(self.engine._ask_consolidate.await_count, 3, "the successful stage must not be regenerated")
        self.assertEqual(len(self.db.raw.query_memories(memory_type="dream")), 1)
        self.assertEqual(len(self.engine.profiles.facts(SESSION, "42")), 1)
