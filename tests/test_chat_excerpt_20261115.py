"""Bounded scene excerpts keep source identity and the end of long messages."""
import unittest

from tests import fake_astrbot
fake_astrbot.install()

from core.chat_excerpt import EXCERPT_OMISSION, chat_excerpt  # noqa: E402
from core.chat_scene import project_chat_scene  # noqa: E402
from core.context_selection import search_terms  # noqa: E402
from core.engine import MessageContext  # noqa: E402
from core.evidence import source_snapshot  # noqa: E402
from core.state import FORWARD_SUMMARY_MARK, WorldState, split_annotation  # noqa: E402
from tests import test_participation as fixture  # noqa: E402

ROOM = fixture.GROUP


class ChatExcerptTests(unittest.TestCase):
    def test_budgets_include_marker_and_short_text_is_verbatim(self):
        text = "首段信息" + "中间记录" * 300 + "最后纠正：时间改为明天。"
        for budget in (0, 2, 10, 20, 200, 500, 800):
            with self.subTest(budget=budget):
                excerpt = chat_excerpt(text, budget)
                self.assertLessEqual(len(excerpt), budget)
                if budget >= 200:
                    self.assertTrue(excerpt.startswith("首段信息"))
                    self.assertTrue(excerpt.endswith("最后纠正：时间改为明天。"))
                    self.assertIn(EXCERPT_OMISSION, excerpt)
        for text in ("短消息。", "正文\n仍是同一人", ""):
            self.assertEqual(chat_excerpt(text, 200), text)

    def test_omission_marker_is_not_annotation_or_topic_evidence(self):
        text = "首段信息" + "记录" * 300 + "末尾内容"
        clipped = chat_excerpt(text, 200)
        self.assertEqual(split_annotation(clipped), (clipped, ""))
        self.assertFalse(set(search_terms(EXCERPT_OMISSION)))
        self.assertFalse(set(search_terms(clipped)) & {"中间", "间省", "省略"})

    def test_persistent_body_reading_and_annotations_remain_separate(self):
        state = WorldState(session_id=ROOM)
        body = "我花了很久整理资料。" * 50 + "已经解决，不要再回复了。"
        note = "\n［这条消息 @ 了另一位；引用了他的原消息］"
        row = state.note_chat(user_id="alice", name="Alice", text=body + note, raw_text=body,
            now=100, origin=ROOM, message_id="long", at=[{"id": "other", "self": False}],
            addressing="others")
        kept_body, kept_note = split_annotation(row["text"])
        self.assertEqual(len(kept_body), 200)
        self.assertTrue(kept_body.endswith("已经解决，不要再回复了。"))
        self.assertEqual(kept_note, note)
        self.assertTrue(row["text_truncated"])
        self.assertEqual(row["reading_text"], kept_body)
        self.assertTrue(row["reading_text_truncated"])
        scene = project_chat_scene([row], current_session=ROOM)[0]
        self.assertTrue(scene["text_truncated"])
        self.assertEqual(scene["text"], kept_body)
        self.assertEqual(scene["addressing"], "others")
        self.assertFalse(scene["is_self"])
        self.assertEqual(scene["context_note"], note.strip())

    def test_forward_and_scene_excerpt_keep_terminal_correction(self):
        state = WorldState(session_id=ROOM)
        body = FORWARD_SUMMARY_MARK + "转发的背景内容。" * 150 + "末尾纠正：该安排已经取消。"
        row = state.note_chat(user_id="alice", name="Alice", text=body, now=100,
                              origin=ROOM, message_id="forward")
        self.assertEqual(len(row["text"]), 800)
        self.assertTrue(row["text"].startswith(FORWARD_SUMMARY_MARK))
        projected = project_chat_scene([row])[0]
        self.assertEqual(len(projected["text"]), 500)
        self.assertTrue(projected["text"].endswith("末尾纠正：该安排已经取消。"))
        self.assertTrue(projected["text_truncated"])

    def test_quotes_preserve_tail_author_and_exact_identifier_with_opt_out(self):
        state = WorldState(session_id=ROOM)
        body = "原作者的前文。" * 80 + "末尾答案：不用再检查，已经修好了。"
        mid = "observed-identifier-" * 5
        row = state.note_chat(user_id="alice", name="Alice", text="这句话后来呢", now=100,
            origin=ROOM, message_id="new", reply_to={"id": "another-user", "message_id": mid,
                                                     "text": body, "is_self": False})
        self.assertEqual(len(row["reply_to"]["text"]), 500)
        quote = project_chat_scene([row], quote_chars=120)[0]["reply_to"]
        self.assertLessEqual(len(quote["text"]), 120)
        self.assertTrue(quote["text"].endswith("末尾答案：不用再检查，已经修好了。"))
        self.assertEqual(quote["message_id"], mid)
        self.assertEqual(quote["id"], "another-user")
        self.assertFalse(quote["is_self"])
        self.assertTrue(quote["text_truncated"])
        disabled = project_chat_scene([row], quote_chars=0)[0]["reply_to"]
        self.assertTrue(disabled["text_omitted"])
        self.assertNotIn("text", disabled)
        self.assertEqual(disabled["message_id"], mid)

    def test_quoted_annotations_do_not_consume_or_become_author_text(self):
        state = WorldState(session_id=ROOM)
        body = "作者的前文。" * 100 + "末尾限制：只给一个答案。"
        note = "\n［引用中的 @ 是指向其他人］"
        row = state.note_chat(user_id="alice", name="Alice", text="后续问题", now=100,
            reply_to={"text": body + note, "message_id": "missing", "is_self": False})
        stored = row["reply_to"]
        self.assertLessEqual(len(stored["text"]) + len(stored.get("context_note", "")), 500)
        quote = project_chat_scene([row], quote_chars=100)[0]["reply_to"]
        self.assertLessEqual(len(quote["text"]) + len(quote.get("context_note", "")), 100)
        self.assertTrue(quote["text"].endswith("末尾限制：只给一个答案。"))
        self.assertNotIn("引用中的", quote["text"])
        self.assertIn("引用中的", quote["context_note"])

    def test_self_quote_ancestor_and_long_annotation_keep_their_own_identity(self):
        state = WorldState(session_id=ROOM)
        body = "自己已经说过的前文。" * 80 + "末尾补充：只讨论这件事。"
        own = state.note_chat(user_id="__self__", name="自己", text=body, now=100,
                              origin=ROOM, message_id="own", is_self=True)
        reply = state.note_chat(user_id="alice", name="Alice", text="后来呢", now=101,
            origin=ROOM, message_id="human", reply_to={"message_id": "own"}, addressing="me")
        scene = project_chat_scene([reply], archive=[own], current_session=ROOM)
        self.assertFalse(scene[0]["is_self"])
        self.assertTrue(scene[1]["is_self"])
        self.assertEqual(scene[0]["reply_to"]["ref"], "q1")
        self.assertTrue(scene[1]["text"].endswith("末尾补充：只讨论这件事。"))
        self.assertTrue(scene[1]["context_only"])
        note = "\n［头部对象：" + "很长的附注" * 80 + "；末尾对象：其他人］"
        unavailable = project_chat_scene([{"text": "问题", "reply_to": {
            "text": body + note, "message_id": "missing", "is_self": False}}], quote_chars=200)[0]["reply_to"]
        self.assertTrue(unavailable["context_note"].startswith("\n［"))
        self.assertTrue(unavailable["context_note"].endswith("其他人］"))
        self.assertTrue(unavailable["context_note_truncated"])
        self.assertLessEqual(len(unavailable["text"]) + len(unavailable["context_note"]), 200)

    def test_source_identity_and_dedup_use_original_not_equal_excerpts(self):
        state = WorldState(session_id=ROOM)
        head, tail = "相同的开头" * 30, "相同的末尾" * 30
        first = head + "中间事实甲" * 30 + tail
        second = head + "中间事实乙" * 30 + tail
        row = state.note_chat(user_id="alice", name="Alice", text=first, now=100)
        self.assertIsNone(state.note_chat(user_id="alice", name="Alice", text=first, now=101))
        changed = state.note_chat(user_id="alice", name="Alice", text=second, now=100)
        self.assertEqual(row["text"], changed["text"])
        self.assertNotEqual(row["text_fingerprint"], changed["text_fingerprint"])
        self.assertNotEqual(source_snapshot(row, ROOM)["id"], source_snapshot(changed, ROOM)["id"])
        self.assertEqual(source_snapshot(row, ROOM)["id"], source_snapshot({**row, "text": first}, ROOM)["id"])
        observed = state.note_chat(user_id="alice", name="Alice", text=first, now=102,
                                   origin=ROOM, message_id="exact-event")
        self.assertEqual(observed["event_key"], ROOM + "|m:exact-event")


class ProductionExcerptTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.ParticipationTests.asyncSetUp

    async def test_presence_to_participation_prompt_keeps_long_tail_within_original_budget(self):
        body = "很久之前开始准备这件事。" * 60 + "现在已经解决，请不要再回复。"
        async with self.engine.session_state(ROOM) as state:
            self.engine._note_presence(state, MessageContext(ROOM, user_id="alice", user_name="Alice",
                text=body, raw_text=body, message_id="production-long"))
            stored = state.recent_chat[-1]
            _, prompt, targets = self.engine.participation_prompt(state, ROOM, "喜欢风车和旅行。")
            self.assertEqual(len(stored["text"]), 200)
            self.assertIn("现在已经解决，请不要再回复。", prompt)
            self.assertIn('"text_truncated":true', prompt)
            self.assertIn(EXCERPT_OMISSION, prompt)
            self.assertEqual(targets, {"m1": "production-long"})
            source = self.engine.db.raw.source_record(stored["source_id"])
            self.assertEqual(source["text"], body)
        self.assertFalse(self.llm.calls)
        self.assertFalse(self.messenger.sent)


if __name__ == "__main__":
    unittest.main()
