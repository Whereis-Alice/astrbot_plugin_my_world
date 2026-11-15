"""Actual native message entrances retain long endings inside existing budgets."""
from __future__ import annotations

import importlib
import unittest
from unittest.mock import AsyncMock

from tests import test_proactive_observer_context_20261107 as observer


class NativeExcerptTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = observer.ObserverContextTests.asyncSetUp
    fixture = observer.ObserverContextTests.fixture
    options = observer.ObserverContextTests.options
    source = observer.ObserverContextTests.source
    observation = observer.ObserverContextTests.observation

    async def test_native_source_keeps_resolved_ending_in_original_body_budget(self):
        native = self.fixture(AsyncMock(return_value=False))
        source = self.source()
        body = "这是很久以前开始讨论的安排。" * 180 + "最后更新：已经解决，请不要继续回复。"
        note = "\n［这条消息引用了另一位的旧消息］"
        source.message_str = body + note
        await native.prepare(self.options())
        data = self.observation(native.request)
        self.assertEqual(len(data["text"]), 1500)
        self.assertTrue(data["text"].startswith("这是很久以前"))
        self.assertTrue(data["text"].endswith("已经解决，请不要继续回复。"))
        self.assertTrue(data["text_truncated"])
        self.assertEqual(data["context_note"], note.strip())
        self.assertNotIn("引用了另一位", data["text"])
        self.assertEqual(data["author"]["id"], source.get_sender_id())
        self.assertEqual(source.message_str, body + note)

    async def test_native_quote_keeps_ending_and_author_inside_configured_limit(self):
        native = self.fixture(AsyncMock(return_value=False))
        source = self.source()
        text = "原作者早先的讨论。" * 180 + "末尾纠正：安排已经取消，请不要追答。"
        source.message_obj.message[0].message_str = text
        self.plugin.engine.world.context.quote_chars = 120
        await native.prepare(self.options())
        quote = self.observation(native.request)["quoted_message"]
        self.assertEqual(len(quote["text"]), 120)
        self.assertTrue(quote["text"].endswith("安排已经取消，请不要追答。"))
        self.assertTrue(quote["text_truncated"])
        self.assertEqual(quote["id"], "guest-a")
        self.assertEqual(quote["message_id"], "parent")
        self.assertFalse(quote["is_self"])
        self.assertEqual(source.message_obj.message[0].message_str, text)

    async def test_native_quote_hard_cap_and_opt_out_keep_original_contract(self):
        text = "原作者的引用记录。" * 180 + "末尾结论：不要回复这条旧消息。"
        for configured in (2000, 0):
            with self.subTest(configured=configured):
                native = self.fixture(AsyncMock(return_value=False))
                source = self.source()
                source.message_obj.message[0].message_str = text
                self.plugin.engine.world.context.quote_chars = configured
                await native.prepare(self.options())
                quote = self.observation(native.request)["quoted_message"]
                self.assertEqual(quote["id"], "guest-a")
                self.assertEqual(quote["message_id"], "parent")
                if configured:
                    self.assertEqual(len(quote["text"]), 1000)
                    self.assertTrue(quote["text"].endswith("不要回复这条旧消息。"))
                    self.assertTrue(quote["text_truncated"])
                else:
                    self.assertTrue(quote["text_omitted"])
                    self.assertNotIn("text", quote)
                    self.assertNotIn("text_truncated", quote)

    async def test_incoming_quote_excerpt_and_flag_reach_archive_and_scene(self):
        self.fixture(AsyncMock(return_value=False))
        source = self.source()
        text = "原作者较早留下的背景。" * 100 + "最后更新：问题已经解决，请不要回复。"
        source.message_obj.message[0].message_str = text
        messages = importlib.import_module(self.module.__name__ + ".core.messages")
        states = importlib.import_module(self.module.__name__ + ".core.state")
        scenes = importlib.import_module(self.module.__name__ + ".core.chat_scene")
        identity = messages._incoming_identity(source, source.message_str)
        quote = identity["reply_to"]
        self.assertEqual(len(quote["text"]), 500)
        self.assertTrue(quote["text"].endswith("问题已经解决，请不要回复。"))
        self.assertTrue(quote["text_truncated"])
        state = states.WorldState(session_id=self.session)
        row = state.note_chat(user_id=identity["user_id"], name=identity["user_name"],
            text=source.message_str, raw_text=identity["raw_text"], now=100, origin=self.session,
            message_id=identity["message_id"], reply_to=quote)
        projected = scenes.project_chat_scene([row], current_session=self.session)[0]["reply_to"]
        self.assertEqual(projected["text"], quote["text"])
        self.assertTrue(projected["text_truncated"])
        self.assertTrue(projected["unavailable"])
        self.assertEqual(projected["message_id"], "parent")
        self.assertEqual(projected["id"], "guest-a")
        self.assertFalse(projected["is_self"])
        self.assertEqual(row["reply_to"]["text"], quote["text"])
        self.assertTrue(row["reply_to"]["text_truncated"])
        self.assertEqual(source.message_obj.message[0].message_str, text)


if __name__ == "__main__":
    unittest.main()
