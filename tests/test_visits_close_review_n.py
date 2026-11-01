"""A closed instance cannot send or reopen resources after awaited admission."""
from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web

from tests import test_visits_lifecycle_races as lifecycle
from core.visits import VisitConfig, VisitService
from core.visits.persistence import KEY
from core.visits.transport import VisitTransport, VisitTransportError


class VisitCloseAdmissionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = lifecycle.VisitLifecycleRaceTests.asyncSetUp
    pair = lifecycle.VisitLifecycleRaceTests.pair
    exchange = lifecycle.VisitLifecycleRaceTests.exchange

    async def test_close_during_local_read_cannot_create_or_send_a_new_wire(self):
        visit, _ = self.pair(arrived=True, automatic=True)
        before = copy.deepcopy(visit)
        draws = copy.deepcopy(self.a._state["draws"])
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.a_actor.describe

        async def blocked(scope):
            entered.set()
            await release.wait()
            return await original(scope)

        self.a_actor.describe = blocked
        self.a.transport.request = AsyncMock(side_effect=self.exchange)
        sending = asyncio.create_task(self.a._send(visit["id"], {"op": "status"}))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await self.a.close()
        finally:
            release.set()
        with self.assertRaises(ValueError):
            await sending
        self.a.transport.request.assert_not_awaited()
        self.assertEqual(visit, before)
        self.assertEqual(self.a._state["draws"], draws)
        self.assertEqual(self.a_actor.calls, [])

    async def test_close_during_save_keeps_the_original_wire_for_reload_without_sending(self):
        visit, _ = self.pair(arrived=True, automatic=True)
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.a.db.call
        draws = copy.deepcopy(self.a._state["draws"])

        async def blocked(operation, key, value):
            if operation == "kv_set" and self.a._state["visits"][visit["id"]].get("outbox"):
                entered.set()
                await release.wait()
            return await original(operation, key, value)

        self.a.db.call = blocked
        self.a.transport.request = AsyncMock(side_effect=self.exchange)
        payload = {"op": "sync", "visit": self.a._wire(visit)}
        sending = asyncio.create_task(self.a._send(visit["id"], payload))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            old_wire = copy.deepcopy(visit["outbox"])
            await self.a.close()
        finally:
            release.set()
        with self.assertRaises(ValueError):
            await sending
        self.a.transport.request.assert_not_awaited()
        self.a.db.call = original
        stored = await self.a.db.call("kv_get", KEY, {})
        self.assertEqual(stored["visits"][visit["id"]]["outbox"], old_wire)
        self.assertEqual(self.a._state["draws"], draws)

        resumed = VisitService(self.a.db, self.a.config, self.a_actor, clock=lambda: self.now)
        self.addAsyncCleanup(resumed.close)
        await resumed._load()
        request = AsyncMock(side_effect=self.exchange)
        resumed.transport.request = request
        await resumed._send(visit["id"], payload)
        self.assertEqual(request.await_args.args[1], old_wire)
        self.assertNotIn("outbox", resumed._state["visits"][visit["id"]])
        self.assertEqual(resumed._state["draws"], draws)
        self.assertEqual(self.a_actor.calls, [])

    async def test_close_during_reply_read_does_not_expire_or_rewrite_the_accepted_wire(self):
        visit, _ = self.pair(arrived=True, automatic=True)
        entered, release = asyncio.Event(), asyncio.Event()
        original, reads = self.a_actor.describe, 0

        async def blocked(scope):
            nonlocal reads
            reads += 1
            if reads == 2:
                entered.set()
                await release.wait()
            return await original(scope)

        self.a_actor.describe = blocked
        request = AsyncMock(side_effect=self.exchange)
        self.a.transport.request = request
        sending = asyncio.create_task(self.a._send(visit["id"], {"op": "status"}))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            admitted = copy.deepcopy(visit)
            await self.a.close()
            self.now = visit["expires_at"] + 1
        finally:
            release.set()
        with self.assertRaises(ValueError):
            await sending
        request.assert_awaited_once()
        self.assertEqual(visit, admitted)
        self.assertEqual(self.a_actor.calls, [])

    async def test_real_tick_keeps_a_saved_wire_and_does_not_finish_or_retry_after_close(self):
        visit, _ = self.pair(arrived=True, automatic=True)
        self.now = visit["next_at"]
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.a.db.call

        async def blocked(operation, key, value):
            if operation == "kv_set" and visit.get("outbox"):
                entered.set()
                await release.wait()
            return await original(operation, key, value)

        self.a.db.call = blocked
        self.a.transport.request = AsyncMock(side_effect=self.exchange)
        ticking = asyncio.create_task(self.a.tick())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            admitted = copy.deepcopy(visit)
            draws = copy.deepcopy(self.a._state["draws"])
            await self.a.close()
        finally:
            release.set()
        await asyncio.wait_for(ticking, 1)
        self.a.transport.request.assert_not_awaited()
        self.assertEqual(visit, admitted)
        self.assertEqual(self.a._state["draws"], draws)
        self.assertEqual(self.a_actor.calls, [])
        self.assertFalse(self.a._tick_lock.locked())


class VisitTransportCloseTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_transport_cannot_reopen_but_a_new_instance_can_serve(self):
        config = VisitConfig(enabled=True, listen_host="127.0.0.1", listen_port=0)
        old = VisitTransport(AsyncMock(), config)
        await old.close()
        with patch("core.visits.transport.aiohttp.ClientSession") as factory:
            with self.assertRaises(VisitTransportError):
                await old.request("http://not-contacted.example", {"message": "old wire"})
            with self.assertRaises(VisitTransportError):
                await old.start()
        factory.assert_not_called()
        self.assertIsNone(old.client)
        self.assertIsNone(old.runner)

        handler = AsyncMock(return_value={"received": True})
        fresh = VisitTransport(handler, config)
        self.addAsyncCleanup(fresh.close)
        await fresh.start()
        reply = await fresh.request(f"http://127.0.0.1:{fresh.bound_port}", {"message": "fresh wire"})
        self.assertEqual(reply, {"received": True})
        handler.assert_awaited_once()

    async def test_repeated_cancellation_and_concurrent_close_share_one_complete_cleanup(self):
        entered, release = asyncio.Event(), asyncio.Event()
        channel = VisitTransport(None, VisitConfig())

        async def cleanup():
            entered.set()
            await release.wait()

        runner = SimpleNamespace(cleanup=AsyncMock(side_effect=cleanup))
        client = SimpleNamespace(close=AsyncMock())
        channel.runner, channel.client = runner, client
        closing = asyncio.create_task(channel.close())
        concurrent = None
        try:
            await asyncio.wait_for(entered.wait(), 1)
            closing.cancel()
            await asyncio.sleep(0)
            closing.cancel()
            await asyncio.sleep(0)
            concurrent = asyncio.create_task(channel.close())
            await asyncio.sleep(0)
            self.assertFalse(closing.done())
            self.assertFalse(concurrent.done())
            self.assertIsNone(channel.runner)
            self.assertIsNone(channel.client)
            with self.assertRaises(VisitTransportError):
                await channel.request("http://not-contacted.example", {})
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(closing, 1)
        if concurrent is not None:
            await asyncio.wait_for(concurrent, 1)
        runner.cleanup.assert_awaited_once()
        client.close.assert_awaited_once()
        self.assertTrue(channel._close_task.done())
        await channel.close()

    async def test_listener_cleanup_failure_still_closes_the_original_client(self):
        channel = VisitTransport(None, VisitConfig())
        runner = SimpleNamespace(cleanup=AsyncMock(side_effect=RuntimeError("isolated cleanup failure")))
        client = SimpleNamespace(close=AsyncMock())
        channel.runner, channel.client = runner, client
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                await channel.close()
        runner.cleanup.assert_awaited_once()
        client.close.assert_awaited_once()
        self.assertIsNone(channel.client)

    async def test_start_waiting_for_bind_cannot_register_a_listener_after_close(self):
        entered, release = asyncio.Event(), asyncio.Event()
        channel = VisitTransport(None, VisitConfig())
        runner = SimpleNamespace(setup=AsyncMock(), cleanup=AsyncMock())

        async def bind():
            entered.set()
            await release.wait()

        site = SimpleNamespace(start=AsyncMock(side_effect=bind))
        with patch("core.visits.transport.web.AppRunner", return_value=runner), \
                patch("core.visits.transport.web.TCPSite", return_value=site):
            starting = asyncio.create_task(channel.start())
            try:
                await asyncio.wait_for(entered.wait(), 1)
                await channel.close()
            finally:
                release.set()
            with self.assertRaises((VisitTransportError, asyncio.CancelledError)):
                await starting
        runner.cleanup.assert_awaited_once()
        self.assertIsNone(channel.runner)

    async def test_close_drains_a_bound_startup_before_a_new_instance_takes_the_same_port(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = web.TCPSite.start
        config = VisitConfig(enabled=True, listen_host="127.0.0.1", listen_port=0)
        old = VisitTransport(None, config)
        self.addAsyncCleanup(old.close)
        sites = []

        async def hold_bound(site):
            await original(site)
            if not sites:
                sites.append(site)
                entered.set()
                await release.wait()

        with patch.object(web.TCPSite, "start", hold_bound):
            starting = asyncio.create_task(old.start())
            try:
                await asyncio.wait_for(entered.wait(), 1)
                port = sites[0]._server.sockets[0].getsockname()[1]
                await asyncio.wait_for(old.close(), 1)
                self.assertTrue(old._start_task.done())
                fresh = VisitTransport(None, replace(config, listen_port=port))
                self.addAsyncCleanup(fresh.close)
                await asyncio.wait_for(fresh.start(), 1)
                self.assertEqual(fresh.bound_port, port)
                self.assertIsNone(old.runner)
            finally:
                release.set()
            with self.assertRaises((VisitTransportError, asyncio.CancelledError)):
                await starting

    async def test_failed_startup_cleanup_is_reported_and_still_releases_the_client(self):
        entered = asyncio.Event()
        channel = VisitTransport(None, VisitConfig())
        runner = SimpleNamespace(setup=AsyncMock(),
            cleanup=AsyncMock(side_effect=RuntimeError("isolated startup cleanup failure")))

        async def bind():
            entered.set()
            await asyncio.Future()

        site = SimpleNamespace(start=AsyncMock(side_effect=bind))
        client = SimpleNamespace(close=AsyncMock())
        channel.client = client
        with patch("core.visits.transport.web.AppRunner", return_value=runner), \
                patch("core.visits.transport.web.TCPSite", return_value=site):
            starting = asyncio.create_task(channel.start())
            await asyncio.wait_for(entered.wait(), 1)
            with self.assertRaisesRegex(RuntimeError, "startup cleanup failure"):
                await channel.close()
            with self.assertRaises(RuntimeError):
                await starting
        runner.cleanup.assert_awaited_once()
        client.close.assert_awaited_once()
        self.assertIsNone(channel.client)
