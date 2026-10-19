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
        computers = self.page.get_by_role('group', name='哪些地点有电脑', exact=True).get_by_role('checkbox')
        computers.first.check()
        computer = computers.first.input_value()
        sources = self.page.get_by_role('group', name='可以翻阅哪些会话', exact=True).get_by_role('checkbox')
        sources.first.check()
        source = sources.first.input_value()
        self.page.get_by_label('每天最多浏览次数', exact=True).fill('2')
        expect(self.page.get_by_label('每次最多读取条数', exact=True)).to_have_value('500')
        self.page.get_by_label('每次最多读取条数', exact=True).fill('1000')
        self.page.get_by_label('每次正文最多字数', exact=True).fill('1800')
        self.page.get_by_role('button', name='应用设置', exact=True).click()
        self.page.locator('#save').click()
        self.page.wait_for_function('browseWrites.length>0')
        config = self.page.evaluate('browseWrites.at(-1).chat_browse')
        self.assertTrue(config['enabled'] and config['phone'])
        self.assertEqual(config['computer_nodes'], [computer])
        self.assertEqual(config['sessions'], [source])
        self.assertEqual((config['daily_max'], config['max_chars']), (2, 1800))
        self.assertEqual(config['max_messages'], 1000)
        self.assertEqual(self.errors, [])

    def test_cancel_keeps_original_device_settings(self):
        self.settings()
        self.page.get_by_label('她有一部手机（随身可用）', exact=True).check()
        self.page.locator('#dialog-cancel').click()
        self.settings()
        expect(self.page.get_by_label('她有一部手机（随身可用）', exact=True)).not_to_be_checked()
        self.assertEqual(self.errors, [])

    def test_reading_feelings_have_collapsible_safe_evidence_and_responsive_layout(self):
        self.page.evaluate("""() => {
          const read = AstrBotPluginPage.apiGet;
          AstrBotPluginPage.apiGet=(key,params)=>key==='thoughts'?Promise.resolve({session:params.session,thoughts:[{
            at:Date.now()/1000,node_name:'窗边',source:'翻消息时',browse_session:'qq:GroupMessage:1001',browse_label:'午后茶室 · 群 1001',
            content:'听他们聊起窗边的花，也想在自己的窗边慢慢养一盆。',
            detail:{env:'窗边',state:'browsing',mood:'平静',intent:'安静看看，然后继续自己的生活'},
            browse_sources:[{name:'小明',at:Date.now()/1000,text:'今天窗边的小花开了。<img src=x onerror="window.badBrowse=1">'}]
          }]}):read(key,params);
        }""")
        self.page.locator('button[data-tab="thoughts"]').click()
        expect(self.page.locator('.thought-row')).to_have_count(1)
        expect(self.page.locator('.thought-row')).to_contain_text('午后茶室 · 群 1001')
        expect(self.page.locator('.thought-row')).to_contain_text('翻消息中')
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
        self.assertEqual(self.errors, [])

    def test_entry_is_available_in_every_life_view_and_restore_only_changes_reading(self):
        for view in ('today', 'concerns', 'experiences'):
            self.page.locator(f'[data-life-view="{view}"][role="tab"]').click()
            self.settings()
            self.page.locator('#dialog-cancel').click()
        self.settings()
        self.page.get_by_label('每天最多浏览次数', exact=True).fill('2')
        self.page.get_by_label('每次最多读取条数', exact=True).fill('1001')
        self.page.locator('#dialog-ok').click()
        expect(self.page.locator('#dialog-error')).to_contain_text('1–1000')
        self.page.get_by_role('button', name='恢复推荐量', exact=True).click()
        expect(self.page.get_by_label('每次最多读取条数', exact=True)).to_have_value('500')
        expect(self.page.get_by_label('每次正文最多字数', exact=True)).to_have_value('60000')
        expect(self.page.get_by_label('消息范围（小时）', exact=True)).to_have_value('12')
        expect(self.page.get_by_label('每天最多浏览次数', exact=True)).to_have_value('2')
        self.assertEqual(self.errors, [])
