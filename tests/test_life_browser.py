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

    def test_execution_evidence_environment_and_no_invented_progress(self):
        self.page.evaluate("""() => {
          lifeData.execution={ready:false,reason:'请配置独立生活沙盒',booter:'shipyard_neo',tools:[
            {name:'astrbot_execute_shell',scope:'sandbox',allowed:true},
            {name:'public_lookup',scope:'host',allowed:false,fingerprint:'synthetic',reason:'需要单独信任：在宿主执行'},
            {name:'astrbot_file_download',scope:'host',allowed:false,reason:'电脑工具仅接入已确认的原生沙盒工具'}]};
          Object.assign(lifeData.items[0],{text:'归类和校验本地动漫截图与资源元数据：先实现脚本，真实素材尚未提供',motivation:'想为自己的收藏做一份清楚的目录，先从可验证的小步开始。',next_step:'在独立沙盒中编写校验脚本',status:'blocked',blocker:'environment',steps:[],history:[],progress:0,obstacle:'请配置独立生活沙盒',recovery_condition:'沙盒配置或工具能力变化后继续'});
          Object.assign(lifeData.items[1],{text:'素材校验脚本与样例验证',motivation:'先把字段和异常处理想清楚，用明确标注的样例测试，真实素材到齐后再继续。',next_step:'检查明确提供到项目沙盒的真实素材',status:'blocked',blocker:'materials',obstacle:'真实截图尚未提供',recovery_condition:'提供项目沙盒内的真实素材路径',progress:50,
            steps:[{id:'script',text:'编写脚本并完成 synthetic 样例验证',action:'native_agent',status:'done',finished_at:Date.now()/1000},{id:'real',text:'收到真实素材后运行校验并留下报告',action:'native_agent',status:'pending'}],
            history:[{at:Date.now()/1000,text:'脚本与样例验证小步已完成，尚未处理真实素材',receipt:{kind:'native_agent'}}],
            artifacts:[{path:'my_world/lm_demo/validate.py',verification:'原生沙盒文件接口已读回，样例不代表真实素材'}],executions:[{id:'le_demo',at:Date.now()/1000,status:'succeeded',events:[{kind:'environment',booter:'shipyard_neo'},{kind:'tool_start',tool:'astrbot_execute_shell'},{kind:'tool_end',tool:'astrbot_execute_shell',ok:true,result:'synthetic tests: 3 passed'}]}]});
          Object.assign(lifeData.items[2],{steps:[],history:[],progress:0,blocker:'capability',next_step:'先确认实际可用的寄送方式'});
        }""")
        self.page.locator('#life-tools summary').click()
        self.page.locator('#life-refresh').click()
        expect(self.page.locator('.life-card').first).not_to_contain_text('0%')
        expect(self.page.locator('#life-grid')).to_contain_text('等待执行环境')
        expect(self.page.locator('#life-grid')).to_contain_text('等待资料或素材')
        self.host.screenshot(path=str(self.shots/'life-execution-desktop.png'))
        self.page.locator('.life-card').filter(has_text='素材校验脚本与样例验证').click()
        expect(self.page.locator('#life-detail')).to_contain_text('my_world/lm_demo/validate.py')
        self.page.locator('.life-execution summary').click()
        expect(self.page.locator('.life-execution')).to_contain_text('synthetic tests: 3 passed')
        self.host.screenshot(path=str(self.shots/'life-execution-drawer.png'))
        self.page.locator('#life-close').click()
        self.host.set_viewport_size({'width':390,'height':844})
        self.host.screenshot(path=str(self.shots/'life-execution-phone.png'))
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'))
        self.page.locator('#life-tools summary').click()
        self.page.locator('#life-settings').click()
        expect(self.page.get_by_label('生活沙盒环境')).to_be_visible()
        self.page.get_by_text('生活可用工具与宿主授权', exact=True).click()
        expect(self.page.get_by_text('public_lookup', exact=True)).to_be_visible()
        self.host.screenshot(path=str(self.shots/'life-execution-settings-phone.png'))
        self.assertEqual(self.errors, [])

    def test_old_save_cannot_close_a_new_session_drawer(self):
        self.page.evaluate("""() => {
          const read=AstrBotPluginPage.apiGet, write=AstrBotPluginPage.apiPost;
          window.secondLife={...structuredClone(lifeData),session:'aiocqhttp:GroupMessage:2002',
            items:[{...structuredClone(lifeData.items[0]),id:'lm_second',text:'另一个会话的生活事项'}]};
          AstrBotPluginPage.apiGet=(endpoint,params)=>endpoint==='life'&&params.session===secondLife.session
            ?Promise.resolve(structuredClone(secondLife)):read(endpoint,params);
          AstrBotPluginPage.apiPost=(endpoint,payload)=>endpoint==='life/update'
            ?new Promise(resolve=>{lifeWrites.push(structuredClone(payload));window.finishLifeSave=resolve})
            :write(endpoint,payload);
        }""")
        self.page.locator('.life-card[data-id="lm_book"]').click()
        self.page.get_by_role('button', name='暂时放下', exact=True).click()
        expect(self.page.get_by_role('button', name='暂时放下', exact=True)).to_be_disabled()
        self.host.keyboard.press('Escape')
        self.page.evaluate("""() => {
          const picker=document.querySelector('#status-session');
          picker.add(new Option('另一个会话','aiocqhttp:GroupMessage:2002'));
          picker.value='aiocqhttp:GroupMessage:2002';picker.dispatchEvent(new Event('change'));
        }""")
        self.page.locator('.life-card[data-id="lm_second"]').click()
        self.page.evaluate('finishLifeSave({ok:true})')
        expect(self.page.locator('#life-drawer')).to_be_visible()
        expect(self.page.locator('#life-drawer-title')).to_have_text('另一个会话的生活事项')
        self.assertEqual(self.page.evaluate('lifeWrites.length'), 1)
        self.assertNotEqual(self.page.evaluate('lifeWrites[0].session'), 'aiocqhttp:GroupMessage:2002')
        self.assertEqual(self.errors, [])
