"""Background extraction keeps message identity, ordering and a finite lifetime."""

import asyncio
import types
import unittest
from unittest.mock import AsyncMock, Mock

from core.profile_extraction import ProfileExtractionQueue


class Event:
    def __init__(self, text='我喜欢修复旧风车', user='42', session='test:GroupMessage:1'):
        self.message_str = text
        self.unified_msg_origin = session
        self.message_obj = types.SimpleNamespace(sender=types.SimpleNamespace(user_id=user, nickname='阿岚'))
        self._extras = {'sent': False}

    def get_sender_id(self):
        return self.message_obj.sender.user_id

    def get_sender_name(self):
        return self.message_obj.sender.nickname


class ExtractionQueueTests(unittest.IsolatedAsyncioTestCase):
    def queue(self, *, timeout=60, max_pending=16):
        service = types.SimpleNamespace(
            proactive_extraction_enabled=True, session_based=True,
            event_session=lambda event: event.unified_msg_origin,
            is_profile_candidate_message=lambda _event, _text: True,
            maybe_extract=AsyncMock(), debug=Mock())
        queue = ProfileExtractionQueue(service, timeout=timeout, max_pending=max_pending)
        self.addAsyncCleanup(queue.close)
        return queue, service

    async def test_original_message_sender_and_metadata_are_detached(self):
        queue, service = self.queue()
        event = Event()
        response = types.SimpleNamespace(tools_call_name=[])
        self.assertTrue(queue.schedule(event, response))
        event.message_str = '后来的一句话'
        event.message_obj.sender.user_id = '99'
        event.message_obj.sender.nickname = '另一个人'
        event._extras['sent'] = True
        response.tools_call_name.append('later_tool')
        await asyncio.gather(*queue.tasks)
        snapshot, observed = service.maybe_extract.await_args.args
        self.assertIsNot(snapshot, event)
        self.assertEqual(snapshot.message_str, '我喜欢修复旧风车')
        self.assertEqual((snapshot.get_sender_id(), snapshot.get_sender_name()), ('42', '阿岚'))
        self.assertFalse(snapshot._extras['sent'])
        self.assertFalse(observed.tools_call_name)
        self.assertFalse(queue.tasks)
        self.assertFalse(queue._locks)

    async def test_duplicate_final_response_does_not_extract_twice(self):
        queue, service = self.queue()
        event = Event()
        self.assertTrue(queue.schedule(event, types.SimpleNamespace()))
        self.assertFalse(queue.schedule(event, types.SimpleNamespace()))
        await asyncio.gather(*queue.tasks)
        service.maybe_extract.assert_awaited_once()

    async def test_same_profile_is_processed_in_arrival_order(self):
        queue, service = self.queue()
        entered, release = asyncio.Event(), asyncio.Event()
        order = []

        async def extract(event, _response):
            order.append(event.message_str)
            if event.message_str == 'first':
                entered.set()
                await release.wait()

        service.maybe_extract.side_effect = extract
        queue.schedule(Event('first'), types.SimpleNamespace())
        await entered.wait()
        queue.schedule(Event('second'), types.SimpleNamespace())
        queue.schedule(Event('third'), types.SimpleNamespace())
        await asyncio.sleep(0)
        self.assertEqual(order, ['first'])
        release.set()
        await asyncio.gather(*queue.tasks)
        self.assertEqual(order, ['first', 'second', 'third'])

    async def test_global_concurrency_and_backlog_are_bounded(self):
        queue, service = self.queue(max_pending=3)
        release, two_running = asyncio.Event(), asyncio.Event()
        active, highest = 0, 0

        async def extract(_event, _response):
            nonlocal active, highest
            active += 1
            highest = max(highest, active)
            if active == 2:
                two_running.set()
            try:
                await release.wait()
            finally:
                active -= 1

        service.maybe_extract.side_effect = extract
        for user in ('42', '43', '44'):
            self.assertTrue(queue.schedule(Event(user=user), types.SimpleNamespace()))
        self.assertFalse(queue.schedule(Event(user='45'), types.SimpleNamespace()))
        await asyncio.wait_for(two_running.wait(), 1)
        self.assertEqual(highest, 2)
        release.set()
        await asyncio.gather(*queue.tasks)
        self.assertEqual(service.maybe_extract.await_count, 3)

    async def test_whole_job_timeout_releases_workers_and_keeps_original_event_clean(self):
        queue, service = self.queue(timeout=.03)
        stopped = asyncio.Event()

        async def extract(snapshot, _response):
            snapshot._profileweaver_auto_extract_running = True
            try:
                await asyncio.Event().wait()
            finally:
                snapshot._profileweaver_auto_extract_running = False
                stopped.set()

        service.maybe_extract.side_effect = extract
        original = Event()
        queue.schedule(original, types.SimpleNamespace())
        await asyncio.wait_for(asyncio.gather(*queue.tasks), 1)
        self.assertTrue(stopped.is_set())
        self.assertFalse(getattr(original, '_profileweaver_auto_extract_running', False))
        self.assertFalse(queue.tasks)
        self.assertFalse(queue._users)
        service.debug.assert_called_once()

    async def test_unload_cancels_work_and_rejects_new_requests(self):
        queue, service = self.queue()
        entered, stopped = asyncio.Event(), asyncio.Event()

        async def extract(_event, _response):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        service.maybe_extract.side_effect = extract
        queue.schedule(Event(), types.SimpleNamespace())
        await entered.wait()
        await queue.close()
        self.assertTrue(stopped.is_set())
        self.assertFalse(queue.tasks)
        self.assertFalse(queue.schedule(Event(), types.SimpleNamespace()))
        self.assertFalse(queue._users)

    async def test_disabled_or_already_handled_messages_do_not_start_work(self):
        queue, service = self.queue()
        event = Event()
        event._profileweaver_llm_tool_used = True
        self.assertFalse(queue.schedule(event, types.SimpleNamespace()))
        event._profileweaver_llm_tool_used = False
        service.proactive_extraction_enabled = False
        self.assertFalse(queue.schedule(event, types.SimpleNamespace()))
        service.maybe_extract.assert_not_awaited()
