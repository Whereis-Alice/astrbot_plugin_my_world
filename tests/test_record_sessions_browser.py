"""Record pages follow their own session, including out-of-order API replies."""
import unittest

from tests import test_generation_browser as generation
from tests.test_generation_browser import expect, sync_playwright


RECORD_BRIDGE = r"""(() => {
  const read = AstrBotPluginPage.apiGet;
  const sessions = {groups:[], sessions:[
    {session_id:'test:GroupMessage:1001',note:'合成会话甲',enabled:true},
    {session_id:'test:GroupMessage:2002',note:'合成会话乙',enabled:true}]};
  window.recordCalls=[]; window.held=[]; window.hold=''; window.recordWrites=[];
  window.recordReply=(key, p={}) => {
    const name=p.session?.endsWith('2002')?'乙':'甲';
    const id=name==='乙'?22:11;
    if(key==='memories') return {memories:[{id,session_id:p.session,content:`${name}的生活记忆`,type:'scene',scope:'group',node_id:'study',created_at:1760000000,weight:.6}]};
    if(key==='memories/stats') return {total:id,by_type:{scene:id},avg_weight:.6};
    if(key==='logs') return {events:[{id,event_type:p.type==='consolidate'?'consolidate':'action',text:`${name}的${p.type==='consolidate'?'整理':'动作'}记录`,world_time:id,created_at:1760000000,detail:{session:p.session}}],types:['action','consolidate']};
    if(key==='thoughts') return {thoughts:[{content:`${name}的心里话`,at:1760000000,node_name:'书房'}]};
    if(key==='prompt') return {prompt:`${name}的提示词`,chars:6};
    if(key==='ticks') return {entries:[{at:1760000000,count:2,sessions:[]}]};
    return null;
  };
  AstrBotPluginPage.apiGet=(key,params={})=>{
    recordCalls.push({key,params:structuredClone(params)});
    if(key==='config')return read(key,params).then(data=>({...data,sessions:structuredClone(sessions)}));
    if(key==='sessions')return Promise.resolve(structuredClone(sessions));
    const reply=recordReply(key,params);
    if(reply!==null){
      const match=hold===key || (hold==='consolidate' && key==='logs' && params.type==='consolidate');
      if(match && params.session?.endsWith('1001'))return new Promise((resolve,reject)=>held.push({key,params,resolve,reject}));
      return Promise.resolve(reply);
    }
    return read(key,params);
  };
  const post=AstrBotPluginPage.apiPost;
  AstrBotPluginPage.apiPost=(key,data)=>{recordWrites.push({key,data:structuredClone(data)});return post(key,data);};
  window.releaseRecords=(fail=false)=>{
    for(const entry of held.splice(0)){
      if(fail)entry.reject(new Error('甲的过期请求错误'));
      else entry.resolve(recordReply(entry.key,entry.params));
    }
  };
})();"""

FIRST = 'test:GroupMessage:1001'
SECOND = 'test:GroupMessage:2002'


