"""An equal later utterance cannot claim a decision that was kept private."""
import unittest

from core.engine import TickOutcome
from core.speech_decision import SpeechDecision
from core.speech_policy import PLAN_SPEECH
from tests import test_participation as fixture

GROUP = fixture.GROUP


class BlockedSpeechIdentityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.ParticipationTests.asyncSetUp

    async def remember(self, outcome, *, blocked=False):
        async with self.engine.session_state(GROUP) as state:
            return await self.engine.remember_speech_decision(state, GROUP,
                SpeechDecision(inner="有一点想接这句话。", say="我也想去看看。",
                               intent="continue_chat"),
                trigger="plan", outcome=outcome, drafts=["我也想去看看。"],
                blocked_reason="先留一点聊天空间" if blocked else "")

    async def test_later_normal_reply_does_not_mark_blocked_thought_as_sent(self):
        outcome = TickOutcome(GROUP, speech_kind="continuation")
        blocked = await self.remember(outcome, blocked=True)
        outcome.speech_kind = "reply"
        outcome.add_speech("我也想去看看。")
        self.assertEqual(outcome.speech_ids[""], [""])
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.flat_messages, ["我也想去看看。"])
        self.assertEqual(blocked["status"], "not_sent")
        self.assertEqual(blocked["sent"], [])
        state = await self.engine.load_state(GROUP)
        thought = next(t for t in state.thoughts if t.get("decision_id") == blocked["id"])
        self.assertEqual(thought["detail"]["speech"]["status"], "not_sent")

    async def test_newer_blocked_draft_does_not_steal_a_valid_equal_reply(self):
        outcome = TickOutcome(GROUP, speech_kind="reply")
        valid = await self.remember(outcome)
        blocked = await self.remember(outcome, blocked=True)
        outcome.add_speech("我也想去看看。")
        self.assertEqual(outcome.speech_ids[""], [valid["id"]])
        await self.engine._deliver(outcome)
        self.assertEqual(valid["status"], "sent")
        self.assertEqual(blocked["status"], "not_sent")

    def test_active_partial_delivery_can_still_bind_its_remaining_segment(self):
        outcome = TickOutcome(GROUP, speech_kind="reply")
        trace = {"id": "active", "target": GROUP, "status": "prepared",
                 "drafts": ["嗯。", "嗯。"]}
        outcome.speech_decisions.append(trace)
        outcome.add_speech("嗯。")
        trace["status"] = "partial"
        outcome.add_speech("嗯。")
        self.assertEqual(outcome.speech_ids[""], ["active", "active"])

    def test_later_plan_step_creates_a_new_receipt_after_an_earlier_rejection(self):
        outcome = TickOutcome(GROUP, speech_kind="continuation")
        blocked = {"id": "blocked", "thought_id": "same-thought", "target": GROUP,
                   "status": "not_sent", "drafts": ["我也想去看看。"], "sent": []}
        outcome.speech_decisions.append(blocked)
        plan = {"id": "same-plan", "thought_id": "same-thought"}
        token = PLAN_SPEECH.set((outcome, plan))
        try:
            outcome.add_speech("我也想去看看。")
        finally:
            PLAN_SPEECH.reset(token)
        self.assertNotEqual(outcome.speech_ids[""][0], blocked["id"])
        self.assertEqual(blocked["drafts"], ["我也想去看看。"])
        self.assertEqual(blocked["status"], "not_sent")
        self.assertEqual(len(outcome.speech_decisions), 2)


if __name__ == "__main__":
    unittest.main()
