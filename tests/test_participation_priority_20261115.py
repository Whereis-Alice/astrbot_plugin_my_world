"""Weak retrieval overlap must not grant the character a fast chat continuation."""
import unittest

from core.participation_opportunity import assess_opportunity


class ParticipationPriorityTests(unittest.TestCase):
    def choice(self, own="这个不错", incoming="这台打印机不错", **overrides):
        arguments = dict(
            now=1000.,
            rows=[{"is_self": True, "at": 990., "text": own,
                   "speech_turn": "turn-a", "message_id": "own-a"}],
            new=[{"text": incoming, "user_id": "stranger"}],
            observation={"evaluated_at": 900.}, attempts=[], persona="",
            affinities={}, level="quiet", hourly_max=6,
        )
        arguments.update(overrides)
        return assess_opportunity(**arguments)

    def test_short_generic_overlap_uses_ordinary_related_attention(self):
        weak = self.choice()
        unrelated = self.choice(incoming="新打印机装好了")
        self.assertFalse(weak.continuation)
        self.assertFalse(weak.continuation_priority)
        self.assertFalse(weak.ready)
        self.assertGreater(weak.relevance, 0)
        self.assertGreater(weak.due_at, 990 + 20)
        self.assertLess(weak.due_at, unrelated.due_at)

    def test_repeated_generic_words_do_not_supply_topic_evidence(self):
        for own, incoming in (("这个好看", "这张照片好看"),
                              ("这台不错", "这台打印机不错"),
                              ("不错", "不错，不错，真的不错"),
                              ("这个不错", "这个不错，真的不错"),
                              ("非常好看", "这张照片非常好看"),
                              ("this looks nice", "this printer looks nice"),
                              ("this", "this printer looks nice")):
            with self.subTest(own=own, incoming=incoming):
                result = self.choice(own, incoming)
                self.assertFalse(result.continuation_priority)
                self.assertGreater(result.relevance, 0)

    def test_single_specific_topic_keeps_fast_continuation_without_quote(self):
        for own, incoming in (("风车", "风车坏了吗"),
                              ("珐琅", "珐琅容易磕坏吗"),
                              ("windmill", "Does the windmill need repair?"),
                              ("Zymora", "Does Zymora need water?")):
            with self.subTest(own=own, incoming=incoming):
                result = self.choice(own, incoming)
                self.assertTrue(result.continuation)
                self.assertTrue(result.continuation_priority)
                self.assertEqual(result.due_at, 1010.)

    def test_one_concrete_word_in_a_longer_turn_is_still_a_topic(self):
        result = self.choice("The unfamiliar Zymora sculpture has several delicate edges and unusual curves",
                             "Does Zymora need water?")
        self.assertTrue(result.continuation_priority)
        self.assertEqual(result.due_at, 1010.)

    def test_multiple_generic_matches_do_not_become_a_specific_topic(self):
        result = self.choice("这个不错，也非常好看", "那些陶罐不错，也非常好看")
        self.assertFalse(result.continuation)
        self.assertFalse(result.continuation_priority)
        self.assertGreater(result.relevance, 0)

    def test_specific_windmill_topic_still_has_the_fast_interval(self):
        result = self.choice("风车叶片需要修理", "风车的叶片换了吗")
        self.assertTrue(result.continuation)
        self.assertTrue(result.continuation_priority)
        self.assertEqual(result.due_at, 1010.)
        self.assertFalse(result.ready)

    def test_specific_english_topic_still_has_the_fast_interval(self):
        result = self.choice("windmill blades need repair", "the windmill blades are slow")
        self.assertTrue(result.continuation_priority)
        self.assertEqual(result.due_at, 1010.)

    def test_separate_new_messages_cannot_construct_a_topic_from_fragments(self):
        result = self.choice("风车", new=[{"text": "风"}, {"text": "车"}])
        self.assertFalse(result.continuation)
        self.assertFalse(result.continuation_priority)

    def test_separate_real_turns_cannot_construct_a_topic_from_fragments(self):
        result = self.choice(rows=[
            {"is_self": True, "at": 980., "text": "风", "speech_turn": "turn-a"},
            {"is_self": True, "at": 990., "text": "车", "speech_turn": "turn-b"},
        ], incoming="风车坏了吗", continuation_turns=3)
        self.assertFalse(result.continuation_priority)

    def test_real_quote_or_addressing_keeps_priority_without_topic_overlap(self):
        for source in ({"addressing": "me"}, {"reply_to": {"message_id": "own-a"}},
                       {"addressing": "others", "reply_to": {"message_id": "own-a"}}):
            with self.subTest(source=source):
                result = self.choice(new=[{"text": "还有个问题", **source}])
                self.assertTrue(result.continuation)
                self.assertTrue(result.continuation_priority)
                self.assertEqual(result.due_at, 1010.)

    def test_quote_of_someone_else_does_not_upgrade_weak_overlap(self):
        result = self.choice(new=[{"text": "这台打印机不错",
                                   "reply_to": {"message_id": "someone-else"}}])
        self.assertFalse(result.continuation_priority)

    def test_specific_topic_addressed_to_others_keeps_ordinary_attention(self):
        result = self.choice("风车叶片", new=[{"text": "风车的叶片换了吗",
                                              "addressing": "others"}])
        self.assertFalse(result.continuation)
        self.assertFalse(result.continuation_priority)

    def test_priority_still_respects_hourly_observation_budget(self):
        result = self.choice("风车叶片", "风车的叶片换了吗", attempts=[970.] * 6)
        self.assertTrue(result.continuation)
        self.assertFalse(result.ready)
        self.assertEqual(result.due_at, 4570.)
        self.assertEqual(result.reason, "本轮观察预算已用完，等待新现场")

    def test_specific_continuation_returns_to_ordinary_pace_after_two_turns(self):
        result = self.choice(rows=[
            {"is_self": True, "at": 980., "text": "风车叶片", "speech_turn": "turn-a"},
            {"is_self": True, "at": 990., "text": "换风车叶片", "speech_turn": "turn-b"},
        ], incoming="风车的叶片换了吗")
        self.assertTrue(result.continuation)
        self.assertFalse(result.continuation_priority)
        self.assertGreater(result.due_at, 1010.)

    def test_old_turn_is_not_a_current_topic_continuation(self):
        result = self.choice(rows=[{"is_self": True, "at": 800., "text": "风车叶片",
                                   "speech_turn": "turn-a", "message_id": "own-a"}],
                             new=[{"text": "风车的叶片换了吗",
                                   "reply_to": {"message_id": "own-a"}}])
        self.assertFalse(result.continuation_priority)


if __name__ == "__main__":
    unittest.main()
