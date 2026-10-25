"""Queued chat cannot replay a scene that another real reply already answered."""
import unittest
import gc

from core.engine import TickOutcome
from core.speech_decision import SpeechDecision
from tests import test_participation as participation

GROUP = participation.GROUP


class QueuedChatContinuityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = participation.ParticipationTests.asyncSetUp
    chat = participation.ParticipationTests.chat

    async def pending_comment(self, *, rows=None, target=GROUP, reply_to='', kind='participation'):
        row = rows[-1] if rows else await self.chat('那张风车照片能给我看吗？', sid=target, message_id='photo-question')
        rows = rows or [row]
        outcome = TickOutcome(target, speech_kind=kind)
        async with self.engine.session_state(target) as state:
            await self.engine.remember_speech_decision(state, target,
                SpeechDecision(inner='想把照片给他看。', say='等下找给你。', intent='join_chat', reply_to=reply_to),
                trigger='message', outcome=outcome)
            self.engine.claim_chat_scene(outcome, target, rows, state=state)
        outcome.add_speech('等下找给你。')
        if reply_to:
            outcome.reply_targets[target] = reply_to
        return outcome, row

    async def test_a_completed_native_reply_cancels_the_old_queued_comment(self):
        outcome, row = await self.pending_comment()
        await self.engine._deliver(TickOutcome(GROUP, messages=['刚才已经发给你了。'], speech_kind='reply'))
        await self.engine.mark_reply_batch_replied(GROUP, batch=[row])
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.sent, [(GROUP, ['刚才已经发给你了。'])])
        self.assertEqual(outcome.speech_decisions[0]['status'], 'not_sent')

    async def test_unanswered_claims_can_still_be_considered(self):
        old = await self.chat('我先聊一张照片。', message_id='old')
        new = await self.chat('另外想看看风车。', message_id='new')
        outcome, _ = await self.pending_comment(rows=[old, new])
        await self.engine.mark_reply_batch_replied(GROUP, batch=[old])
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.sent, [(GROUP, ['等下找给你。'])])

    async def test_answered_quote_is_not_replaced_with_another_unanswered_message(self):
        old = await self.chat('第一张照片在哪里？', message_id='old')
        new = await self.chat('我还有个别的问题。', message_id='new')
        outcome, _ = await self.pending_comment(rows=[old, new], reply_to='old')
        await self.engine.mark_reply_batch_replied(GROUP, batch=[old])
        await self.engine._deliver(outcome)
        self.assertFalse(self.messenger.sent)
        self.assertEqual(outcome.speech_decisions[0]['status'], 'not_sent')

    async def test_unclaimed_new_messages_do_not_resurrect_the_old_comment(self):
        outcome, row = await self.pending_comment()
        await self.engine.mark_reply_batch_replied(GROUP, batch=[row])
        await self.chat('大家换个话题吧。', message_id='new-topic')
        await self.engine._deliver(outcome)
        self.assertFalse(self.messenger.sent)

    async def test_shared_life_reply_watermarks_remain_destination_local(self):
        sessions = self.engine.store.raw_sessions()
        sessions['groups'] = [{'id': 'shared', 'name': '同一个她', 'sessions': [GROUP, participation.OTHER], 'main_session': GROUP}]
        self.engine.store.save_sessions(sessions)
        self.engine.reload_config()
        outcome, _ = await self.pending_comment()
        other = await self.chat('另一个群聊的照片。', sid=participation.OTHER, message_id='elsewhere')
        await self.engine.mark_reply_batch_replied(participation.OTHER, batch=[other])
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.sent, [(GROUP, ['等下找给你。'])])

    async def test_actual_first_autonomous_reply_cancels_a_duplicate_scene(self):
        first, row = await self.pending_comment()
        second, _ = await self.pending_comment(rows=[row], kind='continuation')
        await self.engine._deliver(first)
        await self.engine._deliver(second)
        self.assertEqual(self.messenger.sent, [(GROUP, ['等下找给你。'])])
        self.assertEqual(second.speech_decisions[0]['status'], 'not_sent')

    async def test_persisted_reply_progress_is_refreshed_at_delivery(self):
        outcome, row = await self.pending_comment()
        state = await self.engine.load_state(GROUP, cold_start=False)
        state.chat_replied_seq = row['seq']
        state.chat_replied_until = row['at']
        await self.engine.save_state(state)
        self.assertEqual(outcome.chat_replied_seq[GROUP], 0)
        await self.engine._deliver(outcome)
        self.assertFalse(self.messenger.sent)

    async def test_native_reply_and_life_share_keep_their_own_semantics(self):
        outcome, row = await self.pending_comment(kind='life_share')
        await self.engine.mark_reply_batch_replied(GROUP, batch=[row])
        await self.engine._deliver(outcome)
        await self.engine._deliver(TickOutcome(GROUP, messages=['我还在。'], speech_kind='reply'))
        self.assertEqual(self.messenger.sent, [(GROUP, ['等下找给你。']), (GROUP, ['我还在。'])])

    async def test_observation_does_not_keep_finished_outcomes_alive(self):
        outcome, _ = await self.pending_comment()
        self.assertEqual(len(self.engine._queued_chat_outcomes), 1)
        del outcome
        gc.collect()
        self.assertEqual(len(self.engine._queued_chat_outcomes), 0)
