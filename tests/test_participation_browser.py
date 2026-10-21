"""The participation controls must be readable and editable in the existing theme."""
import unittest

from tests import test_generation_browser as generation
from tests.test_generation_browser import expect, sync_playwright


@unittest.skipIf(sync_playwright is None, "Install playwright for visual acceptance")
class ParticipationBrowserTests(unittest.TestCase):
    setUpClass = classmethod(generation.GenerationBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(generation.GenerationBrowserTests.tearDownClass.__func__)
    setUp = generation.GenerationBrowserTests.setUp

    def test_conversation_settings_explain_listening_and_unanswered_contact(self):
        self.page.locator('button[data-tab="settings"]').click()
        self.page.locator('#settings-tabs').get_by_text('自主发言', exact=True).click()
        expect(self.page.get_by_text('可以主动说话的地方', exact=True)).to_be_visible()
        expect(self.page.get_by_text('新消息到来后自然接话', exact=True)).to_be_visible()
        expect(self.page.get_by_text('参与程度', exact=True)).to_be_visible()
        self.assertEqual(self.page.get_by_text('插话最短间隔（分钟）', exact=True).count(), 0)
        self.assertEqual(self.page.get_by_text('至少几条消息才插话', exact=True).count(), 0)
        self.page.locator('#theme-toggle').click()
        self.page.locator('[data-appearance="nocturne"]').click()
        self.page.get_by_label('关闭外观设置').click()
        self.page.evaluate('window.scrollTo(0, 0)')
        self.host.screenshot(path=str(self.shots / 'participation-overview-dark.png'), animations='disabled')
        self.host.set_viewport_size({'width': 390, 'height': 844})
        self.page.get_by_text('参与程度', exact=True).scroll_into_view_if_needed()
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        self.host.screenshot(path=str(self.shots / 'participation-overview-mobile.png'), animations='disabled')
        self.host.set_viewport_size({'width': 1440, 'height': 1000})
        self.page.get_by_text('主动找人的保护', exact=True).click()
        expect(self.page.get_by_text('未回应联系人等待（小时）', exact=True)).to_be_visible()
        self.page.get_by_text('参与程度', exact=True).scroll_into_view_if_needed()
        self.host.screenshot(path=str(self.shots / 'participation-settings.png'))
        self.host.set_viewport_size({'width': 390, 'height': 844})
        self.page.get_by_text('未回应联系人等待（小时）', exact=True).scroll_into_view_if_needed()
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        self.host.screenshot(path=str(self.shots / 'participation-settings-mobile.png'))
        self.assertEqual(self.errors, [])
