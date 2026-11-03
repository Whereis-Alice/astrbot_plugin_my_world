"""A source reply cannot borrow an unrelated autonomous expression permission."""
import json
import unittest

from core.speech_decision import parse_decision


class ExpressionIntentSourceTests(unittest.TestCase):
    def choose(self, intent, reply_to):
        return parse_decision(json.dumps({"inner": "想接一句，不过先看清是谁在说。",
            "say": "我这边已经关了。", "intent": intent, "reply_to": reply_to}, ensure_ascii=False),
            {"c1": "source-message"}, allowed=("life_share", "outreach", "continue_chat", "listen"))

    def test_life_share_cannot_reply_to_an_old_message(self):
        self.assertTrue(self.choose("life_share", "c1").error)

    def test_outreach_cannot_reply_to_a_different_source(self):
        self.assertTrue(self.choose("outreach", "c1").error)

    def test_genuine_current_continuation_keeps_its_source(self):
        decision = self.choose("continue_chat", "c1")
        self.assertFalse(decision.error)
        self.assertEqual(decision.reply_to, "source-message")

    def test_new_life_fact_does_not_need_a_chat_source(self):
        self.assertFalse(self.choose("life_share", "").error)


if __name__ == "__main__":
    unittest.main()
