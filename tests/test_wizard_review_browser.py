"""First-run setup preserves drafts and reports only successfully saved settings."""
import unittest

from tests import test_generation_browser as generation
from tests.test_generation_browser import expect, sync_playwright
from tests.test_record_sessions_browser import FIRST, RECORD_BRIDGE, SECOND


WIZARD_BRIDGE = r"""(() => {
  const read = AstrBotPluginPage.apiGet, post = AstrBotPluginPage.apiPost;
  window.wizardWrites = []; window.wizardFail = ''; window.wizardHold = false;
  window.wizardWorld = null; window.wizardSessions = null; window.wizardExtra = false;
  window.wizardVoiceHold = false;
  AstrBotPluginPage.apiGet = async (key, p={}) => {
    if(key==='voice-samples') return {scenes:[{id:'night',label:'夜晚'}],samples:[],candidates:[]};
    if(key==='sessions' && wizardSessions) return structuredClone(wizardSessions);
    const data = await read(key,p);
    if(key !== 'config') return data;
    wizardWorld ||= {...data.world, wizard_done:false,
      memory:{...data.world.memory,semantic_provider_id:'embedding-test',recall_max_chars:900}};
    wizardSessions ||= {...data.sessions, groups:[{id:'shared',name:'原有会话组',
      main_session:'test:GroupMessage:1001',sessions:['test:GroupMessage:1001']}]};
    const sessions = structuredClone(wizardSessions);
    if(wizardExtra) sessions.sessions.push({session_id:'test:GroupMessage:3003',note:'新增会话',enabled:true});
    return {...data,world:structuredClone(wizardWorld),sessions,
      embedding_providers:[{id:'embedding-test',model:'synthetic-embedding'}]};
  };
  AstrBotPluginPage.apiPost = async (key,p={}) => {
    wizardWrites.push({key,data:structuredClone(p)});
    if(wizardHold && key==='config/world') await new Promise(resolve=>window.wizardRelease=resolve);
    if(key===wizardFail) throw new Error('测试保存失败');
    if(key==='voice-samples/generate') {
      if(wizardVoiceHold) await new Promise(resolve=>window.wizardVoiceRelease=resolve);
      return {scenes:[{scene:'night',label:'夜晚',candidates:[{text:'夜里想喝点热茶。'}]}]};
    }
    if(key==='config/world') wizardWorld=structuredClone(p.world);
    if(key==='config/sessions') wizardSessions=structuredClone(p.sessions);
    if(key==='config/proactive') {
      wizardWorld.proactive_chat_paused=!p.enabled;
      if(p.whitelist) wizardWorld.proactive_session_whitelist=structuredClone(p.whitelist);
      if(p.life_share_whitelist) wizardWorld.life_share_session_whitelist=structuredClone(p.life_share_whitelist);
    }
    return post(key,p);
  };
})();"""


