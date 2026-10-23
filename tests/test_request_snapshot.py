"""Request diagnostics must reflect the hook boundary without retaining live objects."""

import asyncio
import json
import types
import unittest
from unittest.mock import AsyncMock, patch

from tests import test_dialogue_inject as fixture


class RequestSnapshotTests(unittest.TestCase):
    module = fixture.MODULE
    _plugin = fixture.DialogueInjectTests._plugin

    def test_text_sizes_include_external_parts_without_retaining_or_counting_media(self):
        from core.prompt_snapshot import request_sizes
        history = [{"role":"user", "content":[{"type":"text", "text":"历史"},
                    {"type":"image_url", "image_url":{"url":"data:image/png;base64," + "a" * 10000}}]}]
        extra = [types.SimpleNamespace(type="text", text="场景记忆")]
        original = json.dumps(history)
        sizes = request_sizes("系统", "提问", history, extra)
        self.assertEqual(sizes["history_text_chars"], 2)
        self.assertEqual(sizes["extra_text_chars"], 4)
        self.assertEqual(sizes["text_chars_observed"], 10)
        self.assertTrue(sizes["text_scan_complete"])
        self.assertEqual(json.dumps(history), original)
        sizes = request_sizes("", "", history * 1000, extra)
        self.assertFalse(sizes["text_scan_complete"])

    def test_native_snapshot_includes_runtime_external_content_and_real_prompt(self):
        session = "aiocqhttp:GroupMessage:snapshot-native"
        plugin, _ = self._plugin(session)
        self.addCleanup(plugin.db.raw.close)
        event = fixture.Event("隔板装好了吗", session=session)
        req = fixture.Request("隔板装好了吗", "# Tool Rules\n宿主规则需要保留")
        req.contexts = [{"role": "tool", "tool_call_id": "real-call", "content": "之前的工具结果"}]
        req.extra_user_content_parts = [self.module.TextPart(text="外部插件的场景与记忆")]
        req.image_urls = ["https://example.test/picture.png"]
        plugin._reply_images = AsyncMock(return_value=(req.image_urls, {}, []))
        original = plugin.engine.handle_reply

        async def planned(*args, **kwargs):
            plan = await original(*args, **kwargs)
            plan.runtime_part += "\n刚给展示柜装好了一块蓝色隔板"
            return plan

        with patch.object(plugin.engine, "handle_reply", side_effect=planned):
            asyncio.run(plugin.on_llm_request(event, req))
        preview = asyncio.run(plugin.engine.preview_last_reply_request(session))
        self.assertIn("刚给展示柜装好了一块蓝色隔板", preview)
        self.assertIn("外部插件的场景与记忆", preview)
        self.assertIn("宿主规则需要保留", preview)
        self.assertIn("之前的工具结果", preview)
        self.assertIn("My World 注入完成", preview)
        self.assertIn("后续钩子", preview)
        self.assertNotIn("实际发出去的完整请求", preview)
        snapshot = plugin.engine._prompt_snapshots[0]
        self.assertEqual(snapshot["user_prompt"], req.prompt)
        self.assertEqual(snapshot["original_user_text"], event.get_message_str())
        self.assertEqual(snapshot["media"]["image_urls"], 1)
        self.assertTrue(snapshot["context_selection"])
        self.assertEqual(len(plugin.engine._prompt_snapshots), 1)

        # Later hooks and tool turns may mutate any nested request object.
        req.contexts[0]["content"] = "后续工具结果"
        req.extra_user_content_parts[0].text = "后续注入"
        self.assertEqual(asyncio.run(plugin.engine.preview_last_reply_request(session)), preview)

    def test_snapshot_is_bounded_and_never_copies_media_payloads(self):
        session = "aiocqhttp:GroupMessage:snapshot-bounded"
        plugin, _ = self._plugin(session)
        self.addCleanup(plugin.db.raw.close)
        media = "data:image/png;base64," + "A" * 1_000_000
        contexts = [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": media}},
            {"type": "text", "text": "旧文字"},
        ]}, {"role": "user", "content": [types.SimpleNamespace(type="text", text="对象文字")]}]
        plugin.engine._remember_prompt_snapshot(session_id=session, system_prompt="规则",
            user_prompt="当前消息", contexts=contexts, batch=[], chat_rows=[])
        preview = asyncio.run(plugin.engine.preview_last_reply_request(session))
        self.assertIn("对象文字", preview)
        self.assertNotIn("A" * 100, preview)
        self.assertIn("image_url", preview)
        contexts[0]["content"][1]["text"] = "已改写"
        self.assertEqual(asyncio.run(plugin.engine.preview_last_reply_request(session)), preview)

        for _ in range(5):
            plugin.engine._remember_prompt_snapshot(session_id=session,
                system_prompt="规则" * 200_000, user_prompt="正文" * 200_000,
                contexts=[{"role": "user", "content": "历史" * 100_000}] * 1000,
                batch=[], chat_rows=[])
        self.assertEqual(len(plugin.engine._prompt_snapshots), 3)
        self.assertLess(len(json.dumps(plugin.engine._prompt_snapshots[0], ensure_ascii=False)), 220_000)
        self.assertIn("截断", asyncio.run(plugin.engine.preview_last_reply_request(session)))
