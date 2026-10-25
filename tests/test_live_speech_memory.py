"""Actual transport acceptance must become known before a follow-up reply starts."""
import asyncio
import importlib
import logging
import types
import unittest
from unittest.mock import AsyncMock

from tests import test_dialogue_inject as host
from tests import test_participation as contact

HostTickOutcome = importlib.import_module(host.MODULE.__name__ + '.core.engine.models').TickOutcome
create_plan = importlib.import_module(host.MODULE.__name__ + '.core.planner').create_plan

class LiveSpeechMemoryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = contact.ParticipationTests.asyncSetUp

    async def real_transport(self, *, delay=0, accepted=True):
        previous = self.engine
        self.engine = host.MODULE.MyWorldEngine(store=previous.store, db=self.db,
            llm=self.llm, persona=previous.persona, clock=self.clock)
        self.addAsyncCleanup(self.engine.close_background)
        self.accepted = asyncio.Event()
        self.transported = []

        async def send(sid, chain):
            if accepted:
                self.transported.append((sid, chain))
                self.accepted.set()
            return accepted

        plugin = types.SimpleNamespace(engine=self.engine, context=types.SimpleNamespace(send_message=send),
            typing_delay_for=lambda _: delay, logger=logging.getLogger('live-memory-test'))
        self.messenger = host.MODULE.AstrBotMessenger(plugin)
        self.engine.messenger = self.messenger
        self.engine.say_sink = lambda sid, text: self.messenger.send_text(sid, [text])
        self.engine._wait_for_quiet = AsyncMock()

    async def reply_prompt(self):
        plan = await self.engine.handle_reply(host.MODULE.MessageContext(
            session_id=contact.GROUP, user_id='alice', user_name='alice',
            text='你刚刚说到哪了？', is_mentioned=True, message_id='followup'), inject_only=True)
        self.assertTrue(plan.ok)
        return plan.runtime_part

    async def test_reply_sees_accepted_first_segment_before_the_batch_finishes(self):
        await self.real_transport(delay=60)
        outcome = HostTickOutcome(contact.GROUP, speech_kind='life_share',
            messages=['我刚把山上的风车修好了。', '还没发出的后半句。'])
        task = asyncio.create_task(self.engine._deliver(outcome))

        async def cleanup():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.addAsyncCleanup(cleanup)
        await asyncio.wait_for(self.accepted.wait(), 3)
        self.assertFalse(task.done())
        prompt = await asyncio.wait_for(self.reply_prompt(), 3)
        self.assertIn('我刚把山上的风车修好了。', prompt)
        self.assertNotIn('还没发出的后半句。', prompt)

    async def test_instant_speech_is_known_before_final_delivery(self):
        await self.real_transport()
        outcome = HostTickOutcome(contact.GROUP, speech_kind='life_share',
            messages=['我刚给窗台上的花浇了水。'])
        await self.engine._flush_say(outcome)
        self.assertEqual(outcome.live_messages, ['我刚给窗台上的花浇了水。'])
        self.assertIn('我刚给窗台上的花浇了水。', await self.reply_prompt())

    async def test_completed_delivery_records_each_accepted_segment_once(self):
        await self.real_transport()
        outcome = HostTickOutcome(contact.GROUP, speech_kind='life_share', messages=['我刚修好了风车。'])
        await self.engine._deliver(outcome)
        state = await self.engine.load_state(contact.GROUP, cold_start=False)
        self.assertEqual([row['text'] for row in state.recent_chat if row.get('is_self')], ['我刚修好了风车。'])

    async def test_rejected_transport_is_not_remembered_as_speech(self):
        await self.real_transport(accepted=False)
        await self.engine._deliver(HostTickOutcome(contact.GROUP, speech_kind='life_share', messages=['没有成功发出的分享。']))
        state = await self.engine.load_state(contact.GROUP, cold_start=False)
        self.assertFalse([row for row in state.recent_chat if row.get('is_self')])

    async def test_real_image_receipt_is_known_to_a_followup_reply(self):
        await self.real_transport()
        outcome = HostTickOutcome(contact.GROUP, speech_kind='life_share')
        self.engine._attach_images(outcome, ['https://example.com/flower.png'],
            session_id=contact.GROUP, label='窗台上的花')
        await self.engine._deliver(outcome)
        state = await self.engine.load_state(contact.GROUP, cold_start=False)
        own = [r for r in state.recent_chat if r.get('is_self')]
        self.assertEqual(len(own), 1)
        self.assertEqual(own[0]['images'], ['https://example.com/flower.png'])
        self.assertIn('窗台上的花', await self.reply_prompt())

    async def test_real_transport_drops_invalid_image_before_recording(self):
        await self.real_transport()
        outcome = HostTickOutcome(contact.GROUP, speech_kind='life_share',
            images=['unusable-image', 'https://example.com/flower.png'])
        await self.engine._deliver(outcome)
        state = await self.engine.load_state(contact.GROUP, cold_start=False)
        own = [r for r in state.recent_chat if r.get('is_self')]
        self.assertEqual(own[0]['image_count'], 1)
        self.assertEqual(own[0]['images'], ['https://example.com/flower.png'])
        self.assertNotIn('unusable-image', str(own))

    async def test_real_rejected_image_does_not_create_an_own_memory(self):
        await self.real_transport(accepted=False)
        outcome = HostTickOutcome(contact.GROUP, speech_kind='life_share')
        self.engine._attach_images(outcome, ['https://example.com/flower.png'],
            session_id=contact.GROUP, label='还未发出的图片')
        await self.engine._deliver(outcome)
        state = await self.engine.load_state(contact.GROUP, cold_start=False)
        self.assertFalse(any(r.get('is_self') for r in state.recent_chat))

    async def test_plan_image_receipt_updates_the_original_thought(self):
        await self.real_transport()
        outcome = await self.prepare_plan(['给你看看。'])
        trace = outcome.speech_decisions[0]
        plan = {'id': trace['plan_id'], 'thought_id': trace['thought_id'], 'created_ts': trace['at']}
        policy = importlib.import_module(host.MODULE.__name__ + '.core.speech_policy')
        token = policy.PLAN_SPEECH.set((outcome, plan))
        try:
            self.engine._attach_images(outcome, ['https://example.com/flower.png'], session_id=contact.GROUP)
        finally:
            policy.PLAN_SPEECH.reset(token)
        await self.engine._deliver(outcome)
        thought = (await self.engine.thoughts_payload(contact.GROUP))['thoughts'][0]
        self.assertEqual(thought['detail']['speech']['sent_images'], ['https://example.com/flower.png'])
        self.assertEqual(thought['detail']['speech']['status'], 'sent')
        self.assertEqual(thought['reply'], '给你看看。')

    async def prepare_plan(self, lines, *, targets=None):
        outcome = HostTickOutcome(contact.GROUP, speech_kind='life_share')
        async with self.engine.session_state(contact.GROUP) as state:
            plan = create_plan(steps=[{'action': 'say', 'messages': [line],
                'send_to': (targets or [contact.GROUP] * len(lines))[i]} for i, line in enumerate(lines)],
                world_time=state.world_time, now=self.clock.now(), source='llm', reason='说一句，再去充电。')
            plan['inner'] = '大家还在拿我的照片开玩笑呢。'
            self.engine._freeze_plan_target(state, outcome, plan, fallback=contact.GROUP)
            await self.engine._apply_plan(state, self.engine.node(state.node_id), outcome, plan)
            await self.engine._tick_plan(state, self.engine.node(state.node_id), outcome, depth=0)
        return outcome

    async def test_plan_speech_updates_its_original_thought(self):
        await self.real_transport()
        outcome = await self.prepare_plan(['大半夜的，都去休息吧。'])
        await self.engine._deliver(outcome)
        rows = (await self.engine.thoughts_payload(contact.GROUP))['thoughts']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['source'], '自主决定')
        self.assertEqual(rows[0]['reply'], '大半夜的，都去休息吧。')
        self.assertEqual(rows[0]['detail']['speech']['status'], 'sent')

    async def test_plan_rejected_speech_is_not_shown_as_sent(self):
        await self.real_transport(accepted=False)
        await self.engine._deliver(await self.prepare_plan(['没有发出的台词。']))
        row = (await self.engine.thoughts_payload(contact.GROUP))['thoughts'][0]
        self.assertFalse(row['reply'])
        self.assertEqual(row['detail']['speech']['status'], 'not_sent')

    async def test_shared_world_plan_keeps_private_speech_scoped(self):
        await self.real_transport()
        sessions = self.engine.store.raw_sessions()
        sessions['groups'] = [{'id': 'shared', 'name': '同一个她', 'sessions': [contact.GROUP, contact.PRIVATE], 'main_session': contact.GROUP}]
        self.engine.store.save_sessions(sessions)
        self.engine.reload_config()
        outcome = await self.prepare_plan(['群里的一句。', '只对你说的秘密。'],
            targets=[contact.GROUP, contact.PRIVATE])
        await self.engine._deliver(outcome)
        group = (await self.engine.thoughts_payload(contact.GROUP))['thoughts'][0]
        private = (await self.engine.thoughts_payload(contact.PRIVATE))['thoughts'][0]
        self.assertEqual(group['reply'], '群里的一句。')
        self.assertNotIn('只对你说的秘密', str(group))
        self.assertEqual(private['reply'], '只对你说的秘密。')
        self.assertNotIn('群里的一句', str(private))
        snapshot = await self.engine.snapshot(contact.GROUP)
        self.assertNotIn('只对你说的秘密', str(snapshot['thoughts']))
        prompt = await self.reply_prompt()
        self.assertIn('群里的一句。', prompt)
        # Shared worlds deliberately retain the same character's other chats;
        # they must remain labelled as another conversation, not this one.
        self.assertIn('〔私聊 alice〕你：只对你说的秘密。', prompt)

    async def test_plan_speech_accumulates_across_ticks_without_duplication(self):
        await self.real_transport()
        tick = self.engine._tick_plan

        async def one_step(*args, depth, **kwargs):
            if depth == 0:
                await tick(*args, depth=depth, **kwargs)

        # Execute one actual instant step per tick to exercise a deferred plan.
        self.engine._tick_plan = one_step
        await self.engine._deliver(await self.prepare_plan(['第一轮说的话。', '第二轮说的话。']))
        self.clock.advance(181)
        outcome = HostTickOutcome(contact.GROUP)
        async with self.engine.session_state(contact.GROUP) as state:
            await self.engine._tick_plan(state, self.engine.node(state.node_id), outcome, depth=0)
        await self.engine._deliver(outcome)
        row = (await self.engine.thoughts_payload(contact.GROUP))['thoughts'][0]
        self.assertEqual(row['reply'], '第一轮说的话。\n第二轮说的话。')
        self.assertEqual(row['detail']['speech']['status'], 'sent')
        state = await self.engine.load_state(contact.GROUP, cold_start=False)
        self.assertEqual([r['text'] for r in state.recent_chat if r.get('is_self')], ['第一轮说的话。', '第二轮说的话。'])

    async def test_plan_first_segment_is_visible_while_second_is_waiting(self):
        await self.real_transport(delay=60)
        outcome = await self.prepare_plan(['已经说出的第一句。', '没有发出的第二句。'])
        task = asyncio.create_task(self.engine._deliver(outcome))

        async def cleanup():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.addAsyncCleanup(cleanup)
        await asyncio.wait_for(self.accepted.wait(), 3)
        # Platform acceptance wakes us before its receipt commit; wait for the
        # adapter to enter the typing pause, without taking a reply/world lock.
        for _ in range(100):
            rows = (await self.engine.thoughts_payload(contact.GROUP))['thoughts']
            if rows and rows[0]['reply']:
                break
            await asyncio.sleep(.01)
        self.assertEqual(rows[0]['reply'], '已经说出的第一句。')
        self.assertEqual(rows[0]['detail']['speech']['status'], 'partial')
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        rows = (await self.engine.thoughts_payload(contact.GROUP))['thoughts']
        self.assertEqual(rows[0]['reply'], '已经说出的第一句。')
        state = await self.engine.load_state(contact.GROUP, cold_start=False)
        self.assertEqual([r['text'] for r in state.recent_chat if r.get('is_self')], ['已经说出的第一句。'])

    async def test_instant_plan_speech_reuses_the_owned_world_lock_and_records_once(self):
        await self.real_transport()
        outcome = await self.prepare_plan(['先说一句，然后去忙。'])
        async with self.engine.session_state(contact.GROUP):
            # A timeout in this task preserves the owner of STATE_LEASE.
            async with asyncio.timeout(3):
                await self.engine._flush_say(outcome)
        row = (await self.engine.thoughts_payload(contact.GROUP))['thoughts'][0]
        self.assertEqual(row['reply'], '先说一句，然后去忙。')
        await self.engine._deliver(outcome)
        state = await self.engine.load_state(contact.GROUP, cold_start=False)
        self.assertEqual([r['text'] for r in state.recent_chat if r.get('is_self')], ['先说一句，然后去忙。'])
        self.assertEqual(outcome.speech_decisions[0]['sent'], ['先说一句，然后去忙。'])

    async def test_echo_with_identical_text_does_not_become_character_speech(self):
        await self.real_transport()
        outcome = await self.prepare_plan(['同一段文字。'])
        outcome.debug_messages = ['同一段文字。']
        outcome.debug_positions = [0]
        calls = 0

        async def send(sid, chain):
            nonlocal calls
            calls += 1
            return calls == 1  # Echo accepted, real speech rejected.

        self.messenger.plugin.context.send_message = send
        await self.engine._deliver(outcome)
        row = (await self.engine.thoughts_payload(contact.GROUP))['thoughts'][0]
        self.assertFalse(row['reply'])
        state = await self.engine.load_state(contact.GROUP, cold_start=False)
        self.assertFalse([r for r in state.recent_chat if r.get('is_self')])

    async def test_repeated_actual_lines_are_each_recorded_once(self):
        await self.real_transport()
        await self.engine._deliver(await self.prepare_plan(['嗯。', '嗯。']))
        row = (await self.engine.thoughts_payload(contact.GROUP))['thoughts'][0]
        self.assertEqual(row['reply'], '嗯。\n嗯。')
        self.assertEqual(row['detail']['speech']['status'], 'sent')
        state = await self.engine.load_state(contact.GROUP, cold_start=False)
        self.assertEqual([r['text'] for r in state.recent_chat if r.get('is_self')], ['嗯。', '嗯。'])

    async def test_receipt_save_failure_does_not_resend_accepted_text(self):
        await self.real_transport()
        outcome = await self.prepare_plan(['只发送一次。'])
        save = self.engine.save_state
        failed = False

        async def fail_once(state):
            nonlocal failed
            if not failed:
                failed = True
                raise OSError('temporary storage error')
            await save(state)

        self.engine.save_state = fail_once
        await self.engine._deliver(outcome)
        self.assertEqual(len(self.transported), 1)
        self.assertFalse(self.messenger.blocked(contact.GROUP))
        row = (await self.engine.thoughts_payload(contact.GROUP))['thoughts'][0]
        self.assertEqual(row['reply'], '只发送一次。')
        state = await self.engine.load_state(contact.GROUP, cold_start=False)
        self.assertEqual([r['text'] for r in state.recent_chat if r.get('is_self')], ['只发送一次。'])

    async def test_cancelled_instant_speech_retains_receipt_and_contact_count(self):
        await self.real_transport()
        outcome = await self.prepare_plan(['已经即时说出的话。'])
        accept_segment = importlib.import_module(host.MODULE.__name__ + '.core.speech_policy').accept_segment

        async def accepted_then_cancelled(sid, text):
            await accept_segment(sid, text, 0)
            raise asyncio.CancelledError()

        self.engine.say_sink = accepted_then_cancelled
        before = self.engine.life_share_status(contact.GROUP)['left']
        async with self.engine.session_state(contact.GROUP):
            with self.assertRaises(asyncio.CancelledError):
                await self.engine._flush_say(outcome)
        self.assertEqual(self.engine.life_share_status(contact.GROUP)['left'], before - 1)
        await self.engine._deliver(outcome)
        row = (await self.engine.thoughts_payload(contact.GROUP))['thoughts'][0]
        self.assertEqual(row['reply'], '已经即时说出的话。')
        self.assertEqual(row['detail']['speech']['status'], 'sent')
        self.assertFalse(self.transported)  # Final delivery never replays the live line.
