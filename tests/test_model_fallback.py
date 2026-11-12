"""Role overrides own one request and never borrow a different model on failure."""
import asyncio
import unittest

from core.engine.actions import ActionsMixin
from core.background_budget import capture_background_admission
from core.llm_attempt import capture_attempt
from core.model_calls import model_context
from core.ports import LLMReply
from tests import test_engine as fixture


class Channel:
    def __init__(self, provider, result):
        self.provider = provider
        self.result = result
        self.calls = []

    async def resolve_provider_id(self, session_id):
        return self.provider

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class Engine(ActionsMixin):
    def __init__(self, primary, helper):
        self.event_llm = self.judge_llm = primary
        self.helper_llm = helper
        self.notes = []
        self.tick_seconds = 60

    def _now(self):
        return 1000

    async def _background_generate(self, channel, **kwargs):
        return await channel.generate(**kwargs)

    def _log(self, level, text):
        self.notes.append(text)


class ModelFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_wrappers_for_one_provider_do_not_duplicate_failed_or_empty_calls(self):
        for method in ('_ask_event', '_ask_judge'):
            for result in (LLMReply(ok=False, error='timeout'), LLMReply(text=''), RuntimeError('offline')):
                with self.subTest(method=method, result=result):
                    primary = Channel('same-provider', result)
                    helper = Channel('same-provider', LLMReply(text='unwanted retry'))
                    engine = Engine(primary, helper)
                    self.assertIsNone(await getattr(engine, method)('session', 'system', 'prompt'))
                    self.assertEqual(len(primary.calls), 1)
                    self.assertFalse(helper.calls)

    async def test_explicit_content_rejection_does_not_route_to_another_model(self):
        for method in ('_ask_event', '_ask_judge'):
            for result in (LLMReply(ok=False, error='PROHIBITED_CONTENT'), RuntimeError('prompt_blocked')):
                with self.subTest(method=method, result=result):
                    primary = Channel('primary', result)
                    helper = Channel('backup', LLMReply(text='unwanted retry'))
                    self.assertIsNone(await getattr(Engine(primary, helper), method)('session', 'system', 'prompt'))
                    self.assertFalse(helper.calls)

    async def test_distinct_helper_cannot_replace_the_selected_role_on_failure(self):
        for method in ('_ask_event', '_ask_judge'):
            primary = Channel('primary', LLMReply(ok=False, error='connection timeout'))
            helper = Channel('backup', LLMReply(text='available'))
            options = {'max_tokens': 321} if method == '_ask_judge' else {}
            result = await getattr(Engine(primary, helper), method)('session', 'system', 'prompt', **options)
            self.assertIsNone(result)
            self.assertEqual(len(primary.calls), 1)
            self.assertFalse(helper.calls)
            if options:
                self.assertEqual(primary.calls[0]['max_tokens'], 321)

    async def test_cancellation_never_falls_back(self):
        for method in ('_ask_event', '_ask_judge'):
            primary = Channel('primary', asyncio.CancelledError())
            helper = Channel('backup', LLMReply(text='unwanted retry'))
            with self.assertRaises(asyncio.CancelledError):
                await getattr(Engine(primary, helper), method)('session', 'system', 'prompt')
            self.assertFalse(helper.calls)

    async def test_unidentified_custom_channel_failure_also_keeps_its_selected_route(self):
        primary = Channel('', LLMReply(ok=False, error='offline'))
        helper = Channel('', LLMReply(text='available'))
        self.assertIsNone(await Engine(primary, helper)._ask_event('session', 'system', 'prompt'))
        self.assertEqual(len(primary.calls), 1)
        self.assertFalse(helper.calls)

    async def test_missing_role_retains_unissued_status_and_never_uses_helper(self):
        for method in ('_ask_event', '_ask_judge'):
            helper = Channel('backup', LLMReply(text='unwanted retry'))
            with capture_background_admission() as admission, capture_attempt('session') as attempt:
                self.assertIsNone(await getattr(Engine(None, helper), method)('session', 'system', 'prompt'))
            self.assertFalse(helper.calls)
            self.assertTrue(admission.deferred)
            self.assertFalse(admission.started)
            self.assertEqual(attempt.error, admission.reason)

    async def test_failure_is_attached_to_the_current_attempt_instead_of_a_helper_success(self):
        for method in ('_ask_event', '_ask_judge'):
            primary = Channel('primary', LLMReply(ok=False, error='connection timeout'))
            helper = Channel('backup', LLMReply(text='unwanted retry'))
            with capture_attempt('session') as attempt:
                self.assertIsNone(await getattr(Engine(primary, helper), method)('session', 'system', 'prompt'))
            self.assertEqual(attempt.error, 'connection timeout')
            self.assertFalse(helper.calls)

    async def test_success_and_same_object_only_call_once(self):
        for same_object in (True, False):
            primary = Channel('primary', LLMReply(text='available'))
            helper = primary if same_object else Channel('backup', LLMReply(text='backup'))
            self.assertEqual(await Engine(primary, helper)._ask_event('session', 'system', 'prompt'), 'available')
            self.assertEqual(len(primary.calls), 1)
            if not same_object:
                self.assertFalse(helper.calls)


