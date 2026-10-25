"""The participation controls must be readable and editable in the existing theme."""
import unittest

from tests import test_generation_browser as generation
from tests.test_generation_browser import expect, sync_playwright


@unittest.skipIf(sync_playwright is None, "Install playwright for visual acceptance")
class ParticipationBrowserTests(unittest.TestCase):
    setUpClass = classmethod(generation.GenerationBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(generation.GenerationBrowserTests.tearDownClass.__func__)
    setUp = generation.GenerationBrowserTests.setUp

    def test_cadence_media_and_peer_controls_save_and_fit_the_existing_settings(self):
        self.page.locator('button[data-tab="settings"]').click()
        self.page.locator('#settings-tabs').get_by_text('自主发言', exact=True).click()
        self.page.get_by_text('续聊节奏与机器人互聊', exact=True).click()
        self.page.evaluate("""() => {
          const post=AstrBotPluginPage.apiPost;window.cadenceWrites=[];
          AstrBotPluginPage.apiPost=(key,data)=>{
            if(key==='config/world')cadenceWrites.push(structuredClone(data.world));
            return post(key,data);
          };
        }""")
        values = [
            ('续聊观察间隔（秒）','30'), ('续聊优先回合数','3'),
            ('续聊统计窗口（秒）','900'), ('机器人之间最多连续接话','2'),
            ('机器人互聊后留白（秒）','360'),
        ]
        for label,value in values:
            field=self.page.get_by_text(label,exact=True).locator('xpath=ancestor::label[1]').locator('input')
            field.fill(value)
            field.dispatch_event('change')
        peers=self.page.get_by_text('其他机器人账号（可选）',exact=True).locator('xpath=ancestor::label[1]').locator('textarea')
        peers.fill('10000001\n10000001\naiocqhttp:10000002')
        peers.dispatch_event('change')
        media=self.page.get_by_text('无文字图片与表情接话',exact=True).locator('xpath=ancestor::label[1]').locator('select')
        media.select_option('directed')
        self.page.locator('#save').click()
        self.page.wait_for_function('cadenceWrites.length===1')
        config=self.page.evaluate('cadenceWrites[0].decider')
        self.assertEqual(config['continuation_interval_seconds'],30)
        self.assertEqual(config['continuation_priority_turns'],3)
        self.assertEqual(config['continuation_window_seconds'],900)
        self.assertEqual(config['peer_chat_max_turns'],2)
        self.assertEqual(config['peer_chat_pause_seconds'],360)
        self.assertEqual(config['peer_bot_ids'],['10000001','aiocqhttp:10000002'])
        self.assertEqual(config['media_only_participation'],'directed')
        self.page.locator('button[data-tab="life"]').click()
        self.page.locator('button[data-tab="settings"]').click()
        self.page.locator('#settings-tabs').get_by_text('自主发言', exact=True).click()
        self.page.get_by_text('续聊节奏与机器人互聊', exact=True).click()
        for label,value in values:
            expect(self.page.get_by_text(label,exact=True).locator('xpath=ancestor::label[1]').locator('input')).to_have_value(value)
        for width,height,palette in [(1440,1000,'cloud'),(1440,1000,'nocturne'),(390,844,'nocturne')]:
            self.host.set_viewport_size({'width':width,'height':height})
            self.page.evaluate("p=>{document.documentElement.dataset.palette=p;document.documentElement.dataset.theme=p==='cloud'?'light':'dark'}",palette)
            self.page.get_by_text('续聊观察间隔（秒）',exact=True).scroll_into_view_if_needed()
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'))
            self.host.screenshot(path=str(self.shots/f'cadence-{palette}-{width}.png'),animations='disabled')
        self.assertEqual(self.errors,[])

    def test_autonomous_thought_shows_its_actual_speech_in_the_existing_card(self):
        self.page.evaluate("""() => {
          const select=document.getElementById('thought-session');
          select.innerHTML='<option value="aiocqhttp:GroupMessage:1001">测试群</option>';
          select.value='aiocqhttp:GroupMessage:1001';
          const read=AstrBotPluginPage.apiGet;
          AstrBotPluginPage.apiGet=(key,params)=>key==='thoughts'?Promise.resolve({
            session:'aiocqhttp:GroupMessage:1001',thoughts:[{
              content:'真是的，拿着我的照片瞎扯什么啊……',source:'自主决定',
              at:Date.now()/1000,node_name:'艾拉的房间',reply:'大半夜的，都去休息吧。',
              detail:{intent:'说一句，然后去充电。',speech:{trigger:'plan',status:'sent',
                target:'aiocqhttp:GroupMessage:1001',sent:['大半夜的，都去休息吧。']}}
            }]}):read(key,params);
        }""")
        self.page.locator('button[data-tab="thoughts"]').click()
        expect(self.page.locator('#thoughts-list')).to_contain_text('真是的，拿着我的照片瞎扯什么啊……')
        expect(self.page.locator('#thoughts-list')).to_contain_text('大半夜的，都去休息吧。')
        self.host.set_viewport_size({'width':390,'height':844})
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'))
        self.assertEqual(self.errors, [])

    def test_conversation_settings_explain_listening_and_unanswered_contact(self):
        self.page.locator('button[data-tab="settings"]').click()
        self.page.locator('#settings-tabs').get_by_text('自主发言', exact=True).click()
        expect(self.page.get_by_text('可以主动说话的地方', exact=True)).to_be_visible()
        expect(self.page.get_by_text('新消息到来后自然接话', exact=True)).to_be_visible()
        expect(self.page.get_by_text('参与程度', exact=True)).to_be_visible()
        expect(self.page.get_by_text('每小时最多观察次数', exact=True)).to_be_visible()
        expect(self.page.get_by_text('好感对观察的影响', exact=True)).to_be_visible()
        expect(self.page.get_by_text('自然沉默', exact=True)).to_have_count(0)
        expect(self.page.get_by_text('每天最多分享生活', exact=True)).to_be_visible()
        expect(self.page.get_by_text('生活分享前留白（秒）', exact=True)).to_be_visible()
        self.assertEqual(self.page.get_by_text('插话最短间隔（分钟）', exact=True).count(), 0)
        self.assertEqual(self.page.get_by_text('至少几条消息才插话', exact=True).count(), 0)
        self.page.locator('#theme-toggle').click()
        self.page.locator('[data-appearance="nocturne"]').click()
        self.page.get_by_label('关闭外观设置').click()
        self.page.evaluate('window.scrollTo(0, 0)')
        self.host.screenshot(path=str(self.shots / 'participation-overview-dark.png'), animations='disabled')
        self.host.set_viewport_size({'width': 390, 'height': 844})
        self.page.get_by_text('参与程度', exact=True).scroll_into_view_if_needed()
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        self.host.screenshot(path=str(self.shots / 'participation-overview-mobile.png'), animations='disabled')
        self.host.set_viewport_size({'width': 1440, 'height': 1000})
        self.page.get_by_text('主动找人的保护', exact=True).click()
        expect(self.page.get_by_text('未回应联系人等待（小时）', exact=True)).to_be_visible()
        self.page.get_by_text('参与程度', exact=True).scroll_into_view_if_needed()
        self.host.screenshot(path=str(self.shots / 'participation-settings.png'))
        self.host.set_viewport_size({'width': 390, 'height': 844})
        self.page.get_by_text('未回应联系人等待（小时）', exact=True).scroll_into_view_if_needed()
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        self.host.screenshot(path=str(self.shots / 'participation-settings-mobile.png'))
        self.assertEqual(self.errors, [])

    def test_attention_budget_and_relationship_weight_save_without_forcing_speech(self):
        self.page.locator('button[data-tab="settings"]').click()
        self.page.locator('#settings-tabs').get_by_text('自主发言', exact=True).click()
        self.page.evaluate("""() => {
          const post = AstrBotPluginPage.apiPost; window.silenceWrites=[];
          AstrBotPluginPage.apiPost=(key,data)=>{
            if(key==='config/world')silenceWrites.push(structuredClone(data.world));
            return post(key,data);
          };
        }""")
        control = self.page.get_by_text('每小时最多观察次数', exact=True).locator('xpath=ancestor::label[1]').locator('input')
        relation = self.page.get_by_text('好感对观察的影响', exact=True).locator('xpath=ancestor::label[1]').locator('input')
        expect(control).to_have_value('12')
        expect(relation).to_have_value('1')
        control.fill('10')
        control.dispatch_event('change')
        relation.fill('0.5')
        relation.dispatch_event('change')
        self.page.locator('#save').click()
        self.page.wait_for_function('silenceWrites.length===1')
        self.assertEqual(self.page.evaluate('silenceWrites[0].decider.participation_hourly_max'), 10)
        self.assertEqual(self.page.evaluate('silenceWrites[0].decider.participation_relation_weight'), .5)
        self.assertFalse(self.page.evaluate('silenceWrites[0].decider.natural_silence'))
        self.page.locator('button[data-tab="life"]').click()
        self.page.locator('button[data-tab="settings"]').click()
        self.page.locator('#settings-tabs').get_by_text('自主发言', exact=True).click()
        control = self.page.get_by_text('每小时最多观察次数', exact=True).locator('xpath=ancestor::label[1]').locator('input')
        relation = self.page.get_by_text('好感对观察的影响', exact=True).locator('xpath=ancestor::label[1]').locator('input')
        expect(control).to_have_value('10')
        expect(relation).to_have_value('0.5')
        relation.fill('0')
        relation.dispatch_event('change')
        self.page.locator('#save').click()
        self.page.wait_for_function('silenceWrites.length===2')
        self.assertEqual(self.page.evaluate('silenceWrites[1].decider.participation_relation_weight'), 0)
        self.page.locator('#toast').evaluate('(node)=>node.remove()')
        for width, height, palette in [(1440, 1000, 'nocturne'), (390, 844, 'nocturne'), (1440, 1000, 'cloud')]:
            self.host.set_viewport_size({'width': width, 'height': height})
            self.page.evaluate("p=>{document.documentElement.dataset.palette=p;document.documentElement.dataset.theme=p==='cloud'?'light':'dark'}", palette)
            control.locator('xpath=ancestor::div[contains(@class,"proactive-controls")]').evaluate('(node)=>node.scrollIntoView({block:"center"})')
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'))
            self.host.screenshot(path=str(self.shots / f'contextual-participation-{palette}-{width}.png'))
        self.assertEqual(self.errors, [])
