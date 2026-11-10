"""The phone cannot select a friend's unrelated group from its global catalog."""
import unittest

from tests import test_visits_browser as fixture
from tests.test_generation_browser import expect, sync_playwright


@unittest.skipIf(sync_playwright is None, "Install playwright for browser verification")
class VisitGroupRoutingBrowserTests(unittest.TestCase):
    setUpClass = classmethod(fixture.VisitsBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.VisitsBrowserTests.tearDownClass.__func__)
    setUp = fixture.VisitsBrowserTests.setUp

    def test_only_matching_group_can_be_called_even_when_another_role_is_first(self):
        self.page.evaluate("""() => {
          visitData.scope_routing=true;
          visitData.connections[0].actors.unshift({id:'wrong-group',name:'同名角色',world:'另一群'});
        }""")
        refresh = self.page.locator('#tab-visits').get_by_role('button', name='刷新', exact=True)
        refresh.click()
        self.page.get_by_role('button', name='打电话', exact=True).click()
        expect(self.page.get_by_label('去见哪位朋友', exact=True)).to_have_count(0)
        self.page.get_by_role('button', name='去做客', exact=True).click()
        self.page.wait_for_function('visitWrites.length===1')
        self.assertEqual(self.page.evaluate('visitWrites[0].payload.peer_id'), 'friend')
        self.assertEqual(self.page.evaluate('visitWrites[0].payload.session'), 'test:GroupMessage:100')
        self.page.evaluate("""() => {visitData.peers=[];}""")
        refresh.click()
        expect(self.page.get_by_role('button', name='去做客', exact=True)).to_be_disabled()
        expect(self.page.get_by_role('button', name='请她来', exact=True)).to_be_disabled()
        expect(self.page.locator('.visits-phone')).to_contain_text('尚未确认对方在当前群聊中的生活')
        self.assertEqual(len(self.page.evaluate('visitWrites')), 1)
        self.assertEqual(self.errors, [])
