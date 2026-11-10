"""Two-digit journal dates stay intact with the actual desktop and phone styles."""
from __future__ import annotations

import unittest

from tests import test_visits_browser as fixture
from tests.test_generation_browser import expect, sync_playwright


@unittest.skipIf(sync_playwright is None, "Install playwright for browser verification")
class VisitHistoryDateBrowserTests(unittest.TestCase):
    setUpClass = classmethod(fixture.VisitsBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.VisitsBrowserTests.tearDownClass.__func__)

    def setUp(self):
        fixture.VisitsBrowserTests.setUp(self)
        self.page.evaluate("""() => {
          visitData.history=[8,10,28].map(day=>({id:`day-${day}`,status:'completed',
            arrived_at:new Date(2026,10,day,18,8).getTime()/1000,
            ended_at:new Date(2026,10,day,19,21).getTime()/1000,
            remote:{id:'friend',name:'爱丽丝'},scene:{place:'树根藏书室'},
            log:[{actor_id:'friend',kind:'say',text:'来得正好，一起喝杯茶吧。'}]}));
        }""")
        self.page.locator('#tab-visits').get_by_role('button', name='刷新', exact=True).click()
        expect(self.page.locator('.visits-history .visits-entry-date b')).to_have_count(3)

    def test_two_digit_days_never_wrap_or_overlap_the_record_copy(self):
        for width, height in ((1600, 900), (1440, 1000), (390, 844), (320, 740)):
            self.host.set_viewport_size({'width': width, 'height': height})
            self.page.locator('.visits-history').scroll_into_view_if_needed()
            metrics = self.page.locator('.visits-entry-summary').evaluate_all("""nodes => nodes.map(node => {
              const day=node.querySelector('.visits-entry-date b'), date=day.parentElement,
                copy=node.querySelector('.visits-entry-copy'), style=getComputedStyle(day),
                range=document.createRange();range.selectNodeContents(day);
              return {text:day.textContent,lineHeight:parseFloat(style.lineHeight),
                height:day.getBoundingClientRect().height, rects:range.getClientRects().length,
                right:day.getBoundingClientRect().right,dateRight:date.getBoundingClientRect().right,
                copyLeft:copy.getBoundingClientRect().left};
            })""")
            for metric in metrics:
                with self.subTest(width=width, day=metric['text']):
                    self.assertLessEqual(metric['height'], metric['lineHeight'] + 1)
                    self.assertEqual(metric['rects'], 1)
                    self.assertLessEqual(metric['right'], metric['dateRight'] + 1)
                    self.assertLess(metric['dateRight'], metric['copyLeft'])
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'))
            self.host.screenshot(path=str(self.shots / f'visit-history-dates-{width}.png'))
        self.assertEqual(self.errors, [])
