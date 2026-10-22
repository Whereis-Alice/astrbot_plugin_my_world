"""Cross-page links and asynchronous editors keep their source conversation."""
import unittest

from tests import test_generation_browser as generation
from tests.test_generation_browser import expect, sync_playwright
from tests.test_record_sessions_browser import FIRST, RECORD_BRIDGE, SECOND


SOURCE_BRIDGE = r"""(() => {
  const read = AstrBotPluginPage.apiGet;
  window.sourceReads=[]; window.sourceWrites=[]; window.sourceHeld=[];
  window.sourceHold=''; window.sourceHoldSession='test:GroupMessage:1001';
  window.emptyBrief=false;
  window.latestBrief=false;
  window.sourceReply=(key,p={})=>{
    const name=p.session?.endsWith('2002')?'乙':'甲';
    if(key==='persona-source')return {session:p.session,text:`${name}的主人设`,chars:5};
    if(key==='persona-brief')return {persona_brief:emptyBrief && name==='乙'?(latestBrief?{latest:{brief:'之前人格的摘要',at:1760000000}}:{}):
      {brief:`${name}的简易人设`,source:'generated',persona_chars:5,key:name}};
    if(key==='profile/weaver/audit')return {items:[{at:'2026-10-23',subject_name:`${name}的审计`,old_value:'旧值',new_value:'新值'}]};
    if(key==='life')return {session:p.session,items:[{id:'lm_book',text:`${name}的读书打算`,kind:'project',status:'working',progress:40,at:1760000000}]};
    if(key==='life/week')return {session:p.session,week_start:name==='甲'?'2026-10-19':'2027-02-01',
      week_end:name==='甲'?'2026-10-25':'2027-02-07',today:name==='甲'?'2026-10-20':'2027-02-03',days:[]};
    if(key==='model-calls')return {items:[],summary:{},series:[],hours:p.hours,page:1,pages:1,
      facets:{sessions:['test:GroupMessage:1001','test:GroupMessage:2002'],purposes:['life_matter','chat'],providers:['synthetic']}};
    return null;
  };
  AstrBotPluginPage.apiGet=(key,p={})=>{
    sourceReads.push({key,params:structuredClone(p)});
    const reply=sourceReply(key,p);
    if(reply===null)return read(key,p);
    if(key===sourceHold && p.session===sourceHoldSession)
      return new Promise((resolve,reject)=>sourceHeld.push({key,params:p,resolve,reject}));
    return Promise.resolve(reply);
  };
  AstrBotPluginPage.apiPost=(key,p={})=>{
    sourceWrites.push({key,params:structuredClone(p)});
    if(key===sourceHold && p.session===sourceHoldSession)
      return new Promise((resolve,reject)=>sourceHeld.push({key,params:p,resolve,reject}));
    return Promise.resolve({ok:true,...sourceReply(key,p)});
  };
  window.releaseSource=(fail=false)=>{
    sourceHold='';
    for(const entry of sourceHeld.splice(0))
      if(fail)entry.reject(new Error('过期的甲请求失败'));
      else entry.resolve({ok:true,...sourceReply(entry.key,entry.params)});
  };
})();"""


