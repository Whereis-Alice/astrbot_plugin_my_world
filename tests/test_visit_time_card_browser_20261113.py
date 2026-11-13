"""Visit scenes distinguish elapsed plans from actual activity and departure."""
from __future__ import annotations

import unittest

from tests import test_visits_browser as fixture
from tests.test_generation_browser import expect, sync_playwright


@unittest.skipIf(sync_playwright is None, "Install playwright for browser verification")
class VisitTimeCardBrowserTests(unittest.TestCase):
    setUpClass = classmethod(fixture.VisitsBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.VisitsBrowserTests.tearDownClass.__func__)
    setUp = fixture.VisitsBrowserTests.setUp

    def show_visit(self, *, live=True, starts=100, until=500, ended=0, travel=0, route_end=0):
        self.page.evaluate("""data => {
          const visit={id:'timed-visit',status:data.live?'active':'completed',arrived_at:90,
            observed_at:150,ended_at:data.ended,role:'guest',local:{name:'艾拉'},remote:{name:'爱丽丝'},
            outing_version:1,outing:{version:1,stage:0,place:'庭院',activity:'喝茶',
              origin_place:'客厅',started_at:data.starts,until:data.until,travel_seconds:data.travel,
              route:[{place:'庭院',activity:'喝茶',started_at:data.starts,until:data.until,ended_at:data.route_end},
                {place:'山丘',activity:'散步',started_at:data.until,until:data.until+120}]},
            log:[]};
          visitData.active=data.live?visit:null;visitData.history=data.live?[]:[visit];
        }""", {"live": live, "starts": starts, "until": until, "ended": ended,
               "travel": travel, "route_end": route_end})
        self.page.locator('#tab-visits').get_by_role('button', name='刷新', exact=True).click()
        if live:
            return self.page.locator('.visits-current')
        record = self.page.locator('.visits-history details[data-visit-id="timed-visit"]')
        record.locator(':scope > summary').click()
        return record

    def test_future_activity_without_a_journey_is_not_shown_as_happening(self):
        card = self.show_visit(starts=180)
        expect(card.locator('.visits-scene-activity')).to_contain_text('还没开始')
        expect(card.locator('.visits-time-progress')).to_have_count(0)
        expect(card.locator('.visits-scene-clock')).to_contain_text('约定')
        self.assertEqual(self.errors, [])

    def test_elapsed_activity_time_is_a_checkpoint_without_a_completion_claim(self):
        card = self.show_visit(until=140)
        expect(card.locator('.visits-scene-activity')).to_contain_text('约好的时间已过')
        expect(card.locator('.visits-scene-activity')).to_contain_text('还在商量')
        expect(card.locator('.visits-time-progress')).to_have_count(0)
        self.assertEqual(self.errors, [])

    def test_history_ended_on_the_road_does_not_claim_the_destination_activity(self):
        card = self.show_visit(live=False, starts=180, ended=160, travel=80)
        expect(card.locator('.visits-scene-activity')).to_have_text('当时还在一起赶路，共同活动尚未开始。')
        expect(card.locator('.visits-scene-copy h4')).to_have_text('客厅 → 庭院')
        expect(card.locator('.visits-entry-title')).to_contain_text('途中')
        expect(card.locator('.visits-entry-preview')).to_have_text('那时还在路上，约好的活动尚未开始。')
        expect(card.locator('.visits-route li.is-past')).to_have_count(0)
        self.assertEqual(self.errors, [])

    def test_historical_activity_uses_actual_departure_instead_of_planned_end(self):
        card = self.show_visit(live=False, ended=300, route_end=180)
        expected = self.page.evaluate("""() => {
          const stamp=time=>new Date(time*1000).toLocaleString('zh-CN',
            {month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'});
          return `实际相处 ${stamp(100)} — ${stamp(180)}`;
        }""")
        expect(card.locator('.visits-scene-clock')).to_have_text(expected)
        expect(card.locator('.visits-route li.is-past')).to_have_count(1)
        self.assertEqual(self.errors, [])

    def test_live_time_bar_describes_elapsed_time_and_never_activity_completion(self):
        card = self.show_visit()
        expect(card.get_by_role('progressbar')).to_have_attribute('aria-label', '约定活动时段已过去的比例')
        expect(card.locator('.visits-scene-clock')).to_contain_text('约定')
        self.assertEqual(self.errors, [])
