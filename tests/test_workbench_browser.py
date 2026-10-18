"""Exercise compact workspaces and their controls in Chromium."""

import asyncio
import unittest
from concurrent.futures import ThreadPoolExecutor

from tests import test_generation_browser as generation
from tests.test_generation_browser import expect, sync_playwright


async def console_snapshot():
    from tests.test_engine import EngineTestCase, SESSION

    fixture = EngineTestCase()
    await fixture.asyncSetUp()
    try:
        await fixture.engine.load_state(SESSION, cold_start=False)
        return await fixture.engine.snapshot(SESSION)
    finally:
        await fixture.db.close()
        fixture._tmp.cleanup()


@unittest.skipIf(sync_playwright is None, "Install playwright to run browser tests")
class WorkbenchBrowserTests(unittest.TestCase):
    setUpClass = classmethod(generation.GenerationBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(generation.GenerationBrowserTests.tearDownClass.__func__)
    setUp = generation.GenerationBrowserTests.setUp

    def capture_writes(self):
        self.page.evaluate("""() => {
          const original = AstrBotPluginPage.apiPost;
          window.policyWrites = [];
          AstrBotPluginPage.apiPost = (endpoint, data) => {
            if (endpoint === 'config/world') policyWrites.push(structuredClone(data.world));
            return original(endpoint, data);
          };
        }""")

    def test_event_policy_validates_weights_and_saves_the_new_switch(self):
        self.capture_writes()
        self.page.locator('button[data-tab="events"]').click()
        self.page.locator('#event-auto').click()
        expect(self.page.locator('#dialog-title')).to_have_text('自动生成事件')
        for key in ['daily', 'good', 'bad', 'annoying', 'strange']:
            self.page.locator(f'[data-dialog-key="{key}"]').fill('0')
        self.page.locator('#dialog-ok').click()
        expect(self.page.locator('#dialog')).to_be_visible()
        self.page.locator('[data-dialog-key="good"]').fill('10')
        self.page.locator('[data-dialog-key="enabled"]').uncheck()
        self.page.locator('[data-dialog-key="max_per_day"]').fill('4')
        self.page.locator('#dialog-ok').click()
        expect(self.page.locator('#dialog')).to_be_hidden()
        self.page.locator('#save').click()
        self.page.wait_for_function('policyWrites.length > 0')
        policy = self.page.evaluate('policyWrites.at(-1).events.auto')
        self.assertFalse(policy['enabled'])
        self.assertEqual(policy['max_per_day'], 4)
        self.assertEqual(policy['tone_weights']['good'], 10)
        self.assertEqual(self.errors, [])

    def test_companion_preset_retains_life_and_stops_duplicate_chat_summaries(self):
        self.capture_writes()
        self.page.locator('button[data-tab="memories"]').click()
        self.page.locator('#memory-policy').click()
        self.page.locator('[data-dialog-key="mode"]').select_option('companion')
        expect(self.page.locator('[data-dialog-key="automatic"]')).to_have_value('life_only')
        expect(self.page.locator('[data-dialog-key="summary"]')).not_to_be_checked()
        self.page.locator('[data-dialog-key="chars"]').fill('800')
        self.page.locator('#dialog-ok').click()
        self.page.locator('#save').click()
        self.page.wait_for_function('policyWrites.length > 0')
        memory = self.page.evaluate('policyWrites.at(-1).memory')
        self.assertEqual(memory['automatic_recall'], 'life_only')
        self.assertFalse(memory['dialogue_summary'])
        self.assertEqual(memory['recall_max_chars'], 800)

    def test_console_menus_and_action_search_leave_space_for_content(self):
        self.page.locator('button[data-tab="status"]').click()
        expect(self.page.locator('#state-tick')).to_be_hidden()
        self.page.locator('.console-toolbar .command-menu').first.locator('summary').click()
        expect(self.page.locator('#state-tick')).to_be_visible()
        expect(self.page.locator('#state-clear-context')).to_be_visible()
        self.page.locator('button[data-tab="actions"]').click()
        self.page.locator('#action-search').fill('想事情')
        expect(self.page.locator('.action-card')).to_have_count(1)
        self.page.locator('.action-card').focus()
        self.page.locator('.action-card').press('Enter')
        expect(self.page.locator('#action-drawer')).to_be_visible()
        self.page.locator('#action-cancel').click()
        self.host.set_viewport_size({'width': 390, 'height': 844})
        menu = self.page.locator('#action-menu')
        self.assertTrue(menu.evaluate('node => node.scrollWidth > node.clientWidth && node.clientHeight < 64'))
        menu.evaluate('node => node.scrollLeft = node.scrollWidth')
        self.assertTrue(menu.evaluate('node => node.lastElementChild.getBoundingClientRect().right <= node.getBoundingClientRect().right + 1'))
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        self.assertEqual(self.errors, [])

    def test_dual_storage_preset_keeps_chat_summaries_and_budget(self):
        self.capture_writes()
        self.page.locator('button[data-tab="memories"]').click()
        self.page.locator('#memory-policy').click()
        self.page.locator('[data-dialog-key="mode"]').select_option('dual')
        expect(self.page.locator('[data-dialog-key="summary"]')).to_be_checked()
        expect(self.page.locator('[data-dialog-key="automatic"]')).to_have_value('life_only')
        self.page.locator('[data-dialog-key="chars"]').fill('900')
        self.page.locator('#dialog-ok').click()
        self.page.locator('#save').click()
        self.page.wait_for_function('policyWrites.length > 0')
        memory = self.page.evaluate('policyWrites.at(-1).memory')
        self.assertTrue(memory['dialogue_summary'])
        self.assertEqual(memory['automatic_recall'], 'life_only')
        self.assertEqual(memory['recall_max_chars'], 900)

    def test_long_chat_summaries_scroll_inside_their_card(self):
        # Playwright's sync bridge owns an event loop on this thread.
        with ThreadPoolExecutor(max_workers=1) as executor:
            snapshot = executor.submit(asyncio.run, console_snapshot()).result()
        snapshot['chat_summaries'] = [{'session': f's{index}', 'label': f'合成会话 {index}',
                                      'text': '\n'.join(f'第 {line} 行：完整聊天摘要。' * 4 for line in range(45))}
                                     for index in range(3)]
        snapshot['chat_summaries'][-1]['text'] += '\n摘要末尾标记'
        self.page.evaluate("""data => {
          const original = AstrBotPluginPage.apiGet;
          AstrBotPluginPage.apiGet = (key, params) => key === 'state' ? Promise.resolve(data) : original(key, params);
          const select = document.querySelector('#status-session');
          select.add(new Option('合成测试', 's')); select.value='s'; select.dispatchEvent(new Event('change'));
        }""", snapshot)
        expect(self.page.locator('.chat-summary')).to_have_count(3)
        for width, height in [(1600, 900), (1280, 692), (390, 844)]:
            self.host.set_viewport_size({'width': width, 'height': height})
            box = self.page.locator('#status-context')
            self.assertTrue(box.evaluate('node => node.scrollHeight > node.clientHeight && node.clientHeight <= 240'))
            box.evaluate('node => node.scrollTop = node.scrollHeight')
            self.assertTrue(box.evaluate("""node => {
              const last=node.querySelector('.chat-summary:last-child .chat-line:last-child').getBoundingClientRect();
              return last.bottom <= node.getBoundingClientRect().bottom + 1;
            }"""))
            self.assertTrue(box.evaluate("node => node.getBoundingClientRect().bottom <= node.closest('.card').getBoundingClientRect().bottom"))
        self.assertEqual(self.errors, [])

    def test_map_commands_and_property_panel_do_not_cover_each_other(self):
        self.page.locator('button[data-tab="map"]').click()
        for width, height in [(1600, 900), (1280, 692), (1024, 640), (390, 844)]:
            self.host.set_viewport_size({'width': width, 'height': height})
            self.assertTrue(self.page.locator('.map-topbar').evaluate("""bar => {
              const controls=[...bar.querySelectorAll('button,select,summary')].filter(el=>el.getClientRects().length && !el.closest('.tool-menu-body'));
              return controls.every(el=>{const r=el.getBoundingClientRect();const b=bar.getBoundingClientRect();
                return r.left>=b.left && r.right<=b.right+1 && r.top>=b.top && r.bottom<=b.bottom+1;});
            }"""), f'toolbar bounds at {width}')
            for selector in ['#map-routes', '#zone-add', '#map-side-toggle', '#toolbar-world .tool-menu > summary']:
                self.page.locator(selector).click(trial=True)
            menu = self.page.locator('#toolbar-world .tool-menu')
            menu.locator('summary').click()
            self.page.locator('#map-relayout').click(trial=True)
            menu.locator('summary').click()
            if width > 1100:
                self.assertTrue(self.page.locator('#map-side').evaluate("node => node.getBoundingClientRect().top >= document.querySelector('.map-topbar').getBoundingClientRect().bottom"))
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        self.assertEqual(self.errors, [])

    def test_short_desktop_sidebar_retains_the_first_navigation_item(self):
        self.host.set_viewport_size({'width': 1600, 'height': 640})
        self.page.locator('button[data-tab="presets"]').click()
        self.page.locator('button[data-tab="actions"]').click()
        self.assertTrue(self.page.locator('.sidebar .tabs').evaluate('node => node.scrollHeight <= node.clientHeight + 1 && node.scrollTop === 0'))
        self.assertEqual(self.errors, [])

    def test_many_expanded_schedule_records_retain_full_intrinsic_height(self):
        logs = {'events': [{
            'id': index + 1, 'created_at': 1792230000 + index, 'world_time': index,
            'event_type': 'schedule', 'detail': {
                'run_id': f'run:{index}', 'id': f'plan_{index}', 'time': '20:30',
                'actions': '读书 → 散步', 'notes': ['执行完毕'],
                'steps': [{'event_type': 'action', 'world_time': step,
                           'text': ('完整的过程与结果，不应被列表压扁。' * 25) + f'末尾标记{step}'} for step in range(12)],
            },
        } for index in range(6)], 'types': ['schedule'], 'has_more': False}
        self.page.evaluate("""logs => {
          const original = AstrBotPluginPage.apiGet;
          AstrBotPluginPage.apiGet = (endpoint, params) => endpoint === 'logs' ? Promise.resolve(logs) : original(endpoint, params);
        }""", logs)
        self.page.locator('button[data-tab="schedules"]').click()
        self.page.locator('#schedule-session').evaluate("node => {node.add(new Option('测试', 's')); node.value='s'; node.dispatchEvent(new Event('change'));}")
        expect(self.page.locator('.schedule-run-row')).to_have_count(6)
        self.page.locator('.schedule-run-head').first.click()
        row = self.page.locator('.schedule-run-row').first
        self.assertTrue(row.evaluate('node => node.clientHeight >= node.scrollHeight - 2'))
        tail = row.locator('.schedule-run-step-text').last
        tail.scroll_into_view_if_needed()
        self.assertTrue(tail.evaluate("node => {const row=node.closest('.schedule-run-row').getBoundingClientRect(); const tail=node.getBoundingClientRect(); return tail.bottom <= row.bottom;}"))
        expect(tail).to_contain_text('末尾标记11')
        self.host.screenshot(path=str(self.shots / 'schedule-long-record.png'))
        self.assertEqual(self.errors, [])

    def test_route_inspector_sends_the_current_draft_and_explains_one_way_exits(self):
        self.page.evaluate("""() => {
          window.routeRequests = [];
          const original = AstrBotPluginPage.apiPost;
          AstrBotPluginPage.apiPost = (endpoint, data) => {
            if (endpoint !== 'map/inspect') return original(endpoint, data);
            routeRequests.push(structuredClone(data));
            return Promise.resolve({path:[data.start,data.target],ticks:3,tick_seconds:30,reachable:[data.start,data.target],unreachable:[],no_return:[data.target],warnings:[]});
          };
        }""")
        self.page.locator('button[data-tab="map"]').click()
        self.page.locator('#map-routes').click()
        self.page.locator('#dialog-ok').click()
        expect(self.page.locator('.map-route-result')).to_contain_text('无法沿地图返回')
        self.assertGreater(len(self.page.evaluate('routeRequests[0].map.nodes')), 1)
        self.assertEqual(self.errors, [])
