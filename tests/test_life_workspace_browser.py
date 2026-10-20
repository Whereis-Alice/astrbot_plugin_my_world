"""Unified life navigation, preserved editors and responsive visual acceptance."""
import unittest

from tests import test_life_browser as life
from tests.test_generation_browser import expect, sync_playwright


@unittest.skipIf(sync_playwright is None, "Install playwright for browser verification")
class LifeWorkspaceBrowserTests(unittest.TestCase):
    setUpClass = classmethod(life.LifeBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(life.LifeBrowserTests.tearDownClass.__func__)
    install_console = life.LifeBrowserTests.install_console

    def setUp(self):
        life.LifeBrowserTests.setUp(self)
        self.page.evaluate("""() => Object.assign(lifeData, {
          day:'2026-10-20',
          agenda:[
            {id:'morning',time:'08:30',title:'在露台吃早餐，看看今天天气',status:'past'},
            {id:'book',time:'20:00',title:'读完一部长篇，留下自己的读书笔记',matter_id:'lm_book',status:'delayed'},
            {id:'rest',time:'23:00',title:'收起书，留一点安静的睡前时间',status:'upcoming'}],
          rhythms:[{period:'晚间',label:'阅读',explanation:'最近三个不同日期里，她都在晚间读过书。近期投入权重更高；这只是生活节奏参考。',
            evidence:[{day:'2026-10-19',period:'晚间',minutes:20},{day:'2026-10-18',period:'晚间',minutes:25}]}],
          experiences:[
            {id:'evening',title:'在露台遇见一场很轻的晚霞',status:'closed',result:'云缝里的光慢慢落下，她想把这一刻留下来。',matter_id:'lm_book'},
            {id:'wind',title:'风车的叶片又响了',status:'open',result:'决定等风小一点再去看看。'},
            {id:'tea',title:'换了一种热茶',status:'closed',result:'把杯子放在窗边，让这一小会儿安静下来。'}]
        })""")
        self.page.evaluate("""() => {
          window.weekReads=[]; window.weekPosts=[];
          window.weekData={
            session:lifeData.session,week_start:'2026-10-19',week_end:'2026-10-25',today:'2026-10-20',
            calendar:{year:2026,world_type:'original',world_name:'风之世界',
              culture_notes:'十月的风祭以旧钟楼的钟声开始。',events:[]},
            days:['mon','tue','wed','thu','fri','sat','sun'].map((weekday,index)=>({
              date:`2026-10-${String(19+index).padStart(2,'0')}`,weekday,is_today:index===1,
              calendar_generated:true,
              agenda_source:index<1?'recorded':index===1?'today':'projected',
              agenda:index===1?[{id:'read',time:'20:00',title:'继续读风车的维修说明',status:'soon',matter_id:'lm_book'}]:[],
              calendar:index===4?[{id:'wind',title:'风祭',source:'inferred',description:'在旧钟楼听风，是原创世界里的地方节庆。'}]:[],
              diary:index===0?{day:'2026-10-19',title:'风从书页间经过',mood:'安静',content:'今天读到很慢，却把一句喜欢的话记了下来。'}:null
            }))
          };
          const read=AstrBotPluginPage.apiGet, write=AstrBotPluginPage.apiPost;
          AstrBotPluginPage.apiGet=(endpoint,params)=>{
            if(endpoint==='life/week'){weekReads.push(structuredClone(params));return Promise.resolve(structuredClone(weekData));}
            return read(endpoint,params);
          };
          AstrBotPluginPage.apiPost=(endpoint,payload)=>{
            if(endpoint==='calendar/generate'||endpoint==='diary/generate'){
              weekPosts.push({endpoint,payload:structuredClone(payload)});return Promise.resolve({ok:true,calendar:weekData.calendar,entry:{day:payload.day,title:'补写',content:'回顾'}});
            }
            return write(endpoint,payload);
          };
        }""")
        self.page.locator('[data-life-view="today"][role="tab"]').click()
        self.page.locator('[data-life-view="concerns"][role="tab"]').click()
        self.page.locator('#life-tools summary').click()
        self.page.locator('#life-refresh').click()
        self.page.locator('[data-life-view="today"][role="tab"]').click()
        expect(self.page.locator('.life-agenda-row')).to_have_count(3)

    def test_today_links_to_same_matter_and_saves_appointment(self):
        expect(self.page.locator('#tabs button[data-tab="events"]')).to_have_count(0)
        expect(self.page.locator('#tabs button[data-tab="schedules"]')).to_have_count(0)
        self.page.locator('.life-agenda-row').nth(1).click()
        expect(self.page.locator('#life-drawer-title')).to_have_text('读完一部长篇，留下自己的读书笔记')
        self.page.get_by_role('button', name='安排时间…', exact=True).click()
        self.page.get_by_label('哪一天', exact=True).fill('2026-10-21')
        self.page.get_by_label('几点开始', exact=True).fill('20:30')
        self.page.get_by_role('button', name='保存到日程', exact=True).click()
        expect(self.page.locator('#life-drawer')).not_to_be_visible()
        write = self.page.evaluate('lifeWrites.at(-1)')
        self.assertEqual((write['operation'], write['id'], write['date'], write['time']),
                         ('appointment', 'lm_book', '2026-10-21', '20:30'))
        self.assertEqual(self.errors, [])

    def test_management_preserves_generation_editors_scope_and_nested_dialogs(self):
        self.page.get_by_role('button', name='编辑日程 ↗', exact=True).click()
        expect(self.page.locator('.life-manager[open]')).to_contain_text('全部日程')
        expect(self.page.locator('.life-manager[open] #config-scope')).to_be_visible()
        self.page.locator('#schedule-add').click()
        choice = self.page.get_by_label('到点做什么', exact=True)
        choice.select_option('lm_book')
        expect(self.page.locator('#schedule-form')).to_contain_text('沿用原有投入与进度')
        self.page.locator('#schedule-generate').click()
        expect(self.page.locator('#dialog')).to_be_visible()
        self.page.locator('#dialog-cancel').click()
        expect(self.page.locator('.life-manager[open]')).to_be_visible()
        self.page.locator('.life-manager[open] .life-manager-save').click()
        self.page.wait_for_function('saveCalls.includes("config/schedules")')
        self.host.keyboard.press('Escape')
        expect(self.page.locator('.life-manager[open]')).to_have_count(0)
        expect(self.page.locator('.topbar #config-scope')).to_be_visible()
        self.page.locator('[data-life-view="experiences"][role="tab"]').click()
        self.page.get_by_role('button', name='投递与事件设置…', exact=True).click()
        self.page.locator('#event-auto').click()
        expect(self.page.locator('#dialog-title')).to_have_text('自动生成事件')
        self.page.locator('#dialog-cancel').click()
        self.host.keyboard.press('Escape')
        expect(self.page.get_by_role('button', name='投递与事件设置…', exact=True)).to_be_focused()
        self.assertEqual(self.errors, [])

    def test_today_six_themes_phone_and_manager_layout(self):
        for width, height in [(1440, 1000), (390, 844)]:
            self.host.set_viewport_size({'width':width, 'height':height})
            for palette in ['nocturne', 'aurora', 'rose', 'cloud', 'sand', 'jade']:
                self.page.evaluate("palette=>{document.documentElement.dataset.palette=palette;document.documentElement.dataset.theme=['cloud','sand','jade'].includes(palette)?'light':'dark'}", palette)
                expect(self.page.locator('.life-agenda-row').first).to_be_visible()
                self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'))
                self.host.screenshot(path=str(self.shots / f'life-workspace-{palette}-{width}.png'))
            self.page.get_by_role('button', name='编辑日程 ↗', exact=True).click()
            self.host.screenshot(path=str(self.shots / f'life-manager-{width}.png'))
            self.assertTrue(self.page.locator('.life-manager[open]').evaluate('(e)=>e.scrollWidth<=e.clientWidth+1'))
            self.assertTrue(self.page.locator('.life-manager[open]').evaluate('(e)=>{const r=e.getBoundingClientRect();return r.top>=0&&r.bottom<=innerHeight+1}'))
            self.page.locator('.life-manager[open]').get_by_role('button', name='完成 ×', exact=True).click()
        self.assertEqual(self.errors, [])

    def test_week_view_navigation_generation_and_mobile_layout(self):
        self.page.locator('[data-life-view="week"][role="tab"]').click()
        self.assertEqual(self.errors, [])
        self.assertEqual(
            self.page.locator('#tab-life').get_attribute('data-life-view'), 'week'
        )
        self.assertTrue(self.page.locator('#status-session').input_value())
        week_text = self.page.locator('#life-view-week').inner_text()
        self.assertGreater(
            self.page.evaluate('weekReads.length'),
            0,
            f'周视图没有请求 life/week：{week_text}',
        )
        expect(self.page.locator('.life-week-day')).to_have_count(7)
        expect(self.page.locator('.life-week-day.is-today')).to_contain_text('10.20')
        expect(self.page.locator('#life-week-grid')).to_contain_text('风祭')
        expect(self.page.locator('#life-week-grid')).to_contain_text('风从书页间经过')
        self.page.locator('#life-week-next').click()
        expect(self.page.locator('.life-week-day')).to_have_count(7)
        self.assertEqual(self.page.evaluate('weekReads.at(-1).start'), '2026-10-26')
        self.page.locator('#life-week-generate-calendar').click()
        self.page.locator('#life-week-generate-diary').click()
        expect(self.page.locator('#life-week-generate-calendar')).to_be_enabled()
        expect(self.page.locator('#life-week-generate-diary')).to_be_enabled()
        self.assertEqual(
            [row['endpoint'] for row in self.page.evaluate('weekPosts')],
            ['calendar/generate', 'diary/generate'],
        )
        for width, height in [(1440, 1000), (390, 844)]:
            self.host.set_viewport_size({'width': width, 'height': height})
            expect(self.page.locator('.life-week-day').first).to_be_visible()
            self.assertTrue(
                self.page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
            )
            if width == 390:
                boxes = self.page.locator('.life-week-day').evaluate_all(
                    '(els)=>els.slice(0,2).map(e=>e.getBoundingClientRect().x)'
                )
                self.assertEqual(boxes[0], boxes[1])
            self.host.screenshot(
                path=str(self.shots / f'life-week-{width}.png')
            )
        self.assertEqual(self.errors, [])
