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
