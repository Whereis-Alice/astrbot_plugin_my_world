"""Real HTTP limits and listener lifecycle without providers or external messages."""
import asyncio
from dataclasses import replace
import unittest
import aiohttp
from aiohttp import web
from core.visits import VisitConfig
from core.visits.protocol import MAX_BYTES
from core.visits.transport import VisitTransport, VisitTransportError


class VisitTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = 0
        async def handler(body):
            self.calls += 1
            return {"received": body["message"]}
        self.config = VisitConfig(enabled=True, listen_host="127.0.0.1", listen_port=0)
        self.channel = VisitTransport(handler, self.config)
        await self.channel.start()
        self.url = f"http://127.0.0.1:{self.channel.bound_port}"
        self.addAsyncCleanup(self.channel.close)

    async def test_round_trip_and_start_are_idempotent(self):
        runner = self.channel.runner
        await self.channel.start()
        self.assertIs(self.channel.runner, runner)
        self.assertEqual(await self.channel.request(self.url, {"message": "邀请"}), {"received": "邀请"})

    async def test_bad_input_never_reaches_handler(self):
        async with aiohttp.ClientSession() as client:
            for data, expected in ((b"x" * 40000, 413), (b'{"value": NaN}', 400), (b"not json", 400)):
                async with client.post(self.url + "/v1/message", data=data) as reply:
                    self.assertEqual(reply.status, expected)
        self.assertEqual(self.calls, 0)

    async def test_close_releases_exact_port_for_reload(self):
        port = self.channel.bound_port
        await self.channel.close()
        second = VisitTransport(self.channel.handler, replace(self.config, listen_port=port))
        self.addAsyncCleanup(second.close)
        await second.start()
        self.assertEqual(second.bound_port, port)
        self.assertIsNone(self.channel.runner)

    async def test_redirect_does_not_forward_signed_payload(self):
        app = web.Application()
        async def redirect(request):
            raise web.HTTPFound(self.url + "/v1/message")
        app.router.add_post("/v1/message", redirect)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        with self.assertRaises(ValueError):
            await self.channel.request(url, {"message": "signed content"})
        self.assertEqual(self.calls, 0)

    async def response_server(self, *, status=200, raw=b"{}", delay=0):
        app = web.Application()
        async def respond(request):
            if delay:
                await asyncio.sleep(delay)
            return web.Response(status=status, body=raw)
        app.router.add_post("/v1/message", respond)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        return f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"

    async def test_http_status_is_a_channel_failure_without_echoing_unsigned_body(self):
        for status in (400, 404, 429, 503):
            with self.subTest(status=status):
                url = await self.response_server(status=status, raw=b"private upstream diagnostic")
                with self.assertRaises(VisitTransportError) as raised:
                    await self.channel.request(url, {"message": "probe"})
                error = raised.exception
                self.assertIsInstance(error, ValueError)
                self.assertEqual(error.category, "channel")
                self.assertEqual(error.http_status, status)
                self.assertIn(f"HTTP {status}", str(error))
                self.assertNotIn("private upstream diagnostic", str(error))

    async def test_invalid_json_values_share_a_safe_protocol_error(self):
        cases = (b"<html>untrusted diagnostic</html>", b'{"value": NaN}',
                 b'{"value": Infinity}', b"\xff", b"[]", b"null")
        for raw in cases:
            with self.subTest(raw=raw):
                url = await self.response_server(raw=raw)
                with self.assertRaises(VisitTransportError) as raised:
                    await self.channel.request(url, {"message": "probe"})
                self.assertEqual(raised.exception.category, "protocol")
                self.assertIsNone(raised.exception.http_status)
                self.assertEqual(str(raised.exception), "对方返回的串门数据格式无效")

    async def test_oversized_reply_is_a_protocol_error_and_is_bounded(self):
        url = await self.response_server(raw=b"x" * (MAX_BYTES + 1))
        with self.assertRaises(VisitTransportError) as raised:
            await self.channel.request(url, {"message": "probe"})
        self.assertEqual(raised.exception.category, "protocol")
        self.assertEqual(str(raised.exception), "对方返回的串门消息过长")

    async def chunked_response_server(self, chunks):
        app = web.Application()
        async def respond(request):
            response = web.StreamResponse(headers={"Content-Type": "application/json"})
            await response.prepare(request)
            try:
                for chunk in chunks:
                    await response.write(chunk)
                    await asyncio.sleep(.01)
                await response.write_eof()
            except ConnectionResetError:
                pass  # A bounded client may close once the overflow byte arrives.
            return response
        app.router.add_post("/v1/message", respond)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        return f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"

    async def test_valid_chunked_reply_waits_for_the_complete_bounded_document(self):
        url = await self.chunked_response_server((b'{"received":', b'"complete"', b'}'))
        self.assertEqual(await self.channel.request(url, {"message": "probe"}), {"received": "complete"})

    async def test_oversized_chunked_reply_is_rejected_as_overflow(self):
        url = await self.chunked_response_server((b'{"received":"', b"x" * MAX_BYTES, b'"}'))
        with self.assertRaises(VisitTransportError) as raised:
            await self.channel.request(url, {"message": "probe"})
        self.assertEqual(raised.exception.category, "protocol")
        self.assertEqual(str(raised.exception), "对方返回的串门消息过长")

    async def test_connection_and_timeout_are_channel_errors(self):
        await self.channel.close()
        with self.assertRaises(VisitTransportError) as raised:
            await self.channel.request(self.url, {"message": "probe"})
        self.assertEqual(raised.exception.category, "channel")
        self.assertIsNone(raised.exception.http_status)
        await self.channel.close()
        self.channel.config = replace(self.config, timeout_seconds=.01)
        url = await self.response_server(delay=.1)
        with self.assertRaises(VisitTransportError) as raised:
            await self.channel.request(url, {"message": "probe"})
        self.assertEqual(raised.exception.category, "channel")
        self.assertEqual(str(raised.exception), "暂时联系不上对方，请检查串门服务")

    async def test_invalid_outgoing_data_does_not_leak_serialisation_details(self):
        with self.assertRaises(VisitTransportError) as raised:
            await self.channel.request(self.url, {"message": float("nan")})
        self.assertEqual(raised.exception.category, "protocol")
        self.assertEqual(str(raised.exception), "串门请求数据格式无效")
        self.assertEqual(self.calls, 0)
