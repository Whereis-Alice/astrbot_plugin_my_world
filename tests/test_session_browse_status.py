"""Regression for stale reply expectations and real platform group names."""
import asyncio
import gc
import time
import types
import unittest
from unittest.mock import AsyncMock, patch

from core.engine.models import MessageContext
from tests import test_participation as participation
from tests import test_scope_api as scope


class BrowsePendingTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = participation.ParticipationTests.asyncSetUp
    chat = participation.ParticipationTests.chat
    burst = participation.ParticipationTests.burst

    async def test_router_stopped_event_releases_a_fresh_reply_expectation(self):
        class Event:
            stopped = False

            def is_stopped(self):
                return self.stopped

        event = Event()
        record = self.engine.register_incoming(participation.GROUP, '被路由拦下', expected_event=event)
        state = await self.engine.load_state(participation.GROUP, cold_start=False)
        self.assertEqual(self.engine.participation_gate(state, participation.GROUP), 'reply_pending')
        event.stopped = True
        self.assertEqual(self.engine.participation_gate(state, participation.GROUP), '')
        self.assertFalse(record['absorbed'])
        self.assertFalse(record['picked'])

    async def test_finished_host_event_is_not_kept_alive_by_merge_registration(self):
        class Event:
            def is_stopped(self):
                return False

        event = Event()
        self.engine.register_incoming(participation.GROUP, '宿主已释放的消息', expected_event=event)
        del event
        gc.collect()
        state = await self.engine.load_state(participation.GROUP, cold_start=False)
        self.assertEqual(self.engine.participation_gate(state, participation.GROUP), '')

    async def test_abandoned_reply_expectation_does_not_block_later_chat(self):
        record = self.engine.register_incoming(participation.GROUP, "曾被路由叫起的一条消息")
        record['at'] = time.monotonic() - 3600
        # Wall time is independent from dispatch/merge's monotonic clock.
        record['wall_at'] = self.clock.now() + 86400
        await self.burst()
        await self.engine.maybe_participate(participation.GROUP)
        self.assertEqual(self.messenger.sent, [(participation.GROUP, ['那个我也想试试。'])])
        # A delayed real host hook may still claim the original event.
        self.assertFalse(record['absorbed'])
        self.assertFalse(record['picked'])

    async def test_active_reply_preparation_outlives_dispatch_expectation(self):
        record = self.engine.register_incoming(participation.GROUP, "在吗", identity={'message_id': 'pending'})
        record['at'] = time.monotonic() - 3600
        entered, release = asyncio.Event(), asyncio.Event()

        async def wait(*args, **kwargs):
            entered.set()
            await release.wait()

        self.engine._wait_for_quiet = wait
        task = asyncio.create_task(self.engine.handle_reply(MessageContext(
            session_id=participation.GROUP, user_id='alice', user_name='alice',
            text='在吗', message_id='pending', is_mentioned=True), inject_only=True))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            state = await self.engine.load_state(participation.GROUP, cold_start=False)
            self.assertEqual(self.engine.participation_gate(state, participation.GROUP), 'reply_pending')
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(self.engine.participation_gate(state, participation.GROUP), '')
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


class SessionNamesTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = scope.ScopeApiTests.asyncSetUp
    call = scope.ScopeApiTests.call

    def platform(self, names, platform_id='test'):
        bot = types.SimpleNamespace(call_action=AsyncMock(return_value=names))
        adapter = types.SimpleNamespace(get_client=lambda: bot)
        self.plugin.context.get_platform_inst = lambda key: adapter if key == platform_id else None
        return bot

    async def test_group_without_metadata_never_uses_a_members_name(self):
        state = await self.plugin.engine.load_state(self.session, cold_start=False)
        state.session_activity[self.session] = {'user_name': '某个群友', 'at': time.time()}
        self.assertEqual(self.plugin.engine.session_label(self.session, state), '群 100')

    async def test_config_reads_real_group_names_with_one_cached_platform_call(self):
        before = self.plugin.store.raw_sessions()
        bot = self.platform([{'group_id': 100, 'group_name': '  真正的群名称  '},
                             {'group_id': 999, 'group_name': '未配置的群'}])
        # One shared immutable GET request: overlapping patch contexts would
        # restore each other's temporary proxy and contaminate later API tests.
        request = types.SimpleNamespace(method='GET', query={}, json=AsyncMock(return_value={}))
        with patch.object(self.module.web.api, 'request', request):
            results = await asyncio.gather(self.routes['config'](), self.routes['config']())
        for result in results:
            self.assertEqual(result['data']['session_names'], {self.session: '真正的群名称'})
        bot.call_action.assert_awaited_once_with('get_group_list', no_cache=True)
        self.assertEqual(self.plugin.store.raw_sessions(), before)

    async def test_metadata_read_failure_is_cached_and_preserves_event_group_name(self):
        self.plugin.engine.note_session_name(self.session, '事件里的真实群名')
        bot = self.platform([])
        bot.call_action.side_effect = TimeoutError('offline')
        for _ in range(2):
            result = await self.call('config')
            self.assertEqual(result['data']['session_names'], {self.session: '事件里的真实群名'})
        self.assertEqual(bot.call_action.await_count, 1)

    async def test_expired_metadata_cache_refreshes_a_renamed_group(self):
        bot = self.platform([{'group_id':100, 'group_name':'旧群名'}])
        self.plugin.messenger.GROUP_NAMES_TTL = 0
        self.assertEqual((await self.call('config'))['data']['session_names'][self.session], '旧群名')
        bot.call_action.return_value = [{'group_id':100, 'group_name':'新群名'}]
        self.assertEqual((await self.call('config'))['data']['session_names'][self.session], '新群名')
        self.assertEqual(bot.call_action.await_count, 2)

    async def test_replaced_client_cannot_reuse_a_previous_bots_names(self):
        first = self.platform([{'group_id':100, 'group_name':'第一个机器人所在群'}])
        await self.call('config')
        second = self.platform([{'group_id':100, 'group_name':'第二个机器人所在群'}])
        self.assertEqual((await self.call('config'))['data']['session_names'][self.session], '第二个机器人所在群')
        first.call_action.assert_awaited_once()
        second.call_action.assert_awaited_once()

    async def test_private_and_malformed_sessions_are_not_sent_to_group_api(self):
        bot = self.platform([{'group_id':100, 'group_name':'真实群名'}])
        names = await self.plugin.messenger.fetch_group_names(['test:PrivateMessage:100', 'broken'])
        self.assertEqual(names, {})
        bot.call_action.assert_not_awaited()

    async def test_other_platforms_event_is_never_borrowed_for_missing_group(self):
        wrong_bot = types.SimpleNamespace(call_action=AsyncMock(return_value=[{'group_id':100,'group_name':'错误机器人'}]))
        self.plugin._last_events['other:GroupMessage:100'] = types.SimpleNamespace(bot=wrong_bot)
        result = await self.call('config')
        self.assertEqual(result['data']['session_names'], {})
        wrong_bot.call_action.assert_not_awaited()

    async def test_thought_payload_uses_live_group_metadata_without_editing_archive(self):
        bot = self.platform([{'group_id':100, 'group_name':'新群名称'}])
        async with self.plugin.engine.session_state(self.session) as state:
            state.thoughts.append({'at': self.plugin.engine._now(), 'content':'读完有个想法',
                                   'expires_at': self.plugin.engine._now() + 86400,
                                   'browse_session': self.session, 'browse_id':'read-1'})
        data = await self.plugin.engine.thoughts_payload(self.session)
        self.assertEqual(data['thoughts'][0]['browse_label'], '群 100「新群名称」')
        bot.call_action.assert_awaited_once()

