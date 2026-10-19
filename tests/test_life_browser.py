"""Life board acceptance in Chromium, using synthetic isolated data."""
import unittest

from tests import test_generation_browser as generation
from tests import test_workbench_browser as workbench
from tests.test_generation_browser import expect, sync_playwright


@unittest.skipIf(sync_playwright is None, "Install playwright to run browser tests")
class LifeBrowserTests(unittest.TestCase):
    setUpClass = classmethod(generation.GenerationBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(generation.GenerationBrowserTests.tearDownClass.__func__)
    install_console = workbench.WorkbenchBrowserTests.install_console

    def setUp(self):
        generation.GenerationBrowserTests.setUp(self)
        self.host.emulate_media(reduced_motion="reduce")
        snapshot = self.install_console()
        self.page.evaluate("""snapshot => {
          const now=Date.now()/1000;
          window.lifeWrites=[];
          window.lifeData={session:snapshot.session_id,activity:{label:'在书房慢慢读书'},reason:'想把上次没读完的一章看完',
            items:[
              {id:'lm_book',text:'读完一部长篇，留下自己的读书笔记',motivation:'想知道那些人物最后走向哪里，也想留下自己的感受。',next_step:'周末继续第三章',kind:'project',status:'working',progress:40},
              {id:'lm_wind',text:'研究旧风车为什么停了',motivation:'不急着修好，先从手边能查明的事做起。',next_step:'对照叶片的资料',kind:'project',status:'waiting',progress:20},
              {id:'lm_photo',text:'给小明寄一张亲手挑的明信片',motivation:'答应过的事情，想认真记着。',obstacle:'尚无邮寄能力，等待实际工具结果',kind:'promise',status:'blocked',progress:0},
              {id:'lm_done',text:'读完关于星星的一章',kind:'project',status:'completed',progress:100,result:'世界内实际阅读了20分钟'}
            ].map((row,i)=>({...row,session:snapshot.session_id,at:now-86400*(i+1),updated_at:now-i*3600,
              steps:[{id:'s1',text:'阅读第一部分并整理思路',status:'done',minutes:20,finished_at:now-86400},{id:'s2',text:'继续读下一部分',status:'pending',minutes:25}],
              history:[{at:now-86400,text:'实际读过第一部分',receipt:{kind:'in_world_action',minutes:20}},{at:now,text:'安排下一步，尚未执行'}]})),
            interests:{read:{label:'阅读',days:['a','b','c'],sessions:4,minutes:90}},
            artifacts:[{label:'星星的一章',result:'在世界内实际阅读20分钟',at:now,scope:'in_world'}],
            budget:{calls:7,tokens:3500,unknown:1,reserved:2200},semantic:'未启用'};
          const read=AstrBotPluginPage.apiGet, write=AstrBotPluginPage.apiPost;
          AstrBotPluginPage.apiGet=(endpoint,params)=>endpoint==='life'?Promise.resolve(structuredClone(lifeData)):read(endpoint,params);
          AstrBotPluginPage.apiPost=(endpoint,payload)=>{
            if(endpoint!=='life/update')return write(endpoint,payload);
            lifeWrites.push(structuredClone(payload));
            if(payload.operation==='pause')lifeData.items.find(row=>row.id===payload.id).status='paused';
            if(payload.operation==='create')lifeData.items.push({...payload,id:'lm_new',status:'active',progress:0,at:now});
            return Promise.resolve({ok:true});
          };
        }""", snapshot)
        self.page.locator('button[data-tab="life"]').click()
        expect(self.page.locator('.life-card')).to_have_count(3)

    def test_filter_sort_drawer_and_management(self):
        self.page.locator('#life-sort').select_option('progress')
        expect(self.page.locator('.life-card').first).to_contain_text('读完一部长篇')
        self.page.locator('#life-filter').select_option('blocked')
        expect(self.page.locator('.life-card')).to_have_count(2)
        self.page.locator('#life-filter').select_option('active')
        self.page.locator('.life-card[data-id="lm_book"]').click()
        expect(self.page.locator('#life-detail')).to_contain_text('实际读过第一部分')
        expect(self.page.locator('#life-detail')).to_contain_text('尚未执行')
        self.page.get_by_role('button', name='暂时放下', exact=True).click()
        expect(self.page.locator('#life-drawer')).not_to_be_visible()
        expect(self.page.locator('.life-card[data-id="lm_book"]')).to_contain_text('暂时放下')
        self.assertEqual(self.page.evaluate('lifeWrites[0].operation'), 'pause')
        self.page.locator('#life-add').click()
        self.page.get_by_label('想做什么', exact=True).fill('慢慢学一首新的曲子')
        self.page.get_by_role('button', name='保存事项', exact=True).click()
        expect(self.page.locator('.life-card[data-id="lm_new"]')).to_contain_text('慢慢学一首新的曲子')
        self.page.locator('#life-tools summary').click()
        self.page.locator('#life-collection').click()
        expect(self.page.locator('#life-detail')).to_contain_text('3 天 · 4 次')
        expect(self.page.locator('#life-detail')).to_contain_text('3,500 token')
        self.host.keyboard.press('Escape')
        expect(self.page.locator('#life-drawer')).not_to_be_visible()
        self.assertEqual(self.errors, [])

    def test_six_palettes_desktop_phone_and_scroll(self):
        for width,height in [(1440,1000),(390,844)]:
            self.host.set_viewport_size({'width':width,'height':height})
            for palette in ['nocturne','aurora','rose','cloud','sand','jade']:
                self.page.evaluate("palette=>{document.documentElement.dataset.palette=palette;document.documentElement.dataset.theme=['cloud','sand','jade'].includes(palette)?'light':'dark'}", palette)
                expect(self.page.locator('.life-card').first).to_be_visible()
                self.assertTrue(self.page.evaluate("""()=>{
                  const grid=document.querySelector('#life-grid'),r=grid.getBoundingClientRect();
                  return r.width>200&&r.left>=0&&r.right<=innerWidth+1&&r.height>120
                    &&document.documentElement.scrollWidth<=innerWidth+1;
                }"""), (width,palette))
                self.host.screenshot(path=str(self.shots/f'life-{palette}-{width}.png'))
            self.page.locator('.life-card').first.click()
            self.assertTrue(self.page.locator('#life-drawer').evaluate('(e)=>{const r=e.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth+1&&r.bottom<=innerHeight+1}'))
            self.host.screenshot(path=str(self.shots/f'life-drawer-{width}.png'))
            self.page.locator('#life-close').click()
        self.assertEqual(self.errors, [])
