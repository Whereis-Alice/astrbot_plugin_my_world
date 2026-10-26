"""Proactive hooks need native responses and may nest unlocked life work."""
import asyncio
import importlib
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

from tests import test_integration_integrity as integration
from tests.test_dialogue_inject import Event


class ProactiveHookContractTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = integration.IntegrationIntegrityTests.asyncSetUp

    def modules(self):
        return (importlib.import_module(self.module.__name__ + '.adapters.proactive_delivery'),
                importlib.import_module(self.module.__name__ + '.core.engine.life_inference'))

    async def test_accepted_hooks_run_once_inside_a_life_lease_and_restore_its_lock(self):
        delivery, life = self.modules()
        event = types.SimpleNamespace(get_extra=lambda *_: None)
        row = {'text':'已经发出的台词', 'event':event, 'accepted':False, 'history_target':None}
        wire = delivery.WIRE.set({(self.session,0):row})
        work = life.LIFE_WORK.set(True)
        provider = types.ModuleType('astrbot.core.provider.entities')
        provider.LLMResponse = types.SimpleNamespace
        try:
            with patch.dict(sys.modules, {provider.__name__:provider}), \
                    patch.object(delivery, 'append_history', AsyncMock()) as history, \
                    patch.object(delivery, '_fire_hook', AsyncMock(return_value=False)) as hook:
                async with self.plugin.engine.session_state(self.session):
                    await delivery.accepted(self.plugin,self.session,row['text'],0)
                    self.assertTrue(self.plugin.engine.lock(self.session).locked())
                    await delivery.accepted(self.plugin,self.session,row['text'],0)
                history.assert_awaited_once()
                self.assertEqual(hook.await_count,2)
                self.assertEqual([call.args[2] for call in hook.call_args_list],
                                 ['OnLLMResponseEvent','OnAfterMessageSentEvent'])
                self.assertFalse(self.plugin.engine.lock(self.session).locked())
        finally:
            life.LIFE_WORK.reset(work)
            delivery.WIRE.reset(wire)

    async def test_nested_hooks_validate_external_changes_only_when_outer_work_rejoins(self):
        _, life = self.modules()
        work = life.LIFE_WORK.set(True)
        try:
            async with self.plugin.engine.session_state(self.session) as state:
                with self.assertRaises(life.LifeWorkDiscarded):
                    async with life.released_life_lock(self.plugin.engine):
                        async with life.released_life_lock(self.plugin.engine):
                            async def edit():
                                async with self.plugin.engine.session_state(self.session) as current:
                                    current.external_state['human'] = {'text':'外部修改'}
                            await asyncio.create_task(edit())
                        self.assertFalse(self.plugin.engine.lock(self.session).locked())
                self.assertEqual(state.external_state['human']['text'],'外部修改')
                self.assertTrue(self.plugin.engine.lock(self.session).locked())
            self.assertFalse(self.plugin.engine.lock(self.session).locked())
        finally:
            life.LIFE_WORK.reset(work)

    async def test_cancelling_nested_hooks_waits_for_rejoin_and_preserves_the_other_writer(self):
        _, life = self.modules()
        entered = asyncio.Event()

        async def infer():
            work = life.LIFE_WORK.set(True)
            try:
                async with self.plugin.engine.session_state(self.session):
                    async with life.released_life_lock(self.plugin.engine):
                        async with life.released_life_lock(self.plugin.engine):
                            entered.set()
                            await asyncio.Event().wait()
            finally:
                life.LIFE_WORK.reset(work)

        task = asyncio.create_task(infer())
        try:
            await asyncio.wait_for(entered.wait(),2)
            async with self.plugin.engine.session_state(self.session) as current:
                current.external_state['human'] = {'text':'保留人工修改'}
                task.cancel()
                await asyncio.sleep(.01)
                task.cancel()
            result = await asyncio.wait_for(asyncio.gather(task,return_exceptions=True),2)
            self.assertIsInstance(result[0],asyncio.CancelledError)
            saved = await self.plugin.engine.load_state(self.session,cold_start=False)
            self.assertEqual(saved.external_state['human']['text'],'保留人工修改')
            self.assertFalse(self.plugin.engine.lock(self.session).locked())
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)

    async def test_public_chain_is_detached_before_a_native_style_text_setter_runs(self):
        from astrbot.api.event import MessageChain
        from astrbot.api import message_components
        delivery, _ = self.modules()

        class Plain:
            def __init__(self,text):
                self.text = text

        class Response:
            reasoning_content = '私有想法'

            def __init__(self):
                self.result_chain = MessageChain(chain=[Plain(text='私有草稿'),message_components.Image(file='private.png')])

            @property
            def completion_text(self):
                return ''.join(part.text for part in self.result_chain.chain if isinstance(part,Plain))

            @completion_text.setter
            def completion_text(self,text):
                self.result_chain.chain[:] = [Plain(text=text), *[
                    part for part in self.result_chain.chain if not isinstance(part,Plain)]]

        source = Response()
        original = source.result_chain
        provider = types.ModuleType('astrbot.core.provider.entities')
        provider.LLMResponse = types.SimpleNamespace
        with patch.dict(sys.modules,{provider.__name__:provider}), patch.object(message_components,'Plain',Plain):
            first = delivery.public_response('第一段',source)
            second = delivery.public_response('第二段',source)
        first.result_chain.chain[0].text = 'TTS 净化后'
        self.assertEqual(source.completion_text,'私有草稿')
        self.assertIs(source.result_chain,original)
        self.assertEqual(len(original.chain),2)
        self.assertEqual(second.completion_text,'第二段')
        self.assertEqual(len(second.result_chain.chain),1)
        self.assertEqual(first.reasoning_content,'')
        self.assertEqual(source.reasoning_content,'私有想法')

    async def test_marked_proactive_source_does_not_warn_that_a_real_user_was_blocked(self):
        event = Event('观察到的旧消息',session=self.session)
        event.set_extra('my_world_proactive',True)
        token = self.plugin.set_self_initiated()
        try:
            with patch.object(self.plugin.logger,'warning') as warning:
                await self.plugin.on_llm_request(event,types.SimpleNamespace())
            self.assertFalse(any('标记拦下' in str(call) for call in warning.call_args_list))
        finally:
            self.plugin.reset_self_initiated(token)

    async def test_unmarked_internal_user_event_still_warns_about_the_call_guard(self):
        event = Event('异常重入的用户事件',session=self.session)
        token = self.plugin.set_self_initiated()
        try:
            with patch.object(self.plugin.logger,'warning') as warning:
                await self.plugin.on_llm_request(event,types.SimpleNamespace())
            self.assertTrue(any('标记拦下' in str(call) for call in warning.call_args_list))
        finally:
            self.plugin.reset_self_initiated(token)
