"""Quoted speech stays attached to its author across autonomous entry points."""
from __future__ import annotations

import copy
import json
import unittest
from types import SimpleNamespace

from core.chat_scene import project_chat_scene
from core.engine.reply import ReplyMixin
from core.engine.speech_decisions import SpeechDecisionsMixin
from core.models import WorldConfig
from core.participation import build_participation_prompt, parse_participation
from core.prompt import PromptBuilder
from core.state import WorldState


ROOM = "adapter:GroupMessage:room"
OTHER = "adapter:GroupMessage:other"


def exchange():
    return [
        {"origin": ROOM, "message_id": "a", "user_id": "bot", "is_self": True,
         "text": "已经把两场演出看完了？", "at": 100},
        {"origin": ROOM, "message_id": "b", "user_id": "h1", "name": "同名角色",
         "is_self": False, "text": "对\n［这条消息 @ 了你；引用了你自己的消息］", "at": 101,
         "at_targets": [{"id": "bot", "self": True}],
         "reply_to": {"id": "bot", "message_id": "a"}, "addressing": "me"},
        {"origin": ROOM, "message_id": "c", "user_id": "h2", "is_self": False,
         "text": "你俩都看完了，我才刚回来\n［他引用了上一位的消息，原文：@你 对］", "at": 1000,
         "reply_to": {"id": "h1", "message_id": "b"}, "addressing": "others"},
    ]


