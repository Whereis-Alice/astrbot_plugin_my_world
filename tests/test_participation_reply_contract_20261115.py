"""Quote capabilities follow actual reply progress without losing chat context."""
import json
import unittest

from core.participation import build_participation_prompt
from core.speech_policy import reply_target
from tests import test_participation as participation


class ParticipationReplyContractTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = participation.ParticipationTests.asyncSetUp
    chat = participation.ParticipationTests.chat

    @staticmethod
    def decision(reference, *, text="左边那一道折痕很有意思。"):
        return json.dumps({"inner": "想看看那道折痕是怎么折的。", "intent": "join_chat",
                           "say": text, "reply_to": reference}, ensure_ascii=False)

    @staticmethod
    def scene(prompt):
        return json.JSONDecoder().raw_decode(prompt[prompt.index('[{"ref"'):])[0]

    def capture_quotes(self):
        quotes = []
        send = self.messenger.send_text

        async def capture(target, lines):
            quotes.extend(reply_target(target, index) for index in range(len(lines)))
            return await send(target, lines)

        self.messenger.send_text = capture
        return quotes

    async def test_invalid_message_quote_is_repaired_once_before_sending(self):
        await self.chat("这张图的纸是怎么折的？", message_id="real-source")
        self.llm.replies = [self.decision("m99"), self.decision("m1")]
        quotes = self.capture_quotes()
        outcome = await self.engine.maybe_participate(participation.GROUP)
        self.assertEqual(len(self.llm.calls), 2)
        self.assertEqual(self.messenger.flat_messages, ["左边那一道折痕很有意思。"])
        self.assertEqual(quotes, ["real-source"])
        self.assertEqual(outcome.speech_decisions[0]["reply_to"], "real-source")

    async def test_invalid_message_quote_after_repair_stays_silent(self):
        await self.chat(message_id="real-source")
        self.llm.replies = [self.decision("m99"), self.decision("m98")]
        await self.engine.maybe_participate(participation.GROUP)
        self.assertEqual(len(self.llm.calls), 2)
        self.assertFalse(self.messenger.sent)
        state = await self.engine.load_state(participation.GROUP, cold_start=False)
        self.assertEqual(state.participation[participation.GROUP]["decision"], "failed")

    async def answered_exchange(self):
        old = await self.chat("照片是在哪里拍的？", message_id="answered-source")
        await self.chat("在城郊的河边。", mine=True, message_id="actual-answer")
        await self.engine.mark_reply_batch_replied(participation.GROUP, batch=[old])
        await self.chat("现在发现新的折纸图了。", message_id="new-source")

    async def test_answered_history_remains_context_but_cannot_be_quoted_again(self):
        await self.answered_exchange()
        self.llm.replies = [self.decision("m1"), self.decision("m3")]
        quotes = self.capture_quotes()
        await self.engine.maybe_participate(participation.GROUP)
        scene = self.scene(self.llm.calls[0]["prompt"])
        self.assertEqual(scene[0]["text"], "照片是在哪里拍的？")
        self.assertTrue(scene[0]["answered"])
        self.assertNotIn("can_reply", scene[0])
        self.assertEqual(scene[1]["text"], "在城郊的河边。")
        self.assertTrue(scene[2]["can_reply"])
        self.assertEqual(len(self.llm.calls), 2)
        self.assertEqual(quotes, ["new-source"])

    async def test_open_join_can_add_a_new_angle_without_quoting(self):
        await self.answered_exchange()
        self.llm.replies = [self.decision("", text="这张新图的左翼看着很轻巧。")]
        await self.engine.maybe_participate(participation.GROUP)
        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual(self.messenger.flat_messages, ["这张新图的左翼看着很轻巧。"])
        self.assertNotIn("这次实际读过", self.llm.calls[0]["system_prompt"])

    async def test_resolution_words_do_not_remove_a_new_source_capability(self):
        await self.chat("刚才的问题已经解决，不过这处折痕还有新问题。", message_id="new-question")
        self.llm.replies = [self.decision("m1")]
        await self.engine.maybe_participate(participation.GROUP)
        scene = self.scene(self.llm.calls[0]["prompt"])
        self.assertTrue(scene[0]["can_reply"])
        self.assertNotIn("answered", scene[0])
        self.assertEqual(self.messenger.flat_messages, ["左边那一道折痕很有意思。"])

    async def test_browse_can_still_quote_an_older_unanswered_read_source(self):
        old = await self.chat("早些时候拍的窗边的花。", age=7200, message_id="read-original")
        new = await self.chat("后来换到了另一个话题。", message_id="not-read")
        async with self.engine.session_state(participation.GROUP) as state:
            _, prompt, targets = self.engine.participation_prompt(state, participation.GROUP, "简短自然。",
                rows=[old, new], source="browse", reply_seqs=frozenset({old["seq"]}))
        scene = self.scene(prompt)
        self.assertTrue(scene[0]["can_reply"])
        self.assertNotIn("answered", scene[0])
        self.assertNotIn("can_reply", scene[1])
        self.assertEqual(targets, {"m1": "read-original"})

    async def test_timestamp_watermark_keeps_new_legacy_row_quoteable(self):
        rows = [{"at": self.clock.now() - 20, "message_id": "legacy-old", "text": "过去的问题。"},
                {"at": self.clock.now() - 1, "message_id": "legacy-new", "text": "后来新的分享。"}]
        async with self.engine.session_state(participation.GROUP) as state:
            state.chat_replied_until, state.chat_replied_seq = self.clock.now() - 10, 0
            _, prompt, targets = self.engine.participation_prompt(state, participation.GROUP, "简短自然。", rows=rows)
        scene = self.scene(prompt)
        self.assertTrue(scene[0]["answered"])
        self.assertNotIn("can_reply", scene[0])
        self.assertTrue(scene[1]["can_reply"])
        self.assertEqual(targets, {"m2": "legacy-new"})


class SharedPromptReplyCapabilityTests(unittest.TestCase):
    def test_internal_scene_row_cannot_grant_a_quote_capability(self):
        rows = [{"seq": 1, "at": 100, "message_id": "internal", "text": "内部事件。", "internal": True}]
        system, _, targets = build_participation_prompt(persona="简短自然", life={}, rows=rows, now=102,
            natural_silence=True, require_reply=True)
        self.assertEqual(targets, {})
        self.assertIn("intent 只能选 listen", system)
        self.assertNotIn("continue_chat", system)

    def test_explicit_answered_rows_remain_visible_and_override_quote_selection(self):
        rows = [{"seq": 1, "at": 100, "message_id": "old", "text": "已回答过的背景。"},
                {"seq": 2, "at": 101, "message_id": "new", "text": "现在的新话题。"}]
        _, prompt, targets = build_participation_prompt(persona="简短自然", life={}, rows=rows, now=102,
            natural_silence=True, reply_seqs=frozenset({1, 2}), answered_seqs=frozenset({1}))
        scene = ParticipationReplyContractTests.scene(prompt)
        self.assertTrue(scene[0]["answered"])
        self.assertEqual(scene[0]["text"], "已回答过的背景。")
        self.assertNotIn("can_reply", scene[0])
        self.assertEqual(targets, {"m2": "new"})


if __name__ == "__main__":
    unittest.main()
