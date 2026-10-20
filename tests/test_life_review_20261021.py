"""Regression scenarios for unattended life, lock ownership and journal recovery."""
import asyncio
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock

from core.engine.life_inference import life_work
from core.engine.life_inference import decision_state
from core.ports import LLMReply
from core.world_calendar import validate_calendar
from tests import test_engine as fixture
from tests import test_sleep_rhythm as sleep_fixture

S = fixture.SESSION


class SleepInferenceTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = sleep_fixture.SleepRhythmTests.asyncSetUp
    set_state = fixture.EngineTestCase.set_state
    get_state = fixture.EngineTestCase.get_state

    async def test_sleep_model_releases_state_lock_and_discards_changed_life(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def generate(**kwargs):
            entered.set()
            await release.wait()
            return LLMReply(ok=True, text='{"decision":"sleep","hours":8,"reason":"累了"}')

        self.llm.generate = generate
        self.engine._maybe_run_event = AsyncMock()
        self.engine._sync_nickname = AsyncMock()
        task = asyncio.create_task(self.engine._tick_session(S))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            self.assertFalse(self.engine.is_busy(S), "sleep inference must release the writer lock")
            async with asyncio.timeout(.5):
                async with self.engine.session_state(S) as state:
                    state.node_id = "window"
            release.set()
            await asyncio.wait_for(task, 2)
            state = await self.get_state()
            self.assertEqual(state.node_id, "window")
            self.assertIsNone(state.current_action)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)


class JournalRecoveryTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    async def test_daily_planning_uses_saved_calendar_and_yesterdays_diary(self):
        self.engine.life_context_text = AsyncMock(return_value="风祭；昨天把风车修到一半，今天想继续。")
        self.engine._ask_creator = AsyncMock(return_value='{"schedules":[]}')
        await self.engine.generate_schedules(2, "once")
        self.engine.life_context_text.assert_awaited_once_with(S)
        self.assertIn("昨天把风车修到一半", self.engine._ask_creator.call_args.args[2])

    async def test_disabled_life_context_stays_out_of_daily_planning(self):
        self.engine.world.life_context.enabled = False
        self.engine.calendar_for_year = AsyncMock(side_effect=AssertionError("disabled context read"))
        self.engine._ask_creator = AsyncMock(return_value='{"schedules":[]}')
        await self.engine.generate_schedules(2, "once")
        self.engine.calendar_for_year.assert_not_awaited()

    async def test_background_failure_diagnostics_hide_credentials(self):
        self.llm.generate = AsyncMock(side_effect=RuntimeError("Bearer private-secret-123 https://provider.invalid?token=private-secret-123"))
        self.assertIsNone(await self.engine._ask_llm(S, "", "next activity"))
        failures = str(self.engine._background_failures)
        self.assertNotIn("private-secret-123", failures)
        self.assertNotIn("provider.invalid", failures)

    async def test_slow_failure_backoff_starts_when_the_failure_returns(self):
        async def fail(**kwargs):
            self.clock.advance(121)
            raise RuntimeError("provider unavailable")

        self.llm.generate = AsyncMock(side_effect=fail)
        await self.engine._ask_llm(S, "", "next activity")
        await self.engine._ask_llm(S, "", "next activity")
        self.llm.generate.assert_awaited_once()
        self.assertEqual(self.engine._background_active, 0)

    async def test_automatic_schedule_respects_budget_while_manual_generation_remains_available(self):
        raw = self.store.raw_schedules()
        raw["auto"] = {"enabled": True, "hour": 0, "per_day": 1}
        self.store.save_schedules(raw)
        self.engine.reload_config()
        self.engine.world.limits.background_daily_calls = 1
        await self.db.call("reserve_background", self.engine._today_key(self.clock.now()),
                           calls=1, tokens=0, reserve=0)
        self.llm.generate = AsyncMock(return_value=LLMReply(ok=True, text='{"schedules":[]}'))
        with self.assertRaises(ValueError):
            await self.engine.maybe_auto_generate_schedules()
        self.llm.generate.assert_not_awaited()
        await self.engine.generate_schedules(1, "once")
        self.llm.generate.assert_awaited_once()

    async def test_failed_or_malformed_calendar_remains_retryable(self):
        for raw in (None, "not JSON", "{}", '{"world_type":"original","events":"broken"}'):
            with self.subTest(raw=raw):
                self.engine._ask_journal = AsyncMock(return_value=raw)
                self.assertIsNone(await self.engine.ensure_calendar(S, 2023, force=True))
                self.assertIsNone(await self.engine.calendar_for_year(S, 2023))

    async def test_explicit_empty_calendar_is_a_valid_choice(self):
        self.engine._ask_journal = AsyncMock(return_value='{"world_type":"original","events":[]}')
        self.assertIsNotNone(await self.engine.ensure_calendar(S, 2023))

    async def test_old_ambiguous_empty_calendar_can_recover_automatically(self):
        _, persona = await self.engine._journal_persona(S)
        await self.db.call("calendar_year_save", self.engine._journal_scope(), persona, 2023,
            {"year": 2023, "world_type": "original", "events": [],
             "_config_signature": self.engine._calendar_config_signature(self.engine.world.calendar, 2023)})
        self.engine._ask_journal = AsyncMock(return_value='{"world_type":"original","events":[]}')
        result = await self.engine.ensure_calendar(S, 2023)
        self.engine._ask_journal.assert_awaited_once()
        self.assertEqual(result["_generation"], "model")

    async def test_public_calendar_fallback_does_not_consume_a_successful_generation(self):
        self.engine.world.calendar.world_type = "real"
        self.engine.world.calendar.countries = ["CN"]
        self.engine.world.calendar.auto_generate = True
        self.engine._ask_journal = AsyncMock(side_effect=RuntimeError("temporary outage"))
        result = await self.engine.ensure_calendar(S, 2023, automatic=True)
        self.assertTrue(result["events"])
        self.assertEqual(result["_generation"], "public_fallback")
        self.clock.advance(601)
        self.engine._ask_journal = AsyncMock(return_value='{"world_type":"real","events":[]}')
        result = await self.engine.ensure_calendar(S, 2023, automatic=True)
        self.engine._ask_journal.assert_awaited_once()
        self.assertEqual(result["_generation"], "model")

    async def test_automatic_retry_count_resets_on_a_new_day(self):
        self.engine.world.calendar.auto_generate = True
        _, persona = await self.engine._journal_persona(S)
        key = f"calendar_auto:{self.engine._journal_scope()}:{persona}:2023"
        await self.db.call("kv_set", key, {"day": "old-day", "count": 3, "retry_at": 0})
        self.engine._ask_journal = AsyncMock(return_value=None)
        await self.engine.ensure_calendar(S, 2023, automatic=True)
        self.assertEqual((await self.db.call("kv_get", key))["count"], 1)

    async def test_calendar_cannot_save_after_the_world_settings_change(self):
        async def generate(*args, **kwargs):
            self.engine.world.calendar.enabled = False
            return '{"world_type":"original","events":[]}'

        self.engine._ask_journal = AsyncMock(side_effect=generate)
        self.assertIsNone(await self.engine.ensure_calendar(S, 2023))
        _, persona = await self.engine._journal_persona(S)
        self.assertIsNone(await self.db.call("calendar_year_get", self.engine._journal_scope(), persona, 2023))

    async def saved_yesterday(self):
        day = (self.engine.local_now().date() - timedelta(days=1)).isoformat()
        start, _ = self.engine._journal_day_bounds(day)
        _, persona = await self.engine._journal_persona(S)
        async with self.engine.session_state(S) as state:
            state.thoughts = [{"at": start.timestamp() + 10, "content": "风车还需要修整", "persona_key": persona}]
        return day, persona

    async def test_diary_draft_cannot_restore_an_experience_removed_during_generation(self):
        day, _ = await self.saved_yesterday()

        async def generate(*args, **kwargs):
            async with self.engine.session_state(S) as state:
                state.thoughts = []
            return '{"title":"风车","content":"风车还需要修整"}'

        self.engine._ask_journal = AsyncMock(side_effect=generate)
        self.assertIsNone(await self.engine.ensure_diary(S, day))
        self.assertIsNone(await self.engine.diary_for_day(S, day))

    async def test_private_promise_never_enters_a_diary_shared_with_other_sessions(self):
        day, persona = await self.saved_yesterday()
        start, _ = self.engine._journal_day_bounds(day)
        async with self.engine.session_state(S) as state:
            item = self.engine.add_life_matter(state, "私下约好一件保密的事", session=S,
                                              who="42", kind="promise", persona_key=persona)
            item["history"][0]["at"] = start.timestamp() + 30
        sources = await self.engine._diary_sources(S, day, persona)
        self.assertEqual(sources["matters"], [])

    async def test_throttled_yesterday_does_not_starve_an_older_diary(self):
        self.engine.world.calendar.auto_generate = False
        self.engine.world.diary.automatic = True
        self.engine.world.diary.write_hour = 0
        yesterday, persona = await self.saved_yesterday()
        older = (self.engine.local_now().date() - timedelta(days=2)).isoformat()
        start, _ = self.engine._journal_day_bounds(older)
        async with self.engine.session_state(S) as state:
            state.thoughts.append({"at": start.timestamp() + 10, "content": "昨天画了云", "persona_key": persona})
        with self.engine.config_scope(S):
            key = f"diary_auto:{self.engine._journal_scope()}:{persona}:{yesterday}"
        await self.db.call("kv_set", key, {"day": self.engine._today_key(self.clock.now()), "count": 3, "retry_at": 0})
        self.engine._ask_journal = AsyncMock(return_value='{"title":"云","content":"画了云"}')
        await self.engine.maintain_calendar_diaries()
        args = self.engine._ask_journal.call_args
        self.assertIsNotNone(args, "a blocked recent date must not starve other saved days")
        self.assertEqual(args.args[3]["day"], older)

    async def test_slow_character_does_not_starve_other_characters_journals(self):
        self.engine.world.calendar.auto_generate = True
        self.store.add_session(fixture.OTHER_SESSION, cold_start_node="study")
        self.engine.reload_config()
        self.engine.world.calendar.auto_generate = True
        pending = asyncio.create_task(asyncio.Event().wait())
        self.engine._life_clock_jobs = {self.engine.state_key(S): pending}
        self.engine.ensure_calendar = AsyncMock(return_value={"year": 2023})
        try:
            await self.engine.maintain_calendar_diaries()
            self.engine.ensure_calendar.assert_awaited_once_with(fixture.OTHER_SESSION, 2023, automatic=True)
            self.assertNotIn(self.engine.state_key(S), self.engine._calendar_diary_checked_at)
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


class LifeCancellationTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    async def test_refreshing_a_life_overview_keeps_a_pending_decision(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def generate(**kwargs):
            entered.set()
            await release.wait()
            return LLMReply(ok=True, text="继续读下一页")

        self.llm.generate = generate
        async with self.engine.session_state(S) as state:
            self.engine.life_choice(state)

        @life_work()
        async def infer(engine):
            async with engine.session_state(S):
                return await engine._ask_llm(S, "", "choose next")

        task = asyncio.create_task(infer(self.engine))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            self.clock.advance(5)
            # The life API recomputes this view while holding session_state.
            async with self.engine.session_state(S) as state:
                self.engine.life_choice(state)
            release.set()
            self.assertEqual(await asyncio.wait_for(task, 2), "继续读下一页")
            self.assertEqual((await self.engine.load_state(S)).life_decision["at"], self.clock.now())
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    async def test_routine_schedule_check_keeps_a_slow_life_decision_and_its_cursor(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def generate(**kwargs):
            entered.set()
            await release.wait()
            return LLMReply(ok=True, text="慢慢读完下一页")

        self.llm.generate = generate

        @life_work()
        async def infer(engine):
            async with engine.session_state(S) as state:
                text = await engine._ask_llm(S, "", "choose next")
                state.external_state["result"] = {"text": text}
            return text

        task = asyncio.create_task(infer(self.engine))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            self.clock.advance(60)
            await self.engine.run_schedules()
            cursor = (await self.engine.load_state(S)).schedule_cursor
            self.assertGreater(cursor, 0)
            release.set()
            self.assertEqual(await asyncio.wait_for(task, 2), "慢慢读完下一页")
            state = await self.engine.load_state(S)
            self.assertEqual(state.external_state["result"]["text"], "慢慢读完下一页")
            self.assertEqual(state.schedule_cursor, cursor, "do not rewind the concurrent schedule scan")
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    async def test_shutdown_does_not_commit_a_cancelled_model_decision(self):
        entered = asyncio.Event()

        async def generate(**kwargs):
            entered.set()
            await asyncio.Event().wait()

        self.llm.generate = generate

        @life_work()
        async def infer(engine):
            async with engine.session_state(S) as state:
                await engine._ask_llm(S, "", "选择下一步")
                state.external_state["result"] = {"text": "不得写入"}

        task = asyncio.create_task(infer(self.engine))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertNotIn("result", (await self.engine.load_state(S)).external_state)
        self.assertFalse(self.engine.is_busy(S))

    async def test_shutdown_with_a_concurrent_edit_is_not_swallowed_as_a_stale_decision(self):
        entered = asyncio.Event()

        async def generate(**kwargs):
            entered.set()
            await asyncio.Event().wait()

        self.llm.generate = generate

        @life_work()
        async def infer(engine):
            async with engine.session_state(S) as state:
                await engine._ask_llm(S, "", "next activity")
                state.node_id = "study"

        task = asyncio.create_task(infer(self.engine))
        await asyncio.wait_for(entered.wait(), 2)
        async with self.engine.session_state(S) as state:
            state.node_id = "window"
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual((await self.engine.load_state(S)).node_id, "window")
        self.assertFalse(self.engine.is_busy(S))

    async def test_in_place_setting_change_invalidates_an_old_life_decision(self):
        async def generate(**kwargs):
            self.engine.world.proactive_chat_enabled = False
            return LLMReply(ok=True, text="old decision")

        self.llm.generate = generate

        @life_work()
        async def infer(engine):
            async with engine.session_state(S) as state:
                text = await engine._ask_llm(S, "", "choose next")
                state.external_state["result"] = {"text": text}
            return text

        self.assertIsNone(await infer(self.engine))
        self.assertNotIn("result", (await self.engine.load_state(S)).external_state)


class CalendarShapeTests(unittest.TestCase):
    def test_life_choice_changes_still_invalidate_a_pending_decision(self):
        before = {"life_decision": {"at": 1, "kind": "idle", "reason": "暂时空闲"}}
        refreshed = {"life_decision": {"at": 2, "kind": "idle", "reason": "暂时空闲"}}
        rest = {"life_decision": {"at": 2, "kind": "rest", "reason": "需要休息"}}
        self.assertEqual(decision_state(before), decision_state(refreshed))
        self.assertNotEqual(decision_state(before), decision_state(rest))

    def test_invalid_event_collection_is_rejected(self):
        for events in ("broken", {}, ["not an event"], [{"title": "bad", "start": "not a date"}]):
            with self.subTest(events=events), self.assertRaises(ValueError):
                validate_calendar({"world_type": "original", "events": events}, year=2026)
