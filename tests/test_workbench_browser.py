"""Exercise compact workspaces and their controls in Chromium."""

import unittest

from tests import test_generation_browser as generation
from tests.test_generation_browser import expect, sync_playwright


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
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
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