class ChatSceneIdentityTests(unittest.TestCase):
    def test_nested_quote_links_do_not_inherit_the_old_bot_mention(self):
        rows = exchange()
        before = copy.deepcopy(rows)
        scene = project_chat_scene(rows, current_session=ROOM)
        self.assertEqual(scene[2]["reply_to"]["ref"], "m2")
        self.assertIs(scene[2]["reply_to"]["is_self"], False)
        self.assertEqual(scene[1]["reply_to"]["ref"], "m1")
        self.assertIs(scene[1]["reply_to"]["is_self"], True)
        self.assertEqual(scene[1]["text"], "对")
        self.assertIn("@", scene[1]["context_note"])
        self.assertNotIn("at_targets", scene[2])
        self.assertEqual(scene[2]["addressing"], "others")
        self.assertFalse(scene[1]["is_self"])
        self.assertEqual(rows, before)

    def test_quote_context_from_archive_cannot_add_a_reply_capability(self):
        rows = exchange()
        fixture = SimpleNamespace(recent_chat=rows)
        engine = SimpleNamespace()
        text = SpeechDecisionsMixin.expression_contract(engine, fixture, ROOM, "continuation",
            options=(["listen", "continue_chat"], [rows[2]], {"c1": "c"}))
        scene = json.loads(text[text.index('[{"ref"'):])
        self.assertEqual([row["ref"] for row in scene], ["c1", "q1", "q2"])
        self.assertEqual(scene[0]["reply_to"]["ref"], "q1")
        self.assertEqual(scene[1]["reply_to"]["ref"], "q2")
        self.assertTrue(scene[0]["can_reply"])
        self.assertTrue(scene[2]["is_self"])
        self.assertNotIn("can_reply", scene[1])
        self.assertNotIn("can_reply", scene[2])
        self.assertTrue(scene[1]["context_only"])
        self.assertTrue(scene[2]["context_only"])

    def test_missing_or_ambiguous_parent_does_not_guess_identity(self):
        row = {"origin": ROOM, "text": "你们看完了吗", "reply_to": {
            "message_id": "missing", "name": "机器人", "text": "原话"}}
        for archive in ([], [{"origin": OTHER, "message_id": "missing", "is_self": True}],
                        [{"origin": ROOM, "message_id": "missing", "text": "甲"},
                         {"origin": ROOM, "message_id": "missing", "text": "乙"}]):
            with self.subTest(archive=archive):
                scene = project_chat_scene([row], archive=archive, current_session=ROOM)
                self.assertEqual(len(scene), 1)
                self.assertTrue(scene[0]["reply_to"]["unavailable"])
                self.assertNotIn("ref", scene[0]["reply_to"])
                self.assertNotIn("is_self", scene[0]["reply_to"])
                self.assertEqual(scene[0]["reply_to"]["text"], "原话")

    def test_cycle_and_legacy_bad_annotation_are_bounded(self):
        row = {"origin": ROOM, "message_id": "a", "text": "你在说谁", "addressing": ["m", "e"],
               "reply_to": {"message_id": "b"}}
        parent = {"origin": ROOM, "message_id": "b", "text": "看看引用", "reply_to": {"message_id": "a"}}
        scene = project_chat_scene([row], archive=[parent], current_session=ROOM)
        self.assertEqual(len(scene), 2)
        self.assertEqual(scene[0]["reply_to"]["ref"], "q1")
        self.assertEqual(scene[1]["reply_to"]["ref"], "m1")
        self.assertNotIn("addressing", scene[0])

    def test_missing_quote_body_respects_disabled_or_limited_quote_setting(self):
        row = {"text": "后来呢", "reply_to": {"message_id": "not-loaded", "text": "真实引用正文"}}
        disabled = project_chat_scene([row], quote_chars=0)[0]["reply_to"]
        self.assertEqual(disabled["message_id"], "not-loaded")
        self.assertTrue(disabled["text_omitted"])
        self.assertNotIn("text", disabled)
        limited = project_chat_scene([row], quote_chars=2)[0]["reply_to"]
        self.assertEqual(limited["text"], "真实")
        self.assertTrue(limited["text_truncated"])

    def test_participation_keeps_old_parent_but_only_new_message_is_replyable(self):
        system, prompt, targets = build_participation_prompt(persona="简短自然", life={},
            rows=exchange(), now=1001, live_window=180, natural_silence=True)
        scene, _ = json.JSONDecoder().raw_decode(prompt[prompt.index('[{"ref"'):])
        self.assertEqual(targets, {"m3": "c"})
        self.assertEqual(scene[2]["reply_to"]["ref"], "m2")
        self.assertNotIn("can_reply", scene[1])
        self.assertIn("不自动成为本句的对象", system)
        self.assertEqual(parse_participation('{"say":"回来得挺晚呀。","reply_to":"m3"}', targets),
                         ("回来得挺晚呀。", "c"))

    def test_message_id_and_observed_quote_survive_recording(self):
        state = WorldState(session_id=ROOM, node_id="study")
        for quote in ({"message_id": "a"}, {"text": "只有原话", "is_self": False}):
            row = state.note_chat(user_id="h1", name="群友", text="你们看完了吗", now=100,
                                  origin=ROOM, message_id=str(len(state.recent_chat)), reply_to=quote)
            for key, value in quote.items():
                self.assertEqual(row["reply_to"][key], value)

    def test_refresh_keeps_addressing_a_string_and_event_identity_stable(self):
        record = {"message_id": "original", "event_id": "first"}
        ReplyMixin.refresh_incoming(SimpleNamespace(), record, "注解后的正文",
            identity={"message_id": "replacement", "event_id": "second", "addressing": "others"})
        self.assertEqual(record["addressing"], "others")
        self.assertEqual(record["message_id"], "original")
        self.assertEqual(record["event_id"], "first")
        ReplyMixin.refresh_incoming(SimpleNamespace(), {}, "正文", identity={"addressing": ["m", "e"]})

    def test_background_cannot_claim_latest_message_from_old_self_or_mention(self):
        builder = PromptBuilder(WorldConfig())
        rows = exchange()
        text = "\n".join(builder.chat_blocks(rows, mine_names=["同名角色"]))
        self.assertNotIn("多半是对你说的", text)
        self.assertNotIn("其中有话是对你说的", text)
        self.assertIn("最后一句的引用或 @ 连接另一位参与者", text)
        self.assertIn("旧消息里的 @", text)
        rows[2].pop("addressing")
        text = "\n".join(builder.chat_blocks(rows, mine_names=["同名角色"]))
        self.assertIn("对象不明确时保留不确定", text)
        self.assertNotIn("他们是在互相说话", text)


if __name__ == "__main__":
    unittest.main()