@unittest.skipIf(sync_playwright is None, 'Install playwright for browser verification')
class WizardReviewBrowserTests(unittest.TestCase):
    setUpClass = classmethod(generation.GenerationBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(generation.GenerationBrowserTests.tearDownClass.__func__)

    def setUp(self):
        generation.GenerationBrowserTests.setUp(self)
        self.host.emulate_media(reduced_motion='reduce')
        self.context.add_init_script(script=RECORD_BRIDGE + WIZARD_BRIDGE)
        self.host.reload()
        self.page = self.host.frame(name='editor')
        expect(self.page.get_by_role('dialog', name='小世界设置向导')).to_be_visible()

    def tearDown(self):
        self.assertEqual(self.errors, [])

    def next(self, count=1):
        for _ in range(count):
            self.page.locator('.wizard-foot').get_by_role('button', name='下一步', exact=True).click()

    def finish(self):
        self.page.locator('.wizard-foot').get_by_role('button', name='完成并保存', exact=True).click()

    def test_refresh_keeps_local_toggles_and_gets_new_groups_and_sessions(self):
        self.page.get_by_role('checkbox', name='启用会话：合成会话甲', exact=True).uncheck()
        # Another client updated the group and registered a conversation.
        self.page.evaluate("async()=>{const data=await AstrBotPluginPage.apiGet('config');"
                           "data.sessions.groups[0].name='后台的新名字';wizardSessions=data.sessions;wizardExtra=true;}")
        self.page.get_by_role('button', name='重新获取', exact=True).click()
        expect(self.page.get_by_role('checkbox', name='启用会话：新增会话', exact=True)).to_be_visible()
        expect(self.page.get_by_role('checkbox', name='启用会话：合成会话甲', exact=True)).not_to_be_checked()
        self.next(6)
        expect(self.page.locator('.wizard-review')).to_contain_text('新增会话')
        expect(self.page.locator('.wizard-review')).not_to_contain_text('合成会话甲')
        self.finish()
        expect(self.page.locator('.wizard-backdrop')).to_have_count(0)
        saved = self.page.evaluate('wizardSessions')
        self.assertFalse(next(row for row in saved['sessions'] if row['session_id'] == FIRST)['enabled'])
        self.assertEqual(saved['groups'][0]['name'], '后台的新名字')
        self.assertEqual({row['session_id'] for row in saved['sessions']}, {FIRST, SECOND, 'test:GroupMessage:3003'})

    def test_refresh_preserves_group_edits_made_before_opening_the_wizard(self):
        self.page.get_by_role('button', name='暂时关闭', exact=True).click()
        self.page.locator('button[data-tab="sessions"]').click()
        self.page.locator('#session-seg [data-seg="groups"]').click()
        self.page.locator('#group-list').get_by_role('button', name='编辑', exact=True).click()
        self.page.locator('.group-editor input[type="text"]').fill('未保存的会话组名')
        self.page.locator('button[data-tab="settings"]').click()
        self.page.get_by_role('button', name='重新运行设置向导…', exact=True).click()
        self.page.evaluate('wizardExtra=true')
        self.page.get_by_role('button', name='重新获取', exact=True).click()
        expect(self.page.get_by_role('checkbox', name='启用会话：新增会话', exact=True)).to_be_visible()
        expect(self.page.locator('.wizard-body')).to_contain_text('未保存的会话组名')
        self.next(6)
        self.finish()
        expect(self.page.locator('.wizard-backdrop')).to_have_count(0)
        self.assertEqual(self.page.evaluate('wizardSessions.groups[0].name'), '未保存的会话组名')

    def test_life_and_memory_preferences_are_prefilled_saved_and_do_not_call_generation(self):
        self.next(3)
        self.page.get_by_role('combobox', name='作息模式', exact=True).select_option('activity')
        self.page.get_by_role('checkbox', name='自动安排每天的生活', exact=True).uncheck()
        self.next()
        embed = self.page.get_by_role('combobox', name='生活记忆嵌入模型', exact=True)
        expect(embed).to_have_value('embedding-test')
        self.page.get_by_role('combobox', name='何时回想生活', exact=True).select_option('manual')
        self.next(2)
        expect(self.page.locator('.wizard-review')).to_contain_text('活动恢复，无需睡眠')
        expect(self.page.locator('.wizard-review')).to_contain_text('只在主动查询时回想')
        self.host.screenshot(path=str(self.shots / 'wizard-review-desktop.png'))
        self.finish()
        expect(self.page.locator('.wizard-backdrop')).to_have_count(0)
        expect(self.page.locator('#toast')).to_contain_text('已保存，可以开始用了')
        data = self.page.evaluate("async()=>await AstrBotPluginPage.apiGet('config')")
        self.assertTrue(data['world']['wizard_done'])
        self.assertEqual(data['world']['sleep']['mode'], 'activity')
        self.assertEqual(data['world']['memory']['automatic_recall'], 'manual')
        self.assertEqual(data['world']['memory']['semantic_provider_id'], 'embedding-test')
        self.assertFalse(data['schedules']['auto']['enabled'])
        self.assertTrue(all(row['key'].startswith('config/') for row in self.page.evaluate('wizardWrites')))

    def test_failed_save_keeps_wizard_open_and_completion_false_until_retry(self):
        self.next(6)
        self.page.evaluate("wizardFail='config/sessions'")
        self.finish()
        expect(self.page.locator('#toast')).to_contain_text('测试保存失败')
        expect(self.page.locator('.wizard-backdrop')).to_be_visible()
        self.assertFalse(self.page.evaluate('wizardWorld.wizard_done'))
        self.page.evaluate("wizardFail=''")
        self.finish()
        expect(self.page.locator('.wizard-backdrop')).to_have_count(0)
        self.assertTrue(self.page.evaluate('wizardWorld.wizard_done'))

    def test_final_completion_write_failure_remains_retryable(self):
        self.next(6)
        self.page.evaluate("""() => {
          const post=AstrBotPluginPage.apiPost;
          AstrBotPluginPage.apiPost=(key,p)=>p.world?.wizard_done
            ? Promise.reject(new Error('完成标记保存失败')) : post(key,p);
        }""")
        self.finish()
        expect(self.page.locator('#toast')).to_contain_text('完成标记保存失败')
        expect(self.page.locator('.wizard-backdrop')).to_be_visible()
        expect(self.page.get_by_role('button', name='完成并保存', exact=True)).to_be_enabled()
        self.assertFalse(self.page.evaluate('wizardWorld.wizard_done'))

    def test_saving_prevents_duplicate_submits_and_navigation(self):
        self.next(6)
        self.page.evaluate('wizardHold=true')
        self.finish()
        expect(self.page.get_by_role('button', name='保存中…', exact=True)).to_be_disabled()
        expect(self.page.locator('.wizard-foot').get_by_role('button', name='上一步', exact=True)).to_be_disabled()
        self.assertEqual(len(self.page.evaluate('wizardWrites')), 1)
        self.page.evaluate('wizardHold=false;wizardRelease()')
        expect(self.page.locator('.wizard-backdrop')).to_have_count(0)

    def test_close_keeps_visible_unsaved_draft_and_makes_no_writes(self):
        self.next()
        self.page.locator('.wizard-body textarea').fill('向导里的角色草稿')
        self.page.get_by_role('button', name='暂时关闭', exact=True).click()
        expect(self.page.locator('.wizard-backdrop')).to_have_count(0)
        expect(self.page.locator('#toast')).to_contain_text('未保存草稿')
        self.page.locator('button[data-tab="settings"]').click()
        expect(self.page.locator('[data-section-key="persona"] textarea')).to_have_value('向导里的角色草稿')
        self.assertEqual(self.page.evaluate('wizardWrites'), [])

    def test_mobile_memory_setup_fits_and_permissions_remain_explicit(self):
        self.host.set_viewport_size({'width': 390, 'height': 844})
        self.page.locator('.wizard-options summary').click()
        expect(self.page.get_by_role('button', name='应用全局白名单', exact=True).filter(visible=True)).to_be_visible()
        self.assertEqual(self.page.evaluate('wizardWrites'), [])
        self.next(4)
        self.host.screenshot(path=str(self.shots / 'wizard-memory-mobile.png'))
        size = self.page.locator('.wizard-card').evaluate('(node)=>({width:node.getBoundingClientRect().width,viewport:innerWidth})')
        self.assertLessEqual(size['width'], size['viewport'])
        expect(self.page.get_by_role('combobox', name='生活记忆嵌入模型', exact=True).filter(visible=True)).to_have_value('embedding-test')

    def test_voice_generation_uses_the_visible_persona_mode_and_keeps_candidates_drafts(self):
        self.next()
        self.page.locator('.wizard-body select').select_option('plugin')
        self.page.locator('.wizard-body textarea').fill('向导里未保存的角色卡')
        self.next(4)
        self.page.locator('.wizard-body').get_by_role('button', name='生成候选…', exact=True).click()
        self.page.locator('#dialog-ok').click()
        expect(self.page.locator('#dialog')).to_be_hidden()
        call = self.page.evaluate('wizardWrites.find(row=>row.key==="voice-samples/generate")')
        self.assertEqual(call['data']['persona_mode'], 'plugin')
        self.assertEqual(call['data']['persona'], '向导里未保存的角色卡')
        self.next()
        self.finish()
        expect(self.page.locator('.wizard-backdrop')).to_have_count(0)
        persona = self.page.evaluate('wizardWorld.persona')
        self.assertEqual(persona['sample_candidates'][0]['text'], '夜里想喝点热茶。')
        self.assertEqual(persona['samples'], [])

    def test_voice_reply_for_a_different_session_is_not_added_to_the_draft(self):
        self.next(5)
        self.page.evaluate('wizardVoiceHold=true')
        self.page.locator('.wizard-body').get_by_role('button', name='生成候选…', exact=True).click()
        self.page.locator('#dialog-ok').click()
        self.page.wait_for_function('typeof wizardVoiceRelease==="function"')
        self.page.locator('#status-session').evaluate('(node,value)=>{node.value=value;node.dispatchEvent(new Event("change"));}', SECOND)
        self.page.evaluate('wizardVoiceRelease()')
        expect(self.page.locator('#dialog')).to_be_hidden()
        expect(self.page.locator('#toast')).to_contain_text('本次候选未加入')
        self.next()
        self.finish()
        expect(self.page.locator('.wizard-backdrop')).to_have_count(0)
        self.assertEqual(self.page.evaluate('wizardWorld.persona.sample_candidates || []'), [])
