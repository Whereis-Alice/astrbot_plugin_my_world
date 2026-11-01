"""Routing, request preparation and shared admission keep independent background work available."""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.model_calls import model_context
from core.ports import LLMReply
from core.speech_runtime import SPEECH_REQUEST
from tests import test_engine as fixture


class RoutedChannel:
    def __init__(self):
        self.routes = {fixture.SESSION: "provider-a", fixture.OTHER_SESSION: "provider-b"}
        self.failing = {"provider-a"}
        self.calls = []

    async def resolve_provider_id(self, session_id):
        return self.routes[session_id]

    async def request(self, session_id, provider):
        self.calls.append((session_id, provider))
        return LLMReply(text="ok", ok=provider not in self.failing, error="Provider unavailable")

    async def generate(self, *, session_id, **kwargs):
        return await self.request(session_id, await self.resolve_provider_id(session_id))

    async def generate_resolved(self, *, resolved_provider_id, session_id, **kwargs):
        return await self.request(session_id, resolved_provider_id)


class BackgroundAdmissionReviewTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    async def generate(self, channel, session=fixture.SESSION, **options):
        with self.engine.config_scope(session), model_context("life_decision", "自主生活", session):
            return await self.engine._budgeted_generate(channel, session_id=session,
                system_prompt="world", prompt="life", max_tokens=options.get("max_tokens", 100))

    async def status(self):
        return await self.db.call("background_status", self.engine._today_key(self.clock.now()))

    async def test_distinct_sessions_and_providers_do_not_share_backoff(self):
        channel = RoutedChannel()
        with self.assertRaisesRegex(RuntimeError, "Provider unavailable"):
            await self.generate(channel)
        self.assertTrue((await self.generate(channel, fixture.OTHER_SESSION)).ok)
        self.assertFalse((await self.generate(channel)).ok)
        self.assertEqual(channel.calls, [(fixture.SESSION, "provider-a"), (fixture.OTHER_SESSION, "provider-b")])

    async def test_switching_provider_in_one_session_preserves_each_channels_own_backoff(self):
        channel = RoutedChannel()
        with self.assertRaises(RuntimeError):
            await self.generate(channel)
        channel.routes[fixture.SESSION] = "provider-b"
        self.assertTrue((await self.generate(channel)).ok)
        channel.routes[fixture.SESSION] = "provider-a"
        self.assertFalse((await self.generate(channel)).ok)
        self.assertEqual([provider for _, provider in channel.calls], ["provider-a", "provider-b"])

    async def test_same_provider_in_another_session_is_not_blocked_by_the_first_prompt(self):
        channel = RoutedChannel()
        channel.routes[fixture.OTHER_SESSION] = "provider-a"
        with self.assertRaises(RuntimeError):
            await self.generate(channel)
        channel.failing.clear()
        self.assertTrue((await self.generate(channel, fixture.OTHER_SESSION)).ok)
        self.assertEqual(len(channel.calls), 2)

    async def test_generic_channel_keeps_per_session_backoff_without_a_provider_resolver(self):
        calls = []

        async def request(*, session_id, **kwargs):
            calls.append(session_id)
            return LLMReply(text="ok", ok=session_id != fixture.SESSION, error="local request failed")

        channel = SimpleNamespace(generate=request)
        with self.assertRaises(RuntimeError):
            await self.generate(channel)
        self.assertTrue((await self.generate(channel, fixture.OTHER_SESSION)).ok)
        self.assertFalse((await self.generate(channel)).ok)
        self.assertEqual(calls, [fixture.SESSION, fixture.OTHER_SESSION])

    async def test_invalid_option_leaves_no_slot_budget_or_provider_backoff(self):
        with self.assertRaises(ValueError):
            await self.generate(self.llm, max_tokens="invalid")
        self.assertEqual(getattr(self.engine, "_background_active", 0), 0)
        self.assertEqual(await self.status(), dict(calls=0, tokens=0, unknown=0, reserved=0))
        self.assertFalse(getattr(self.engine, "_background_failures", {}))
        self.assertTrue((await self.generate(self.llm)).ok)

    async def test_tool_preparation_failure_is_not_a_provider_failure(self):
        token = SPEECH_REQUEST.set({"state": SimpleNamespace(node_id="study"), "allow_tools": True})
        try:
            with patch.object(self.engine, "allowed_tool_names", side_effect=RuntimeError("tool context changed")):
                with self.assertRaisesRegex(RuntimeError, "tool context changed"):
                    await self.generate(self.llm)
        finally:
            SPEECH_REQUEST.reset(token)
        self.assertEqual(getattr(self.engine, "_background_active", 0), 0)
        self.assertEqual(await self.status(), dict(calls=0, tokens=0, unknown=0, reserved=0))
        self.assertFalse(getattr(self.engine, "_background_failures", {}))
        self.assertTrue((await self.generate(self.llm)).ok)

    async def test_failed_or_cancelled_routing_lookup_spends_nothing(self):
        channel = RoutedChannel()
        entered = asyncio.Event()

        async def failure(session):
            raise RuntimeError("provider selection unavailable")

        with patch.object(channel, "resolve_provider_id", side_effect=failure):
            with self.assertRaises(RuntimeError):
                await self.generate(channel)

        async def waiting(session):
            entered.set()
            await asyncio.Event().wait()

        with patch.object(channel, "resolve_provider_id", side_effect=waiting):
            task = asyncio.create_task(self.generate(channel))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(getattr(self.engine, "_background_active", 0), 0)
        self.assertEqual(await self.status(), dict(calls=0, tokens=0, unknown=0, reserved=0))
        self.assertFalse(getattr(self.engine, "_background_failures", {}))
        self.assertEqual(channel.calls, [])

    async def test_async_routing_cannot_over_admit_a_slot_taken_during_the_lookup(self):
        self.engine.world.limits.background_concurrency = 1
        slow, occupying = RoutedChannel(), RoutedChannel()
        slow.failing.clear()
        occupying.failing.clear()
        resolving, release_routing, generating, release_model = (asyncio.Event() for _ in range(4))

        async def resolve(session):
            resolving.set()
            await release_routing.wait()
            return "provider-a"

        async def request(session, provider):
            generating.set()
            await release_model.wait()
            return LLMReply(text="ok")

        slow.resolve_provider_id = resolve
        occupying.request = request
        first = asyncio.create_task(self.generate(slow))
        await asyncio.wait_for(resolving.wait(), 2)
        second = asyncio.create_task(self.generate(occupying, fixture.OTHER_SESSION))
        try:
            await asyncio.wait_for(generating.wait(), 2)
            release_routing.set()
            reply = await first
            self.assertFalse(reply.ok)
            self.assertIn("通道忙", reply.error)
            self.assertEqual(slow.calls, [])
            self.assertEqual((await self.status())["calls"], 1)
        finally:
            release_routing.set()
            release_model.set()
            await asyncio.gather(first, second, return_exceptions=True)
        self.assertEqual(self.engine._background_active, 0)

    async def test_provider_lookup_keeps_its_timeout_without_entering_the_budget(self):
        self.engine.world.limits.background_llm_timeout_seconds = .02
        channel = RoutedChannel()

        async def waiting(session):
            await asyncio.Event().wait()

        with patch.object(channel, "resolve_provider_id", side_effect=waiting):
            with self.assertRaises(TimeoutError):
                await self.generate(channel)
        self.assertEqual(getattr(self.engine, "_background_active", 0), 0)
        self.assertEqual(await self.status(), dict(calls=0, tokens=0, unknown=0, reserved=0))
        self.assertFalse(getattr(self.engine, "_background_failures", {}))
        self.assertEqual(channel.calls, [])

    async def test_resolved_provider_is_kept_while_budget_reservation_waits(self):
        channel = RoutedChannel()
        entered, release = asyncio.Event(), asyncio.Event()
        original_call = self.db.call

        async def held(method, *args, **kwargs):
            if method == "reserve_background":
                entered.set()
                await release.wait()
            return await original_call(method, *args, **kwargs)

        with patch.object(self.db, "call", side_effect=held):
            task = asyncio.create_task(self.generate(channel))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                channel.routes[fixture.SESSION] = "provider-b"
                release.set()
                with self.assertRaises(RuntimeError):
                    await task
                self.assertTrue((await self.generate(channel)).ok)
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual([provider for _, provider in channel.calls], ["provider-a", "provider-b"])
