"""Background observation must not replay ambient words as a direct user request."""
from __future__ import annotations

import json
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

from tests import test_proactive_tool_contract as contract


class Reply:
    def __init__(self, message_id, sender_id, sender_name, text):
        self.id, self.sender_id, self.sender_nickname = message_id, sender_id, sender_name
        self.message_str, self.chain = text, []


class At:
    def __init__(self, qq):
        self.qq, self.name = qq, ""


class Video:
    pass


class ObserverContextTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = contract.ProactiveToolContractTests.asyncSetUp
    fixture = contract.ProactiveToolContractTests.fixture
    options = contract.ProactiveToolContractTests.options

    def source(self):
        source = self.plugin._last_events[self.session]
        source.message_str = "你们刚才忙完了吗，我下午才过来"
        source.is_wake, source.is_at_or_wake_command, source.call_llm = True, True, True
        source.message_obj.message = [Reply("parent", "guest-a", "甲", "@另一个朋友 好了"), At("guest-a")]
        source.message_obj.sender = types.SimpleNamespace(user_id="42", nickname="tester")
        return source

    def observation(self, request):
        part = next(part for part in request.extra_user_content_parts
                    if part.text.startswith("# 已观察的原生来源"))
        return json.loads(part.text.rsplit("\n", 1)[1])

    async def test_native_builder_sees_observation_not_bare_source_words(self):
        seen = []

        async def hook(plugin, event, name, request):
            seen.append((event, request, request.prompt))
            return False

        native = self.fixture(hook)
        source = self.source()
        host = sys.modules["astrbot.core.astr_main_agent"]
        original_build = host.build_main_agent
        built = []

        async def build(**kwargs):
            built.append((kwargs["event"], kwargs["req"].prompt))
            return await original_build(**kwargs)

        host.build_main_agent = build
        options = self.options()
        prepared = await native.prepare(options)
        self.assertEqual(built[0][1], native_module(native).OBSERVER_REQUEST)
        self.assertEqual(seen[0][2], native_module(native).OBSERVER_REQUEST)
        self.assertNotEqual(prepared["prompt"], source.message_str)
        self.assertNotIn(source.message_str, prepared["prompt"])
        data = self.observation(native.request)
        self.assertEqual(data["text"], source.message_str)
        self.assertEqual(data["origin"], self.session)
        self.assertEqual(data["message_id"], "4711")
        self.assertEqual(data["author"], {"id": "42", "name": "tester", "is_self": False})
        self.assertEqual(data["quoted_message"]["id"], "guest-a")
        self.assertEqual(data["quoted_message"]["message_id"], "parent")
        self.assertEqual(data["quoted_message"]["text"], "@另一个朋友 好了")
        self.assertFalse(data["quoted_message"]["is_self"])
        self.assertEqual(data["at_targets"], [{"id": "guest-a", "name": "guest-a", "self": False}])
        self.assertIs(seen[0][1], native.request)
        self.assertTrue(all(part._no_save for part in native.request.extra_user_content_parts))

    async def test_only_inference_copy_loses_native_dispatch_flags(self):
        native = self.fixture(AsyncMock(return_value=False))
        source = self.source()
        await native.prepare(self.options())
        for flag in ("is_wake", "is_at_or_wake_command", "call_llm"):
            self.assertFalse(getattr(native.event, flag), flag)
            self.assertTrue(getattr(source, flag), flag)
        public = native_module(native).isolated_event(source, self.plugin.context, self.session)
        for flag in ("is_wake", "is_at_or_wake_command", "call_llm"):
            self.assertTrue(getattr(public, flag), flag)
        native.event.message_obj.message[0].sender_id = "changed"
        native.event.set_extra("background_only", True)
        self.assertEqual(source.message_obj.message[0].sender_id, "guest-a")
        self.assertIsNone(source.get_extra("background_only"))

    async def test_quote_body_opt_out_preserves_identity_without_sending_saved_text(self):
        native = self.fixture(AsyncMock(return_value=False))
        source = self.source()
        self.plugin.engine.world.context.quote_chars = 0
        await native.prepare(self.options())
        data = self.observation(native.request)
        quote = data["quoted_message"]
        self.assertEqual(quote["message_id"], "parent")
        self.assertEqual(quote["id"], "guest-a")
        self.assertTrue(quote["text_omitted"])
        self.assertNotIn("text", quote)
        self.assertNotIn(source.message_obj.message[0].message_str, json.dumps(data, ensure_ascii=False))

    async def test_quote_body_limit_marks_truncation_without_copying_persisted_text(self):
        native = self.fixture(AsyncMock(return_value=False))
        source = self.source()
        self.plugin.engine.world.context.quote_chars = 4
        await native.prepare(self.options())
        quote = self.observation(native.request)["quoted_message"]
        self.assertEqual(quote["text"], source.message_obj.message[0].message_str[:4])
        self.assertEqual(len(quote["text"]), 4)
        self.assertTrue(quote["text_truncated"])
        self.assertNotIn("text_omitted", quote)

    async def test_source_annotation_is_separate_and_does_not_rewrite_native_hook_text(self):
        native = self.fixture(AsyncMock(return_value=False))
        source = self.source()
        body = source.message_str
        note = "［引用或媒体附注：" + "旧消息内容" * 80 + "］"
        source.message_str += "\n" + note
        await native.prepare(self.options())
        data = self.observation(native.request)
        self.assertEqual(data["text"], body)
        self.assertEqual(data["context_note"], note[:240])
        self.assertEqual(native.event.message_str, body + "\n" + note)
        self.assertEqual(source.message_str, body + "\n" + note)

    async def test_host_name_prefix_tools_and_hook_content_are_preserved(self):
        allowed = contract.Tools(["lookup_weather"])
        seen = []

        async def hook(plugin, event, name, request):
            seen.append(request.func_tool)
            request.extra_user_content_parts.append(contract.Part("世界书补充，仍属于这次观察").mark_as_temp())
            return False

        native = self.fixture(hook)
        source = self.source()
        native.tool_agent = types.SimpleNamespace(prepare=AsyncMock(return_value=allowed))
        host = sys.modules["astrbot.core.astr_main_agent"]
        original_build = host.build_main_agent

        async def build(**kwargs):
            result = await original_build(**kwargs)
            result.provider_request.prompt = "tester(42)：" + result.provider_request.prompt
            return result

        host.build_main_agent = build
        prepared = await native.prepare(self.options())
        self.assertTrue(prepared["prompt"].startswith(native_module(native).OBSERVER_REQUEST))
        self.assertIn("tester(42)：", prepared["prompt"])
        self.assertIs(native.toolset, allowed)
        self.assertEqual(seen, [allowed])
        self.assertEqual(native.event.get_sender_id(), source.get_sender_id())
        texts = "\n".join(part.text for part in prepared["extra_user_content_parts"])
        self.assertIn("世界书补充", texts)
        self.assertEqual(texts.count(self.options()["prompt"]), 1)
        self.assertNotIn(source.message_str, prepared["prompt"])

    async def test_hook_rebuilding_prompt_cannot_restore_a_bare_direct_user_turn(self):
        async def hook(plugin, event, name, request):
            request.prompt = event.message_str
            request.extra_user_content_parts.append(contract.Part("保留宿主附加资料").mark_as_temp())
            return False

        native = self.fixture(hook)
        source = self.source()
        prepared = await native.prepare(self.options())
        self.assertTrue(prepared["prompt"].startswith(native_module(native).OBSERVER_REQUEST))
        self.assertIn(source.message_str, prepared["prompt"])
        self.assertNotEqual(source.message_str, prepared["prompt"])
        self.assertEqual(self.observation(native.request)["author"]["id"], source.get_sender_id())
        self.assertIn("保留宿主附加资料", "\n".join(part.text for part in prepared["extra_user_content_parts"]))

    async def test_observer_binding_preserves_media_and_one_native_generation(self):
        native = self.fixture(AsyncMock(return_value=False))
        source = self.source()
        source.message_obj.message.append(Video())
        native.speech["media_event"] = source
        native._collect_video = AsyncMock()
        runner_patch = patch.object(native_module(native), "run_agent",
            AsyncMock(return_value=types.SimpleNamespace(completion_text='{"say":"下午好"}')))
        runner_patch.start()
        self.addCleanup(runner_patch.stop)
        host = sys.modules["astrbot.core.astr_main_agent"]
        host.prepare_request_images = AsyncMock()
        host._provider_supports_modality = lambda *_: True
        utilities = types.ModuleType("astrbot.core.utils.media_utils")
        utilities.normalize_model_image_max_size = lambda value: value
        native.config = None
        options = {**self.options(), "image_urls": ["https://image.invalid/observed.png"]}
        with patch.dict(sys.modules, {utilities.__name__: utilities}):
            # Match the actual host's media config without decoding or downloading.
            original_config = native_module(native).build_config

            def config(*args):
                result = original_config(*args)
                result.provider_settings = {}
                return result

            with patch.object(native_module(native), "build_config", config):
                prepared = await native.prepare(options)
        native._collect_video.assert_awaited_once_with(native.request)
        self.assertEqual(prepared["image_urls"], options["image_urls"])
        self.assertEqual(self.observation(native.request)["attachments"], ["Video"])
        self.plugin.engine.native_turn_busy = lambda *_: False
        response = await native.invoke(**prepared)
        runner = native_module(native).run_agent
        runner.assert_awaited_once()
        self.assertIs(runner.call_args.kwargs["event"], native.event)
        self.assertIs(runner.call_args.kwargs["request"], native.request)
        self.assertEqual(runner.call_args.kwargs["max_steps"], 1)
        self.assertEqual(runner.call_args.kwargs["request_max_retries"], 1)
        self.assertEqual(str(response.completion_text), '{"say":"下午好"}')


def native_module(owner):
    return sys.modules[type(owner).__module__]


if __name__ == "__main__":
    unittest.main()
