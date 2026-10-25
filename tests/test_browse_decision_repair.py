"""Existing reading thoughts must survive bounded repairs of speech decisions."""
import json
import unittest

from tests import test_chat_browse as fixture


class BrowseDecisionRepairTests(unittest.IsolatedAsyncioTestCase):
    node_id = fixture.ChatBrowseTests.node_id
    asyncSetUp = fixture.ChatBrowseTests.asyncSetUp
    configure = fixture.ChatBrowseTests.configure
    note = fixture.ChatBrowseTests.note
    prepare = fixture.ChatBrowseTests.prepare

    async def begin(self, *decisions):
        action = await self.prepare(reply_enabled=True, message_id='flower')
        self.llm.replies = [json.dumps({'inner': '也想在自己的窗边留一小盆花。',
            'source_seqs': [action['browse_rows'][0]['seq']]}), *decisions]
        return action

    async def test_recorded_thought_is_reused_after_one_decision_repair(self):
        action = await self.begin('窗边的花真好看。',
            json.dumps({'intent': 'join_chat', 'say': '窗边的花真好看。', 'reply_to': 'm1'}))
        await self.engine._complete_chat_browse(fixture.S, action['id'])
        self.assertEqual(len(self.llm.calls), 3)
        self.assertEqual(self.messenger.sent, [(fixture.S, ['窗边的花真好看。'])])
        state = await self.engine.load_state(fixture.S)
        self.assertEqual(len(state.thoughts), 1)
        self.assertEqual(state.thoughts[0]['content'], '也想在自己的窗边留一小盆花。')
        self.assertTrue(state.thoughts[0]['browse_replied'])

    async def test_repair_can_keep_the_decision_to_listen_without_a_new_thought(self):
        action = await self.begin('{"intent":"listen",', '{"intent":"listen","say":""}')
        await self.engine._complete_chat_browse(fixture.S, action['id'])
        self.assertEqual(len(self.llm.calls), 3)
        self.assertFalse(self.messenger.sent)
        state = await self.engine.load_state(fixture.S)
        self.assertEqual(state.thoughts[0]['detail']['speech']['status'], 'listening')
        self.assertEqual(state.thoughts[0]['content'], '也想在自己的窗边留一小盆花。')

    async def test_empty_provider_result_is_not_replayed(self):
        action = await self.begin('')
        await self.engine._complete_chat_browse(fixture.S, action['id'])
        self.assertEqual(len(self.llm.calls), 2)
        self.assertFalse(self.messenger.sent)

    async def test_two_invalid_decisions_stop_after_one_repair(self):
        action = await self.begin('窗边有花。', '仍然没有JSON。')
        await self.engine._complete_chat_browse(fixture.S, action['id'])
        self.assertEqual(len(self.llm.calls), 3)
        self.assertFalse(self.messenger.sent)
        state = await self.engine.load_state(fixture.S)
        self.assertEqual(state.thoughts[0]['detail']['browse_reply']['status'], 'failed')

    async def test_scene_expiring_during_repair_is_not_sent(self):
        action = await self.begin('窗边有花。', '{"intent":"join_chat","say":"好想看看。"}')
        original = self.llm.generate

        async def slow(**kwargs):
            if len(self.llm.calls) == 2:
                self.clock.advance(901)
            return await original(**kwargs)

        self.llm.generate = slow
        await self.engine._complete_chat_browse(fixture.S, action['id'])
        self.assertEqual(len(self.llm.calls), 3)
        self.assertFalse(self.messenger.sent)

    async def test_browse_repair_keeps_tool_results_without_repeating_tools(self):
        from core.engine.models import TickOutcome
        from core.speech_runtime import SpeechDraft, SPEECH_REQUEST

        action = await self.prepare(reply_enabled=False, message_id='flower')
        self.llm.replies = [json.dumps({'inner': '也想在自己的窗边留一小盆花。',
            'source_seqs': [action['browse_rows'][0]['seq']]})]
        await self.engine._complete_chat_browse(fixture.S, action['id'])
        self.engine.world.chat_browse.reply_enabled = True
        requests = []

        async def generate(*args, **kwargs):
            requests.append(SPEECH_REQUEST.get()['allow_tools'])
            if len(requests) == 1:
                return SpeechDraft('看到了。', images=['/synthetic/image.png'],
                    tools=[{'name': 'lookup', 'status': 'completed'}])
            return '{"intent":"join_chat","say":"看到了。"}'

        self.engine._ask_llm = generate
        outcome = TickOutcome(fixture.S, speech_kind='browse_reply')
        async with self.engine.session_state(fixture.S) as state:
            await self.engine.participation_text(state, fixture.S, source='browse',
                browse_id=action['id'], outcome=outcome)
            self.assertEqual(requests, [True, False])
            self.assertEqual(state.thoughts[0]['detail']['speech']['tools'][0]['name'], 'lookup')
            self.assertIn('/synthetic/image.png', outcome.images)
