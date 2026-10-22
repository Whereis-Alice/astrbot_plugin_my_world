"""Memory evidence cannot open a stale dialog after navigation or a newer action."""
import unittest

from tests import test_workspace_sources_browser as source
from tests.test_generation_browser import expect, sync_playwright
from tests.test_record_sessions_browser import SECOND


@unittest.skipIf(sync_playwright is None, 'Install playwright for browser verification')
class MemoryDetailsBrowserTests(unittest.TestCase):
    setUpClass = classmethod(source.WorkspaceSourcesBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(source.WorkspaceSourcesBrowserTests.tearDownClass.__func__)
    setUp = source.WorkspaceSourcesBrowserTests.setUp
    tearDown = source.WorkspaceSourcesBrowserTests.tearDown
    open_page = source.WorkspaceSourcesBrowserTests.open_page

    def pending_detail(self):
        self.page.evaluate('''() => {
          const read=AstrBotPluginPage.apiGet;
          window.memoryEvidencePending=[];
          AstrBotPluginPage.apiGet=(key,p)=>key==='memories/evidence'
            ? new Promise((resolve,reject)=>memoryEvidencePending.push({resolve,reject,p})) : read(key,p);
          window.releaseMemoryEvidence=(fail)=>{
            for(const row of memoryEvidencePending.splice(0))
              if(fail)row.reject(Error('过期的依据错误'));
              else row.resolve({sources:[],revisions:[]});
          };
        }''')
        self.open_page('memories')
        expect(self.page.locator('#memory-list')).to_contain_text('甲的生活记忆')
        self.page.locator('#memory-list').get_by_role('button', name='详情', exact=True).click()
        self.page.wait_for_function('memoryEvidencePending.length===1')

    def release(self, fail=False):
        self.page.evaluate('(fail)=>releaseMemoryEvidence(fail)', fail)
        self.page.wait_for_timeout(100)

    def test_session_change_during_evidence_read_cannot_open_previous_memory(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                self.pending_detail()
                self.page.locator('#memory-session').select_option(SECOND)
                expect(self.page.locator('#memory-list')).to_contain_text('乙的生活记忆')
                self.release(fail)
                try:
                    expect(self.page.locator('#dialog')).to_be_hidden()
                finally:
                    if self.page.locator('#dialog').is_visible():
                        self.page.locator('#dialog-cancel').click()
                    self.page.locator('#memory-session').select_option(source.FIRST)

    def test_navigation_away_and_back_invalidates_pending_detail(self):
        self.pending_detail()
        self.open_page('logs')
        self.open_page('memories')
        self.release()
        expect(self.page.locator('#dialog')).to_be_hidden()

    def test_new_editor_is_not_replaced_by_pending_detail(self):
        self.pending_detail()
        self.page.locator('#memory-list').get_by_role('button', name='编辑', exact=True).click()
        expect(self.page.locator('#dialog-title')).to_have_text('编辑记忆 #11')
        self.page.locator('[data-dialog-key="content"]').fill('编辑中的草稿')
        self.release()
        expect(self.page.locator('#dialog-title')).to_have_text('编辑记忆 #11')
        expect(self.page.locator('[data-dialog-key="content"]')).to_have_value('编辑中的草稿')

    def test_newest_detail_wins_and_legacy_evidence_failure_still_opens(self):
        self.pending_detail()
        self.page.locator('#memory-list').get_by_role('button', name='详情', exact=True).click()
        self.page.wait_for_function('memoryEvidencePending.length===2')
        self.page.evaluate('memoryEvidencePending.pop().reject(Error("旧记录没有依据"))')
        expect(self.page.locator('#dialog')).to_be_visible()
        expect(self.page.locator('#dialog-body')).to_contain_text('甲的生活记忆')
        self.page.locator('#dialog-ok').click()
        self.release()
        expect(self.page.locator('#dialog')).to_be_hidden()
