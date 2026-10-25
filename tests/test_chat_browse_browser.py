"""The quiet-reading controls and source cards, using synthetic data only."""
import unittest

from tests import test_life_browser as life
from tests.test_generation_browser import expect, sync_playwright


@unittest.skipIf(sync_playwright is None, "Install playwright for browser verification")
class ChatBrowseBrowserTests(unittest.TestCase):
    setUpClass = classmethod(life.LifeBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(life.LifeBrowserTests.tearDownClass.__func__)
    install_console = life.LifeBrowserTests.install_console
    def setUp(self):
        life.generation.GenerationBrowserTests.setUp(self)
        self.host.emulate_media(reduced_motion='reduce')
        self.context.add_init_script(script="""(() => {
          const read = AstrBotPluginPage.apiGet;
          const sessions={groups:[],sessions:[{session_id:'aiocqhttp:GroupMessage:1001',type:'group',note:'午后茶室',enabled:true}]};
          AstrBotPluginPage.apiGet=async(key,params)=>{
            const data=await read(key,params);
            if(key==='config')data.sessions=structuredClone(sessions);
            return key==='sessions'?structuredClone(sessions):data;
          };
        })();""")
        self.host.reload()
        self.page = self.host.frame(name='editor')
        expect(self.page.locator('#app')).to_be_visible()
        self.install_console()
        self.page.locator('button[data-tab="life"]').click()

    def settings(self):
        self.page.get_by_role('button', name='手机与电脑', exact=True).click()
        expect(self.page.locator('#dialog-title')).to_have_text('手机与电脑')

    def test_both_categories_can_filter_old_dates_before_expanding_rows(self):
        self.page.evaluate("""() => {
          const read = AstrBotPluginPage.apiGet;
          window.historyReads = [];
          AstrBotPluginPage.apiGet = (key, params) => {
            if (key !== 'thoughts') return read(key, params);
            historyReads.push(params);
            const dated = (content, date, browse = false) => ({content, date, at: 1792857600,
              source: browse ? '翻消息时' : '聊完一轮', ...(browse ? {browse_session: params.session} : {})});
            return Promise.resolve({thoughts: [
              ...Array.from({length: 60}, (_, i) => dated(`当天心里话${i}`, '2026-10-26')),
              ...Array.from({length: 45}, (_, i) => dated(`昨天心里话${i}`, '2026-10-25')),
              dated('前天翻消息的想法', '2026-10-24', true),
              {content: '老记录没有时间', source: '聊完一轮'}
            ]});
          };
        }""")
        self.page.locator('button[data-tab="thoughts"]').click()
        date = self.page.get_by_label('内心活动日期', exact=True)
        expect(date).to_have_value('')
        self.assertEqual(self.page.evaluate('historyReads.at(-1).limit'), 3000)
        date.select_option('2026-10-25')
        expect(self.page.locator('.thought-row')).to_have_count(40)
        expect(self.page.locator('#thoughts-list')).to_contain_text('昨天心里话0')
        expect(self.page.locator('#thoughts-list')).not_to_contain_text('当天心里话')
        self.page.locator('.thought-more').click()
        expect(self.page.locator('.thought-row')).to_have_count(45)
        date.select_option('undated')
        expect(self.page.locator('.thought-row')).to_have_count(1)
        expect(self.page.locator('#thoughts-list')).to_contain_text('老记录没有时间')
        self.page.get_by_role('tab', name='翻消息时', exact=True).click()
        date.select_option('2026-10-24')
        expect(self.page.locator('.thought-row')).to_have_count(1)
        expect(self.page.locator('#thoughts-list')).to_contain_text('前天翻消息的想法')
        expect(self.page.locator('#thoughts-hint')).not_to_contain_text('保留最近 24 小时')
        self.host.set_viewport_size({'width': 390, 'height': 844})
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'))
        date.select_option('')
        expect(self.page.locator('.thought-row')).to_have_count(1)
        self.assertEqual(self.errors, [])

    def test_saved_reply_gate_is_displayed_as_history_instead_of_live_progress(self):
        self.page.evaluate("""() => {
          const read = AstrBotPluginPage.apiGet;
          AstrBotPluginPage.apiGet=(key,params)=>key==='thoughts'?Promise.resolve({
            session:params.session,thoughts:[{at:Date.now()/1000-3600,source:'翻消息时',
              content:'读过后想继续整理桌子。',browse_session:params.session,
              detail:{browse_reply:{status:'reply_pending',reason:'正在处理对话回复，暂未进入接话判断'}}}]
          }):read(key,params);
        }""")
        self.page.locator('button[data-tab="thoughts"]').click()
        self.page.get_by_role('tab', name='翻消息时', exact=True).click()
        expect(self.page.locator('#thoughts-list')).to_contain_text('本次未进入接话判断：当时有对话等待回复或正在处理')
        expect(self.page.locator('#thoughts-list')).not_to_contain_text('正在处理对话回复，暂未进入接话判断')
        self.assertEqual(self.errors, [])

    def test_platform_names_fill_record_selectors_and_manual_notes_still_win(self):
        self.context.add_init_script(script="""(() => {
          const read = AstrBotPluginPage.apiGet;
          AstrBotPluginPage.apiGet=async(key,params)=>{
            const data=await read(key,params);
            if(key==='config'){
              data.sessions.sessions[0].note=localStorage.getItem('qa-session-note')||'';
              data.session_names={'aiocqhttp:GroupMessage:1001':'真正的群名称'};
            }
            if(key==='sessions')data.sessions[0].note=localStorage.getItem('qa-session-note')||'';
            return data;
          };
        })();""")
        self.host.reload()
        self.page = self.host.frame(name='editor')
        for select in ('thought-session', 'memory-session', 'log-session', 'contacts-session'):
            expect(self.page.locator(f'#{select} option').first).to_have_text('真正的群名称 · 1001')
        self.page.evaluate("localStorage.setItem('qa-session-note','我写的备注')")
        self.host.reload()
        self.page = self.host.frame(name='editor')
        expect(self.page.locator('#thought-session option').first).to_have_text('我写的备注 · 1001')
        self.assertEqual(self.errors, [])

    def test_devices_source_range_and_budget_save_together(self):
        self.page.evaluate("""() => {
          const post = AstrBotPluginPage.apiPost; window.browseWrites=[];
          AstrBotPluginPage.apiPost=(key,data)=>{
            if(key==='config/world')browseWrites.push(structuredClone(data.world));
            return post(key,data);
          };
        }""")
        self.settings()
        expect(self.page.get_by_label('空闲时可以安静翻消息', exact=True)).not_to_be_checked()
        expect(self.page.get_by_label('她有一部手机（随身可用）', exact=True)).not_to_be_checked()
        self.page.get_by_label('空闲时可以安静翻消息', exact=True).check()
        self.page.get_by_label('她有一部手机（随身可用）', exact=True).check()
        expect(self.page.get_by_label('浏览后允许她判断是否接话', exact=True)).not_to_be_checked()
        self.page.get_by_label('浏览后允许她判断是否接话', exact=True).check()
        expect(self.page.get_by_label('短暂忙碌最多等待（秒）', exact=True)).to_have_value('120')
        self.page.get_by_label('短暂忙碌最多等待（秒）', exact=True).fill('45')
        expect(self.page.get_by_label('阅读时参考已有关系与兴趣简述', exact=True)).to_be_checked()
        self.page.get_by_label('阅读时参考已有关系与兴趣简述', exact=True).uncheck()
        computers = self.page.get_by_role('group', name='哪些地点有电脑', exact=True).get_by_role('checkbox')
        computers.first.check()
        computer = computers.first.input_value()
        sources = self.page.get_by_role('group', name='可以翻阅哪些会话', exact=True).get_by_role('checkbox')
        sources.first.check()
        source = sources.first.input_value()
        daily = self.page.get_by_label('每天最多浏览次数', exact=True)
        expect(daily).to_have_attribute('max', '100')
        daily.fill('100')
        expect(self.page.get_by_label('每次最多读取条数', exact=True)).to_have_value('500')
        self.page.get_by_label('每次最多读取条数', exact=True).fill('1000')
        self.page.get_by_label('每次正文最多字数', exact=True).fill('1800')
        self.page.get_by_role('button', name='应用设置', exact=True).click()
        self.page.locator('#save').click()
        self.page.wait_for_function('browseWrites.length>0')
        config = self.page.evaluate('browseWrites.at(-1).chat_browse')
        self.assertTrue(config['enabled'] and config['phone'])
        self.assertTrue(config['reply_enabled'])
        self.assertEqual(config['reply_wait_seconds'], 45)
        self.assertFalse(config['relationship_context'])
        self.assertEqual(config['computer_nodes'], [computer])
        self.assertEqual(config['sessions'], [source])
        self.assertEqual((config['daily_max'], config['max_chars']), (100, 1800))
        self.assertEqual(config['max_messages'], 1000)
        self.assertEqual(self.errors, [])

    def test_cancel_keeps_original_device_settings(self):
        self.settings()
        self.page.get_by_label('她有一部手机（随身可用）', exact=True).check()
        self.page.get_by_label('浏览后允许她判断是否接话', exact=True).check()
        self.page.locator('#dialog-cancel').click()
        self.settings()
        expect(self.page.get_by_label('她有一部手机（随身可用）', exact=True)).not_to_be_checked()
        expect(self.page.get_by_label('浏览后允许她判断是否接话', exact=True)).not_to_be_checked()

    def test_daily_limit_validation_and_legacy_reading_explanation(self):
        self.settings()
        self.page.get_by_label('每天最多浏览次数', exact=True).fill('101')
        self.page.locator('#dialog-ok').click()
        expect(self.page.locator('#dialog-error')).to_contain_text('1–100')
        self.page.locator('#dialog-cancel').click()
        self.page.evaluate("""() => {
          const read = AstrBotPluginPage.apiGet;
          AstrBotPluginPage.apiGet=(key,params)=>key==='thoughts'?Promise.resolve({thoughts:[],
            browse:{started_today:4,daily_max:100,last_read_at:Date.now()/1000,last_result:'quiet',
              reply_enabled:true,reply_allowed:true,last_error_at:Date.now()/1000,last_error:'模型返回了空想法'}}):read(key,params);
        }""")
        self.page.locator('button[data-tab="thoughts"]').click()
        self.page.get_by_role('tab', name='翻消息时', exact=True).click()
        expect(self.page.locator('#thoughts-browse-status')).to_contain_text('新版成功读完必留想法')
        expect(self.page.locator('#thoughts-browse-status')).to_contain_text('由她决定是否接话')
        expect(self.page.locator('#thoughts-browse-status')).to_contain_text('模型返回了空想法')
        expect(self.page.locator('#thoughts-browse-status')).not_to_contain_text('这次读过了，没有特别感想')
        self.assertEqual(self.errors, [])
        self.assertEqual(self.errors, [])

    def test_reading_feelings_have_collapsible_safe_evidence_and_responsive_layout(self):
        self.page.evaluate("""() => {
          const read = AstrBotPluginPage.apiGet;
          AstrBotPluginPage.apiGet=(key,params)=>key==='thoughts'?Promise.resolve({session:params.session,
          browse:{reason:'等待最短浏览间隔结束',started_today:2,daily_max:10,cooldown_minutes:120,
            next_at:Date.now()/1000+3600,last_read_at:Date.now()/1000-3600,last_result:'feeling',
            reply_enabled:false,reply_allowed:false,note:'空闲、有未读消息和可用设备时才浏览。'},
          thoughts:[{at:Date.now()/1000,source:'聊完后',content:'下次再问问那盆花长得怎么样。'}, {
            at:Date.now()/1000,node_name:'窗边',source:'翻消息时',browse_session:'qq:GroupMessage:1001',browse_label:'午后茶室 · 群 1001',
            content:'听他们聊起窗边的花，也想在自己的窗边慢慢养一盆。',
            detail:{env:'窗边',state:'browsing',mood:'平静',intent:'安静看看，然后继续自己的生活'},
            browse_sources:[{name:'小明',at:Date.now()/1000,text:'今天窗边的小花开了。<img src=x onerror="window.badBrowse=1">'}]
          }]}):read(key,params);
        }""")
        self.page.locator('button[data-tab="thoughts"]').click()
        expect(self.page.locator('.thought-row')).to_have_count(1)
        expect(self.page.locator('.thought-row')).to_contain_text('下次再问问')
        expect(self.page.locator('#thoughts-browse-status')).to_be_hidden()
        self.page.get_by_role('tab', name='翻消息时', exact=True).click()
        expect(self.page.locator('.thought-row')).to_have_count(1)
        expect(self.page.locator('.thought-row')).to_contain_text('午后茶室 · 群 1001')
        expect(self.page.locator('.thought-row')).to_contain_text('翻消息中')
        expect(self.page.locator('#thoughts-browse-status')).to_contain_text('2 / 10')
        expect(self.page.locator('#thoughts-browse-status')).to_contain_text('至少间隔 120 分钟')
        self.page.get_by_role('button', name='浏览设置', exact=True).click()
        expect(self.page.locator('#dialog-title')).to_have_text('手机与电脑')
        self.page.locator('#dialog-cancel').click()
        expect(self.page.locator('.thought-browse-sources')).not_to_have_attribute('open', '')
        self.page.locator('.thought-browse-sources summary').click()
        expect(self.page.locator('.thought-browse-sources p')).to_contain_text('<img')
        expect(self.page.locator('.thought-browse-sources img')).to_have_count(0)
        for width,height,palette in [(1440,1000,'nocturne'), (390,844,'nocturne'), (1440,1000,'cloud'), (390,844,'cloud')]:
            self.host.set_viewport_size({'width':width,'height':height})
            self.page.evaluate("p=>{document.documentElement.dataset.palette=p;document.documentElement.dataset.theme=p==='cloud'?'light':'dark'}", palette)
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'))
            self.host.screenshot(path=str(self.shots/f'browse-thought-{palette}-{width}.png'))
        self.assertIsNone(self.page.evaluate('window.badBrowse'))
        self.assertEqual(self.errors, [])

    def test_clear_only_current_thought_category_and_keyboard_tabs(self):
        self.page.evaluate("""() => {
          const post = AstrBotPluginPage.apiPost; window.clearedThoughts=[];
          AstrBotPluginPage.apiPost=(key,data)=>{
            if(key==='thoughts/clear'){clearedThoughts.push(structuredClone(data));return Promise.resolve({ok:true,removed:1});}
            return post(key,data);
          };
        }""")
        self.page.locator('button[data-tab="thoughts"]').click()
        self.page.get_by_role('tab', name='心里的话', exact=True).press('End')
        expect(self.page.get_by_role('tab', name='翻消息时', exact=True)).to_have_attribute('aria-selected', 'true')
        self.page.locator('#thoughts-clear').click()
        self.page.locator('#dialog-ok').click()
        self.page.wait_for_function('clearedThoughts.length===1')
        self.assertEqual(self.page.evaluate('clearedThoughts[0].category'), 'browse')
        self.page.get_by_role('tab', name='翻消息时', exact=True).press('Home')
        self.page.locator('#thoughts-clear').click()
        self.page.locator('#dialog-ok').click()
        self.page.wait_for_function('clearedThoughts.length===2')
        self.assertEqual(self.page.evaluate('clearedThoughts[1].category'), 'inner')
        self.assertEqual(self.errors, [])

    def test_settings_dark_light_and_phone_fit_dialog(self):
        for width,height,palette in [(1440,1000,'nocturne'), (390,844,'nocturne'), (1440,1000,'cloud'), (390,844,'cloud')]:
            self.host.set_viewport_size({'width':width,'height':height})
            self.page.evaluate("p=>{document.documentElement.dataset.palette=p;document.documentElement.dataset.theme=p==='cloud'?'light':'dark'}", palette)
            self.host.screenshot(path=str(self.shots/f'browse-entry-{palette}-{width}.png'))
            self.settings()
            expect(self.page.get_by_label('每次最多读取条数', exact=True)).to_have_value('500')
            self.assertTrue(self.page.locator('#dialog').evaluate('(e)=>e.scrollWidth<=e.clientWidth+1'))
            self.assertTrue(self.page.locator('#dialog').evaluate('(e)=>{const r=e.getBoundingClientRect();return r.top>=0&&r.bottom<=innerHeight+1}'))
            self.host.screenshot(path=str(self.shots/f'browse-settings-{palette}-{width}.png'))
            self.page.locator('#dialog-cancel').click()

    def test_reading_intents_distinguish_real_choice_missing_data_and_skipped_judgement(self):
        self.page.evaluate("""() => {
          const read = AstrBotPluginPage.apiGet;
          AstrBotPluginPage.apiGet=(key,params)=>key==='thoughts'?Promise.resolve({session:params.session,
          browse:{reply_enabled:true,reply_allowed:true},thoughts:[
            {at:Date.now()/1000,source:'翻消息时',browse_session:params.session,content:'旧的阅读感想',
             detail:{intent:'安静看看，然后继续自己的生活'}},
            {at:Date.now()/1000,source:'翻消息时',browse_session:params.session,content:'想问花的情况',
             detail:{intent:'安静看看，然后继续自己的生活',speech:{intent:'join_chat',status:'sent',motivation:'想问问花是不是晒得太久。'}}},
            {at:Date.now()/1000,source:'翻消息时',browse_session:params.session,content:'有点惦记自己的窗边',
             detail:{intent:'先看看自己的花。',browse_reply:{status:'no_scene',reason:'最近15分钟没有仍可回应的消息'}}},
            {at:Date.now()/1000,source:'翻消息时',browse_session:params.session,content:'现在并不想插话',
             detail:{intent:'安静看看，然后继续自己的生活',speech:{intent:'listen',status:'listening',motivation:'他们已经商量好了，先听着。'}}}
          ]}):read(key,params);
        }""")
        self.page.locator('button[data-tab="thoughts"]').click()
        self.page.get_by_role('tab', name='翻消息时', exact=True).click()
        expect(self.page.locator('.thought-row')).to_have_count(4)
        expect(self.page.locator('.thought-row').filter(has_text='旧的阅读感想').locator('.thought-intent')).to_have_text('打算：阅读时未记录具体打算')
        expect(self.page.locator('.thought-row').filter(has_text='想问花的情况').locator('.thought-intent')).to_have_text('打算：想问问花是不是晒得太久。')
        expect(self.page.locator('.thought-row').filter(has_text='有点惦记自己的窗边')).to_contain_text('接话：最近15分钟没有仍可回应的消息')
        expect(self.page.locator('.thought-row').filter(has_text='有点惦记自己的窗边').locator('.thought-intent')).to_have_text('打算：先看看自己的花。')
        expect(self.page.locator('.thought-row').filter(has_text='现在并不想插话').locator('.thought-intent')).to_have_text('打算：他们已经商量好了，先听着。')
        expect(self.page.locator('#thoughts-list')).not_to_contain_text('安静看看，然后继续自己的生活')
        self.assertEqual(self.errors, [])
        self.assertEqual(self.errors, [])

    def test_entry_is_available_in_every_life_view_and_restore_only_changes_reading(self):
        for view in ('today', 'concerns', 'experiences'):
            self.page.locator(f'[data-life-view="{view}"][role="tab"]').click()
            self.settings()
            self.page.locator('#dialog-cancel').click()
        self.settings()
        self.page.get_by_label('每天最多浏览次数', exact=True).fill('2')
        self.page.get_by_label('短暂忙碌最多等待（秒）', exact=True).fill('0')
        self.page.get_by_label('阅读时参考已有关系与兴趣简述', exact=True).uncheck()
        self.page.get_by_label('每次最多读取条数', exact=True).fill('1001')
        self.page.locator('#dialog-ok').click()
        expect(self.page.locator('#dialog-error')).to_contain_text('1–1000')
        self.page.get_by_role('button', name='恢复推荐量', exact=True).click()
        expect(self.page.get_by_label('每次最多读取条数', exact=True)).to_have_value('500')
        expect(self.page.get_by_label('每次正文最多字数', exact=True)).to_have_value('60000')
        expect(self.page.get_by_label('消息范围（小时）', exact=True)).to_have_value('12')
        expect(self.page.get_by_label('每天最多浏览次数', exact=True)).to_have_value('2')
        expect(self.page.get_by_label('短暂忙碌最多等待（秒）', exact=True)).to_have_value('0')
        expect(self.page.get_by_label('阅读时参考已有关系与兴趣简述', exact=True)).not_to_be_checked()
        self.assertEqual(self.errors, [])
