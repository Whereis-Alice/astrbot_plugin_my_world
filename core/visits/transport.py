"""A dedicated bounded HTTP channel, separate from the dashboard and its credentials."""
from __future__ import annotations

import asyncio
import json
from typing import Literal

import aiohttp
from aiohttp import web

from .protocol import MAX_BYTES, canonical
from .ports import VisitStateError


class VisitTransportError(ValueError):
    """A safe diagnostic with stable meaning for protocol and editor callers."""

    def __init__(self, message: str, *, category: Literal["channel", "protocol"],
                 http_status: int | None = None) -> None:
        super().__init__(message)
        self.category = category
        self.http_status = http_status


def _decode_reply(raw: bytes) -> dict:
    try:
        result = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (ValueError, TypeError, RecursionError) as exc:
        raise VisitTransportError("对方返回的串门数据格式无效", category="protocol") from exc
    if not isinstance(result, dict):
        raise VisitTransportError("对方返回的串门数据格式无效", category="protocol")
    return result


class VisitTransport:
    def __init__(self, handler, config):
        self.handler, self.config = handler, config
        self.runner = None
        self.client = None
        self.bound_port = config.listen_port
        self._closed = False
        self._start_task = None
        self._close_task = None

    async def start(self):
        self._open()
        if self.runner is not None:
            return
        if self._start_task is None or self._start_task.done():
            self._start_task = asyncio.create_task(self._start_listener(), name="world-visit-listener-start")
        task = self._start_task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            task.cancel()
            try:
                await self._drain_cleanup(task)
            finally:
                raise

    async def _start_listener(self):
        app = web.Application(client_max_size=MAX_BYTES)
        app.router.add_post("/v1/message", self._handle)
        app.router.add_post("/v1/pair", self._handle)
        runner = web.AppRunner(app, access_log=None)
        try:
            await runner.setup()
            self._open()
            site = web.TCPSite(runner, self.config.listen_host, self.config.listen_port)
            await site.start()
            self._open()
            self.bound_port = site._server.sockets[0].getsockname()[1]
            self.runner = runner
        except BaseException:
            await self._drain_cleanup(asyncio.create_task(runner.cleanup(), name="world-visit-listener-cleanup"))
            raise

    async def _handle(self, request):
        try:
            # One request has one deadline, even if it reads several lives or
            # rechecks a directory more than once before saving its receipt.
            async with asyncio.timeout(self.config.timeout_seconds):
                raw = await request.read()
                if len(raw) > MAX_BYTES:
                    raise ValueError("消息过长")
                body = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
                reply = await self.handler(body)
                return web.Response(body=canonical(reply), content_type="application/json")
        except web.HTTPRequestEntityTooLarge:
            return web.json_response({"error": "消息过长"}, status=413)
        except (TimeoutError, VisitStateError):
            return web.json_response({"error": "暂时无法确认当前生活状态，请稍后再联系"}, status=503)
        except (ValueError, TypeError, KeyError, RecursionError):
            return web.json_response({"error": "串门请求被拒绝，请检查配对和旅程状态"}, status=400)

    async def request(self, url, body):
        self._open()
        try:
            data = canonical(body)
        except (ValueError, TypeError, RecursionError) as exc:
            raise VisitTransportError("串门请求数据格式无效", category="protocol") from exc
        if len(data) > MAX_BYTES:
            raise VisitTransportError("串门消息过长", category="protocol")
        if self.client is None or self.client.closed:
            self.client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.config.timeout_seconds),
                                                trust_env=False)
        try:
            async with self.client.post(url.rstrip("/") + "/v1/message", data=data,
                                        headers={"Content-Type": "application/json"}, allow_redirects=False) as response:
                if response.status != 200:
                    raise VisitTransportError(f"对方串门服务暂时无法接收（HTTP {response.status}），请检查地址和服务状态",
                                              category="channel", http_status=response.status)
                try:
                    # A proxy may split JSON across chunks. Wait for EOF while
                    # retaining only the allowed bytes plus one overflow byte.
                    raw = await response.content.readexactly(MAX_BYTES + 1)
                except asyncio.IncompleteReadError as exc:
                    raw = exc.partial
                if len(raw) > MAX_BYTES:
                    raise VisitTransportError("对方返回的串门消息过长", category="protocol")
                return _decode_reply(raw)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            raise VisitTransportError("暂时联系不上对方，请检查串门服务", category="channel") from exc

    def _open(self):
        if self._closed:
            raise VisitTransportError("本次串门服务已关闭，旧联系作废", category="channel")

    async def close(self):
        if self._close_task is None:
            self._closed = True
            starting = self._start_task
            starting_pending = starting is not None and not starting.done()
            if starting_pending:
                starting.cancel()
            runner, self.runner = self.runner, None
            client, self.client = self.client, None

            async def release():
                try:
                    if starting_pending:
                        result, = await asyncio.gather(starting, return_exceptions=True)
                        if isinstance(result, Exception) and not isinstance(result, VisitTransportError):
                            raise result  # Do not report a failed listener cleanup as released.
                    if runner is not None:
                        await runner.cleanup()
                finally:
                    if client is not None:
                        await client.close()

            self._close_task = asyncio.create_task(release(), name="world-visit-channel-close")
        await self._drain_cleanup(self._close_task)

    @staticmethod
    async def _drain_cleanup(task):
        """An interrupted caller still owns its listener and session cleanup."""
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                break
        try:
            task.result()
        except BaseException:
            if cancelled:
                raise asyncio.CancelledError() from None
            raise
        if cancelled:
            raise asyncio.CancelledError()
