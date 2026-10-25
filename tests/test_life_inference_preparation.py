"""Prepared decisions retain their character; interrupted quiet work retains its time."""

import json
import unittest

from core.models import ChainStep
from core.engine import TickOutcome
from core.state import WorldState
from tests import test_engine as fixture
from tests import test_life_resume_boundaries as resume

SESSION = fixture.SESSION


class LifePreparationIdentityTests(unittest.IsolatedAsyncioTestCase):
    node_id = 'study'
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    async def test_persona_change_after_prompt_preparation_does_not_rebind_old_decision(self):
        original_persona = self.persona.text
        self.engine.world.proactive_chat_paused = True
        self.engine.decider.should_ask_llm = lambda _state: True
        self.llm.replies = [json.dumps({'plan': [{'action': 'read', 'duration': 600}],
                                       'reason': '想接着读少女漫画', 'inner': '刚才那段很有趣'})]
        async with self.engine.session_state(SESSION) as state:
            state.state, state.energy = 'idle', .9
        original_generate = self.engine._background_generate
        prepared = []

        async def change_before_request(channel, **kwargs):
            prepared.append(kwargs['system_prompt'])
            self.persona.text = '你是严谨克制的学者。'
            return await original_generate(channel, **kwargs)

        self.engine._background_generate = change_before_request
        result = await self.engine.maybe_decide(SESSION, force=True)
        self.assertTrue(prepared)
        self.assertIn(original_persona, prepared[0])
        self.assertIsNone(result)
        saved = await self.engine.load_state(SESSION, cold_start=False)
        self.assertIsNone(saved.current_action)
        self.assertIsNone(saved.current_plan)
        self.assertEqual(self.llm.calls, [], 'a known stale prompt should not consume a model request')

    async def test_world_reload_after_prompt_preparation_does_not_adopt_old_request(self):
        self.engine.world.proactive_chat_paused = True
        self.engine.decider.should_ask_llm = lambda _state: True
        async with self.engine.session_state(SESSION) as state:
            state.state, state.energy = 'idle', .9
        original_generate = self.engine._background_generate
        prepared = []

        async def reload_before_request(channel, **kwargs):
            prepared.append(kwargs['system_prompt'])
            self.engine.reload_config()
            return await original_generate(channel, **kwargs)

        self.engine._background_generate = reload_before_request
        result = await self.engine.maybe_decide(SESSION, force=True)
        self.assertTrue(prepared)
        self.assertIsNone(result)
        self.assertEqual(self.llm.calls, [])


class MixedPlanResumptionTests(unittest.IsolatedAsyncioTestCase):
    node_id = 'study'
    asyncSetUp = fixture.EngineTestCase.asyncSetUp
    configure_action = resume.LifeResumeBoundaryTests.configure_action
    quiet_action = resume.LifeResumeBoundaryTests.quiet_action
    work_minutes = resume.LifeResumeBoundaryTests.work_minutes
    interrupt_and_resume = resume.LifeResumeBoundaryTests.interrupt_and_resume

    async def test_pending_speech_does_not_erase_running_quiet_work_or_replay_later_tail(self):
        first = self.quiet_action(quota={'day': 1})
        speech = self.quiet_action('closing_note', category='instant', visible=True,
                                   template='我刚整理完书架。')
        later = self.quiet_action('labels_work', name='给书贴标签', duration=1800)
        async with self.engine.session_state(SESSION) as state:
            await self.engine._run_chain(state, [ChainStep(type=first), ChainStep(type=speech),
                ChainStep(type=later)], TickOutcome(SESSION, speech_kind='life_share'), depth=0)
        await self.work_minutes(3)
        state = await self.interrupt_and_resume()
        self.assertIsNotNone(state.current_action)
        self.assertEqual(state.current_action['type'], first)
        self.assertEqual(state.current_action['duration_ticks'], 7)
        self.assertEqual([s['action'] for s in state.current_plan['steps']], [first])
        await self.work_minutes(7)
        async with self.engine.session_state(SESSION) as state:
            await self.engine._tick_plan(state, self.engine.node(state.node_id), TickOutcome(SESSION), depth=0)
        self.assertIsNone(state.current_action)
        self.assertEqual(self.messenger.flat_messages, [])
        self.assertEqual(state.action_usage[first][self.engine._usage_keys()['day']], 1)
        self.assertNotIn(later, state.action_usage)


class PhysiologyPromptTests(unittest.IsolatedAsyncioTestCase):
    node_id = 'study'
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    async def test_activity_planning_does_not_suggest_naps_or_overnight_sleep(self):
        self.engine.world.sleep.mode = 'activity'
        self.engine.world.sleep.recovery_threshold = .65
        self.engine.world.sleep.recovery_target = .9
        self.persona.text = '你是艾拉，不需要睡眠，通过充电恢复精力。'
        state = WorldState(SESSION, node_id='study', energy=.55)
        await self.engine._ask_llm_for_plan(state, self.engine.node('study'), TickOutcome(SESSION), force=True)
        request = self.llm.calls[-1]
        self.assertNotIn('可以睡一整晚', request['prompt'])
        self.assertNotIn('白天只是犯困就先小睡', request['prompt'])
        self.assertNotIn('再结合精力、困意', request['prompt'])
        self.assertIn('不需要睡眠', request['system_prompt'])
        self.assertNotIn('低于 0.25 会明显犯困', request['system_prompt'])
        self.assertIn('低于 0.65', request['system_prompt'])
        self.assertIn('0.90', request['system_prompt'])

    async def test_both_runtime_paths_follow_physiology_without_mutating_old_sleep_state(self):
        self.engine.world.sleep.mode = 'activity'
        self.engine.world.sleep.recovery_threshold = .65
        state = WorldState(SESSION, node_id='study', energy=.55, world_time=10,
                           sleep_pressure=.8, sleep_delay_until=20, sleep_delay_reason='稍后再睡')
        before = state.to_payload()
        request = self.engine.context_request(state, SESSION, task='reply', text='早上好')
        for selected in (None, request):
            with self.subTest(task_context=selected is not None):
                text = self.engine.prompts.runtime_layer(state, node=self.engine.node('study'),
                                                        context_request=selected)
                self.assertNotIn('低于 0.25 会明显犯困', text)
                self.assertNotIn('- 困意 ', text)
                self.assertNotIn('你答应了「等会儿再睡」', text)
                self.assertIn('低于 0.65', text)
        self.assertEqual(state.to_payload(), before)

    async def test_sleeping_character_keeps_daytime_naps_and_overnight_rules(self):
        for mode in ('fixed', 'autonomous'):
            with self.subTest(mode=mode):
                self.engine.world.sleep.mode = mode
                state = WorldState(SESSION, node_id='study', energy=.55, sleep_pressure=.8)
                await self.engine._ask_llm_for_plan(state, self.engine.node('study'), TickOutcome(SESSION), force=True)
                request = self.llm.calls[-1]
                self.assertIn('白天只是犯困就先小睡', request['prompt'])
                self.assertIn('低于 0.25 会明显犯困', request['system_prompt'])
                self.assertIn('- 困意 ', request['system_prompt'])
