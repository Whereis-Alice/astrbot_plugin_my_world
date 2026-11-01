"""Unstarted budget deferrals keep message and reading opportunities intact."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import time
import unittest
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from core.background_budget import record_background_admission
from core.background_rhythm import PACED
from core.engine.participation_queue import registries, settle_message, wake
from tests import test_participation as participation
from tests import test_chat_browse as browsing

GROUP = participation.GROUP


class ParticipationBudgetPacingTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = participation.ParticipationTests.asyncSetUp
    chat = participation.ParticipationTests.chat

    def morning(self):
        self.clock._now = datetime(2026, 11, 1, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        self.clock.set_struct(datetime.fromtimestamp(self.clock.now(), ZoneInfo("Asia/Shanghai")))
        self.engine.world.limits.background_daily_calls = 24
        self.engine.world.limits.background_pacing = True

    async def occupy_startup(self):
        await self.db.call("reserve_background", self.engine._today_key(self.clock.now()),
                           calls=24, tokens=0, reserve=10)

    async def test_pacing_preflight_does_not_consume_message_or_observation_attempt(self):
        self.morning()
        await self.occupy_startup()
        await self.chat(message_id="fresh")
        async with self.engine.session_state(GROUP) as state:
            state.participation[GROUP] = {"checked_seq": 0, "attempts": [self.clock.now() - 500]}
        outcome = await self.engine.maybe_participate(GROUP)
        state = await self.engine.load_state(GROUP, cold_start=False)
        observation = state.participation[GROUP]
        self.assertEqual(observation["decision"], "deferred")
        self.assertEqual(observation["reason"], PACED)
        self.assertGreater(observation["budget_next_at"], self.clock.now())
        self.assertEqual(observation["checked_seq"], 0)
        self.assertEqual(observation["attempts"], [self.clock.now() - 500])
        self.assertFalse(state.thoughts)
        self.assertFalse(self.llm.calls)
        self.assertFalse(self.messenger.sent)
        self.assertFalse(outcome.speech_decisions)

    async def test_token_reservation_denial_rolls_back_unstarted_observation(self):
        self.engine.world.limits.background_daily_tokens = 10
        await self.chat(message_id="fresh")
        await self.engine.maybe_participate(GROUP)
        state = await self.engine.load_state(GROUP, cold_start=False)
        observation = state.participation[GROUP]
        self.assertEqual(observation["decision"], "deferred")
        self.assertFalse(observation.get("checked_seq"))
        self.assertFalse(observation.get("checked_at"))
        self.assertFalse(observation.get("evaluated_at"))
        self.assertFalse(observation.get("attempts"))
        self.assertFalse(self.llm.calls)
        self.assertFalse(state.thoughts)
        self.assertFalse(await self.db.call("query_events", session_id=GROUP, event_type="speech_decision_failed"))
        traces = await self.db.call("decision_traces", GROUP)
        self.assertTrue(any(row["status"] == "deferred" for row in traces))
        self.assertFalse(any(row["status"] == "failed" for row in traces))

    async def test_shared_budget_race_restores_previous_watermark_and_attempts(self):
        self.morning()
        await self.chat(message_id="race")
        original = self.engine._background_budget_status

        async def admit_then_spend(**kwargs):
            result = await original(**kwargs)
            self.assertTrue(result["allowed"])
            await self.occupy_startup()
            return result

        self.engine._background_budget_status = admit_then_spend
        async with self.engine.session_state(GROUP) as state:
            state.participation[GROUP] = {"checked_seq": 0, "attempts": [self.clock.now() - 500],
                                          "evaluated_at": self.clock.now() - 500}
        await self.engine.maybe_participate(GROUP)
        state = await self.engine.load_state(GROUP, cold_start=False)
        observation = state.participation[GROUP]
        self.assertEqual(observation["decision"], "deferred")
        self.assertEqual(observation["checked_seq"], 0)
        self.assertEqual(observation["attempts"], [self.clock.now() - 500])
        self.assertEqual(observation["evaluated_at"], self.clock.now() - 500)
        self.assertFalse(self.llm.calls)

    async def test_unstarted_deferral_does_not_rollback_a_newer_observation(self):
        await self.chat(message_id="race")
        newer = {"checked_seq": 99, "attempts": [1, 2], "request_id": "newer", "decision": "listening"}

        async def replace_observation(state, *args, **kwargs):
            state.participation[GROUP] = dict(newer)
            record_background_admission(deferred=True, reason=PACED, next_at=self.clock.now() + 500)
            return "", ""

        self.engine.participation_text = replace_observation
        await self.engine.maybe_participate(GROUP)
        state = await self.engine.load_state(GROUP, cold_start=False)
        self.assertEqual(state.participation[GROUP], newer)

    async def test_unstarted_cancellation_preserves_live_message_for_next_judgment(self):
        await self.chat(message_id="cancel-before-dispatch")
        entered = asyncio.Event()

        async def preparing(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        original = self.engine.participation_text
        self.engine.participation_text = preparing
        task = asyncio.create_task(self.engine.maybe_participate(GROUP))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        state = await self.engine.load_state(GROUP, cold_start=False)
        self.assertFalse(state.participation[GROUP].get("checked_seq"))
        self.assertFalse(state.participation[GROUP].get("attempts"))
        self.engine.participation_text = original
        await self.engine.maybe_participate(GROUP)
        self.assertEqual(len(self.llm.calls), 1)
        self.assertTrue(self.messenger.sent)

    async def test_started_cancellation_consumes_observation_without_replaying_model(self):
        await self.chat(message_id="cancel-after-dispatch")
        entered = asyncio.Event()

        async def requested(*args, **kwargs):
            record_background_admission(started=True)
            entered.set()
            await asyncio.Event().wait()

        self.engine.participation_text = requested
        task = asyncio.create_task(self.engine.maybe_participate(GROUP))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        state = await self.engine.load_state(GROUP, cold_start=False)
        self.assertGreater(state.participation[GROUP]["checked_seq"], 0)
        self.assertEqual(len(state.participation[GROUP]["attempts"]), 1)
        self.assertIsNone(await self.engine.maybe_participate(GROUP))

    async def test_unstarted_cancellation_cannot_restore_over_newer_observation(self):
        await self.chat(message_id="newer-cancel")
        newer = {"checked_seq": 99, "attempts": [1, 2], "request_id": "newer", "decision": "listening"}

        async def cancelled(state, *args, **kwargs):
            state.participation[GROUP] = dict(newer)
            raise asyncio.CancelledError()

        self.engine.participation_text = cancelled
        with self.assertRaises(asyncio.CancelledError):
            await self.engine.maybe_participate(GROUP)
        self.assertEqual((await self.engine.load_state(GROUP, cold_start=False)).participation[GROUP], newer)

    async def test_started_bad_output_is_consumed_even_when_its_repair_is_denied(self):
        self.engine.world.limits.background_daily_calls = 1
        self.llm.replies = ['{"intent":"join_chat","say":"试试看。"}']
        row = await self.chat(message_id="repair")
        await self.engine.maybe_participate(GROUP)
        state = await self.engine.load_state(GROUP, cold_start=False)
        observation = state.participation[GROUP]
        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual(observation["checked_seq"], row["seq"])
        self.assertEqual(len(observation["attempts"]), 1)
        self.assertEqual(observation["decision"], "failed")
        self.assertTrue(await self.db.call("query_events", session_id=GROUP, event_type="speech_decision_failed"))

    async def test_known_release_wait_does_not_repeat_preflight_and_config_change_rechecks(self):
        self.morning()
        await self.occupy_startup()
        await self.chat(message_id="waiting")
        pending, _, _ = registries(self.engine)
        key, arrived = self.engine.state_key(GROUP), time.monotonic()
        pending[key] = {GROUP: arrived}
        self.engine._background_budget_status = AsyncMock(wraps=self.engine._background_budget_status)
        first = await settle_message(self.engine, key, GROUP, arrived)
        second = await settle_message(self.engine, key, GROUP, arrived)
        self.assertGreater(first, 30)
        self.assertGreater(second, 30)
        self.assertEqual(self.engine._background_budget_status.await_count, 1)
        self.engine.world.limits.background_pacing = False
        await settle_message(self.engine, key, GROUP, arrived)
        self.assertEqual(self.engine._background_budget_status.await_count, 2)
        self.assertEqual(len(self.llm.calls), 1)

    async def test_expired_budget_wait_is_discarded_after_wakeup_without_calling_model(self):
        self.morning()
        await self.occupy_startup()
        self.engine.world.decider.settle_seconds = 0
        await self.chat(message_id="waiting")
        self.engine._background_budget_status = AsyncMock(wraps=self.engine._background_budget_status)
        self.engine.queue_participation(GROUP)
        task = self.engine._participation_settlers[self.engine.state_key(GROUP)]
        async with asyncio.timeout(3):
            while True:
                state = await self.engine.load_state(GROUP, cold_start=False)
                if state.participation.get(GROUP, {}).get("decision") == "deferred":
                    break
                await asyncio.sleep(.01)
        self.clock.advance(181)
        wake(self.engine, GROUP)
        await asyncio.wait_for(task, 3)
        state = await self.engine.load_state(GROUP, cold_start=False)
        self.assertEqual(state.participation[GROUP]["decision"], "idle")
        self.assertFalse(self.llm.calls)
        self.assertEqual(self.engine._background_budget_status.await_count, 1)

    async def test_unstarted_request_refund_invalidates_cached_wait_and_rechecks_live_scene(self):
        self.engine.world.limits.background_daily_calls = 1
        await self.chat(message_id="still-live")
        entered = asyncio.Event()

        @asynccontextmanager
        async def not_started(_engine):
            entered.set()
            await asyncio.Event().wait()
            yield

        pending, _, _ = registries(self.engine)
        key, arrived = self.engine.state_key(GROUP), time.monotonic()
        pending[key] = {GROUP: arrived}
        budget_wakeup = asyncio.Event()
        self.engine._participation_wakeups[key] = budget_wakeup
        self.engine._background_budget_status = AsyncMock(wraps=self.engine._background_budget_status)
        with patch("core.engine.background.released_life_lock", not_started):
            reserved = asyncio.create_task(self.engine._budgeted_generate(self.llm,
                session_id=GROUP, system_prompt="hold", prompt="unstarted", max_tokens=10))
            try:
                await asyncio.wait_for(entered.wait(), 3)
                delay = await settle_message(self.engine, key, GROUP, arrived)
                self.assertGreater(delay, 30)
                state = await self.engine.load_state(GROUP, cold_start=False)
                previous_policy = list(state.participation[GROUP]["budget_policy"])
                self.assertFalse(self.llm.calls)
                budget_wakeup.clear()
            finally:
                reserved.cancel()
                await asyncio.gather(reserved, return_exceptions=True)
        # Cancellation completes the real SQLite refund and the engine's change
        # callback. No synthetic revision bump or fresh message wakes this scene.
        budget = await self.db.call("background_status", self.engine._today_key(self.clock.now()))
        self.assertEqual(budget["calls"], 0)
        self.assertNotEqual(previous_policy[-1], self.engine._background_budget_revision)
        self.assertTrue(budget_wakeup.is_set())
        await settle_message(self.engine, key, GROUP, arrived)
        self.assertEqual(self.engine._background_budget_status.await_count, 2)
        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual(self.messenger.sent, [(GROUP, ["那个我也想试试。"])])


class BrowseBudgetPacingTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = browsing.ChatBrowseTests.asyncSetUp
    configure = browsing.ChatBrowseTests.configure
    note = browsing.ChatBrowseTests.note
    prepare = browsing.ChatBrowseTests.prepare
    complete = browsing.ChatBrowseTests.complete

    async def test_new_message_observation_preserved_while_unstarted_reading_marker_is_withdrawn(self):
        from core.browse_reply import BrowseReply
        from core.engine.life_inference import released_life_lock
        action = await self.prepare(message_id="old-reading", reply_enabled=True)
        newer = {"request_id": "newer", "checked_seq": 99, "attempts": [1, 2], "decision": "listening"}
        self.engine._background_budget_status = AsyncMock(return_value={"allowed": True})

        async def replace():
            async with self.engine.session_state(browsing.S) as current:
                current.participation[browsing.S] = dict(newer)

        async def prepare_then_change(state, *args, **kwargs):
            async with released_life_lock(self.engine):
                await asyncio.create_task(replace())
            return "", ""

        self.engine.participation_text = prepare_then_change
        state = await self.complete(action)
        thought = next(row for row in state.thoughts if row.get("browse_id") == action["id"])
        self.assertEqual(state.participation[browsing.S], newer)
        self.assertNotIn("browse_reply_started_at", thought)
        ticket = BrowseReply.capture(self.engine, state, browsing.S, action["id"])
        self.assertEqual(ticket.error(self.engine, state), ("", ""))

    async def test_new_observation_does_not_withdraw_already_started_reading(self):
        from core.browse_reply import BrowseReply
        from core.engine.life_inference import released_life_lock
        action = await self.prepare(message_id="started-reading", reply_enabled=True)
        newer = {"request_id": "newer", "checked_seq": 99, "decision": "listening"}

        async def replace():
            async with self.engine.session_state(browsing.S) as current:
                current.participation[browsing.S] = dict(newer)

        async def requested_then_change(state, *args, **kwargs):
            record_background_admission(started=True)
            async with released_life_lock(self.engine):
                await asyncio.create_task(replace())
            return "", ""

        self.engine.participation_text = requested_then_change
        state = await self.complete(action)
        thought = next(row for row in state.thoughts if row.get("browse_id") == action["id"])
        self.assertEqual(state.participation[browsing.S], newer)
        self.assertIn("browse_reply_started_at", thought)
        ticket = BrowseReply.capture(self.engine, state, browsing.S, action["id"])
        self.assertEqual(ticket.error(self.engine, state)[0], "already_considered")

    async def test_reading_waits_with_original_deadline_and_unstarted_marker_then_can_reply(self):
        action = await self.prepare(message_id="original", reply_enabled=True)
        self.engine._background_budget_status = AsyncMock(return_value={
            "allowed": False, "reason": PACED, "next_at": self.clock.now() + 30})
        state = await self.complete(action, decision='{"inner":"还想问问花。","intent":"continue_chat",'
                                    '"say":"花现在开得怎么样？","reply_to":"m1"}')
        thought = next(row for row in state.thoughts if row.get("browse_id") == action["id"])
        self.assertNotIn("browse_reply_started_at", thought)
        self.assertEqual(thought["detail"]["browse_reply"]["status"], "waiting")
        self.assertEqual(len(self.llm.calls), 1)
        task = self.engine._participation_settlers[self.engine.state_key(browsing.S)]
        self.clock.advance(30)
        self.engine._background_budget_status.return_value = {"allowed": True}
        wake(self.engine, browsing.S)
        await asyncio.wait_for(task, 3)
        self.assertEqual(self.messenger.sent, [(browsing.S, ["花现在开得怎么样？"])])
        self.assertEqual(len(self.llm.calls), 2)

    async def test_release_beyond_reading_deadline_is_not_queued_for_later_replay(self):
        action = await self.prepare(message_id="original", reply_enabled=True)
        self.engine._background_budget_status = AsyncMock(return_value={
            "allowed": False, "reason": PACED, "next_at": self.clock.now() + 3600})
        state = await self.complete(action)
        thought = next(row for row in state.thoughts if row.get("browse_id") == action["id"])
        self.assertEqual(thought["detail"]["browse_reply"]["status"], "budget")
        self.assertNotIn("browse_reply_started_at", thought)
        self.assertFalse(getattr(self.engine, "_participation_settlers", {}))
        self.assertEqual(len(self.llm.calls), 1)
        self.assertFalse(self.messenger.sent)

    async def test_central_token_denial_does_not_mark_reading_as_started(self):
        action = await self.prepare(message_id="original", reply_enabled=True)
        self.engine._background_budget_status = AsyncMock(return_value={"allowed": True})
        original = self.engine.participation_text

        async def reserve_too_large(*args, **kwargs):
            self.engine.world.limits.background_daily_tokens = 10
            return await original(*args, **kwargs)

        self.engine.participation_text = reserve_too_large
        state = await self.complete(action)
        thought = next(row for row in state.thoughts if row.get("browse_id") == action["id"])
        self.assertNotIn("browse_reply_started_at", thought)
        self.assertEqual(thought["detail"]["browse_reply"]["status"], "budget")
        self.assertEqual(state.participation[browsing.S]["decision"], "deferred")
        self.assertEqual(len(self.llm.calls), 1)
        self.assertFalse(self.messenger.sent)
        self.assertFalse(await self.db.call("query_events", session_id=browsing.S,
                                           event_type="speech_decision_failed"))
