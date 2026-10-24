"""Actual prompt and send regressions for routing ownership and reply priority."""
import asyncio
import time
import unittest

from core.engine.models import MessageContext, TickOutcome
from core.json_actions import PlannedAction
from core.speech_policy import segment_allowed
from tests import test_participation as contact
from tests import test_reply_batch as reply


class HostEvent:
    stopped = False

    def is_stopped(self):
        return self.stopped


class MergeFlowTests(unittest.IsolatedAsyncioTestCase):
    node_id = 'study'
    asyncSetUp = reply.ReplyBatchTest.asyncSetUp
    ctx = reply.ReplyBatchTest.ctx
    register = reply.ReplyBatchTest.register
    merge_mode = reply.ReplyBatchTest.merge_mode

    def watched(self, text='路由拒绝的内容', message_id='rejected'):
        event = HostEvent()
        record = self.engine.register_incoming(reply.SESSION, text,
            identity={'user_id':'1', 'user_name':'小明', 'message_id':message_id}, expected_event=event)
        return event, record

    async def test_stopped_message_is_not_merged_into_a_real_reply(self):
        self.register('真实问题', message_id='trigger')
        event, record = self.watched()
        event.stopped = True
        result = await self.engine.handle_reply(self.ctx('真实问题', message_id='trigger'))
        self.assertTrue(result.ok, result.error)
        self.assertNotIn('路由拒绝的内容', self.llm.user_prompt())
        self.assertFalse(record['absorbed'])
        self.assertFalse(record['picked'])

    async def test_old_registration_is_not_a_new_continuous_message(self):
        old = self.register('一小时前遗留的消息', message_id='old')
        old['at'] = time.monotonic() - 3600
        self.register('眼前的真实问题', message_id='current')
        result = await self.engine.handle_reply(self.ctx('眼前的真实问题', message_id='current'))
        self.assertTrue(result.ok, result.error)
        self.assertNotIn('一小时前遗留的消息', self.llm.user_prompt())
        self.assertFalse(old['absorbed'])
        self.assertFalse(old['picked'])

    async def test_stopped_message_does_not_restart_the_quiet_wait(self):
        event, _ = self.watched()
        event.stopped = True
        self.assertEqual(self.engine._latest_incoming_at(reply.SESSION, '1'), 0)

    async def test_recent_message_before_preparation_is_still_merged(self):
        recent = self.register('刚刚的补充', message_id='recent')
        recent['at'] = time.monotonic() - .1
        self.register('本轮触发问题', message_id='trigger')
        result = await self.engine.handle_reply(self.ctx('本轮触发问题', message_id='trigger'))
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.llm.user_prompt().count('刚刚的补充'), 1)
        self.assertTrue(recent['absorbed'])

    async def test_explicit_late_host_request_can_still_claim_its_original_record(self):
        record = self.register('迟到的真实触发', message_id='late')
        record['at'] = time.monotonic() - 3600
        result = await self.engine.handle_reply(self.ctx('迟到的真实触发', message_id='late'))
        self.assertTrue(result.ok, result.error)
        self.assertIn('迟到的真实触发', self.llm.user_prompt())
        self.assertTrue(record['absorbed'])


class ReplyPriorityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = contact.ParticipationTests.asyncSetUp
    chat = contact.ParticipationTests.chat

    async def hold_preparation(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def wait(*args, **kwargs):
            entered.set()
            await release.wait()

        self.engine._wait_for_quiet = wait
        task = asyncio.create_task(self.engine.handle_reply(MessageContext(
            session_id=contact.GROUP, user_id='alice', user_name='alice', text='在吗',
            message_id='preparing', is_mentioned=True), inject_only=True))

        async def cleanup():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.addAsyncCleanup(cleanup)
        await asyncio.wait_for(entered.wait(), 2)
        return task

    async def test_actual_preparation_blocks_life_share_status(self):
        await self.hold_preparation()
        self.assertTrue(self.engine.life_share_status(contact.GROUP)['reason'])

    async def test_actual_preparation_blocks_contact_status(self):
        await self.hold_preparation()
        self.assertTrue(self.engine.contact_status(contact.GROUP)['reason'])

    async def test_reply_preparation_blocks_delivery_of_a_prepared_chat_draft(self):
        row = await self.chat('刚才的群聊')
        outcome = TickOutcome(contact.GROUP, messages=['还没发出的接话'], speech_kind='participation')
        self.engine.claim_chat_scene(outcome, contact.GROUP, [row])
        await self.hold_preparation()
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.sent, [])

    async def test_new_reply_between_segments_stops_life_text_and_following_image(self):
        self.engine.world.life_share_daily_max = 1
        received = []

        async def send(sid, lines):
            for index, line in enumerate(lines):
                if not segment_allowed(sid, index):
                    break
                received.append(line)
                self.engine.register_incoming(sid, '现在有个问题', identity={'message_id':'new', 'user_id':'alice'})
                await asyncio.sleep(0)
            return bool(received)

        self.messenger.send_text = send
        self.messenger.take_sent_texts = lambda sid: list(received)
        outcome = TickOutcome(contact.GROUP, messages=['先分享一句', '尚未发送的第二句'],
                              images=['https://example.test/synthetic.png'], speech_kind='life_share')
        await self.engine._deliver(outcome)
        self.assertEqual(received, ['先分享一句'])
        self.assertEqual(self.messenger.sent_images, [])
        self.assertEqual(self.engine.life_share_status(contact.GROUP)['left'], 0)

    async def test_native_reply_after_text_blocks_reserved_life_share_image(self):
        async def send(sid, lines):
            self.engine.begin_native_turn({'session_id':sid, 'started_at':self.clock.now()})
            return True

        self.messenger.send_text = send
        outcome = TickOutcome(contact.GROUP, messages=['分享正文'],
                              images=['https://example.test/synthetic.png'], speech_kind='life_share')
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.sent_images, [])

    async def test_instant_send_rechecks_a_new_reply_before_speech(self):
        accepted = []

        async def sink(sid, line):
            self.engine.register_incoming(sid, '有个问题', identity={'message_id':'new', 'user_id':'alice'})
            await asyncio.sleep(0)
            if segment_allowed(sid):
                accepted.append(line)
                return True
            return False

        self.engine.say_sink = sink
        await self.engine._flush_say(TickOutcome(contact.GROUP, messages=['生活分享'], speech_kind='life_share'))
        self.assertEqual(accepted, [])

    async def test_image_adapter_rechecks_a_reply_after_engine_filtering(self):
        accepted = []

        async def send(sid, refs):
            self.engine.register_incoming(sid, '临时有个问题', identity={'message_id':'image-wait'})
            await asyncio.sleep(0)
            if segment_allowed(sid):
                accepted.extend(refs)
                return True
            return False

        self.messenger.send_images = send
        await self.engine._deliver(TickOutcome(contact.GROUP,
            images=['https://example.test/synthetic.png'], speech_kind='life_share'))
        self.assertEqual(accepted, [])
        self.assertEqual(self.engine.life_share_status(contact.GROUP)['left'],
                         self.engine.world.life_share_daily_max)
        self.assertFalse(self.engine._contact_pending)

    async def test_poke_adapter_rechecks_a_reply_after_contact_reservation(self):
        accepted = []

        async def poke(sid, uid):
            self.engine.register_incoming(sid, '现在找你', identity={'message_id':'poke-wait'})
            await asyncio.sleep(0)
            if segment_allowed(sid):
                accepted.append(uid)
                return True
            return False

        self.messenger.poke = poke
        async with self.engine.session_state(contact.GROUP) as state:
            await self.engine._run_poke(state, self.engine.node(state.node_id),
                TickOutcome(contact.GROUP, speech_kind='life_share'),
                self.engine.world.action_map()['poke'], PlannedAction(type='poke', target='alice'))
        self.assertEqual(accepted, [])
        self.assertEqual(self.engine.contact_status(contact.GROUP)['left'],
                         self.engine.world.profile.proactive_daily_max)
        self.assertFalse(self.engine._contact_pending)

    async def test_reserved_last_share_allows_all_segments_and_image_without_double_counting(self):
        self.engine.world.life_share_daily_max = 1
        outcome = TickOutcome(contact.GROUP, messages=['第一句', '第二句'],
            images=['https://example.test/synthetic.png'], speech_kind='life_share')
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.sent, [(contact.GROUP, ['第一句', '第二句'])])
        self.assertEqual(self.messenger.sent_images, [(contact.GROUP, ['https://example.test/synthetic.png'])])
        self.assertEqual(self.engine.life_share_status(contact.GROUP)['left'], 0)
        self.assertFalse(self.engine._contact_pending)

    async def test_normal_reply_is_delivered_even_while_another_reply_is_registered(self):
        self.engine.register_incoming(contact.GROUP, '下一条', identity={'message_id':'next'})
        outcome = TickOutcome(contact.GROUP, messages=['正常回复'],
                              images=['https://example.test/synthetic.png'], speech_kind='reply')
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.sent, [(contact.GROUP, ['正常回复'])])
        self.assertEqual(self.messenger.sent_images, [(contact.GROUP, ['https://example.test/synthetic.png'])])

    async def test_cancelled_preparation_releases_outreach(self):
        task = await self.hold_preparation()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertFalse(self.engine.contact_status(contact.GROUP)['reason'])
        self.assertFalse(self.engine.life_share_status(contact.GROUP)['reason'])

