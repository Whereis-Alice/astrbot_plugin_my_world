"""Visit allowance belongs to a life and never consumes the instance routine day."""
from __future__ import annotations

import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.background_budget import capture_background_admission
from core.llm_usage import record_llm_usage
from core.model_calls import model_context
from core.ports import LLMReply
from tests import test_engine as fixture


S, OTHER = fixture.SESSION, fixture.OTHER_SESSION
EMPTY = dict(calls=0, tokens=0, unknown=0, reserved=0)


class VisitBudgetIsolationTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    add_group = fixture.EngineTestCase.add_group

    async def asyncSetUp(self):
        await fixture.EngineTestCase.asyncSetUp(self)
        self.engine.world.limits.background_pacing = False
        self.engine.visits = SimpleNamespace(config=SimpleNamespace(daily_calls=24, daily_tokens=0))

        async def measured(**kwargs):
            record_llm_usage(dict(prompt_tokens=7, completion_tokens=3))
            return LLMReply(text="ok")

        self.channel = SimpleNamespace(generate=AsyncMock(side_effect=measured))

    async def generate(self, session=S, *, purpose="world_visit", channel=None, request_session=None):
        with model_context(purpose, "visit isolation regression", session if request_session is None else request_session):
            return await self.engine._budgeted_generate(channel or self.channel, session_id=session,
                system_prompt="world", prompt="life", max_tokens=100)

    async def status(self, session=S):
        return await self.engine._background_budget_status(purpose="world_visit", session_id=session)

    async def ordinary(self):
        return await self.db.call("background_status", self.engine._today_key(self.clock.now()))

    def usage(self, status):
        return {key: status[key] for key in EMPTY}

    async def test_exhausted_120_call_routine_day_still_allows_a_visit(self):
        day = self.engine._today_key(self.clock.now())
        self.engine.world.limits.background_daily_calls = 120
        for _ in range(120):
            await self.db.call("reserve_background", day, calls=120, tokens=0, reserve=1)
        before = await self.ordinary()
        self.assertFalse((await self.engine._background_budget_status())["allowed"])
        self.assertTrue((await self.generate()).ok)
        self.assertEqual(await self.ordinary(), before)
        self.assertEqual(self.usage(await self.status()), dict(calls=1, tokens=10, unknown=0, reserved=0))
        self.channel.generate.assert_awaited_once()

    async def test_default_24_call_visit_pool_is_bounded_without_spending_routine(self):
        self.engine.visits = SimpleNamespace(config=SimpleNamespace())
        initial = await self.status()
        self.assertEqual((initial["call_limit"], initial["token_limit"]), (24, 0))
        for _ in range(24):
            self.assertTrue((await self.generate()).ok)
        with capture_background_admission() as admission:
            denied = await self.generate()
        self.assertFalse(denied.ok)
        self.assertIn("今日串门预算", denied.error)
        self.assertEqual(admission.reason, denied.error)
        self.assertTrue(admission.deferred)
        self.assertFalse(admission.started)
        self.assertEqual((await self.status())["calls"], 24)
        self.assertEqual(await self.ordinary(), EMPTY)
        self.assertEqual(self.channel.generate.await_count, 24)

    async def test_distinct_lives_each_have_their_own_allowance(self):
        self.store.add_session(OTHER)
        self.engine.reload_config()
        self.engine.visits.config.daily_calls = 1
        self.assertTrue((await self.generate()).ok)
        self.assertFalse((await self.generate()).ok)
        self.assertTrue((await self.generate(OTHER)).ok)
        self.assertFalse((await self.generate(OTHER)).ok)
        first, second = await self.status(), await self.status(OTHER)
        self.assertNotEqual(first["ledger"], second["ledger"])
        self.assertEqual((first["calls"], second["calls"]), (1, 1))
        self.assertEqual(await self.ordinary(), EMPTY)

    async def test_group_and_member_aliases_share_the_actual_state_key(self):
        self.add_group(group_id="one-life", main=OTHER)
        self.engine.visits.config.daily_calls = 1
        before = [await self.status(value) for value in (S, OTHER, "one-life")]
        self.assertEqual({item["scope"] for item in before}, {self.engine.state_key(S)})
        self.assertEqual(len({item["ledger"] for item in before}), 1)
        self.assertTrue((await self.generate(S, request_session=OTHER)).ok)
        self.assertFalse((await self.generate(OTHER)).ok)
        self.assertFalse((await self.generate("one-life")).ok)
        self.assertEqual((await self.status())["calls"], 1)
        self.channel.generate.assert_awaited_once()

    async def test_no_explicit_life_or_conflicting_lives_cannot_bypass_ownership(self):
        self.store.add_session(OTHER)
        self.engine.reload_config()
        self.channel.resolve_provider_id = AsyncMock(return_value="provider-a")
        for session in ("", "unknown:GroupMessage:1"):
            self.assertFalse((await self.status(session))["allowed"])
            self.assertFalse((await self.generate(session)).ok)
        self.assertFalse((await self.generate(S, request_session=OTHER)).ok)
        self.engine.session_config(S).enabled = False
        self.assertFalse((await self.status())["allowed"])
        self.assertFalse((await self.generate()).ok)
        self.channel.resolve_provider_id.assert_not_awaited()
        self.channel.generate.assert_not_awaited()
        self.assertEqual(await self.ordinary(), EMPTY)
        self.assertFalse(self.db.raw._query("SELECT key FROM kv WHERE key LIKE 'background_budget:%'"))

    async def test_explicit_call_context_can_supply_the_life(self):
        with model_context("world_visit", "context owns the call", S):
            self.assertTrue((await self.engine._budgeted_generate(self.channel,
                system_prompt="world", prompt="life", max_tokens=100)).ok)
        self.assertEqual((await self.status())["calls"], 1)
        self.assertEqual(await self.ordinary(), EMPTY)

    def split_group(self):
        raw = self.store.raw_sessions()
        raw["groups"] = []
        self.store.save_sessions(raw)
        self.engine.reload_config()

    async def test_context_and_call_aliases_split_during_provider_lookup_spend_neither_life(self):
        self.add_group(main=S)

        async def resolve(session):
            self.split_group()
            return "provider-a"

        self.channel.resolve_provider_id = AsyncMock(side_effect=resolve)
        with capture_background_admission() as admission:
            reply = await self.generate(S, request_session=OTHER)
        self.assertFalse(reply.ok)
        self.assertFalse(admission.started)
        self.assertEqual(self.usage(await self.status(S)), EMPTY)
        self.assertEqual(self.usage(await self.status(OTHER)), EMPTY)
        self.assertEqual(await self.ordinary(), EMPTY)
        self.channel.generate.assert_not_awaited()

    async def test_life_owner_changes_during_provider_lookup_spend_neither_life(self):
        self.add_group(main=S)
        old = await self.status()

        async def resolve(session):
            self.engine.session_config(S).enabled = False
            return "provider-a"

        self.channel.resolve_provider_id = AsyncMock(side_effect=resolve)
        with capture_background_admission() as admission:
            reply = await self.generate()
        self.assertFalse(reply.ok)
        self.assertFalse(admission.started)
        self.assertEqual(await self.db.call("background_status", old["ledger"]), EMPTY)
        new = await self.status()
        self.assertEqual(new["scope"], OTHER)
        self.assertEqual(self.usage(new), EMPTY)
        self.assertEqual(await self.ordinary(), EMPTY)
        self.channel.generate.assert_not_awaited()

    async def test_context_and_call_aliases_split_at_dispatch_refund_the_original_life(self):
        self.add_group(main=S)
        old = await self.status()

        async def prepare(*, before_request, **kwargs):
            self.split_group()
            await before_request()
            self.fail("a call whose aliases now belong to distinct lives must prepare again")

        channel = SimpleNamespace(generate=AsyncMock(), resolve_provider_id=AsyncMock(return_value="provider-a"),
                                  generate_admitted=prepare)
        with capture_background_admission() as admission:
            reply = await self.generate(S, channel=channel, request_session=OTHER)
        self.assertFalse(reply.ok)
        self.assertFalse(admission.started)
        self.assertEqual(await self.db.call("background_status", old["ledger"]), EMPTY)
        self.assertEqual(self.usage(await self.status(S)), EMPTY)
        self.assertEqual(self.usage(await self.status(OTHER)), EMPTY)
        self.assertEqual(await self.ordinary(), EMPTY)
        self.assertFalse(self.engine._background_failures)
        channel.generate.assert_not_awaited()

    async def test_routine_and_interactive_work_keep_existing_accounting(self):
        self.engine.visits.config.daily_calls = 1
        self.assertTrue((await self.generate()).ok)
        self.assertTrue((await self.generate(purpose="participation")).ok)
        before = await self.ordinary()
        self.assertEqual(before["calls"], 1)
        self.assertTrue((await self.generate(purpose="reflection")).ok)
        self.assertEqual(await self.ordinary(), before)
        self.assertFalse((await self.generate()).ok)
        self.assertEqual((await self.status())["calls"], 1)

    async def test_unstarted_cancellation_refunds_only_the_visit_ledger(self):
        day = self.engine._today_key(self.clock.now())
        await self.db.call("reserve_background", day, calls=0, tokens=0, reserve=50)
        before = await self.ordinary()
        entered, release = asyncio.Event(), threading.Event()
        original = self.db.raw.reserve_background
        loop = asyncio.get_running_loop()

        def held(*args, **kwargs):
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(3):
                raise AssertionError("reservation worker was not released")
            return original(*args, **kwargs)

        self.db.raw.reserve_background = held
        self.addCleanup(setattr, self.db.raw, "reserve_background", original)
        self.addCleanup(release.set)
        task = asyncio.create_task(self.generate())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            await asyncio.sleep(0)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(task.cancelled())
        self.assertEqual(self.usage(await self.status()), EMPTY)
        self.assertEqual(await self.ordinary(), before)
        self.channel.generate.assert_not_awaited()
        self.assertEqual(self.engine._background_active, 0)

    async def test_started_cancellation_keeps_unknown_usage_only_in_visit_pool(self):
        entered = asyncio.Event()

        async def pending(**kwargs):
            entered.set()
            await asyncio.Event().wait()

        self.channel.generate = AsyncMock(side_effect=pending)
        task = asyncio.create_task(self.generate())
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        status = await self.status()
        self.assertEqual((status["calls"], status["tokens"], status["unknown"]), (1, 0, 1))
        self.assertGreater(status["reserved"], 0)
        self.assertEqual(await self.ordinary(), EMPTY)
        self.assertEqual(self.engine._background_active, 0)

    async def test_midnight_during_preparation_refunds_the_original_visit_day(self):
        old = await self.status()

        async def prepare(*, before_request, **kwargs):
            self.clock.advance(86400)
            await before_request()
            self.fail("a prepared request from yesterday must not enter the provider")

        channel = SimpleNamespace(generate=AsyncMock(), resolve_provider_id=AsyncMock(return_value="provider-a"),
                                  generate_admitted=prepare)
        self.assertFalse((await self.generate(channel=channel)).ok)
        self.assertEqual(await self.db.call("background_status", old["ledger"]), EMPTY)
        new = await self.status()
        self.assertNotEqual(new["ledger"], old["ledger"])
        self.assertTrue(new["allowed"])
        self.assertEqual(self.usage(new), EMPTY)
        self.assertEqual(await self.ordinary(), EMPTY)
        self.assertFalse(self.engine._background_failures)
        channel.generate.assert_not_awaited()

    async def test_life_owner_change_during_preparation_refunds_original_life(self):
        self.add_group(group_id="one-life", main=S)
        old = await self.status()

        async def prepare(*, before_request, **kwargs):
            self.engine.session_config(S).enabled = False
            await before_request()
            self.fail("a reassigned life must prepare again")

        channel = SimpleNamespace(generate=AsyncMock(), resolve_provider_id=AsyncMock(return_value="provider-a"),
                                  generate_admitted=prepare)
        self.assertFalse((await self.generate(channel=channel)).ok)
        self.assertEqual(await self.db.call("background_status", old["ledger"]), EMPTY)
        new = await self.status()
        self.assertEqual(new["scope"], OTHER)
        self.assertNotEqual(new["ledger"], old["ledger"])
        self.assertTrue(new["allowed"])
        self.assertEqual(self.usage(new), EMPTY)
        self.assertEqual(await self.ordinary(), EMPTY)
        self.assertFalse(self.engine._background_failures)

    async def test_restart_and_next_day_keep_independent_durable_usage(self):
        self.engine.visits.config.daily_calls = 1
        self.assertTrue((await self.generate()).ok)
        old = await self.status()
        self.engine.reload_config()
        reloaded = await self.status()
        self.assertEqual(old, reloaded)
        self.assertFalse(reloaded["allowed"])
        self.clock.advance(86400)
        self.assertTrue((await self.generate()).ok)
        current = await self.status()
        self.assertNotEqual(current["ledger"], old["ledger"])
        self.assertEqual(current["calls"], 1)
        self.assertEqual((await self.db.call("background_status", old["ledger"]))["calls"], 1)
        self.assertEqual(await self.ordinary(), EMPTY)


if __name__ == "__main__":
    unittest.main()
