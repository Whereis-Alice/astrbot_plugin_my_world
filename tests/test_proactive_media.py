"""Media processing is request-local, bounded and releases every temporary lease."""
import asyncio
import importlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace, ModuleType
from unittest.mock import AsyncMock, patch

from tests import test_dialogue_inject as fixture

media_module = importlib.import_module(fixture.MODULE.__name__ + ".adapters.proactive_media")
ProactiveMedia = media_module.ProactiveMedia


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
        image_utils = ModuleType("astrbot.core.utils.media_utils")
        self.prepare = AsyncMock(side_effect=lambda ref, **kw: "prepared:" + ref)
        image_utils.prepare_model_image = self.prepare
        self.patch = patch.dict(sys.modules, {"astrbot.api.provider": provider,
            "astrbot.core.utils.media_utils": image_utils})
        self.patch.start()
        self.addCleanup(self.patch.stop)

    async def test_dynamic_frames_and_text_are_scoped_without_mutating_live_event(self):
        async def handle(event, request):
            event._extras["original"]["value"] = 2
            request.image_urls = []
            request.extra_user_content_parts = [{"text": "连续两帧"}, {"image_url": {"url": "frame1.png"}}]
        self.motion.handle.side_effect = handle
        async with self.media.context(self.event.unified_msg_origin, [self.row]) as data:
            self.assertEqual(data["images"], ["prepared:frame1.png"])
            self.assertIn("连续两帧", data["text"])
            self.assertIn("4711", data["text"])
            self.motion.release_media_request.assert_not_awaited()
        self.assertEqual(self.event._extras["original"]["value"], 1)
        self.motion.release_media_request.assert_awaited_once()

    async def test_latest_video_does_not_borrow_an_older_messages_image(self):
        video = {"origin": self.row["origin"], "message_id": "video-2", "has_videos": True, "text": "视频"}
        async with self.media.context(self.row["origin"], [self.row, video]) as data:
            self.assertEqual(data['images'], [])
            self.assertIn('video-2', data['text'])
            self.assertIn('视频', data['text'])
        self.motion.handle.assert_not_awaited()

    async def test_each_prepared_image_retains_its_own_message_identity(self):
        self.plugin.context.get_all_stars = lambda: []
        other = {**self.row, "message_id": "other-image", "images": ["other.png"]}
        async with self.media.context(self.row['origin'], [self.row, other]) as data:
            self.assertEqual(data['images'], ['prepared:original.gif', 'prepared:other.png'])
            self.assertIn('4711', data['image_contexts'][0]['text'])
            self.assertNotIn('other-image', data['image_contexts'][0]['text'])
            self.assertIn('other-image', data['image_contexts'][1]['text'])

    async def test_video_snapshot_is_used_only_from_the_exact_selected_message(self):
        video = {**self.row, 'images': [], 'has_videos': True}
        async with self.media.context(self.row['origin'], [video]) as data:
            self.assertEqual(data['media_event'].unified_msg_origin, self.row['origin'])
            self.assertEqual(data['images'], [])
        self.motion.handle.assert_not_awaited()

    async def test_other_conversation_and_own_messages_are_excluded(self):
        async with self.media.context("test:GroupMessage:b", [self.row]) as data:
            self.assertEqual(data, {"images": [], "text": ""})
        async with self.media.context(self.event.unified_msg_origin, [{**self.row, "is_self": True}]) as data:
            self.assertFalse(data["images"])
        self.motion.handle.assert_not_awaited()

    async def test_disabled_plugin_keeps_static_fallback(self):
        self.allowed = []
        async with self.media.context(self.event.unified_msg_origin, [self.row]) as data:
            self.assertEqual(data["images"], ["prepared:original.gif"])
        self.motion.handle.assert_not_awaited()

    async def test_hook_failure_still_releases_lease(self):
        self.motion.handle.side_effect = RuntimeError("decode failed")
        async with self.media.context(self.event.unified_msg_origin, [self.row]) as data:
            self.assertIn("暂时无法解析", data["text"])
        self.motion.release_media_request.assert_awaited_once()

    async def test_missing_or_expired_event_still_prepares_original_images(self):
        for missing in (False, True):
            if missing:
                self.media.events.clear()
            else:
                key = next(iter(self.media.events))
                self.media.events[key] = (-1000, self.media.events[key][1])
            async with self.media.context(self.event.unified_msg_origin, [self.row]) as data:
                self.assertEqual(data["images"], ["prepared:original.gif"])
        self.motion.handle.assert_not_awaited()

    async def test_failed_normalization_drops_only_bad_image_before_captioning(self):
        self.prepare.side_effect = lambda ref, **kw: None if ref == "original.gif" else "prepared:" + ref
        self.plugin.vision = SimpleNamespace(enabled=True, describe=AsyncMock(return_value=["一杯茶"]))
        row = {**self.row, "images": ["original.gif", "tea.png"]}
        async with self.media.context(self.event.unified_msg_origin, [row]) as data:
            self.assertEqual(data["images"], [])
            self.assertIn("未能读取", data["text"])
            self.assertIn("一杯茶", data["text"])
        self.assertEqual(self.plugin.vision.describe.call_args.args[0], ["prepared:tea.png"])
        self.assertEqual(self.plugin.vision.describe.call_args.kwargs["cache_sources"], ["model-image-v1:tea.png"])

    async def test_motion_replacement_is_not_reintroduced_by_extra_images(self):
        async def handle(event, request):
            request.image_urls = ["frame.png"]
        self.motion.handle.side_effect = handle
        async with self.media.context(self.event.unified_msg_origin, [self.row],
                images=["original.gif", "tool.gif"]) as data:
            self.assertEqual(data["images"], ["prepared:frame.png", "prepared:tool.gif"])
        self.assertEqual([call.args[0] for call in self.prepare.call_args_list], ["frame.png", "tool.gif"])

    async def test_tool_images_are_prepared_even_without_chat_rows(self):
        async with self.media.context(self.event.unified_msg_origin, [], images=["tool.gif"]) as data:
            self.assertEqual(data["images"], ["prepared:tool.gif"])
        self.motion.handle.assert_not_awaited()

    async def test_current_sdk_image_result_uses_only_its_path(self):
        self.prepare.side_effect = lambda ref, **kw: ('prepared:' + ref, True, True, ref)
        async with self.media.context(self.event.unified_msg_origin, [], images=['tool.gif']) as data:
            self.assertEqual(data['images'], ['prepared:tool.gif'])
            self.assertIn('不同时刻', data['text'])

    async def test_animation_metadata_reaches_caption_model_and_main_context(self):
        self.prepare.side_effect = lambda ref, **kw: ('prepared:' + ref, True, True, ref)
        self.plugin.vision = SimpleNamespace(enabled=True, describe=AsyncMock(return_value=['一段动画']))
        async with self.media.context(self.event.unified_msg_origin, [self.row]) as data:
            self.assertFalse(data['images'])
            self.assertIn('不同时刻', data['text'])
            self.assertIn('不同时刻', self.plugin.vision.describe.await_args.kwargs['question'])

    async def test_current_sdk_reused_original_is_preserved_after_request(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temporary:
            original = Path(temporary) / 'original.png'
            original.write_bytes(b'synthetic')
            self.prepare.side_effect = lambda ref, **kw: (str(original), False, False, str(original))
            async with self.media.context(self.event.unified_msg_origin, [], images=[str(original)]) as data:
                self.assertEqual(data['images'], [str(original)])
            self.assertTrue(original.is_file())

    async def test_malformed_sdk_result_is_not_stringified_into_an_image(self):
        for result in (('', False, False, 'original.gif'), (None, True, True, 'original.gif'),
                       ('path',), True, ['path'], {'path': 'path'}):
            with self.subTest(result=result):
                self.prepare.side_effect = None
                self.prepare.return_value = result
                async with self.media.context(self.event.unified_msg_origin, [], images=['tool.gif']) as data:
                    self.assertEqual(data['images'], [])
                    self.assertIn('未能读取', data['text'])

    async def test_prepared_files_live_until_model_returns_and_cleanup_on_cancel(self):
        created = []
        async def prepare(ref, **kw):
            path = kw["output_dir"] / "image.png"
            path.write_bytes(b"synthetic")
            created.append(path)
            return str(path)
        self.prepare.side_effect = prepare
        with self.assertRaises(asyncio.CancelledError):
            async with self.media.context(self.event.unified_msg_origin, [self.row]) as data:
                self.assertTrue(Path(data["images"][0]).is_file())
                raise asyncio.CancelledError()
        self.assertTrue(created)
        self.assertFalse(created[0].parent.exists())
        self.motion.release_media_request.assert_awaited_once()

    async def test_preprocessing_cancellation_releases_both_workspaces(self):
        directories = []
        async def prepare(ref, **kw):
            directories.append(kw["output_dir"])
            (kw["output_dir"] / "partial.png").write_bytes(b"synthetic")
            raise asyncio.CancelledError()
        self.prepare.side_effect = prepare
        with self.assertRaises(asyncio.CancelledError):
            async with self.media.context(self.event.unified_msg_origin, [self.row]):
                self.fail("cancelled preprocessing must not enter the model")
        self.assertFalse(directories[0].exists())
        self.motion.release_media_request.assert_awaited_once()

    async def test_normalization_is_deduplicated_and_bounded(self):
        refs = ["same.png", "same.png", *[f"image-{i}.gif" for i in range(20)]]
        async with self.media.context(self.event.unified_msg_origin, [], images=refs) as data:
            self.assertEqual(len(data["images"]), 12)
            self.assertEqual(self.prepare.await_count, 12)
            self.assertIn("未能读取", data["text"])

    async def test_normalization_timeout_does_not_return_the_original(self):
        self.prepare.side_effect = TimeoutError()
        async with self.media.context(self.event.unified_msg_origin, [self.row]) as data:
            self.assertFalse(data["images"])
            self.assertIn("未能读取", data["text"])

    async def test_exhausted_budget_keeps_successes_without_more_preparation(self):
        prepared = media_module.PreparedImages()
        self.addCleanup(prepared.close)
        images, failed = await prepared.prepare(["first.png"])
        self.assertEqual(images, ["prepared:first.png"])
        self.assertFalse(failed)
        prepared.remaining_seconds = 0
        images, failed = await prepared.prepare(["first.png", "second.gif"])
        self.assertEqual(images, ["prepared:first.png"])
        self.assertTrue(failed)
        self.prepare.assert_awaited_once()

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

    async def test_empty_caption_is_reported_as_missing_perception(self):
        self.plugin.vision = SimpleNamespace(enabled=True, describe=AsyncMock(return_value=[""]))
        async with self.media.context(self.event.unified_msg_origin, [self.row]) as data:
            self.assertFalse(data["images"])
            self.assertIn("没有取得有效转述", data["text"])

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
