"""Provider settings stay independent through plugin construction and dispatch."""

import asyncio
import importlib
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from tests import test_scope_api as scope
from tests.test_dialogue_inject import Event


class IndependentProviderDefaultsTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = scope.ScopeApiTests.asyncSetUp

    def make_plugin(self, **config):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        with patch.dict(os.environ, {"MY_WORLD_DATA_DIR": directory.name}):
            plugin = self.module.MyWorldPlugin(
                self.module.Context(), self.module.AstrBotConfig({"enabled": True, **config}))
        self.addAsyncCleanup(plugin.db.close)
        plugin.context.llm_generate = AsyncMock(return_value=SimpleNamespace(
            completion_text="独立请求的结果", role="assistant"))
        plugin.context.get_current_chat_provider_id = AsyncMock(return_value="session-default")
        return plugin

    @staticmethod
    def channels(plugin):
        engine = plugin.engine
        return {
            "speech": engine.speech_llm, "llm": engine.llm,
            "helper": engine.helper_llm, "judge": engine.judge_llm,
            "creator": engine.creator_llm, "event": engine.event_llm,
            "consolidate": engine.consolidate_llm,
        }

    async def test_constructed_channels_dispatch_to_their_own_explicit_settings(self):
        settings = {f"{slot}_provider_id": f"selected-{slot}"
                    for slot in self.channels(self.plugin)}
        plugin = self.make_plugin(**settings)
        for slot, channel in self.channels(plugin).items():
            with self.subTest(slot=slot):
                reply = await channel.generate(session_id=self.session, system_prompt="角色", prompt=slot)
                self.assertTrue(reply.ok)
                self.assertEqual(plugin.context.llm_generate.call_args.kwargs["chat_provider_id"],
                                 f"selected-{slot}")
        self.assertEqual(plugin.context.llm_generate.await_count, 7)
        plugin.context.get_current_chat_provider_id.assert_not_awaited()

    async def test_blank_channels_do_not_inherit_configured_life_or_helper(self):
        plugin = self.make_plugin(llm_provider_id="life-selected", helper_provider_id="helper-selected")
        for slot, channel in self.channels(plugin).items():
            with self.subTest(slot=slot):
                await channel.generate(session_id=self.session, system_prompt="角色", prompt=slot)
                expected = {"llm": "life-selected", "helper": "helper-selected"}.get(slot, "session-default")
                self.assertEqual(plugin.context.llm_generate.call_args.kwargs["chat_provider_id"], expected)
        self.assertEqual(plugin.context.llm_generate.await_count, 7)

    async def test_session_defaults_are_resolved_each_time_without_cross_session_cache(self):
        plugin = self.make_plugin(llm_provider_id="unrelated-life")
        second = "test:GroupMessage:200"
        defaults = {self.session: "first-default", second: "second-default"}
        plugin.context.get_current_chat_provider_id.side_effect = lambda session: defaults[session]
        await asyncio.gather(*[
            plugin.engine.speech_llm.generate(session_id=session, system_prompt="角色", prompt=session)
            for session in (self.session, second)])
        actual = {call.kwargs["prompt"]: call.kwargs["chat_provider_id"]
                  for call in plugin.context.llm_generate.call_args_list}
        self.assertEqual(actual, defaults)
        defaults[self.session] = "first-new-default"
        await plugin.engine.speech_llm.generate(session_id=self.session, system_prompt="角色", prompt="切换后")
        self.assertEqual(plugin.context.llm_generate.call_args.kwargs["chat_provider_id"], "first-new-default")

    async def test_legacy_context_setting_does_not_fill_an_empty_helper(self):
        plugin = self.make_plugin(context_provider_id="legacy-context", llm_provider_id="life-selected")
        self.assertEqual(plugin.helper_provider_id, "")
        await plugin.engine.helper_llm.generate(session_id=self.session, system_prompt="角色", prompt="整理")
        self.assertEqual(plugin.context.llm_generate.call_args.kwargs["chat_provider_id"], "session-default")

    async def test_main_image_capability_uses_speech_or_session_provider(self):
        self.plugin.llm_provider_id = "life-with-images"
        self.plugin.speech_provider_id = ""
        self.plugin.context.get_current_chat_provider_id = AsyncMock(return_value="session-text-only")
        providers = {
            "session-text-only": SimpleNamespace(provider_config={"modalities": ["text"]}),
            "speech-with-images": SimpleNamespace(provider_config={"modalities": ["text", "image"]}),
            "life-with-images": SimpleNamespace(provider_config={"modalities": ["text", "image"]}),
        }
        self.plugin.context.get_provider_by_id = Mock(side_effect=providers.__getitem__)
        self.assertFalse(await self.plugin._main_model_sees_images(self.session))
        self.plugin.speech_provider_id = "speech-with-images"
        self.assertTrue(await self.plugin._main_model_sees_images(self.session))
        self.assertEqual([call.args[0] for call in self.plugin.context.get_provider_by_id.call_args_list],
                         ["session-text-only", "speech-with-images"])
        self.plugin.context.get_current_chat_provider_id.assert_awaited_once_with(self.session)

    async def test_image_relation_uses_actual_message_origin_inside_another_life_scope(self):
        plugin = self.make_plugin(llm_provider_id="life-selected", vision_provider_id="vision-selected")
        plugin._recent_chat_lines = AsyncMock(return_value=[])
        plugin.context.llm_generate.return_value.completion_text = "与刚才提到的茶杯相符"
        representative = "test:GroupMessage:representative"
        plugin.context.get_current_chat_provider_id.side_effect = lambda session: "default-for-" + session

        async def describe(sources, **kwargs):
            return await plugin.vision._relate(["一只茶杯" for _ in sources], kwargs["question"])

        plugin.vision.describe = AsyncMock(side_effect=describe)
        event = Event("这个茶杯", session=self.session)
        with plugin.engine.config_scope(representative):
            captions = await plugin._caption_old_images(event, ["cup.png"], sources=["cup.png"])
        self.assertEqual(captions, ["图1：与刚才提到的茶杯相符"])
        plugin.context.get_current_chat_provider_id.assert_awaited_once_with(self.session)
        self.assertEqual(plugin.context.llm_generate.call_args.kwargs["chat_provider_id"],
                         "default-for-" + self.session)

    async def test_explicit_image_relation_helper_does_not_consult_session_default(self):
        plugin = self.make_plugin(llm_provider_id="life-selected", helper_provider_id="relation-selected",
                                  vision_provider_id="vision-selected")
        plugin.context.llm_generate.return_value.completion_text = "与话题有关"
        calls = importlib.import_module(self.module.__name__ + ".core.model_calls")
        with calls.model_context("vision", "图片关系", self.session):
            self.assertEqual(await plugin.vision._relate(["茶杯"], "喝茶"), ["与话题有关"])
        self.assertEqual(plugin.context.llm_generate.call_args.kwargs["chat_provider_id"], "relation-selected")
        plugin.context.get_current_chat_provider_id.assert_not_awaited()
