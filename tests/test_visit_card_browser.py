"""Opt-in long-card settings remain explicit and survive failed saves in the editor."""

import unittest

from tests import test_visits_browser as visits_browser
from tests.test_generation_browser import expect, sync_playwright


@unittest.skipIf(sync_playwright is None, "Install playwright for browser verification")
class VisitCardSettingsBrowserTests(unittest.TestCase):
    setUpClass = classmethod(visits_browser.VisitsBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(visits_browser.VisitsBrowserTests.tearDownClass.__func__)
    open_settings = visits_browser.VisitsBrowserTests.open_settings

    def setUp(self):
        visits_browser.VisitsBrowserTests.setUp(self)
        self.page.evaluate("""() => {
          visitData.record_card={enabled:false,sessions:[],choices:[
            {session_id:'test:GroupMessage:100',label:'爱丽丝和艾拉的小客厅',enabled:true},
            {session_id:'test:FriendMessage:200',label:'同一生活的私聊',enabled:true},
            {session_id:'test:GroupMessage:300',label:'森林来信',enabled:false}]};
          window.cardSaveMode='ok';
          const original=AstrBotPluginPage.apiPost;
          AstrBotPluginPage.apiPost=(endpoint,payload)=>{
            if(endpoint!=='visits/record-card-settings')return original(endpoint,payload);
            visitWrites.push({endpoint,payload:structuredClone(payload)});
            if(cardSaveMode==='reject')return Promise.reject(new Error('设置暂时保存失败'));
            visitData.record_card.enabled=payload.enabled;
            visitData.record_card.sessions=[...payload.sessions];
            return Promise.resolve(structuredClone(visitData.record_card));
          };
        }""")
        self.refresh()
        self.open_settings()
        expect(self.page.locator('.visits-card-choice')).to_have_count(3)

    def refresh(self):
        self.page.locator('#tab-visits').get_by_role('button', name='刷新', exact=True).click()

    def toggle(self):
        return self.page.get_by_label('自动发送相见长卡片', exact=True)

    def destination(self, name):
        return self.page.get_by_label(f'接收相见长卡片：{name}', exact=True)

    def save(self):
        return self.page.get_by_role('button', name='保存长卡片设置', exact=True)

    def test_default_is_off_and_each_actual_conversation_is_explicit(self):
        expect(self.toggle()).not_to_be_checked()
        expect(self.save()).to_be_disabled()
        self.assertEqual(self.page.locator('.visits-card-choice input:checked').count(), 0)
        expect(self.page.locator('.visits-card-note')).to_contain_text('共享生活的会话也需分别勾选')
        self.assertEqual(self.page.evaluate('visitWrites.length'), 0)
        self.toggle().check()
        self.destination('同一生活的私聊').check()
        self.save().click()
        expect(self.page.locator('.visits-card-status')).to_have_text('已保存，立即生效')
        self.assertEqual(self.page.evaluate('visitWrites'), [{
            'endpoint': 'visits/record-card-settings',
            'payload': {'config_scope': '', 'enabled': True, 'sessions': ['test:FriendMessage:200']},
        }])
        expect(self.destination('爱丽丝和艾拉的小客厅')).not_to_be_checked()
        expect(self.destination('同一生活的私聊')).to_be_checked()
        expect(self.save()).to_be_disabled()
        self.assertEqual(self.errors, [])

    def test_draft_survives_refresh_life_switch_and_save_failure(self):
        self.toggle().check()
        self.destination('同一生活的私聊').check()
        self.refresh()
        expect(self.toggle()).to_be_checked()
        expect(self.destination('同一生活的私聊')).to_be_checked()
        self.page.locator('#status-session').select_option('test:GroupMessage:200')
        self.page.wait_for_function("visitReads.at(-1).session==='test:GroupMessage:200'")
        expect(self.destination('同一生活的私聊')).to_be_checked()
        self.page.evaluate("cardSaveMode='reject'")
        self.save().click()
        expect(self.page.locator('.visits-card-status')).to_have_text('保存失败，当前选择已保留')
        expect(self.save()).to_be_enabled()
        self.assertFalse(self.page.evaluate('visitData.record_card.enabled'))
        self.refresh()
        expect(self.destination('同一生活的私聊')).to_be_checked()
        self.page.evaluate("cardSaveMode='ok'")
        self.save().click()
        expect(self.page.locator('.visits-card-status')).to_have_text('已保存，立即生效')
        self.assertTrue(self.page.evaluate('visitData.record_card.enabled'))
        self.assertEqual(self.errors, [])

    def test_settings_can_be_prepared_before_visits_are_enabled_and_wrap_on_mobile(self):
        self.toggle().check()
        self.destination('爱丽丝和艾拉的小客厅').check()
        for width, height in [(1440, 1000), (390, 844)]:
            self.host.set_viewport_size({'width': width, 'height': height})
            self.page.locator('.visits-card-settings').scroll_into_view_if_needed()
            self.host.screenshot(path=str(self.shots / f'visit-card-settings-{width}.png'))
        self.page.evaluate("""() => {
          visitData.enabled=false;
          visitData.record_card.choices[0].label='<img src=x onerror="window.injected=true">';
          visitData.record_card.choices[0].session_id='test:GroupMessage:'+('1234567890'.repeat(12));
        }""")
        self.refresh()
        expect(self.toggle()).to_be_enabled()
        expect(self.save()).to_be_enabled()
        self.assertFalse(self.page.evaluate('Boolean(window.injected)'))
        self.assertEqual(self.page.locator('.visits-card-settings img').count(), 0)
        for width, height in [(1440, 1000), (390, 844)]:
            self.host.set_viewport_size({'width': width, 'height': height})
            self.page.locator('.visits-card-settings').scroll_into_view_if_needed()
            expect(self.page.locator('.visits-card-settings')).to_be_visible()
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'))
            self.host.screenshot(path=str(self.shots / f'visit-card-settings-wrapping-{width}.png'))
        self.assertEqual(self.errors, [])
