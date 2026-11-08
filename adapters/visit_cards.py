"""Opt-in encounter cards: local HTML first, bounded fallbacks, exact destinations."""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
from pathlib import Path
import tempfile
from typing import Any
from urllib.parse import urlsplit

from ..core.visit_card import (CARD_SCALE, CARD_WIDTH, build_visit_card_pages,
                               render_visit_card_html, visit_card_template)
from ..core.visits.record_cards import RecordCardRetryableError


def _image_ref(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("renderer returned no image")
    value = value.strip()
    if value.startswith(("http://", "https://", "base64://", "file://")):
        return value
    path = Path(value)
    if not path.is_file():
        raise ValueError("renderer returned a missing image")
    return path.resolve().as_uri()


def _local_endpoint(value: str) -> bool:
    try:
        host = urlsplit(value).hostname or ""
        if host.lower() in {"localhost", "host.docker.internal"} or "." not in host and ":" not in host:
            return bool(host)
        address = ipaddress.ip_address(host)
        return address.is_private or address.is_loopback
    except ValueError:
        return False


def _page_text(page: dict) -> str:
    """A complete readable fallback, with the same evidence labels as the card."""
    lines = ["小世界 · 相见札记", page["title"],
             f"{page['local_name']} 与 {page['remote_name']}",
             f"{page['date']}　{page['interval']}"]
    for entry in page["entries"]:
        if entry["category"] == "route":
            lines.append("一起走过")
            for stop in entry["stops"]:
                lines.append(f"{stop['index']}. {stop['place']} · {stop['activity']}")
            continue
        label = entry.get("tag") if entry["category"] in {"chronicle", "result"} else entry.get("speaker")
        lines.append("\n" + " · ".join(str(value) for value in (label, entry.get("time")) if value))
        for part in entry["parts"]:
            lines.append((part["label"] + "：" if part["label"] else "") + part["text"])
    if page["last"]:
        lines.extend(["\n" + page["status"], page["end_reason"]])
    return "\n\n".join(lines)


def _draw_plain_card(page: dict, target: Path) -> str:
    """Render source strings literally; Markdown must never load transcript URLs."""
    from astrbot.core.utils.t2i.local_strategy import FontManager
    from PIL import Image, ImageDraw

    font = FontManager.get_font(25)
    margin, available = 54, CARD_WIDTH - 108
    rows: list[str] = []
    text = _page_text(page).replace("\r\n", "\n").replace("\r", "\n").expandtabs(4)
    for line in text.split("\n"):
        if not line:
            rows.append("")
        while line:
            low, high = 1, len(line)
            while low <= high:
                middle = (low + high) // 2
                if font.getlength(line[:middle]) <= available:
                    low = middle + 1
                else:
                    high = middle - 1
            count = max(1, high)
            rows.append(line[:count])
            line = line[count:]
    try:
        ascent, descent = font.getmetrics()
        line_height = ascent + descent + 10
    except AttributeError:
        left, top, right, bottom = font.getbbox("Ag国")
        line_height = bottom - top + 10
    with Image.new("RGB", (CARD_WIDTH, margin * 2 + line_height * len(rows)), "#272b48") as image:
        draw = ImageDraw.Draw(image)
        for index, line in enumerate(rows):
            draw.text((margin, margin + index * line_height), line, fill="#f1edff", font=font)
        image.save(target, "PNG")
    return target.resolve().as_uri()


class VisitCardRenderer:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self._files: dict[str, tempfile.TemporaryDirectory] = {}

    def _endpoint(self, scope: str) -> str:
        getter = getattr(self.plugin.context, "get_config", None)
        if not callable(getter):
            return ""
        config = getter(scope) or {}
        return str(config.get("t2i_endpoint") or "").strip()

    @staticmethod
    def _options() -> dict:
        return {"full_page": True, "type": "png", "quality": None,
                "viewport": {"width": CARD_WIDTH, "height": 1000},
                "device_scale_factor": CARD_SCALE, "selector": ".card", "scale": "device"}

    async def _endpoint_pages(self, endpoint: str, pages: list[dict]) -> list[str]:
        from astrbot.core.utils.t2i.network_strategy import NetworkRenderStrategy
        strategy = NetworkRenderStrategy(endpoint)
        return [_image_ref(await asyncio.wait_for(strategy.render_custom_template(
            visit_card_template(), page, return_url=False, options=self._options()), 40)) for page in pages]

    async def _browser_pages(self, pages: list[dict]) -> list[str]:
        from playwright.async_api import async_playwright
        temporary = tempfile.TemporaryDirectory(prefix="my_world_visit_card_")
        refs: list[str] = []
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True, args=["--no-sandbox"])
                try:
                    context = await browser.new_context(
                        viewport={"width": CARD_WIDTH, "height": 1000}, device_scale_factor=CARD_SCALE)
                    # The template is self-contained. No remote assets or user URLs are fetched.
                    await context.route("**/*", lambda route: route.abort())
                    page = await context.new_page()
                    for index, data in enumerate(pages):
                        await page.set_content(render_visit_card_html(data), wait_until="load", timeout=15000)
                        await page.evaluate("document.fonts.ready")
                        path = Path(temporary.name) / f"page-{index + 1:04}.png"
                        await page.locator(".card").screenshot(path=str(path), type="png", timeout=20000)
                        refs.append(path.resolve().as_uri())
                finally:
                    await browser.close()
            for ref in refs:
                self._files[ref] = temporary
            return refs
        except BaseException:
            temporary.cleanup()
            raise

    async def _host_pages(self, pages: list[dict]) -> list[str]:
        return [_image_ref(await asyncio.wait_for(self.plugin.html_render(
            visit_card_template(), page, return_url=False, options=self._options()), 45)) for page in pages]

    async def _plain_pages(self, pages: list[dict]) -> list[str]:
        temporary = tempfile.TemporaryDirectory(prefix="my_world_visit_text_")
        refs: list[str] = []
        try:
            for index, page in enumerate(pages):
                worker = asyncio.create_task(asyncio.to_thread(
                    _draw_plain_card, page, Path(temporary.name) / f"card-{index + 1:04}.png"))
                try:
                    refs.append(await asyncio.shield(worker))
                except BaseException:
                    # Join the writer before deleting its directory on unload.
                    await asyncio.gather(worker, return_exceptions=True)
                    raise
            for ref in refs:
                self._files[ref] = temporary
            return refs
        except BaseException:
            temporary.cleanup()
            raise

    async def render(self, scope: str, visit: dict, *, timezone: str) -> list[str]:
        pages = build_visit_card_pages(visit, timezone=timezone)
        if not pages:
            return []
        if len(pages) != 1:
            raise ValueError("相见记录应生成一张完整长图")
        endpoint = self._endpoint(scope)
        strategies = []
        if endpoint and _local_endpoint(endpoint):
            strategies.append(("本地 t2i", lambda: self._endpoint_pages(endpoint, pages)))
        strategies.append(("本地浏览器", lambda: self._browser_pages(pages)))
        if endpoint and not _local_endpoint(endpoint):
            strategies.append(("配置的 t2i", lambda: self._endpoint_pages(endpoint, pages)))
        strategies.extend([("AstrBot HTML", lambda: self._host_pages(pages)),
                           ("本地文字图片", lambda: self._plain_pages(pages))])
        for name, render in strategies:
            try:
                refs = await render()
                if len(refs) != len(pages) or not refs:
                    raise ValueError("renderer returned incomplete pages")
                return refs
            except Exception as exc:
                # Endpoints and exception strings may contain credentials or record text.
                self.plugin.logger.debug(f"[my_world] 相见卡片 {name} 不可用：{type(exc).__name__}")
        raise RuntimeError("相见记录的所有图片渲染方式暂时不可用")

    def release(self, pages: list[Any]) -> None:
        temporary = {id(value): value for ref in pages if (value := self._files.pop(str(ref), None))}
        for value in temporary.values():
            value.cleanup()


