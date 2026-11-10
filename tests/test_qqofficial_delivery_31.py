"""Official transport limitations must not become successful world receipts."""
from __future__ import annotations

import importlib
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from tests import test_main_import as main_fixture
from tests import test_visit_card_adapter as card_fixture


GROUP = "official:GroupMessage:group-openid"
PRIVATE = "official:FriendMessage:member-openid"


def official_platform(*, family="qq_official", scene=None, ids=None, proactive=False):
    return SimpleNamespace(meta=lambda: SimpleNamespace(name=family, id="official"),
        _session_scene=dict(scene or {}), _session_last_message_id=dict(ids or {}),
        _allow_group_proactive_send=proactive)


class OfficialMessenger31Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        module = main_fixture.load_plugin_module()
        adapter = importlib.import_module(module.__name__ + ".adapters.messenger")
        self.platform = official_platform()
        self.events = {}
        self.other_bot = SimpleNamespace(call_action=AsyncMock(return_value=True))
        self.other_event = SimpleNamespace(bot=self.other_bot,
            get_platform_name=lambda: "aiocqhttp", unified_msg_origin="onebot:GroupMessage:123")
        self.context = SimpleNamespace(get_platform_inst=lambda _pid: self.platform,
            send_message=AsyncMock(return_value=True))
        self.plugin = SimpleNamespace(context=self.context, logger=Mock(),
            last_event=lambda session: self.events.get(session),
            last_event_any=lambda: self.other_event, _last_events=self.events,
            typing_delay_for=lambda _text: 0)
        self.messenger = adapter.AstrBotMessenger(self.plugin)

    async def test_official_poke_cannot_use_a_silent_drop_or_borrow_other_bot(self):
        for family in ("qq_official", "qq_official_webhook"):
            with self.subTest(family=family):
                self.platform = official_platform(family=family)
                result = await self.messenger.poke(GROUP, "member-openid")
                self.assertFalse(result.ok)
        self.context.send_message.assert_not_awaited()
        self.other_bot.call_action.assert_not_awaited()

    async def test_onebot_false_segment_result_is_not_success_and_true_is(self):
        self.platform = SimpleNamespace(meta=lambda: SimpleNamespace(name="aiocqhttp", id="official"))
        self.context.send_message.return_value = False
        result = await self.messenger.poke(GROUP, "42")
        self.assertFalse(result.ok)
        self.context.send_message.return_value = True
        result = await self.messenger.poke(GROUP, "42")
        self.assertTrue(result.ok)
        self.assertEqual(self.context.send_message.await_count, 2)

    async def test_missing_official_scene_or_message_cannot_create_text_or_image_receipts(self):
        self.assertFalse(await self.messenger.send_text(GROUP, ["还没有送达的生活分享"]))
        self.assertEqual(self.messenger.take_sent_texts(GROUP), [])
        self.messenger.mark_sent(GROUP)  # Exercise the image gate independently of text's cooldown.
        self.assertFalse(await self.messenger.send_images(GROUP, ["https://example.invalid/card.png"]))
        self.assertEqual(self.messenger.take_sent_images(GROUP), [])
        self.context.send_message.assert_not_awaited()

    async def test_official_group_card_does_not_borrow_another_bots_onebot_client(self):
        result = await self.messenger.set_group_card(GROUP, "新的群名片")
        self.assertFalse(result.ok)
        self.assertEqual(await self.messenger.fetch_group_card(GROUP), "")
        self.other_bot.call_action.assert_not_awaited()

    async def test_known_proactive_group_and_private_still_send(self):
        self.platform = official_platform(scene={"group-openid": "group"}, proactive=True)
        self.assertTrue(await self.messenger.send_text(GROUP, ["已经确认的生活分享"]))
        self.assertEqual(self.messenger.take_sent_texts(GROUP), ["已经确认的生活分享"])
        self.assertTrue(await self.messenger.send_images(GROUP, ["https://example.invalid/card.png"]))
        self.assertEqual(self.messenger.take_sent_images(GROUP), ["https://example.invalid/card.png"])
        self.assertTrue(await self.messenger.send_text(PRIVATE, ["私聊问候"]))
        self.assertEqual(self.context.send_message.await_count, 3)

    async def test_cached_reply_context_and_scoped_group_id_are_usable(self):
        self.platform = official_platform(ids={"group-openid": "source-msg"})
        self.assertTrue(await self.messenger.send_text(GROUP, ["有来源消息可回复"]))
        self.platform = official_platform(scene={"group-openid": "group"}, proactive=True)
        scoped = "official:GroupMessage:member-openid_group-openid"
        self.assertTrue(await self.messenger.send_text(scoped, ["同一官方群的独立会话"]))
        self.assertEqual(self.context.send_message.await_count, 2)

    async def test_other_adapters_and_unknown_sdk_contract_keep_existing_transport(self):
        for platform in (SimpleNamespace(meta=lambda: SimpleNamespace(name="aiocqhttp")),
                         SimpleNamespace(meta=lambda: SimpleNamespace(name="qq_official"))):
            with self.subTest(platform=platform):
                self.platform = platform
                self.assertTrue(await self.messenger.send_text(GROUP, ["走现有宿主通道"]))
        self.assertEqual(self.context.send_message.await_count, 2)


class OfficialCard31Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        card_fixture.CardAdapterTests.setUp(self)
        self.enabled.add(GROUP)
        self.owners[GROUP] = "main"
        self.plugin.config["visits_record_card_sessions"] = [GROUP]
        self.platform = official_platform()
        self.plugin.context.get_platform_inst = lambda _pid: self.platform

    async def test_missing_official_delivery_context_does_not_confirm_card(self):
        with self.assertRaises(card_fixture.cards.RecordCardRetryableError):
            await self.port.send_record_card("main", self.visit, GROUP, "base64://card")
        self.plugin.context.send_message.assert_not_awaited()
        self.platform._session_scene["group-openid"] = "group"
        self.platform._allow_group_proactive_send = True
        await self.port.send_record_card("main", self.visit, GROUP, "base64://card")
        self.plugin.context.send_message.assert_awaited_once()

    async def test_webui_rendering_does_not_require_qq_delivery_context(self):
        renderer = SimpleNamespace(render=AsyncMock(return_value=["base64://preview"]))
        self.port._card_renderer = lambda: renderer
        self.assertEqual(await self.port.render_record_card("main", self.visit), ["base64://preview"])
        renderer.render.assert_awaited_once_with("main", self.visit, timezone="Asia/Shanghai")
        self.plugin.context.send_message.assert_not_awaited()
