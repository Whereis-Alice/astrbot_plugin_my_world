"""Media processing is request-local, bounded and releases every temporary lease."""
import asyncio
import importlib
import sys
import unittest
from types import SimpleNamespace, ModuleType
from unittest.mock import AsyncMock, patch

from tests import test_dialogue_inject as fixture

ProactiveMedia = importlib.import_module(fixture.MODULE.__name__ + ".adapters.proactive_media").ProactiveMedia


class ProactiveMediaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.motion = SimpleNamespace(handle=AsyncMock(), release_media_request=AsyncMock())
        self.allowed = ["*"]
        context = SimpleNamespace(get_config=lambda _: {"plugin_set": self.allowed},
            get_all_stars=lambda: [SimpleNamespace(name="astrbot_plugin_motion_vision", activated=True, star_cls=self.motion)])
        self.plugin = SimpleNamespace(context=context, engine=SimpleNamespace(), vision=None)
        self.media = ProactiveMedia(self.plugin)
        self.event = fixture.Event("图片", session="test:GroupMessage:a")
        self.event._extras = {"original": {"value": 1}}
        self.media.remember(self.event, {"message_id": "4711"})
        self.row = {"origin": self.event.unified_msg_origin, "message_id": "4711", "images": ["original.gif"], "text": "图片"}
        provider = ModuleType("astrbot.api.provider")
        provider.ProviderRequest = SimpleNamespace
        self.patch = patch.dict(sys.modules, {"astrbot.api.provider": provider})
        self.patch.start()
        self.addCleanup(self.patch.stop)

    async def test_dynamic_frames_and_text_are_scoped_without_mutating_live_event(self):
        async def handle(event, request):
            event._extras["original"]["value"] = 2
            request.image_urls = []
            request.extra_user_content_parts = [{"text": "连续两帧"}, {"image_url": {"url": "frame1.png"}}]
        self.motion.handle.side_effect = handle
        async with self.media.context(self.event.unified_msg_origin, [self.row]) as data:
            self.assertEqual(data["images"], ["frame1.png"])
            self.assertIn("连续两帧", data["text"])
            self.assertIn("4711", data["text"])
            self.motion.release_media_request.assert_not_awaited()
        self.assertEqual(self.event._extras["original"]["value"], 1)
        self.motion.release_media_request.assert_awaited_once()

    async def test_other_conversation_and_own_messages_are_excluded(self):
        async with self.media.context("test:GroupMessage:b", [self.row]) as data:
            self.assertEqual(data, {"images": [], "text": ""})
        async with self.media.context(self.event.unified_msg_origin, [{**self.row, "is_self": True}]) as data:
            self.assertFalse(data["images"])
        self.motion.handle.assert_not_awaited()

    async def test_disabled_plugin_keeps_static_fallback(self):
        self.allowed = []
        async with self.media.context(self.event.unified_msg_origin, [self.row]) as data:
            self.assertEqual(data["images"], ["original.gif"])
        self.motion.handle.assert_not_awaited()

    async def test_hook_failure_still_releases_lease(self):
        self.motion.handle.side_effect = RuntimeError("decode failed")
        async with self.media.context(self.event.unified_msg_origin, [self.row]) as data:
            self.assertIn("暂时无法解析", data["text"])
        self.motion.release_media_request.assert_awaited_once()

    async def test_model_cancellation_releases_media_and_does_not_touch_live_event(self):
        with self.assertRaises(asyncio.CancelledError):
            async with self.media.context(self.event.unified_msg_origin, [self.row]):
                raise asyncio.CancelledError()
        self.motion.release_media_request.assert_awaited_once()

    async def test_caption_failure_is_explicit_without_inventing_a_scene(self):
        self.plugin.vision = SimpleNamespace(enabled=True, describe=AsyncMock(side_effect=RuntimeError("offline")))
        async with self.media.context(self.event.unified_msg_origin, [self.row]) as data:
            self.assertFalse(data["images"])
            self.assertIn("不猜测", data["text"])
        self.motion.release_media_request.assert_awaited_once()

    async def test_cache_is_bounded_and_cleared_on_close(self):
        for i in range(300):
            self.media.remember(self.event, {"message_id": str(i)})
        self.assertEqual(len(self.media.events), 256)
        self.media.close()
        self.assertFalse(self.media.events)

    async def test_changed_life_during_media_processing_discards_and_releases(self):
        helper = SimpleNamespace(module=fixture.MODULE)
        plugin, _ = fixture.DialogueInjectTests._plugin(helper, self.event.unified_msg_origin)
        self.addAsyncCleanup(plugin.db.close)
        self.addAsyncCleanup(plugin.engine.close_background)
        self.plugin.engine = plugin.engine
        life = importlib.import_module(fixture.MODULE.__name__ + ".core.engine.life_inference")

        async def changed(event, request):
            async with plugin.engine.session_state(self.event.unified_msg_origin) as state:
                state.energy = .1
        self.motion.handle.side_effect = changed
        reached = []

        @life.life_work()
        async def infer(engine):
            async with engine.session_state(self.event.unified_msg_origin) as state:
                state.energy = .9
                async with self.media.context(self.event.unified_msg_origin, [self.row]):
                    reached.append(True)
        await asyncio.wait_for(infer(plugin.engine), timeout=3)
        self.assertFalse(reached)
        self.motion.release_media_request.assert_awaited_once()
        self.assertEqual((await plugin.engine.load_state(self.event.unified_msg_origin)).energy, .1)
