"""An expression keeps the exact source references offered before inference."""
import json
import unittest
from unittest.mock import AsyncMock

from core.engine import TickOutcome
from core.speech_policy import SPEECH_OUTCOME
from tests import test_participation as participation


GROUP = participation.GROUP


class ExpressionSceneSnapshotTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = participation.ParticipationTests.asyncSetUp
    chat = participation.ParticipationTests.chat

    async def expression(self, *, followup=False, reference="c1", answered=True):
        original = await self.chat("山上的风车照片还能给我看看吗？", message_id="original")
        outcome = TickOutcome(GROUP, speech_kind="life_share")
        async with self.engine.session_state(GROUP) as state:
            async def generate(*args, **kwargs):
                if answered:
                    self.engine._set_watermark(state, GROUP, until=original["at"], seq=original["seq"])
                state.note_chat(user_id="bob", name="bob", text="已经换话题了，晚饭吃什么？",
                    now=self.clock.now(), origin=GROUP, message_id="later")
                return json.dumps({"reasoning": {"inner": "想让他看清那张照片。"},
                    "intent": "continue_chat", "reply_to": reference,
                    "actions": [{"type": "say", "messages": ["我找找那张照片。"]}]}, ensure_ascii=False)

            self.engine._ask_llm = AsyncMock(side_effect=generate)
            token = SPEECH_OUTCOME.set(outcome)
            try:
                if followup:
                    await self.engine._llm_followup(state, self.engine.node(state.node_id), outcome,
                        "整理完相册", "")
                else:
                    lines = await self.engine._generate_text_actions(state, self.engine.node(state.node_id),
                        "看看眼前的话题，想说才说。", session_id=GROUP)
                    for line in lines:
                        outcome.add_speech(line, kind=getattr(lines, "speech_kind", "life_share"))
            finally:
                SPEECH_OUTCOME.reset(token)
        return outcome, original

    async def test_text_expression_does_not_retarget_a_number_after_source_was_answered(self):
        outcome, original = await self.expression()
        self.assertEqual(outcome.reply_targets.get(GROUP), "original")
        self.assertEqual(outcome.chat_claims.get(GROUP), [str(original["seq"])])
        await self.engine._deliver(outcome)
        self.assertFalse(self.messenger.sent)

    async def test_completion_expression_keeps_the_prepared_source_after_newer_messages(self):
        outcome, original = await self.expression(followup=True)
        self.assertEqual(outcome.reply_targets.get(GROUP), "original")
        self.assertEqual(outcome.chat_claims.get(GROUP), [str(original["seq"])])
        await self.engine._deliver(outcome)
        self.assertFalse(self.messenger.sent)

    async def test_expression_cannot_invent_a_reference_to_an_unseen_new_message(self):
        outcome, _ = await self.expression(reference="c2", answered=False)
        self.assertFalse(outcome.messages)
        self.assertNotIn(GROUP, outcome.reply_targets)

    async def test_unanswered_original_is_preserved_without_claiming_later_messages(self):
        outcome, original = await self.expression(answered=False)
        self.assertEqual(outcome.reply_targets.get(GROUP), "original")
        self.assertEqual(outcome.chat_claims.get(GROUP), [str(original["seq"])])
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.sent, [(GROUP, ["我找找那张照片。"])])
