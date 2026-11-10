"""Retained encounter counts preserve each world and selected life's identity."""
from __future__ import annotations

import unittest

from tests import test_visits_browser as fixture
from tests.test_generation_browser import expect, sync_playwright


@unittest.skipIf(sync_playwright is None, "Install playwright for browser verification")
class VisitHistoryScopeBrowserTests(unittest.TestCase):
    setUpClass = classmethod(fixture.VisitsBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.VisitsBrowserTests.tearDownClass.__func__)
    setUp = fixture.VisitsBrowserTests.setUp

    def test_counts_survive_catalog_changes_and_keep_worlds_and_lives_separate(self):
        self.page.evaluate("""() => {
          const record=(id,remote,extra={})=>({id,status:'completed',created_at:1794118200,
            arrived_at:1794118200,ended_at:1794121800,remote,
            log:[{actor_id:remote.id,kind:'say',text:'一起喝茶。'}],...extra});
          visitData.history=[
            record('retired-life',{id:'retired-friend',name:'爱丽丝'},{connection_id:'server'}),
            record('current-life',{id:'friend',name:'爱丽丝'}),
            record('former-world',{id:'former:friend',instance_id:'former',name:'爱丽丝'}),
            record('not-arrived',{id:'friend',name:'爱丽丝'},{arrived_at:null,status:'declined'})];
          visitData.active=record('ongoing',{id:'retired-active',instance_id:'server',name:'爱丽丝'},
            {status:'active',ended_at:null});
          visitData.history.unshift(visitData.active);
          const read=AstrBotPluginPage.apiGet;
          AstrBotPluginPage.apiGet=(endpoint,payload)=>{
            if(endpoint==='visits'&&payload.session==='test:GroupMessage:200'){
              const result=structuredClone(visitData);
              result.history=[record('another-life',{id:'friend',name:'爱丽丝'})];
              result.active=null;return Promise.resolve(result);
            }
            return read(endpoint,payload);
          };
        }""")
        refresh = self.page.locator('#tab-visits').get_by_role('button', name='刷新', exact=True)
        refresh.click()
        expect(self.page.locator('.visits-peer-count')).to_have_text('3 次相见')
        expect(self.page.locator('.visits-history > details')).to_have_count(3)
        expect(self.page.locator('.visits-history-note')).to_contain_text('1 篇来自当前友邻列表以外的旧友邻')
        self.assertEqual(self.page.evaluate('visitWrites'), [])
        self.page.locator('#status-session').select_option('test:GroupMessage:200')
        expect(self.page.locator('.visits-peer-count')).to_have_text('1 次相见')
        expect(self.page.locator('.visits-history > details')).to_have_count(1)
        expect(self.page.locator('.visits-history [data-visit-id="retired-life"]')).to_have_count(0)
        expect(self.page.locator('.visits-history-note')).to_have_count(0)
        self.assertEqual(self.page.evaluate('visitWrites'), [])
        self.assertEqual(self.errors, [])
