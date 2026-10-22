"""Registered record endpoints honour page sessions independently of config scope."""
from tests import test_scope_api as scope
import unittest
import types
from unittest.mock import AsyncMock, patch

from werkzeug.datastructures import MultiDict


class RecordSessionsApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = scope.ScopeApiTests.asyncSetUp
    async def call(self, endpoint, **data):
        request = types.SimpleNamespace(method='GET', query=MultiDict(data), json=AsyncMock(return_value=data))
        with patch.object(self.module.web.api, 'request', request):
            return await self.routes[endpoint]()

    async def test_memory_list_and_stats_use_the_selected_session(self):
        other = 'test:GroupMessage:200'
        self.plugin.store.add_session(other)
        self.plugin.engine.reload_config()
        for session, count in ((self.session, 1), (other, 2)):
            for index in range(count):
                self.plugin.engine.memory.remember(
                    session_id=session, persona_id='', node_id='study',
                    content=f'{session} 的经历 {index}',
                )
        for session, count, config_scope in ((self.session, 1, other), (other, 2, self.session)):
            with self.subTest(session=session):
                result = await self.call('memories', session=session, config_scope=config_scope)
                self.assertEqual(result['status_code'], 200, result)
                rows = result['data']['memories']
                self.assertEqual(len(rows), count)
                self.assertTrue(all(row['session_id'] == session for row in rows))
                result = await self.call('memories/stats', session=session, config_scope=config_scope)
                self.assertEqual(result['data']['total'], count)

    async def test_log_list_and_consolidation_use_the_selected_session(self):
        other = 'test:GroupMessage:200'
        self.plugin.store.add_session(other)
        self.plugin.engine.reload_config()
        expected = {}
        for session in (self.session, other):
            expected[session] = {}
            for kind in ('action', 'consolidate'):
                expected[session][kind] = await self.plugin.db.call(
                    'add_event', session_id=session, world_time=10, event_type=kind,
                    detail={'text': f'{session} 的 {kind}'},
                )
        for session, config_scope in ((self.session, other), (other, self.session)):
            with self.subTest(session=session):
                result = await self.call('logs', session=session, config_scope=config_scope)
                self.assertEqual(result['status_code'], 200, result)
                self.assertEqual({row['id'] for row in result['data']['events']}, set(expected[session].values()))
                result = await self.call('logs', session=session, type='consolidate', config_scope=config_scope)
                self.assertEqual([row['id'] for row in result['data']['events']], [expected[session]['consolidate']])
