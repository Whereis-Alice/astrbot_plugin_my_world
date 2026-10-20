"""The studio scene follows real state, stays readable, and never leaks old state."""
import unittest

from tests import test_workbench_browser as workbench
from tests.test_generation_browser import expect, sync_playwright


@unittest.skipIf(sync_playwright is None, "Install playwright for studio verification")
class StudioBrowserTests(unittest.TestCase):
    setUpClass = classmethod(workbench.WorkbenchBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(workbench.WorkbenchBrowserTests.tearDownClass.__func__)
    setUp = workbench.WorkbenchBrowserTests.setUp
    install_console = workbench.WorkbenchBrowserTests.install_console

    def test_scene_tracks_world_time_and_settings_without_inventing_health(self):
        self.install_console()
        focus = self.page.locator('#life-focus')
        for hour, period, label in [(2, 'night', '夜深'), (7, 'morning', '清晨'),
                                     (14, 'day', '午后'), (19, 'evening', '傍晚')]:
            clock = f'现在是：2026-10-20（周二）{hour:02d}:15'
            self.page.evaluate('clock => {numericState.clock_text = clock;}', clock)
            self.page.locator('#status-refresh').click()
            expect(focus).to_have_attribute('data-period', period)
            expect(focus.locator('.life-focus-clock')).to_have_text(clock.replace('现在是：', ''))
            expect(focus.locator('.people-eyebrow').first).to_contain_text(label)
            self.assertEqual(focus.locator('.life-scene[aria-hidden="true"]').count(), 1)
            self.assertNotIn('运行正常', focus.inner_text())
        self.assertEqual(self.errors, [])

    def test_long_text_is_safe_readable_and_old_focus_disappears_without_a_session(self):
        self.install_console()
        title = '<img src=x onerror=alert(1)> 读完一个很长的故事，留下自己的感受。' * 4
        self.page.evaluate('title => {numericState.life_matters = [{text:title, status:"working", motivation:"慢慢读，再把心里的想法记下来。".repeat(20)}];}', title)
        for width, height in [(1440, 900), (1024, 768), (390, 844), (360, 780)]:
            self.host.set_viewport_size({'width': width, 'height': height})
            self.page.locator('#status-refresh').click()
            focus = self.page.locator('#life-focus')
            expect(focus.locator('h3')).to_have_text(title)
            self.assertEqual(focus.locator('img').count(), 0)
            self.assertTrue(focus.evaluate('e => e.scrollWidth <= e.clientWidth + 1'))
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'))
            heading = focus.locator('h3').bounding_box()
            button = focus.locator('button').bounding_box()
            self.assertGreaterEqual(heading['y'], button['y'] + button['height'])
        self.page.evaluate('''() => {
          const select = document.getElementById('status-session');
          select.add(new Option('无会话', '')); select.value = '';
          select.dispatchEvent(new Event('change'));
        }''')
        expect(self.page.locator('#life-focus')).not_to_be_visible()
        expect(self.page.locator('#status-empty')).to_be_visible()
        self.assertEqual(self.errors, [])
