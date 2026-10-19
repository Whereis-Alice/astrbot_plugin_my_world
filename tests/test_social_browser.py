"""Chromium acceptance: learning drawers, theme contrast, mobile layout and late requests."""

import unittest
from tests import test_generation_browser as generation
from tests import test_workbench_browser as workbench
from tests import test_contacts_browser as contacts
from tests.test_generation_browser import expect, sync_playwright


@unittest.skipIf(sync_playwright is None, "Install playwright to run browser tests")
class SocialBrowserTests(unittest.TestCase):
    install_console = workbench.WorkbenchBrowserTests.install_console
    setUpClass = classmethod(generation.GenerationBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(generation.GenerationBrowserTests.tearDownClass.__func__)

    def setUp(self):
        generation.GenerationBrowserTests.setUp(self)
        snapshot = self.install_console()
        self.page.evaluate(
            """data => {
          for (const id of ['memory-session','contacts-session','calls-session']) {
            const select=document.getElementById(id);
            select.add(new Option('星空与旅行 · 100',data.session_id)); select.value=data.session_id;
          }
        }""",
            snapshot,
        )
        self.host.emulate_media(reduced_motion="reduce")
        self.page.evaluate("""() => {
          const read=AstrBotPluginPage.apiGet, write=AstrBotPluginPage.apiPost;
          window.learningWrites=[]; window.learningPending=[]; window.delayLearning=false;
          window.vocabularyData=[{term:'夜航',description:'晚上一起安静读书，不一定需要说话。',manual:false,status:'active',
            evidence:[{user_id:'42',name:'小明',text:'我们这里说夜航，就是晚上一起安静读书。'}]}];
          window.learningDetail={revision:0,cursor:0,manual:false,habits:[{text:'先听他说完，再一起想办法',source_ids:[1]}],
            history:[{delta:0.1,reason:'认真听完彼此的话，没有催促',at:Date.now()/1000,evidence:[{user_id:'42',text:'谢谢你听我说完'}]}]};
          AstrBotPluginPage.apiGet=(endpoint,params)=>{
            if(endpoint==='social-learning') {
              const result={session:params.session,vocabulary:structuredClone(vocabularyData),interaction:structuredClone(learningDetail)};
              if(delayLearning)return new Promise(resolve=>learningPending.push(()=>resolve(result)));
              return Promise.resolve(result);
            }
            if(endpoint==='decision-traces')return Promise.resolve({session:params.session,items:[
              {trace_id:'trace-001',stage:'gate',status:'silent',reason:'已经进入休息安排，暂时不回应普通群聊',at:Date.now()/1000,details:{mode:'sleeping'}},
              {trace_id:'trace-002',stage:'prepare',status:'error',reason:'准备回复失败',at:Date.now()/1000,details:{error:'TimeoutError'}},
              {trace_id:'trace-003',stage:'prepare',status:'awaiting_native',reason:'已交给 AstrBot 原生 Agent',at:Date.now()/1000,details:{memory_ids:[17,21]}}
            ]});
            return read(endpoint,params);
          };
          AstrBotPluginPage.apiPost=(endpoint,payload)=>{
            if(endpoint!=='social-learning/update')return write(endpoint,payload);
            learningWrites.push(structuredClone(payload));
            if(payload.operation==='vocabulary') {
              const old=vocabularyData.find(row=>row.term===payload.term);
              if(old)Object.assign(old,payload,{manual:true});else vocabularyData.push({...payload,manual:true,status:'active'});
            }
            return Promise.resolve({ok:true});
          };
        }""")

    def open_vocabulary(self):
        self.page.locator('button[data-tab="memories"]').click()
        self.page.locator("#memory-vocabulary").click()
        expect(self.page.locator(".learning-card").first).to_contain_text("夜航")

    def test_vocabulary_source_manual_correction_and_disable(self):
        self.open_vocabulary()
        self.page.get_by_text("查看原话来源", exact=True).click()
        expect(self.page.locator(".learning-quote")).to_contain_text("我们这里说夜航")
        self.page.get_by_role("button", name="修改", exact=True).click()
        self.page.get_by_label("词语含义", exact=True).fill("晚上一起读书和分享感想")
        self.page.get_by_role("button", name="保存词条", exact=True).click()
        expect(self.page.locator(".learning-card").first).to_contain_text("晚上一起读书和分享感想")
        self.page.get_by_role("button", name="停用", exact=True).click()
        expect(self.page.locator(".learning-card").first).to_contain_text("已停用")
        self.assertEqual(self.page.evaluate("learningWrites.at(-1).status"), "disabled")
        self.assertEqual(self.errors, [])

    def test_decisions_distinguish_silence_failure_and_unobserved_result(self):
        self.page.locator('button[data-tab="model-calls"]').click()
        self.page.evaluate("""() => {
          const select=document.getElementById('calls-session');
          select.add(new Option('星空与旅行 · 100','test:GroupMessage:100')); select.value='test:GroupMessage:100';
        }""")
        self.page.locator(".calls-help summary").click()
        self.page.locator("#calls-decisions").click()
        expect(self.page.locator(".learning-card")).to_have_count(3)
        expect(self.page.locator(".learning-body")).to_contain_text("主动静默")
        expect(self.page.locator(".learning-body")).to_contain_text("失败")
        expect(self.page.locator(".learning-body")).to_contain_text("尚未观察到")
        self.page.locator(".learning-card").last.locator("summary").click()
        expect(self.page.locator(".learning-card").last).to_contain_text("trace-003")
        self.host.screenshot(path=str(self.shots / "decisions-desktop.png"))

    def test_six_themes_desktop_phone_and_long_source(self):
        self.open_vocabulary()
        for width, height in [(1440, 1000), (390, 844)]:
            self.host.set_viewport_size({"width": width, "height": height})
            self.page.wait_for_function("width => innerWidth === width", arg=width)
            for palette in ["nocturne", "aurora", "rose", "cloud", "sand", "jade"]:
                self.page.evaluate(
                    "p=>{document.documentElement.dataset.palette=p;document.documentElement.dataset.theme=['cloud','sand','jade'].includes(p)?'light':'dark'}",
                    palette,
                )
                self.page.wait_for_function("""() => {
                  const e=document.querySelector('.learning-drawer'),r=e.getBoundingClientRect();
                  return r.left>=0&&r.right<=innerWidth+1&&r.bottom<=innerHeight+1&&e.scrollWidth<=e.clientWidth+1;
                }""")
                self.assertTrue(
                    self.page.locator(".learning-drawer").evaluate(
                        "(e)=>{const r=e.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth+1&&r.bottom<=innerHeight+1&&e.scrollWidth<=e.clientWidth+1}"
                    )
                )
                expect(self.page.get_by_label("词语含义", exact=True)).to_be_visible()
                self.host.screenshot(path=str(self.shots / f"learning-{palette}-{width}.png"))
        self.assertEqual(self.errors, [])

    def test_closed_drawer_cannot_be_repainted_by_delayed_response(self):
        self.page.evaluate("delayLearning=true")
        self.page.locator('button[data-tab="memories"]').click()
        self.page.locator("#memory-vocabulary").click()
        expect(self.page.locator(".learning-body")).to_contain_text("正在读取")
        self.page.get_by_role("button", name="关闭 ×", exact=True).click()
        self.page.evaluate("delayLearning=false")
        self.page.locator("#memory-vocabulary").click()
        expect(self.page.locator(".learning-card").first).to_contain_text("夜航")
        self.page.evaluate("learningPending.shift()()")
        expect(self.page.locator(".learning-card")).to_have_count(1)
        self.assertEqual(self.errors, [])


@unittest.skipIf(sync_playwright is None, "Install playwright to run browser tests")
class SocialPersonBrowserTests(unittest.TestCase):
    setUpClass = classmethod(generation.GenerationBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(generation.GenerationBrowserTests.tearDownClass.__func__)
    setUp = contacts.ContactsBrowserTests.setUp
    open_person = contacts.ContactsBrowserTests.open_person

    def test_contact_entry_preserves_scope_manual_clear_and_resume_revision(self):
        self.page.evaluate("""() => {
          const read=AstrBotPluginPage.apiGet, write=AstrBotPluginPage.apiPost;
          window.styleWrites=[];window.styleDetail={revision:0,manual:false,habits:[{text:'先听他说完',source_ids:[1]}],history:[]};
          AstrBotPluginPage.apiGet=(key,params)=>key==='social-learning'
            ?Promise.resolve({session:params.session,interaction:structuredClone(styleDetail),vocabulary:[]}):read(key,params);
          AstrBotPluginPage.apiPost=(key,params)=>{
            if(key!=='social-learning/update')return write(key,params);
            styleWrites.push(structuredClone(params));styleDetail.revision++;
            styleDetail.manual=params.operation==='habits';
            if(params.operation==='habits')styleDetail.habits=params.text?[{text:params.text,source_ids:[]}]:[];
            return Promise.resolve({ok:true});
          };
        }""")
        self.open_person()
        self.page.locator(".contact-identity-section > summary").click()
        self.page.get_by_role("button", name="相处习惯与互动依据", exact=True).click()
        expect(self.page.get_by_label("学习记录所属会话")).to_be_disabled()
        expect(self.page.get_by_label("相处习惯", exact=True)).to_have_value("先听他说完")
        self.page.get_by_label("相处习惯", exact=True).fill("")
        self.page.get_by_role("button", name="保存并由我维护", exact=True).click()
        expect(self.page.get_by_role("button", name="恢复自动归纳", exact=True)).to_be_enabled()
        self.assertEqual(
            self.page.evaluate("styleWrites[0].session"), "aiocqhttp:GroupMessage:1001"
        )
        self.assertEqual(self.page.evaluate("styleWrites[0].user_id"), "10001")
        self.page.get_by_role("button", name="恢复自动归纳", exact=True).click()
        expect(self.page.get_by_role("button", name="恢复自动归纳", exact=True)).to_be_disabled()
        self.assertEqual(self.page.evaluate("styleWrites[1].revision"), 1)
        self.assertEqual(self.errors, [])
