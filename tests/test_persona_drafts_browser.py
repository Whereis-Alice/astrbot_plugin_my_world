"""Summary generation and manual saves use the visible persona draft."""
import unittest

from tests import test_workspace_sources_browser as source
from tests.test_generation_browser import expect, sync_playwright


@unittest.skipIf(sync_playwright is None, 'Install playwright for browser verification')
class PersonaDraftsBrowserTests(unittest.TestCase):
    setUpClass = classmethod(source.WorkspaceSourcesBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(source.WorkspaceSourcesBrowserTests.tearDownClass.__func__)
    setUp = source.WorkspaceSourcesBrowserTests.setUp
    tearDown = source.WorkspaceSourcesBrowserTests.tearDown
    open_page = source.WorkspaceSourcesBrowserTests.open_page
    hold = source.WorkspaceSourcesBrowserTests.hold
    release = source.WorkspaceSourcesBrowserTests.release

    def draft(self):
        self.open_page('settings')
        self.page.locator('[data-section-key="persona"] select').select_option('plugin')
        self.page.locator('[data-section-key="persona"] textarea').fill('未保存的新角色卡')
        self.page.evaluate('''() => {
          const reply=sourceReply;
          window.sourceReply=(key,p)=>key==='persona-brief' && (p.generate||'brief' in p)
            ? {matches_current_persona:false,persona_brief:{brief:p.brief||'新草稿摘要',source:'generated'}}
            : reply(key,p);
        }''')

    def wizard(self):
        self.page.get_by_role('button', name='重新运行设置向导…', exact=True).click()
        for _ in range(5):
            self.page.locator('.wizard-foot').get_by_role('button', name='下一步', exact=True).click()

    def assert_draft_payload(self):
        params = self.page.evaluate('sourceWrites.filter(x=>x.key==="persona-brief").at(-1).params')
        self.assertEqual(params['persona'], '未保存的新角色卡')
        self.assertEqual(params['persona_mode'], 'plugin')

    def test_settings_generate_and_save_the_visible_draft_with_accurate_feedback(self):
        self.draft()
        area = self.page.locator('#persona-brief-text')
        self.page.get_by_role('button', name='从主人设生成', exact=True).click()
        expect(area).to_have_value('新草稿摘要')
        self.assert_draft_payload()
        expect(self.page.locator('#toast')).to_contain_text('保存主人设后生效')
        expect(self.page.locator('[data-section-key="persona_brief"]')).to_contain_text('草稿')
        area.fill('手动修改的草稿摘要')
        self.page.get_by_role('button', name='保存到这个人格', exact=True).click()
        self.assert_draft_payload()
        expect(area).to_have_value('手动修改的草稿摘要')
        expect(self.page.locator('#toast')).to_contain_text('保存主人设后生效')

    def test_wizard_generate_and_manual_save_use_the_same_draft(self):
        self.draft()
        self.wizard()
        area = self.page.locator('.wizard-body textarea').filter(visible=True).first
        self.page.locator('.wizard-body').get_by_role('button', name='从主人设生成', exact=True).click()
        expect(area).to_have_value('新草稿摘要')
        self.assert_draft_payload()
        expect(self.page.locator('#toast')).to_contain_text('保存主人设后生效')
        area.fill('向导中手动修改的摘要')
        self.page.locator('.wizard-body').get_by_role('button', name='保存到这个人格', exact=True).click()
        self.assert_draft_payload()
        expect(self.page.locator('#persona-brief-text')).to_have_value('向导中手动修改的摘要')
        expect(self.page.locator('#toast')).to_contain_text('保存主人设后生效')

    def test_changed_persona_does_not_receive_an_old_generated_summary(self):
        self.draft()
        area = self.page.locator('#persona-brief-text')
        previous = area.input_value()
        self.hold('persona-brief')
        self.page.get_by_role('button', name='从主人设生成', exact=True).click()
        self.page.wait_for_function('sourceHeld.length===1')
        self.page.locator('[data-section-key="persona"] textarea').fill('生成期间写的另一张角色卡')
        self.release()
        expect(area).to_have_value(previous)
        expect(self.page.locator('#toast')).not_to_contain_text('已生成')

    def test_changed_persona_during_a_read_keeps_the_summary_editor(self):
        self.draft()
        area = self.page.locator('#persona-brief-text')
        self.hold('persona-brief', source.SECOND)
        self.page.locator('#status-session').select_option(source.SECOND)
        self.page.wait_for_function('sourceHeld.length===1')
        self.page.locator('[data-section-key="persona"] textarea').fill('读取期间写的另一张角色卡')
        self.release()
        expect(area).to_have_value('')
        expect(self.page.get_by_role('button', name='保存到这个人格', exact=True)).to_be_enabled()
