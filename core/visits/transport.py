"""A dedicated bounded HTTP channel, separate from the dashboard and its credentials."""
from __future__ import annotations

import asyncio
import json

import aiohttp
from aiohttp import web

from .protocol import MAX_BYTES, canonical


class VisitTransport:
    def __init__(self, handler, config):
        self.handler, self.config = handler, config
        self.runner = None
        self.client = None
        self.bound_port = config.listen_port

    async def start(self):
        if self.runner is not None:
            return
        app = web.Application(client_max_size=MAX_BYTES)
        app.router.add_post("/v1/message", self._handle)
        app.router.add_post("/v1/pair", self._handle)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            site = web.TCPSite(runner, self.config.listen_host, self.config.listen_port)
            await site.start()
            self.bound_port = site._server.sockets[0].getsockname()[1]
            self.runner = runner
        except BaseException:
            await runner.cleanup()
            raise

    async def _handle(self, request):
        try:
            raw = await request.read()
            if len(raw) > MAX_BYTES:
                raise ValueError("消息过长")
            body = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
            reply = await self.handler(body)
            return web.Response(body=canonical(reply), content_type="application/json")
        except web.HTTPRequestEntityTooLarge:
            return web.json_response({"error": "消息过长"}, status=413)
        except (ValueError, TypeError, KeyError, RecursionError):
            return web.json_response({"error": "串门请求被拒绝，请检查配对和旅程状态"}, status=400)

    async def request(self, url, body):
        data = canonical(body)
        if len(data) > MAX_BYTES:
            raise ValueError("串门消息过长")
        if self.client is None or self.client.closed:
            self.client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.config.timeout_seconds),
                                                trust_env=False)
        try:
            async with self.client.post(url.rstrip("/") + "/v1/message", data=data,
                                        headers={"Content-Type": "application/json"}, allow_redirects=False) as response:
                if response.status != 200:
                    raise ValueError("对方暂时不能接收，请检查配对、地址和状态")
                raw = await response.content.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise ValueError("对方返回的消息过长")
                result = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
                if not isinstance(result, dict):
                    raise ValueError("对方返回无效协议数据")
                return result
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise ValueError("暂时联系不上对方，请检查串门服务") from exc

    async def close(self):
        runner, self.runner = self.runner, None
        if runner is not None:
            await runner.cleanup()
        client, self.client = self.client, None
        if client is not None:
            await client.close()
