"""Fallback channels must not repeat rejected work against the same provider."""
import asyncio
import unittest

from core.engine.actions import ActionsMixin
from core.ports import LLMReply


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

    async def test_distinct_provider_can_handle_temporary_failures(self):
        for method in ('_ask_event', '_ask_judge'):
            primary = Channel('primary', LLMReply(ok=False, error='connection timeout'))
            helper = Channel('backup', LLMReply(text='available'))
            options = {'max_tokens': 321} if method == '_ask_judge' else {}
            result = await getattr(Engine(primary, helper), method)('session', 'system', 'prompt', **options)
            self.assertEqual(result, 'available')
            self.assertEqual(len(helper.calls), 1)
            self.assertEqual(helper.calls[0]['prompt'], 'prompt')
            if options:
                self.assertEqual(helper.calls[0]['max_tokens'], 321)

    async def test_cancellation_never_falls_back(self):
        for method in ('_ask_event', '_ask_judge'):
            primary = Channel('primary', asyncio.CancelledError())
            helper = Channel('backup', LLMReply(text='unwanted retry'))
            with self.assertRaises(asyncio.CancelledError):
                await getattr(Engine(primary, helper), method)('session', 'system', 'prompt')
            self.assertFalse(helper.calls)

    async def test_unidentified_custom_channels_keep_existing_fallback(self):
        primary = Channel('', LLMReply(ok=False, error='offline'))
        helper = Channel('', LLMReply(text='available'))
        self.assertEqual(await Engine(primary, helper)._ask_event('session', 'system', 'prompt'), 'available')

    async def test_success_and_same_object_only_call_once(self):
        for same_object in (True, False):
            primary = Channel('primary', LLMReply(text='available'))
            helper = primary if same_object else Channel('backup', LLMReply(text='backup'))
            self.assertEqual(await Engine(primary, helper)._ask_event('session', 'system', 'prompt'), 'available')
            self.assertEqual(len(primary.calls), 1)
            if not same_object:
                self.assertFalse(helper.calls)