class VisitCardActorMixin:
    async def _card_identity_current(self, scope: str, visit: dict) -> None:
        expected = visit.get("life_identity")
        if not expected:
            return
        with self.engine.config_scope(scope):
            _persona, key = await self.engine._journal_persona(scope)
        if hashlib.sha256(str(key or "").encode("utf-8")).hexdigest() != expected:
            raise RecordCardRetryableError("相见记录所属的角色已经切换")

    def record_card_targets(self, scope: str, visit: dict) -> list[str]:
        plugin, engine = self.plugin, self.engine
        config = getattr(plugin, "config", {})
        if (getattr(plugin, "retired", False) or not getattr(plugin, "enabled", False)
                or config.get("visits_record_card_enabled") is not True
                or not scope or self.canonical_scope(scope) != scope
                or visit.get("scope") != scope):
            return []
        selected = config.get("visits_record_card_sessions", [])
        if not isinstance(selected, list):
            return []
        targets = []
        for session in selected:
            if not isinstance(session, str) or not session or session in targets:
                continue
            # Reject group ids and never fan out a selection to its other members.
            if engine.session_config(session) is None or engine.group_by_id(session) is not None:
                continue
            with engine.config_scope(session):
                if engine.is_enabled(session) and engine.state_key(session) == scope:
                    targets.append(session)
        return targets

    def _card_renderer(self) -> VisitCardRenderer:
        renderer = getattr(self, "_visit_card_renderer", None)
        if renderer is None:
            renderer = self._visit_card_renderer = VisitCardRenderer(self.plugin)
        return renderer

    async def render_record_card(self, scope: str, visit: dict) -> list[str]:
        await self._card_identity_current(scope, visit)
        with self.engine.config_scope(scope):
            timezone = self.engine.world.timezone
        return await self._card_renderer().render(scope, visit, timezone=timezone)

    def release_record_card(self, pages: list[Any]) -> None:
        renderer = getattr(self, "_visit_card_renderer", None)
        if renderer is not None:
            renderer.release(pages)

    async def send_record_card(self, scope: str, visit: dict, session: str, page: Any) -> None:
        from astrbot.api.event import MessageChain
        from astrbot.api.message_components import Image
        if session not in self.record_card_targets(scope, visit):
            raise RecordCardRetryableError("相见卡片订阅已关闭，或会话已换组")
        await self._card_identity_current(scope, visit)
        if session not in self.record_card_targets(scope, visit):
            raise RecordCardRetryableError("相见卡片订阅已关闭，或会话已换组")
        messenger = self.engine.messenger
        if callable(getattr(messenger, "blocked", None)) and messenger.blocked(session):
            raise RecordCardRetryableError("平台处于发送失败冷却中")
        ref = _image_ref(page)
        # This explicit subscription has its own authorization. Do not inherit a
        # life-share guard or the conversational reply target of an earlier task.
        try:
            result = await self.plugin.context.send_message(session, MessageChain(chain=[Image(file=ref)]))
        except Exception as exc:
            if callable(getattr(messenger, "mark_failed", None)):
                messenger.mark_failed(session, exc)
            raise
        if not result:
            raise RuntimeError("平台没有确认接收相见卡片")
        if callable(getattr(messenger, "mark_sent", None)):
            messenger.mark_sent(session)
