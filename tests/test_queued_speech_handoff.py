"""Accepted speech retires an older unstarted comment without altering its source."""
from dataclasses import replace
import unittest
from unittest.mock import AsyncMock

from core.engine import TickOutcome
from core.engine.speech_receipts import remember_accepted, remember_accepted_images
from core.speech_policy import segment_allowed
from tests import test_queued_chat_continuity as queued
from tests import test_participation as participation

GROUP = queued.GROUP


class QueuedSpeechHandoffTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = queued.QueuedChatContinuityTests.asyncSetUp
    chat = queued.QueuedChatContinuityTests.chat
    pending_comment = queued.QueuedChatContinuityTests.pending_comment

    async def test_other_life_expression_retires_comment_without_answering_its_source(self):
        outcome, row = await self.pending_comment()
        await self.engine._deliver(TickOutcome(GROUP, messages=['我刚把窗边整理好了。'], speech_kind='life_share'))
        state = await self.engine.load_state(GROUP)
        self.assertLess(self.engine._watermark(state, GROUP)[1], row['seq'])
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.sent, [(GROUP, ['我刚把窗边整理好了。'])])
        self.assertEqual(outcome.speech_decisions[0]['status'], 'not_sent')
        self.assertTrue(any('准备期间' in note for note in outcome.notes))

    async def test_native_reply_to_older_message_still_retires_newer_comment(self):
        older = await self.chat('第一张照片呢？', message_id='older')
        newer = await self.chat('另一张风车照片呢？', message_id='newer')
        outcome, _ = await self.pending_comment(rows=[newer])
        await self.engine._deliver(TickOutcome(GROUP, messages=['第一张找到了。'], speech_kind='reply'))
        await self.engine.mark_reply_batch_replied(GROUP, batch=[older])
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.sent, [(GROUP, ['第一张找到了。'])])
        state = await self.engine.load_state(GROUP)
        self.assertLess(self.engine._watermark(state, GROUP)[1], newer['seq'])

    async def test_native_after_send_record_also_notifies_pending_comments(self):
        outcome, _ = await self.pending_comment()
        await self.engine.record_self_reply(GROUP, '我已经在这里回了一句。')
        await self.engine._deliver(outcome)
        self.assertFalse(self.messenger.sent)
        self.assertEqual(outcome.speech_decisions[0]['status'], 'not_sent')
        self.assertFalse(any('平台没有接收' in note for note in outcome.notes))

    async def test_failed_send_and_unaccepted_draft_leave_pending_comment_valid(self):
        outcome, _ = await self.pending_comment()
        original = self.messenger.send_text
        self.messenger.send_text = AsyncMock(return_value=False)
        await self.engine._deliver(TickOutcome(GROUP, messages=['还没说出去。'], speech_kind='reply'))
        self.assertFalse(outcome.chat_speech_cancelled)
        self.messenger.send_text = original
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.sent, [(GROUP, ['等下找给你。'])])

    async def test_different_room_in_shared_life_is_independent(self):
        sessions = self.engine.store.raw_sessions()
        sessions['groups'] = [{'id': 'shared', 'name': '同一个她', 'sessions': [GROUP, participation.OTHER],
                               'main_session': GROUP}]
        self.engine.store.save_sessions(sessions)
        self.engine.reload_config()
        outcome, _ = await self.pending_comment()
        await self.engine._deliver(TickOutcome(participation.OTHER, messages=['另一个群的回复。'], speech_kind='reply'))
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.sent[-1], (GROUP, ['等下找给你。']))

    async def test_same_started_expression_finishes_text_and_images_after_other_speech(self):
        outcome, _ = await self.pending_comment()
        await remember_accepted(self.engine, outcome, GROUP, '第一段', ('first',), kind='participation')
        self.engine.note_visible_speech(GROUP)
        self.assertFalse(outcome.chat_speech_cancelled)
        self.assertTrue(self.engine.contact_speech_allowed(outcome, GROUP, 'participation'))
        await remember_accepted_images(self.engine, outcome, GROUP, ['https://example.test/flower.png'],
                                       ('picture',), [''], ['participation'])
        self.assertFalse(outcome.chat_speech_cancelled)
        self.assertEqual(len(outcome.accepted_receipts), 2)

    async def test_first_image_receipt_retires_another_unstarted_comment(self):
        outcome, _ = await self.pending_comment()
        picture = TickOutcome(GROUP, speech_kind='reply')
        await remember_accepted_images(self.engine, picture, GROUP, ['https://example.test/flower.png'],
                                       ('picture',), [''], ['reply'])
        self.assertIn(GROUP, outcome.chat_speech_cancelled)

    async def test_receipt_notification_keeps_original_life_and_shared_delivery_view(self):
        outcome, _ = await self.pending_comment()
        view = replace(outcome)
        self.engine.note_chat_speech_accepted(GROUP, state_key='unrelated-life')
        self.assertFalse(outcome.chat_speech_cancelled)
        self.engine.note_chat_speech_accepted(GROUP)
        self.assertIs(view.chat_speech_cancelled, outcome.chat_speech_cancelled)
        self.assertIn(GROUP, view.chat_speech_cancelled)

    async def test_acceptance_during_typing_wait_is_rechecked_before_old_comment(self):
        outcome, _ = await self.pending_comment()
        async def delayed_send(target, messages):
            await self.engine.record_self_reply(target, '等待期间已回复别的事。')
            if not segment_allowed(target, 0):
                return False
            self.messenger.sent.append((target, list(messages)))
            return True
        self.messenger.send_text = delayed_send
        await self.engine._deliver(outcome)
        self.assertFalse(self.messenger.sent)
        self.assertEqual(outcome.speech_decisions[0]['status'], 'not_sent')
        self.assertFalse(any('平台没有接收' in note for note in outcome.notes))

