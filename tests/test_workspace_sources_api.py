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

    async def test_draft_generation_returns_its_own_cache_even_when_current_has_a_brief(self):
        engine = self.plugin.engine
        engine._persona_text = AsyncMock(return_value='已保存的主人设')
        engine._ask_creator = AsyncMock(return_value='新草稿的摘要')
        await engine.save_persona_brief(self.session, '旧主人设的摘要')
        response = await self.call('persona-brief', method='POST', session=self.session,
                                   generate=True, persona='未保存的新主人设', persona_mode='plugin')
        self.assertEqual(response['status_code'], 200)
        state = response['data']['persona_brief']
        self.assertEqual(state['brief'], '新草稿的摘要')
        self.assertEqual(state['key'], engine._persona_brief_key('未保存的新主人设'))
        self.assertFalse(response['data']['matches_current_persona'])
        current = await engine.persona_brief_state(self.session)
        self.assertEqual(current['brief'], '旧主人设的摘要')

    async def test_manual_draft_summary_is_cached_for_the_same_draft(self):
        engine = self.plugin.engine
        engine._persona_text = AsyncMock(return_value='已保存的主人设')
        response = await self.call('persona-brief', method='POST', session=self.session,
                                   brief='手动修改的草稿摘要', persona='未保存的新主人设', persona_mode='plugin')
        self.assertEqual(response['status_code'], 200)
        key = engine._persona_brief_key('未保存的新主人设')
        saved = await self.plugin.db.call('kv_get', key)
        self.assertEqual((saved or {}).get('brief'), '手动修改的草稿摘要')
        self.assertEqual(response['data']['persona_brief']['key'], key)

    async def test_draft_modes_use_the_effective_persona_instead_of_always_own_text(self):
        engine = self.plugin.engine
        engine._persona_text = AsyncMock(return_value='当前正式人设')
        engine._ask_creator = AsyncMock(return_value='按实际人设生成的摘要')
        self.plugin.persona_port = types.SimpleNamespace(astrbot_persona_text=AsyncMock(return_value='AstrBot 主人设'))
        for mode, own, expected in (('astrbot', '未启用的角色卡', 'AstrBot 主人设'),
                                     ('plugin', '', 'AstrBot 主人设'),
                                     ('append', '追加角色卡', 'AstrBot 主人设\n\n追加角色卡')):
            with self.subTest(mode=mode, own=own):
                response = await self.call('persona-brief', method='POST', session=self.session,
                                           generate=True, persona=own, persona_mode=mode)
                self.assertEqual(response['status_code'], 200)
                self.assertEqual(response['data']['persona_brief']['key'], engine._persona_brief_key(expected))

    async def test_generation_keeps_its_captured_persona_when_live_persona_changes(self):
        engine = self.plugin.engine
        engine._persona_text = AsyncMock(return_value='开始时的人设')

        async def generate(*args, **kwargs):
            engine._persona_text.return_value = '生成期间切换的人设'
            return '开始时的人设摘要'

        engine._ask_creator = AsyncMock(side_effect=generate)
        response = await self.call('persona-brief', method='POST', session=self.session, generate=True)
        self.assertEqual(response['status_code'], 200)
        self.assertEqual(response['data']['persona_brief']['brief'], '开始时的人设摘要')
        self.assertEqual(response['data']['persona_brief']['key'], engine._persona_brief_key('开始时的人设'))
        self.assertFalse(response['data']['matches_current_persona'])
        self.assertEqual((await engine.persona_brief_state(self.session))['brief'], '')

    async def test_empty_effective_persona_does_not_generate_from_the_old_persona(self):
        engine = self.plugin.engine
        engine._persona_text = AsyncMock(return_value='已经不再使用的正式人设')
        engine._ask_creator = AsyncMock(return_value='不该生成')
        self.plugin.persona_port = types.SimpleNamespace(astrbot_persona_text=AsyncMock(return_value=''))
        response = await self.call('persona-brief', method='POST', session=self.session,
                                   generate=True, persona='', persona_mode='plugin')
        self.assertNotEqual(response['status_code'], 200)
        engine._ask_creator.assert_not_awaited()

    async def test_summary_becomes_active_when_the_matching_draft_is_saved(self):
        engine = self.plugin.engine
        engine._persona_text = AsyncMock(return_value='旧人设')
        await self.call('persona-brief', method='POST', session=self.session,
                        brief='草稿摘要', persona='草稿人设', persona_mode='plugin')
        self.assertNotEqual(await engine.event_persona_brief(self.session), '草稿摘要')
        engine._persona_text.return_value = '草稿人设'
        self.assertEqual(await engine.event_persona_brief(self.session), '草稿摘要')