class RoleBudgetIsolationTests(unittest.IsolatedAsyncioTestCase):
    node_id = 'study'
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    async def role_call(self, method):
        with model_context('life_decision', 'independent role admission', fixture.SESSION):
            return await getattr(self.engine, method)(fixture.SESSION, 'system', 'prompt')

    async def status(self):
        return await self.db.call('background_status', self.engine._today_key(self.clock.now()))

    async def test_dispatched_failure_spends_one_call_and_never_reserves_for_helper(self):
        self.engine.world.limits.background_pacing = False
        helper = Channel('backup', LLMReply(text='unwanted retry'))
        self.engine.helper_llm = helper
        for index, method in enumerate(('_ask_event', '_ask_judge'), 1):
            primary = Channel('primary-' + method, LLMReply(ok=False, error='connection timeout'))
            self.engine.event_llm = self.engine.judge_llm = primary
            with capture_background_admission() as admission:
                self.assertIsNone(await self.role_call(method))
            self.assertTrue(admission.started)
            self.assertEqual(len(primary.calls), 1)
            self.assertFalse(helper.calls)
            self.assertEqual((await self.status())['calls'], index)

    async def test_budget_wait_calls_neither_role_nor_helper_and_preserves_admission(self):
        self.engine.world.limits.background_pacing = False
        self.engine.world.limits.background_daily_calls = 1
        day = self.engine._today_key(self.clock.now())
        await self.db.call('reserve_background', day, calls=1, tokens=0, reserve=0)
        before = await self.status()
        primary = Channel('primary', LLMReply(text='not dispatched'))
        helper = Channel('backup', LLMReply(text='unwanted retry'))
        self.engine.event_llm = self.engine.judge_llm = primary
        self.engine.helper_llm = helper
        for method in ('_ask_event', '_ask_judge'):
            with capture_background_admission() as admission:
                self.assertIsNone(await self.role_call(method))
            self.assertTrue(admission.deferred)
            self.assertFalse(admission.started)
            self.assertFalse(primary.calls)
            self.assertFalse(helper.calls)
            self.assertEqual(await self.status(), before)

    async def test_local_provider_cancel_propagates_and_spends_no_call(self):
        self.engine.world.limits.background_pacing = False
        for method in ('_ask_event', '_ask_judge'):
            primary = Channel('primary', LLMReply(text='not dispatched'))
            helper = Channel('backup', LLMReply(text='unwanted retry'))
            self.engine.event_llm = self.engine.judge_llm = primary
            self.engine.helper_llm = helper

            async def cancel(session_id):
                raise asyncio.CancelledError()

            primary.resolve_provider_id = cancel
            with capture_background_admission() as admission:
                with self.assertRaises(asyncio.CancelledError):
                    await self.role_call(method)
            self.assertTrue(admission.deferred)
            self.assertFalse(admission.started)
            self.assertFalse(primary.calls)
            self.assertFalse(helper.calls)
            self.assertEqual((await self.status())['calls'], 0)