@unittest.skipIf(sync_playwright is None, 'Install playwright for browser verification')
class RecordSessionsBrowserTests(unittest.TestCase):
    setUpClass = classmethod(generation.GenerationBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(generation.GenerationBrowserTests.tearDownClass.__func__)

    def setUp(self):
        generation.GenerationBrowserTests.setUp(self)
        self.host.emulate_media(reduced_motion='reduce')
        self.context.add_init_script(script=RECORD_BRIDGE)
        self.host.reload()
        self.page = self.host.frame(name='editor')
        expect(self.page.locator('#app')).to_be_visible()
        self.page.wait_for_function('recordCalls.some(x=>x.key==="logs")')

    def tearDown(self):
        self.assertEqual(self.errors, [])
        self.assertEqual(self.page.evaluate('recordWrites'), [])

    def open_page(self, tab):
        self.page.locator(f'#tabs [data-tab="{tab}"]').click()

    def release(self, fail=False):
        self.page.evaluate('(fail)=>{hold="";releaseRecords(fail);}', fail)
        self.page.wait_for_timeout(100)

    def test_memory_session_change_refreshes_all_sections_and_selection(self):
        self.open_page('memories')
        expect(self.page.locator('#memory-list')).to_contain_text('甲的生活记忆')
        self.page.locator('#memory-list input[type="checkbox"]').check()
        self.page.evaluate('recordCalls=[]')
        self.page.locator('#memory-session').select_option(SECOND)
        expect(self.page.locator('#memory-list')).to_contain_text('乙的生活记忆', timeout=1500)
        expect(self.page.locator('#memory-stats')).to_contain_text('总数 22')
        expect(self.page.locator('#memory-stats')).to_contain_text('乙的整理记录')
        self.assertEqual(self.page.locator('#memory-list input:checked').count(), 0)
        calls = self.page.evaluate('recordCalls.filter(x=>["memories","memories/stats","logs"].includes(x.key))')
        self.assertEqual({row['key'] for row in calls}, {'memories', 'memories/stats', 'logs'})
        self.assertTrue(all(row['params']['session'] == SECOND for row in calls))

    def test_memory_old_list_stats_and_consolidation_cannot_replace_new_session(self):
        self.open_page('memories')
        expect(self.page.locator('#memory-list')).to_contain_text('甲的生活记忆')
        for held in ('memories', 'memories/stats', 'consolidate'):
            with self.subTest(held=held):
                self.page.locator('#memory-session').select_option(SECOND)
                expect(self.page.locator('#memory-stats')).to_contain_text('乙的整理记录')
                self.page.evaluate('(key)=>hold=key', held)
                self.page.locator('#memory-session').select_option(FIRST)
                self.page.wait_for_function('held.length>0')
                expect(self.page.locator('#memory-list .memory-card')).to_have_count(0)
                expect(self.page.locator('#memory-stats')).not_to_contain_text('乙的整理记录')
                self.page.locator('#memory-session').select_option(SECOND)
                expect(self.page.locator('#memory-stats')).to_contain_text('乙的整理记录')
                self.release()
                expect(self.page.locator('#memory-list')).to_contain_text('乙的生活记忆')
                expect(self.page.locator('#memory-stats')).to_contain_text('总数 22')
                expect(self.page.locator('#memory-stats')).not_to_contain_text('甲')

    def test_memory_obsolete_error_cannot_replace_new_session(self):
        self.open_page('memories')
        expect(self.page.locator('#memory-list')).to_contain_text('甲的生活记忆')
        self.page.locator('#memory-session').select_option(SECOND)
        expect(self.page.locator('#memory-list')).to_contain_text('乙的生活记忆')
        self.page.evaluate('hold="memories"')
        self.page.locator('#memory-session').select_option(FIRST)
        self.page.wait_for_function('held.length>0')
        self.page.locator('#memory-session').select_option(SECOND)
        expect(self.page.locator('#memory-list')).to_contain_text('乙的生活记忆')
        self.release(fail=True)
        expect(self.page.locator('#memory-list')).to_contain_text('乙的生活记忆')
        expect(self.page.locator('#memory-list')).not_to_contain_text('错误')

    def test_log_switch_clears_detail_and_keeps_raw_data_in_event_detail(self):
        self.open_page('logs')
        expect(self.page.locator('#log-list')).to_contain_text('甲的动作记录')
        self.page.locator('#log-list .log-item').click()
        expect(self.page.locator('#log-detail')).to_contain_text('甲的动作记录')
        self.page.locator('#log-session').select_option(SECOND)
        expect(self.page.locator('#log-list')).to_contain_text('乙的动作记录')
        expect(self.page.locator('#log-detail')).not_to_contain_text('甲', timeout=1500)
        self.assertEqual(self.page.locator('#status-logs').count(), 0)
        self.page.locator('#log-list .log-item').click()
        self.page.locator('#log-detail details summary').click()
        expect(self.page.locator('#log-detail pre')).to_contain_text(SECOND)

    def test_log_old_success_and_error_cannot_replace_new_session(self):
        self.open_page('logs')
        expect(self.page.locator('#log-list')).to_contain_text('甲的动作记录')
        for fail in (False, True):
            with self.subTest(fail=fail):
                self.page.locator('#log-session').select_option(SECOND)
                expect(self.page.locator('#log-list')).to_contain_text('乙的动作记录')
                self.page.evaluate('hold="logs"')
                self.page.locator('#log-session').select_option(FIRST)
                self.page.wait_for_function('held.length>0')
                expect(self.page.locator('#log-list .log-item')).to_have_count(0)
                self.page.locator('#log-session').select_option(SECOND)
                expect(self.page.locator('#log-list')).to_contain_text('乙的动作记录')
                self.release(fail)
                expect(self.page.locator('#log-list')).to_contain_text('乙的动作记录')
                expect(self.page.locator('#log-list')).not_to_contain_text('甲')

    def test_log_refresh_keeps_open_detail_until_it_leaves_the_results(self):
        self.open_page('logs')
        expect(self.page.locator('#log-list')).to_contain_text('甲的动作记录')
        self.page.locator('#log-list .log-item').click()
        self.page.locator('#log-detail details summary').click()
        self.page.locator('#log-refresh').click()
        expect(self.page.locator('#log-list .log-item.selected')).to_have_count(1)
        expect(self.page.locator('#log-detail pre')).to_be_visible()
        self.page.evaluate('''() => {
          const read = AstrBotPluginPage.apiGet;
          AstrBotPluginPage.apiGet=(key,params)=>key==='logs' && params.type==='consolidate'
            ? Promise.resolve({events:[],types:['action','consolidate']}) : read(key,params);
        }''')
        self.page.locator('#log-type').select_option('consolidate')
        expect(self.page.locator('#log-list')).to_contain_text('没有符合条件的记录')
        expect(self.page.locator('#log-detail')).not_to_contain_text('甲')

    def test_thoughts_own_picker_ignores_console_session_and_old_reply(self):
        self.open_page('thoughts')
        expect(self.page.locator('#thoughts-list')).to_contain_text('甲的心里话')
        self.page.locator('#status-session').evaluate('(node,value)=>{node.value=value;node.dispatchEvent(new Event("change"));}', SECOND)
        expect(self.page.locator('#thoughts-list')).to_contain_text('甲的心里话')
        self.page.locator('#thought-session').select_option(SECOND)
        expect(self.page.locator('#thoughts-list')).to_contain_text('乙的心里话')
        self.page.evaluate('hold="thoughts"')
        self.page.locator('#thought-session').select_option(FIRST)
        self.page.wait_for_function('held.length>0')
        self.page.locator('#thought-session').select_option(SECOND)
        expect(self.page.locator('#thoughts-list')).to_contain_text('乙的心里话')
        self.release()
        expect(self.page.locator('#thoughts-list')).to_contain_text('乙的心里话')

    def test_speech_decision_details_show_intent_and_actual_partial_delivery(self):
        self.page.evaluate("""() => {
          const original=recordReply;
          window.recordReply=(key,params)=>{
            const data=original(key,params);
            const speech={id:'synthetic-decision',trace_id:'synthetic-model',trigger:'completion',
              intent:'continue_chat',status:'partial',reply_to:'m42',target:params.session};
            if(key==='thoughts')data.thoughts[0].detail={env:'书房',speech};
            if(key==='logs')data.events[0].detail={decisions:[speech]};
            return data;
          };
        }""")
        self.open_page('thoughts')
        expect(self.page.locator('#thoughts-list')).to_contain_text('继续之前的对话')
        expect(self.page.locator('#thoughts-list')).to_contain_text('部分发送')
        self.host.set_viewport_size({'width': 390, 'height': 900})
        self.page.locator('#thoughts-list').scroll_into_view_if_needed()
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'))
        self.host.screenshot(path=str(self.shots / 'speech-decision-mobile.png'), animations='disabled')
        self.open_page('logs')
        self.page.locator('#log-list .log-item').first.click()
        expect(self.page.locator('#log-detail')).to_contain_text('继续之前的对话')
        expect(self.page.locator('#log-detail')).to_contain_text('部分发送')

    def test_record_and_global_pages_hide_only_the_unused_top_session(self):
        for width in (1440, 390):
            self.host.set_viewport_size({'width': width, 'height': 1000})
            for tab in ('thoughts', 'memories', 'logs', 'ticks', 'debug', 'actions', 'sessions', 'presets'):
                with self.subTest(width=width, tab=tab):
                    self.open_page(tab)
                    if width == 390:
                        self.page.locator('.topbar-context').evaluate('node=>node.open=true')
                    expect(self.page.locator('.field-session')).not_to_be_visible(timeout=1000)
                    expect(self.page.locator('#config-scope')).to_be_visible()
                    expect(self.page.locator('#proactive-enabled')).to_be_visible()
                    expect(self.page.locator('#save')).to_be_visible()
            for tab in ('status', 'life', 'settings'):
                self.open_page(tab)
                if width == 390:
                    self.page.locator('.topbar-context').evaluate('node=>node.open=true')
                expect(self.page.locator('#status-session')).to_be_visible()
            if width == 390:
                self.open_page('memories')
                self.page.locator('.topbar-context > summary').click()
                expect(self.page.locator('.topbar-context > summary')).to_have_text('世界设置')
                self.page.locator('#config-bar-toggle').click()
                expect(self.page.locator('#scope-note')).to_contain_text('页内')
                self.host.screenshot(path=str(self.shots / 'record-sessions-mobile.png'), animations='disabled')

    def test_empty_session_clears_memory_stats_log_details_and_pending_replies(self):
        for tab, select, list_id, details in (
            ('memories', 'memory-session', 'memory-list', 'memory-stats'),
            ('logs', 'log-session', 'log-list', 'log-detail'),
        ):
            with self.subTest(tab=tab):
                self.open_page(tab)
                expect(self.page.locator(f'#{list_id}')).to_contain_text('甲')
                self.page.locator(f'#{select}').evaluate('node=>{node.replaceChildren();node.dispatchEvent(new Event("change"));}')
                expect(self.page.locator(f'#{list_id}')).to_contain_text('还没有白名单会话')
                expect(self.page.locator(f'#{details}')).not_to_contain_text('甲')

    def test_debug_session_change_clears_old_preview_and_rejects_old_reply(self):
        self.open_page('debug')
        self.page.locator('#debug-auto').click()
        expect(self.page.locator('#debug-output')).to_contain_text('甲的提示词')
        self.page.locator('#debug-session').select_option(SECOND)
        expect(self.page.locator('#debug-output')).not_to_contain_text('甲', timeout=1500)
        expect(self.page.locator('#debug-summary')).to_have_text('')
        self.page.locator('#debug-session').select_option(FIRST)
        self.page.evaluate('hold="prompt"')
        self.page.locator('#debug-auto').click()
        self.page.wait_for_function('held.length>0')
        self.page.locator('#debug-session').select_option(SECOND)
        self.page.locator('#debug-auto').click()
        expect(self.page.locator('#debug-output')).to_contain_text('乙的提示词')
        self.release()
        expect(self.page.locator('#debug-output')).to_contain_text('乙的提示词')

    def test_unavailable_consolidation_keeps_current_memory_list_and_stats(self):
        self.page.evaluate('''() => {
          const read = AstrBotPluginPage.apiGet;
          AstrBotPluginPage.apiGet=(key,params)=>key==='logs' && params.type==='consolidate'
            ? Promise.reject(new Error('整理记录暂时不可用')) : read(key,params);
        }''')
        self.open_page('memories')
        expect(self.page.locator('#memory-list')).to_contain_text('甲的生活记忆')
        expect(self.page.locator('#memory-stats')).to_contain_text('总数 11')
        expect(self.page.locator('#memory-stats')).to_contain_text('上次整理：读不到')

    def test_ticks_are_global_and_top_session_does_not_filter_them(self):
        self.open_page('ticks')
        self.page.evaluate('recordCalls=[]')
        self.page.locator('#ticks-refresh').click()
        calls = self.page.evaluate('recordCalls.filter(x=>x.key==="ticks")')
        self.assertEqual(len(calls), 1)
        self.assertNotIn('session', calls[0]['params'])
        expect(self.page.locator('#ticks-list')).to_contain_text('2 个会话在跑')
