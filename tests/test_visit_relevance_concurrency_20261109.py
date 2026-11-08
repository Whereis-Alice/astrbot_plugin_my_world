"""Identical concurrent visit follow-ups share one bounded embedding request."""
from __future__ import annotations

import asyncio
import types
import unittest
from unittest.mock import AsyncMock

from tests import test_engine as fixture


class VisitRelevanceConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    def prepare(self):
        self.engine.world.memory.semantic_enabled = True
        self.started, self.release = asyncio.Event(), asyncio.Event()

        async def embed(texts, *_):
            self.started.set()
            await self.release.wait()
            return [[1., 0.] for _ in texts]

        self.provider = types.SimpleNamespace(
            describe=lambda *_: {"ready": True, "fingerprint": "concurrent-visit"},
            embed=AsyncMock(side_effect=embed))
        self.engine.memory.semantic.provider = self.provider
        return self.engine.memory.semantic

    async def test_concurrent_identical_checks_reuse_result_and_remove_lock(self):
        semantic = self.prepare()
        async def check():
            return await semantic.related(fixture.SESSION, "后来怎么样", ["一起收好了茶杯"], cache_key="visit-one")
        first = asyncio.create_task(check())
        await self.started.wait()
        second = asyncio.create_task(check())
        await asyncio.sleep(0)
        self.assertEqual(1, self.provider.embed.await_count)
        self.release.set()
        self.assertEqual([True, True], await asyncio.gather(first, second))
        self.assertEqual(1, self.provider.embed.await_count)
        self.assertEqual({}, semantic.relevance_locks)

    async def test_cancelling_a_waiter_keeps_the_active_inference_and_other_waiter(self):
        semantic = self.prepare()
        async def check():
            return await semantic.related(fixture.SESSION, "后来怎么样", ["一起收好了茶杯"])
        first = asyncio.create_task(check())
        await self.started.wait()
        cancelled = asyncio.create_task(check())
        third = asyncio.create_task(check())
        await asyncio.sleep(0)
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled
        self.assertFalse(first.done())
        self.release.set()
        self.assertEqual([True, True], await asyncio.gather(first, third))
        self.assertEqual(1, self.provider.embed.await_count)
        self.assertEqual({}, semantic.relevance_locks)

    async def test_cancelling_the_active_request_leaves_no_detached_inference(self):
        semantic = self.prepare()
        first = asyncio.create_task(semantic.related(fixture.SESSION, "后来怎么样", ["一起收好了茶杯"]))
        await self.started.wait()
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertEqual({}, semantic.relevance_locks)
        self.assertEqual({}, semantic.relevance_scores)

    async def test_different_visit_identity_does_not_share_an_inflight_request(self):
        semantic = self.prepare()
        first = asyncio.create_task(semantic.related(fixture.SESSION, "后来怎么样", ["一起收好了茶杯"], cache_key="visit-one"))
        await self.started.wait()
        second = asyncio.create_task(semantic.related(fixture.SESSION, "后来怎么样", ["一起收好了茶杯"], cache_key="visit-two"))
        await asyncio.sleep(0)
        self.assertEqual(2, self.provider.embed.await_count)
        self.release.set()
        self.assertEqual([True, True], await asyncio.gather(first, second))
        self.assertEqual({}, semantic.relevance_locks)

    async def test_failed_embedding_does_not_make_queued_callers_retry_the_provider(self):
        semantic = self.prepare()
        async def unavailable(texts, *_):
            self.started.set()
            await self.release.wait()
            raise RuntimeError("embedding temporarily unavailable")
        self.provider.embed.side_effect = unavailable
        async def check():
            return await semantic.related(fixture.SESSION, "后来怎么样", ["一起收好了茶杯"])
        first = asyncio.create_task(check())
        await self.started.wait()
        second = asyncio.create_task(check())
        await asyncio.sleep(0)
        self.release.set()
        self.assertEqual([False, False], await asyncio.gather(first, second))
        self.assertEqual(1, self.provider.embed.await_count)
        self.assertEqual({}, semantic.relevance_locks)
