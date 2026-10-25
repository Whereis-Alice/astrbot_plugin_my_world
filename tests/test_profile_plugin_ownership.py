"""Profile injection and cleanup must respect the external plugin's ownership."""

import asyncio
import importlib
import types
import unittest
from unittest.mock import AsyncMock

from tests import test_scope_api as scope_api
from tests.test_dialogue_inject import Event, Request
from tests.test_web_routes import load_plugin_module


EXTERNAL = 'astrbot_plugin_profile_weaver'


def external_star():
    return types.SimpleNamespace(name=EXTERNAL, root_dir_name=EXTERNAL,
                                 activated=True, reserved=False, star_cls=object())


def request_text(request):
    return request.system_prompt + '\n' + '\n'.join(
        str(getattr(part, 'text', '')) for part in request.extra_user_content_parts)


class ProfileOwnerHooksTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = scope_api.ScopeApiTests.asyncSetUp

    async def prepare(self, allowed=None):
        self.plugin.context.get_all_stars = lambda: [external_star()]
        event = Event('晚上好', session=self.session)
        event.plugins_name = allowed
        event.message_str = '我喜欢修复旧风车'
        actor = self.plugin.profile_weaver_service.actor(event, 'user_command')
        self.plugin.profile_weaver_service.store.upsert_field(
            user_id='42', subject_name='tester', session_id=self.session,
            field_name='爱好', value='修复旧风车', actor=actor,
            source_kind='manual_edit', evidence='我喜欢修复旧风车', allow_custom_field=True)
        self.plugin.engine.profiles.touch(self.session, '42', 'tester')
        return event

    async def test_config_excluded_external_plugin_does_not_hide_our_profile(self):
        event = await self.prepare(['astrbot_plugin_my_world'])
        request = Request('晚上好')
        await self.plugin.on_llm_request(event, request)
        self.assertIn('修复旧风车', request_text(request))
        self.assertFalse(event.is_stopped())

    async def test_session_config_fallback_does_not_hide_our_profile(self):
        event = await self.prepare()
        self.plugin.context.get_config = lambda session: {'plugin_set': ['astrbot_plugin_my_world']}
        request = Request('晚上好')
        await self.plugin.on_llm_request(event, request)
        self.assertIn('修复旧风车', request_text(request))
        self.assertFalse(event.is_stopped())

    async def test_active_external_injection_is_preserved_without_a_second_copy(self):
        event = await self.prepare()
        request = Request('晚上好', '<ProfileWeaver>爱好：修复旧风车</ProfileWeaver>')
        await self.plugin.on_llm_request(event, request)
        self.assertEqual(request_text(request).count('修复旧风车'), 1)

    async def test_inactive_external_plugin_leaves_our_profile_in_the_request(self):
        event = await self.prepare()
        star = external_star()
        star.activated = False
        self.plugin.context.get_all_stars = lambda: [star]
        request = Request('晚上好')
        await self.plugin.on_llm_request(event, request)
        self.assertIn('修复旧风车', request_text(request))

    async def test_external_profile_extraction_is_not_repeated_by_my_world(self):
        event = await self.prepare()
        self.plugin.profile_weaver_service.maybe_extract = AsyncMock()
        reply = types.SimpleNamespace(role='assistant', completion_text='那一定很有成就感',
                                      reasoning_content='', tools_call_name=[])
        await self.plugin.on_llm_response(event, reply)
        self.plugin.profile_weaver_service.maybe_extract.assert_not_awaited()

    async def test_own_extraction_remains_available_where_external_plugin_is_excluded(self):
        event = await self.prepare(['astrbot_plugin_my_world'])
        self.plugin.profile_weaver_service.maybe_extract = AsyncMock()
        reply = types.SimpleNamespace(role='assistant', completion_text='那一定很有成就感',
                                      reasoning_content='', tools_call_name=[])
        await self.plugin.on_llm_response(event, reply)
        await asyncio.gather(*self.plugin.profile_weaver_service.extraction_queue.tasks)
        self.plugin.profile_weaver_service.maybe_extract.assert_awaited_once()


class ProfileToolOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module(load_plugin_module().__name__ + '.adapters.profile_weaver_tools')
        self.tools = []
        self.removed = []

        def add(*items):
            for item in items:
                self.tools[:] = [old for old in self.tools if old.name != item.name]
                self.tools.append(item)

        def remove(name):
            self.removed.append(name)
            for i, item in enumerate(self.tools):
                if item.name == name:
                    self.tools.pop(i)
                    break

        manager = types.SimpleNamespace(func_list=self.tools)
        self.plugin = types.SimpleNamespace(
            profile_weaver_service=types.SimpleNamespace(llm_tools_enabled=True),
            context=types.SimpleNamespace(add_llm_tools=add,
                                          unregister_llm_tool=remove,
                                          get_llm_tool_manager=lambda: manager),
            logger=types.SimpleNamespace(warning=lambda *_args: None))

    def test_unload_keeps_tools_replaced_by_the_external_plugin(self):
        self.module.register_profile_tools(self.plugin, legacy_active=False, toolset_type=None)
        external = []
        for name in ('profileweaver_view_profile', 'profileweaver_remember_field',
                     'profileweaver_forget_field'):
            tool = types.SimpleNamespace(name=name, owner=EXTERNAL)
            self.plugin.context.add_llm_tools(tool)
            external.append(tool)
        self.module.unregister_profile_tools(self.plugin)
        self.assertEqual(self.removed, ['mw_profile_lookup'])
        self.assertEqual(len(self.tools), 3)
        for tool in external:
            self.assertTrue(any(current is tool for current in self.tools))

    def test_normal_unload_removes_exactly_our_own_tools(self):
        self.module.register_profile_tools(self.plugin, legacy_active=False, toolset_type=None)
        names = {tool.name for tool in self.tools}
        self.module.unregister_profile_tools(self.plugin)
        self.assertEqual(set(self.removed), names)
        self.assertFalse(self.tools)

    def test_cleanup_does_not_remove_an_unknown_tool_when_registry_cannot_be_read(self):
        self.module.register_profile_tools(self.plugin, legacy_active=False, toolset_type=None)
        self.plugin.context.get_llm_tool_manager = lambda: None
        self.module.unregister_profile_tools(self.plugin)
        self.assertFalse(self.removed)


class IntegratedExtractionLatencyTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = scope_api.ScopeApiTests.asyncSetUp

    async def test_slow_integrated_extraction_does_not_hold_the_native_reply(self):
        event = Event('我喜欢修复旧风车', session=self.session)
        event.message_str = '我喜欢修复旧风车'
        turn = {'session_id': self.session}
        event.set_extra(self.module.MW_TURN_EXTRA, turn)
        entered, release = asyncio.Event(), asyncio.Event()

        async def extract(snapshot, _response):
            entered.set()
            await release.wait()

        self.plugin.profile_weaver_service.maybe_extract = AsyncMock(side_effect=extract)
        reply = types.SimpleNamespace(role='assistant', completion_text='那一定很有成就感',
                                      reasoning_content='', tools_call_name=[])
        hook = asyncio.create_task(self.plugin.on_llm_response(event, reply))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            self.assertTrue(hook.done(), '画像提取仍占用最终响应钩子，主回复不能进入发送阶段')
            self.assertEqual(turn['reply'], '那一定很有成就感')
            self.assertFalse(getattr(event, '_profileweaver_auto_extract_running', False))
        finally:
            release.set()
            await hook
            queue = getattr(self.plugin.profile_weaver_service, 'extraction_queue', None)
            if queue is not None:
                await queue.close()