@unittest.skipIf(sync_playwright is None, 'Install playwright for browser verification')
class WorkspaceSourcesBrowserTests(unittest.TestCase):
    setUpClass = classmethod(generation.GenerationBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(generation.GenerationBrowserTests.tearDownClass.__func__)

    def setUp(self):
        generation.GenerationBrowserTests.setUp(self)
        self.host.emulate_media(reduced_motion='reduce')
        self.context.add_init_script(script=RECORD_BRIDGE + SOURCE_BRIDGE)
        self.host.reload()
        self.page = self.host.frame(name='editor')
        expect(self.page.locator('#app')).to_be_visible()
        self.page.wait_for_function('sourceReads.some(x=>x.key==="logs")')

    def tearDown(self):
        self.assertEqual(self.errors, [])

    def open_page(self, tab):
        self.page.locator(f'#tabs [data-tab="{tab}"]').click()

    def hold(self, key, session=FIRST):
        self.page.evaluate('([key,session])=>{sourceHold=key;sourceHoldSession=session;}', [key, session])

    def release(self, fail=False):
        self.page.evaluate('(fail)=>releaseSource(fail)', fail)
        self.page.wait_for_timeout(100)

    def brief(self):
        self.open_page('settings')
        return self.page.locator('#persona-brief-text')

    def test_related_memory_carries_source_session_and_clears_unrelated_filters(self):
        self.open_page('memories')
        expect(self.page.locator('#memory-list')).to_contain_text('甲的生活记忆')
        self.page.locator('#memory-filter-toggle').click()
        self.page.locator('#memory-type').select_option('event')
        self.page.locator('#memory-node').select_option('bedroom')
        self.open_page('life')
        self.page.locator('#status-session').select_option(SECOND)
        self.page.locator('[data-life-view="concerns"][role="tab"]').click()
        self.page.locator('.life-card').click()
        self.page.get_by_role('button', name='相关记忆 ↗', exact=True).click()
        expect(self.page.locator('#memory-session')).to_have_value(SECOND)
        expect(self.page.locator('#memory-type')).to_have_value('')
        expect(self.page.locator('#memory-node')).to_have_value('')
        expect(self.page.locator('#memory-keyword')).to_have_value('乙的读书打算')
        expect(self.page.locator('#memory-list')).to_contain_text('乙的生活记忆')

    def test_related_calls_carry_source_and_remove_previous_filters(self):
        self.open_page('model-calls')
        self.page.wait_for_function('sourceReads.some(x=>x.key==="model-calls")')
        self.page.locator('#calls-status').locator('xpath=ancestor::details[1]/summary').click()
        self.page.locator('#calls-status').select_option('error')
        self.page.locator('#calls-session').select_option(FIRST)
        self.page.locator('#calls-purpose').select_option('chat')
        self.page.locator('#calls-provider').select_option('synthetic')
        self.open_page('life')
        self.page.locator('#status-session').select_option(SECOND)
        self.page.locator('[data-life-view="concerns"][role="tab"]').click()
        self.page.locator('.life-card').click()
        self.page.get_by_role('button', name='相关模型调用 ↗', exact=True).click()
        expect(self.page.locator('#calls-session')).to_have_value(SECOND)
        expect(self.page.locator('#calls-status')).to_have_value('')
        expect(self.page.locator('#calls-purpose')).to_have_value('')
        expect(self.page.locator('#calls-provider')).to_have_value('')
        expect(self.page.locator('#calls-period')).to_have_value('720')
        self.page.wait_for_function('sourceReads.filter(x=>x.key==="model-calls").at(-1).params.q==="lm_book"')
        row = self.page.evaluate('sourceReads.filter(x=>x.key==="model-calls").at(-1).params')
        self.assertEqual(row['session'], SECOND)

    def test_related_memory_keeps_a_shared_life_group_as_the_picker_source(self):
        self.context.add_init_script(script="""(() => {
          const read=AstrBotPluginPage.apiGet;
          AstrBotPluginPage.apiGet=async(key,p={})=>{
            const result=await read(key,p);
            if(key==='config'||key==='sessions'){
              const sessions=key==='config'?result.sessions:result;
              sessions.groups=[{id:'shared',name:'共享生活',sessions:['test:GroupMessage:1001'],main_session:'test:GroupMessage:1001'}];
            }
            if(key==='life' && p.session==='shared')result.session='test:GroupMessage:1001';
            return result;
          };
        })();""")
        self.host.reload()
        self.page = self.host.frame(name='editor')
        expect(self.page.locator('#app')).to_be_visible()
        self.open_page('memories')
        self.page.locator('#memory-session').select_option(SECOND)
        self.open_page('life')
        self.page.locator('#status-session').select_option('shared')
        self.page.locator('[data-life-view="concerns"][role="tab"]').click()
        self.page.locator('.life-card').click()
        self.page.get_by_role('button', name='相关记忆 ↗', exact=True).click()
        expect(self.page.locator('#memory-session')).to_have_value('shared')
        self.assertEqual(self.page.evaluate('recordCalls.filter(x=>x.key==="memories").at(-1).params.session'), 'shared')

    def test_persona_import_uses_selected_session(self):
        self.open_page('settings')
        self.page.locator('#status-session').select_option(SECOND)
        self.page.get_by_role('button', name='从 AstrBot 导入当前人设', exact=True).click()
        row = self.page.evaluate('sourceReads.filter(x=>x.key==="persona-source").at(-1)')
        self.assertEqual(row['params']['session'], SECOND)
        expect(self.page.locator('[data-section-key="persona"] textarea')).to_have_value('乙的主人设')

    def test_persona_import_ignores_result_after_session_switch(self):
        self.open_page('settings')
        area = self.page.locator('[data-section-key="persona"] textarea')
        previous = area.input_value()
        self.hold('persona-source')
        self.page.get_by_role('button', name='从 AstrBot 导入当前人设', exact=True).click()
        self.page.wait_for_function('sourceHeld.length>0')
        self.page.locator('#status-session').select_option(SECOND)
        self.release()
        expect(area).to_have_value(previous)

    def test_persona_import_keeps_edits_made_while_reading(self):
        self.open_page('settings')
        self.hold('persona-source')
        self.page.get_by_role('button', name='从 AstrBot 导入当前人设', exact=True).click()
        self.page.wait_for_function('sourceHeld.length>0')
        area = self.page.locator('[data-section-key="persona"] textarea')
        area.fill('自己正在修改的人设')
        self.release()
        expect(area).to_have_value('自己正在修改的人设')

    def test_wizard_import_uses_current_session(self):
        self.open_page('settings')
        self.page.locator('#status-session').select_option(SECOND)
        self.page.get_by_role('button', name='重新运行设置向导…', exact=True).click()
        self.page.locator('.wizard-foot').get_by_role('button', name='下一步', exact=True).click()
        self.page.locator('.wizard-body').get_by_role('button', name='从 AstrBot 导入当前人设', exact=True).click()
        row = self.page.evaluate('sourceReads.filter(x=>x.key==="persona-source").at(-1)')
        self.assertEqual(row['params']['session'], SECOND)
        expect(self.page.locator('.wizard-body textarea')).to_have_value('乙的主人设')

    def test_wizard_brief_generation_uses_current_session(self):
        self.open_page('settings')
        self.page.locator('#status-session').select_option(SECOND)
        self.page.get_by_role('button', name='重新运行设置向导…', exact=True).click()
        for _ in range(5):
            self.page.locator('.wizard-foot').get_by_role('button', name='下一步', exact=True).click()
        self.page.locator('.wizard-body').get_by_role('button', name='从主人设生成', exact=True).click()
        row = self.page.evaluate('sourceWrites.filter(x=>x.key==="persona-brief").at(-1)')
        self.assertEqual(row['params']['session'], SECOND)
        expect(self.page.locator('.wizard-body textarea')).to_have_value('乙的简易人设')

    def test_brief_old_success_and_failure_cannot_replace_new_session(self):
        area = self.brief()
        expect(area).to_have_value('甲的简易人设')
        for fail in (False, True):
            with self.subTest(fail=fail):
                self.page.locator('#status-session').select_option(SECOND)
                expect(area).to_have_value('乙的简易人设')
                self.hold('persona-brief')
                self.page.locator('#status-session').select_option(FIRST)
                self.page.wait_for_function('sourceHeld.length>0')
                self.page.locator('#status-session').select_option(SECOND)
                expect(area).to_have_value('乙的简易人设')
                self.release(fail)
                expect(area).to_have_value('乙的简易人设')
                expect(self.page.locator('[data-section-key="persona_brief"]')).not_to_contain_text('过期')

    def test_brief_missing_in_new_session_clears_old_text(self):
        area = self.brief()
        expect(area).to_have_value('甲的简易人设')
        self.page.evaluate('emptyBrief=true')
        self.page.locator('#status-session').select_option(SECOND)
        expect(area).to_have_value('')
        expect(self.page.locator('[data-section-key="persona_brief"]')).to_contain_text('还没有生成过')

    def test_brief_read_cannot_replace_text_typed_during_loading(self):
        area = self.brief()
        expect(area).to_have_value('甲的简易人设')
        self.hold('persona-brief', SECOND)
        self.page.locator('#status-session').select_option(SECOND)
        self.page.wait_for_function('sourceHeld.length>0')
        area.fill('乙的手动草稿')
        self.release()
        expect(area).to_have_value('乙的手动草稿')

    def test_brief_preserves_explicit_previous_persona_fallback(self):
        area = self.brief()
        expect(area).to_have_value('甲的简易人设')
        self.page.evaluate('emptyBrief=true;latestBrief=true')
        self.page.locator('#status-session').select_option(SECOND)
        expect(area).to_have_value('之前人格的摘要')
        expect(self.page.locator('[data-section-key="persona_brief"]')).to_contain_text('当前会话读出的人设和这份不一致')

    def test_brief_manual_save_is_single_and_keeps_later_edits(self):
        area = self.brief()
        expect(area).to_have_value('甲的简易人设')
        area.fill('手动摘要')
        self.hold('persona-brief')
        button = self.page.get_by_role('button', name='保存到这个人格', exact=True)
        button.click()
        self.page.wait_for_function('sourceHeld.length>0')
        expect(button).to_be_disabled()
        area.fill('保存期间继续改的摘要')
        self.release()
        expect(area).to_have_value('保存期间继续改的摘要')
        self.assertEqual(self.page.evaluate('sourceWrites.length'), 1)

    def test_brief_no_session_clears_editor_and_disables_writes(self):
        area = self.brief()
        expect(area).to_have_value('甲的简易人设')
        self.page.locator('#status-session').evaluate('node=>{node.value="";node.dispatchEvent(new Event("change"));}')
        expect(area).to_have_value('')
        expect(self.page.get_by_role('button', name='从主人设生成', exact=True)).to_be_disabled()
        expect(self.page.get_by_role('button', name='保存到这个人格', exact=True)).to_be_disabled()

    def test_brief_old_response_cannot_fill_a_redrawn_editor(self):
        area = self.brief()
        expect(area).to_have_value('甲的简易人设')
        self.hold('persona-brief')
        self.page.locator('#status-session').select_option(SECOND)
        self.page.locator('#status-session').select_option(FIRST)
        self.page.wait_for_function('sourceHeld.length>0')
        self.page.evaluate('sourceHold=""')
        persona_mode = self.page.locator('[data-section-key="persona"] select')
        persona_mode.select_option('plugin')
        expect(area).to_have_value('甲的简易人设')
        area.fill('重绘后的草稿')
        self.release()
        expect(area).to_have_value('重绘后的草稿')

    def test_brief_generation_cannot_replace_new_session_and_reports_immediate_save(self):
        area = self.brief()
        expect(area).to_have_value('甲的简易人设')
        self.hold('persona-brief')
        self.page.get_by_role('button', name='从主人设生成', exact=True).click()
        self.page.wait_for_function('sourceHeld.length>0')
        self.page.locator('#status-session').select_option(SECOND)
        expect(area).to_have_value('乙的简易人设')
        self.release()
        expect(area).to_have_value('乙的简易人设')
        self.page.get_by_role('button', name='从主人设生成', exact=True).click()
        expect(self.page.locator('#toast')).to_contain_text('已保存')
        expect(self.page.locator('#toast')).not_to_contain_text('右上角')

    def test_audit_old_success_and_failure_cannot_replace_new_session(self):
        self.open_page('contacts')
        self.page.locator('#contacts-view-menu summary').click()
        self.page.locator('[data-contacts-view="audit"]').click()
        expect(self.page.locator('#contacts-audit-list')).to_contain_text('甲的审计')
        for fail in (False, True):
            with self.subTest(fail=fail):
                self.page.locator('#contacts-session').select_option(SECOND)
                expect(self.page.locator('#contacts-audit-list')).to_contain_text('乙的审计')
                self.hold('profile/weaver/audit')
                self.page.locator('#contacts-session').select_option(FIRST)
                self.page.wait_for_function('sourceHeld.length>0')
                self.page.locator('#contacts-session').select_option(SECOND)
                expect(self.page.locator('#contacts-audit-list')).to_contain_text('乙的审计')
                self.release(fail)
                expect(self.page.locator('#contacts-audit-list')).to_contain_text('乙的审计')

    def test_global_contacts_overview_hides_its_unused_conversation_selector(self):
        self.open_page('contacts')
        self.page.locator('#contacts-view-menu summary').click()
        self.page.locator('[data-contacts-view="overview"]').click()
        expect(self.page.locator('#contacts-overview')).to_contain_text('画像总数')
        rows = self.page.evaluate('sourceReads.filter(x=>x.key==="profile/weaver/stats")')
        self.assertTrue(rows)
        self.assertTrue(all(not row['params'].get('session') for row in rows))
        for width in (1440, 390):
            with self.subTest(width=width):
                self.host.set_viewport_size({'width': width, 'height': 1000})
                expect(self.page.locator('#contacts-session')).not_to_be_visible()
                expect(self.page.locator('#contacts-overview')).to_contain_text('全部会话')
                self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'))
        self.host.set_viewport_size({'width': 1440, 'height': 1000})
        self.page.locator('#contacts-view-menu summary').click()
        self.page.locator('[data-contacts-view="audit"]').click()
        expect(self.page.locator('#contacts-session')).to_be_visible()

    def test_week_loading_cannot_generate_with_previous_conversation_dates(self):
        self.open_page('life')
        self.page.locator('[data-life-view="week"][role="tab"]').click()
        expect(self.page.locator('#life-week-title')).to_contain_text('10.19')
        self.hold('life/week', SECOND)
        self.page.locator('#status-session').select_option(SECOND)
        self.page.wait_for_function('sourceHeld.length>0')
        expect(self.page.locator('#life-week-generate-calendar')).to_be_disabled()
        expect(self.page.locator('#life-week-generate-diary')).to_be_disabled()
        expect(self.page.locator('#life-week-title')).not_to_contain_text('10.19')
        self.release()
        expect(self.page.locator('#life-week-generate-calendar')).to_be_enabled()
        self.page.locator('#life-week-generate-calendar').click()
        self.page.locator('#life-week-generate-diary').click()
        rows = self.page.evaluate('sourceWrites')
        self.assertEqual([(r['key'], r['params']['session']) for r in rows],
                         [('calendar/generate', SECOND), ('diary/generate', SECOND)])
        self.assertEqual(rows[0]['params']['year'], 2027)
        self.assertEqual(rows[1]['params']['day'], '2027-02-02')

    def test_old_week_generation_does_not_reload_or_notify_the_new_session(self):
        for endpoint, button in (('calendar/generate', 'life-week-generate-calendar'),
                                 ('diary/generate', 'life-week-generate-diary')):
            for fail in (False, True):
                with self.subTest(endpoint=endpoint, fail=fail):
                    self.open_page('life')
                    self.page.locator('#status-session').select_option(FIRST)
                    self.page.locator('[data-life-view="week"][role="tab"]').click()
                    expect(self.page.locator('#life-week-title')).to_contain_text('10.19')
                    self.hold(endpoint)
                    self.page.locator(f'#{button}').click()
                    self.page.wait_for_function('sourceHeld.length>0')
                    self.page.locator('#status-session').select_option(SECOND)
                    expect(self.page.locator('#life-week-title')).to_contain_text('02.01')
                    before = self.page.evaluate('sourceReads.filter(x=>x.key==="life/week").length')
                    self.release(fail)
                    self.assertEqual(self.page.evaluate('sourceReads.filter(x=>x.key==="life/week").length'), before)
                    expect(self.page.locator('#toast')).not_to_contain_text('过期')
                    expect(self.page.locator('#toast')).not_to_contain_text('2026 年日历已生成')
