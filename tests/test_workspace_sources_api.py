"""Persona controls resolve the selected conversation and shared-life representative."""
from tests import test_scope_api as scope
import types
import unittest
from unittest.mock import AsyncMock


class WorkspaceSourcesApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = scope.ScopeApiTests.asyncSetUp
    call = scope.ScopeApiTests.call

    async def add_group(self):
        second = 'test:GroupMessage:200'
        self.plugin.store.add_session(second)
        self.plugin.store.save_sessions({'sessions': self.plugin.store.raw_sessions()['sessions'],
                                         'groups': [{'id': 'shared', 'name': '共享生活',
                                                     'sessions': [self.session, second], 'main_session': second}]})
        self.plugin.engine.reload_config()
        return second

    async def test_import_uses_selected_conversation_independently_of_edit_scope(self):
        second = await self.add_group()
        read = AsyncMock(side_effect=lambda session: f'{session}的人设')
        self.plugin.persona_port = types.SimpleNamespace(astrbot_persona_text=read)
        for selected in (second, 'shared'):
            with self.subTest(selected=selected):
                response = await self.call('persona-source', session=selected, config_scope=self.session)
                self.assertEqual(response['status_code'], 200)
                self.assertEqual(response['data']['session'], second)
                self.assertEqual(response['data']['text'], f'{second}的人设')

    async def test_brief_shared_life_picker_reads_the_representatives_persona(self):
        second = await self.add_group()
        read = AsyncMock(side_effect=lambda session: f'{session}的人设')
        self.plugin.engine._persona_text = read
        await self.plugin.engine.save_persona_brief(second, '代表会话的摘要')
        read.reset_mock()
        response = await self.call('persona-brief', session='shared', config_scope=self.session)
        self.assertEqual(response['status_code'], 200)
        self.assertEqual(response['data']['persona_brief']['brief'], '代表会话的摘要')
        read.assert_awaited_once_with(second)

    async def test_brief_shared_life_picker_saves_for_the_representative(self):
        second = await self.add_group()
        self.plugin.engine._persona_text = AsyncMock(side_effect=lambda session: f'{session}的人设')
        response = await self.call('persona-brief', method='POST', session='shared', brief='按当前角色保存')
        self.assertEqual(response['status_code'], 200)
        state = await self.plugin.engine.persona_brief_state(second)
        self.assertEqual(state['brief'], '按当前角色保存')
