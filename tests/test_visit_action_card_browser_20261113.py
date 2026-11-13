"""The visit editor leads with concrete progress and preserves folded original dialogue."""
from __future__ import annotations

import unittest

from tests import test_visits_browser as fixture
from tests.test_generation_browser import expect, sync_playwright


@unittest.skipIf(sync_playwright is None, "Install playwright for browser verification")
class VisitActionCardBrowserTests(unittest.TestCase):
    setUpClass = classmethod(fixture.VisitsBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.VisitsBrowserTests.tearDownClass.__func__)
    open_settings = fixture.VisitsBrowserTests.open_settings

    def setUp(self):
        fixture.VisitsBrowserTests.setUp(self)
        self.page.evaluate("""() => {
          visitData.card_status={enabled:true,model_enabled:true,provider_id:'',max_length:12,
            providers:[{id:'fast',label:'Small model'}]};
          const write=AstrBotPluginPage.apiPost;
          AstrBotPluginPage.apiPost=(endpoint,payload)=>{
            if(endpoint==='visits/card-status-settings'){
              visitWrites.push({endpoint,payload:structuredClone(payload)});
              if(window.rejectStatusSave)return Promise.reject(new Error('save failed'));
              visitData.card_status={...visitData.card_status,...payload};
              return Promise.resolve(structuredClone(visitData.card_status));
            }
            return write(endpoint,payload);
          };
        }""")
        self.page.locator('#tab-visits').get_by_role('button', name='刷新', exact=True).click()

    def test_status_model_and_length_can_be_saved_without_inviting_or_changing_the_world(self):
        self.open_settings()
        settings = self.page.get_by_label('串门群名片状态设置')
        settings.get_by_label('串门名片状态模型', exact=True).select_option('fast')
        settings.get_by_label('状态最多字数', exact=True).fill('10')
        settings.get_by_role('button', name='保存名片状态设置').click()
        expect(settings.locator('.visits-card-status')).to_have_text('已保存，立即生效')
        writes = self.page.evaluate('visitWrites')
        self.assertEqual([row['endpoint'] for row in writes], ['visits/card-status-settings'])
        self.assertEqual(writes[0]['payload']['provider_id'], 'fast')
        self.assertEqual(writes[0]['payload']['max_length'], 10)
        self.assertEqual(self.errors, [])

    def test_failed_status_save_keeps_the_selected_provider_and_length(self):
        self.open_settings()
        self.page.evaluate('window.rejectStatusSave=true')
        settings = self.page.get_by_label('串门群名片状态设置')
        settings.get_by_label('串门名片状态模型', exact=True).select_option('fast')
        settings.get_by_label('状态最多字数', exact=True).fill('8')
        settings.get_by_role('button', name='保存名片状态设置').click()
        expect(settings.locator('.visits-card-status')).to_have_text('保存失败，当前选择已保留')
        expect(settings.get_by_label('串门名片状态模型', exact=True)).to_have_value('fast')
        expect(settings.get_by_label('状态最多字数', exact=True)).to_have_value('8')

    def test_current_and_historical_visits_show_short_progress_before_folded_dialogue(self):
        self.page.evaluate("""() => {
          const now=Date.now()/1000;
          const visit={id:'shared',status:'active',arrived_at:now-120,observed_at:now,role:'host',
            host_id:'self',guest_id:'friend',local:{id:'self',name:'爱丽丝'},remote:{id:'friend',name:'艾拉'},
            outing:{version:1,stage:0,place:'庭院',activity:'喝茶',started_at:now-100,until:now+300},
            shared_activity:{summary:'一起泡好了红茶，正在等茶凉一点。',
              milestones:[1,2,3,4].map(seq=>({seq,at:now-100+seq,summary:'共同进展'+seq})),pending:['还没洗茶杯']},
            log:[{seq:1,actor_id:'self',kind:'say',text:'这是一段保留的完整原话。',at:now-100}]};
          visitData.active=visit;visitData.history=[{...structuredClone(visit),id:'past',status:'completed',ended_at:now-10}];
        }""")
        self.page.locator('#tab-visits').get_by_role('button', name='刷新', exact=True).click()
        current = self.page.locator('.visits-current')
        expect(current.locator('.visits-brief')).to_contain_text('一起泡好了红茶')
        expect(current.locator('.visits-brief-milestones li')).to_have_count(3)
        expect(current.locator('.visits-brief-pending')).to_contain_text('还没洗茶杯')
        expect(current.locator('.visits-log-say')).not_to_be_visible()
        current.get_by_text('完整原话与经过', exact=True).click()
        expect(current.locator('.visits-log-say')).to_have_text('这是一段保留的完整原话。')
        expect(current.locator('.visits-log-say')).to_be_visible()
        record = self.page.locator('.visits-history details[data-visit-id="past"]')
        expect(record.locator('.visits-entry-preview')).to_have_text('一起泡好了红茶，正在等茶凉一点。')
        record.locator(':scope > summary').click()
        expect(record.locator('.visits-brief')).to_contain_text('一起泡好了红茶')
        expect(record.locator('.visits-log-say')).not_to_be_visible()
        self.assertEqual(self.errors, [])
