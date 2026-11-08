"""Card subscriptions and renderer fallbacks never broaden delivery permission."""
from __future__ import annotations

import importlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from tests import fake_astrbot

fake_astrbot.install()
VisitActorPort = importlib.import_module("tests.test_visits_actor").VisitActorPort

cards = importlib.import_module("visit_adapter_fixture.adapters.visit_cards")


class CardAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from contextlib import nullcontext
        self.enabled = {"main", "second", "other"}
        self.owners = {"main": "main", "second": "main", "other": "other"}
        self.engine = SimpleNamespace(
            scope_session=lambda value: "main" if value == "shared" else value,
            state_key=lambda value: self.owners.get(value, value),
            is_enabled=lambda value: value in self.enabled,
            session_config=lambda value: value if value in self.owners else None,
            group_by_id=lambda value: object() if value == "shared" else None,
            config_scope=lambda value: nullcontext(),
            messenger=SimpleNamespace(blocked=lambda value: False),
            world=SimpleNamespace(timezone="Asia/Shanghai"))
        self.plugin = SimpleNamespace(
            enabled=True, retired=False, engine=self.engine,
            config={"visits_record_card_enabled": True,
                    "visits_record_card_sessions": ["second", "shared", "other", "second"]},
            context=SimpleNamespace(get_config=lambda _: {}, send_message=AsyncMock(return_value=True)),
            logger=SimpleNamespace(debug=lambda _: None), html_render=AsyncMock())
        self.port = VisitActorPort(self.plugin)
        self.visit = {"scope": "main", "status": "completed", "arrived_at": 100,
                      "local": {"name": "艾拉"}, "remote": {"name": "爱丽丝"}, "log": []}

    def test_exact_sessions_no_shared_life_fanout_and_default_off(self):
        self.assertEqual(self.port.record_card_targets("main", self.visit), ["second"])
        self.plugin.config.pop("visits_record_card_enabled")
        self.assertEqual(self.port.record_card_targets("main", self.visit), [])

    async def test_revocation_and_regrouping_stop_delivery(self):
        self.owners["second"] = "other"
        with self.assertRaises(cards.RecordCardRetryableError):
            await self.port.send_record_card("main", self.visit, "second", "base64://anything")
        self.plugin.context.send_message.assert_not_awaited()

    async def test_subscription_is_independent_of_earlier_speech_guard(self):
        from core.speech_policy import guard_send
        with guard_send(lambda *_: False):
            await self.port.send_record_card("main", self.visit, "second", "base64://anything")
        self.plugin.context.send_message.assert_awaited_once()
        self.plugin.context.send_message.return_value = False
        with self.assertRaises(RuntimeError):
            await self.port.send_record_card("main", self.visit, "second", "base64://anything")

    async def test_cooldown_is_definitely_before_transport(self):
        self.engine.messenger.blocked = lambda _: True
        with self.assertRaises(cards.RecordCardRetryableError):
            await self.port.send_record_card("main", self.visit, "second", "base64://anything")
        self.plugin.context.send_message.assert_not_awaited()

    async def test_persona_switch_blocks_old_cards_before_transport(self):
        import hashlib
        self.visit["life_identity"] = hashlib.sha256(b"old persona").hexdigest()
        self.engine._journal_persona = AsyncMock(return_value=("private new prompt", "new persona"))
        with self.assertRaises(cards.RecordCardRetryableError):
            await self.port.send_record_card("main", self.visit, "second", "base64://anything")
        self.plugin.context.send_message.assert_not_awaited()

    async def test_local_t2i_precedes_browser_and_host(self):
        self.plugin.context.get_config = lambda _: {"t2i_endpoint": "http://127.0.0.1:9900"}
        renderer = cards.VisitCardRenderer(self.plugin)
        renderer._endpoint_pages = AsyncMock(side_effect=RuntimeError("offline"))
        renderer._browser_pages = AsyncMock(return_value=["base64://page"])
        renderer._host_pages = AsyncMock()
        self.assertEqual(await renderer.render("main", self.visit, timezone="UTC"), ["base64://page"])
        renderer._endpoint_pages.assert_awaited_once()
        renderer._browser_pages.assert_awaited_once()
        renderer._host_pages.assert_not_awaited()

    async def test_host_then_local_text_fallback_and_no_cancellation_swallow(self):
        renderer = cards.VisitCardRenderer(self.plugin)
        renderer._browser_pages = AsyncMock(side_effect=RuntimeError())
        renderer._host_pages = AsyncMock(side_effect=RuntimeError())
        renderer._plain_pages = AsyncMock(return_value=["base64://page"])
        self.assertEqual(await renderer.render("main", self.visit, timezone="UTC"), ["base64://page"])
        renderer._plain_pages.assert_awaited_once()
        import asyncio
        renderer._browser_pages.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await renderer.render("main", self.visit, timezone="UTC")

    async def test_safe_complete_fallback_text(self):
        visit = {**self.visit, "log": [{"kind": "action_plan", "text": "打算去散步", "seq": 1},
                                      {"kind": "result", "text": "散步遇到下雨", "seq": 2}]}
        page = cards.build_visit_card_pages(visit)[0]
        text = cards._page_text(page)
        self.assertIn("当时的打算：打算去散步", text)
        self.assertIn("发生的结果：散步遇到下雨", text)

    def test_release_cleans_only_owned_images(self):
        renderer = cards.VisitCardRenderer(self.plugin)
        temporary = tempfile.TemporaryDirectory(prefix="test_visit_card_")
        path = Path(temporary.name)
        ref = (path / "page.png").as_uri()
        renderer._files[ref] = temporary
        renderer.release([ref, "https://example.test/other.png"])
        self.assertFalse(path.exists())
        self.assertEqual(renderer._files, {})
