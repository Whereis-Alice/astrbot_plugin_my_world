"""Real HTTP limits and listener lifecycle without providers or external messages."""
from dataclasses import replace
import unittest
import aiohttp
from aiohttp import web
from core.visits import VisitConfig
from core.visits.transport import VisitTransport


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
