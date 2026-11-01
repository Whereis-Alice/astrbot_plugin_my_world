"""An entire incoming phone request has one bounded, cancellation-safe lifetime."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import aiohttp

from core.visits.config import VisitConfig
from core.visits.ports import VisitStateError
from core.visits.transport import VisitTransport, VisitTransportError


class IncomingTransportDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def channel(self, handler, *, timeout=.02):
        config = VisitConfig(enabled=True, listen_host="127.0.0.1", listen_port=0,
                             timeout_seconds=timeout)
        channel = VisitTransport(handler, config)
        await channel.start()
        self.addAsyncCleanup(channel.close)
        return channel, f"http://127.0.0.1:{channel.bound_port}"

    async def test_each_phone_route_cancels_hung_state_and_releases_other_lives(self):
        for route in ("/v1/message", "/v1/pair"):
            with self.subTest(route=route):
                lock, cancelled = asyncio.Lock(), asyncio.Event()
                calls = []
                async def handler(body):
                    async with lock:
                        calls.append(body["life"])
                        if body["life"] == "blocked":
                            try:
                                await asyncio.Future()
                            finally:
                                cancelled.set()
                        return {"life": body["life"]}
                _, url = await self.channel(handler)
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1)) as client:
                    async with client.post(url + route, json={"life": "blocked"}) as response:
                        self.assertEqual(response.status, 503)
                        self.assertIn("稍后", (await response.json())["error"])
                    self.assertTrue(cancelled.is_set())
                    self.assertFalse(lock.locked())
                    async with client.post(url + route, json={"life": "other"}) as response:
                        self.assertEqual(response.status, 200)
                        self.assertEqual(await response.json(), {"life": "other"})
                self.assertEqual(calls, ["blocked", "other"])

    async def test_multiple_individually_short_checks_share_one_request_deadline(self):
        checks, cancelled = [], asyncio.Event()
        async def handler(body):
            try:
                for index in range(10):
                    checks.append(index)
                    await asyncio.sleep(.015)
                return {"catalog": "complete"}
            finally:
                cancelled.set()
        _, url = await self.channel(handler, timeout=.04)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1)) as client:
            async with client.post(url + "/v1/message", json={}) as response:
                self.assertEqual(response.status, 503)
        self.assertTrue(cancelled.is_set())
        self.assertLess(len(checks), 10)

    async def test_payload_read_is_also_bounded_without_running_the_handler(self):
        async def read():
            await asyncio.Future()
        handler = AsyncMock()
        channel = VisitTransport(handler, VisitConfig(timeout_seconds=.01))
        response = await asyncio.wait_for(channel._handle(SimpleNamespace(read=read)), .2)
        self.assertEqual(response.status, 503)
        handler.assert_not_awaited()

    async def test_local_state_failure_is_temporary_and_does_not_echo_details(self):
        async def handler(body):
            raise VisitStateError() from OSError("synthetic private state details")
        _, url = await self.channel(handler)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1)) as client:
            async with client.post(url + "/v1/message", json={}) as response:
                self.assertEqual(response.status, 503)
                self.assertNotIn("private", await response.text())

    async def test_identity_rejection_stays_a_rejection_and_cancellation_propagates(self):
        request = SimpleNamespace(read=AsyncMock(return_value=b"{}"))
        channel = VisitTransport(AsyncMock(side_effect=ValueError("synthetic identity details")),
                                 VisitConfig(timeout_seconds=.01))
        response = await channel._handle(request)
        self.assertEqual(response.status, 400)
        self.assertNotIn("identity details", response.text)
        channel.handler = AsyncMock(side_effect=asyncio.CancelledError)
        with self.assertRaises(asyncio.CancelledError):
            await channel._handle(request)

    async def test_temporary_receiver_failure_remains_a_retryable_channel_diagnostic(self):
        async def handler(body):
            raise VisitStateError()
        receiver, url = await self.channel(handler)
        sender = VisitTransport(None, replace(receiver.config, timeout_seconds=1))
        self.addAsyncCleanup(sender.close)
        with self.assertRaises(VisitTransportError) as raised:
            await sender.request(url, {"message": "same invitation"})
        self.assertEqual(raised.exception.category, "channel")
        self.assertEqual(raised.exception.http_status, 503)
