"""Memory model controls stay discoverable, scoped and usable on phones."""
import unittest
from tests import test_generation_browser as generation
from tests import test_workbench_browser as workbench

expect = generation.expect


@unittest.skipIf(generation.sync_playwright is None, "Install playwright")
class MemorySettingsBrowserTests(unittest.TestCase):
    setUpClass = classmethod(generation.GenerationBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(generation.GenerationBrowserTests.tearDownClass.__func__)
    setUp = generation.GenerationBrowserTests.setUp
    capture_writes = workbench.WorkbenchBrowserTests.capture_writes
    install_console = workbench.WorkbenchBrowserTests.install_console

    def install_models(self):
        self.host.emulate_media(reduced_motion='reduce')
        self.page.evaluate("""() => {
          const read=AstrBotPluginPage.apiGet;
          AstrBotPluginPage.apiGet=async (name,args)=>{
            const data=await read(name,args);
            if(name==='config')Object.assign(data,{scope:'',embedding_default:'embedding-default',
              embedding_providers:[{id:'embedding-default',model:'bge-m3'},{id:'embedding-second',model:'text-embedding-3-small'}]});
            return data;
          };
          const picker=document.querySelector('#config-scope');picker.add(new Option('刷新测试模型','model-refresh'));
          picker.value='model-refresh';picker.dispatchEvent(new Event('change'));
        }""")
        self.page.locator('button[data-tab="settings"]').click()
        self.page.get_by_role('button', name='记忆与资料', exact=True).click()
        expect(self.page.get_by_label('生活记忆嵌入模型', exact=True)).to_contain_text('embedding-second')

    def test_memory_models_save_in_the_right_category_and_budget_dialog_is_clean(self):
        self.install_models()
        self.capture_writes()
        self.page.get_by_label('启用语义召回', exact=True).check()
        self.page.get_by_label('生活记忆嵌入模型', exact=True).select_option('embedding-second')
        self.page.locator('#save').click()
        self.page.wait_for_function('policyWrites.length > 0')
        memory = self.page.evaluate('policyWrites.at(-1).memory')
        self.assertEqual(memory['semantic_provider_id'], 'embedding-second')
        self.assertTrue(memory['semantic_enabled'])
        self.assertEqual(memory['semantic_source'], 'provider')
        self.page.locator('button[data-tab="life"]').click()
        self.page.locator('[data-life-view="concerns"][role="tab"]').click()
        self.page.locator('#life-tools summary').click()
        self.page.locator('#life-settings').click()
        expect(self.page.locator('#dialog-body').get_by_label('嵌入模型来源', exact=True)).to_have_count(0)
        expect(self.page.locator('#dialog-body').get_by_text('嵌入服务 API 基址（含 /v1）', exact=True)).to_have_count(0)
        self.assertEqual(self.errors, [])

    def test_relationship_settings_share_optional_name_picker_and_contact_controls(self):
        self.install_models()
        self.page.get_by_role('button', name='记忆与资料', exact=True).click()
        # Find the existing settings editor through its card, without relying on a translated title.
        level = self.page.locator('.level-card').first
        self.page.get_by_text('亲密度分级', exact=True).click()
        level.locator(':scope > summary').click()
        expect(level).not_to_contain_text('每天主动')
        expect(level).not_to_contain_text('称呼')
        expect(level.locator('.relationship-boundary')).to_contain_text('未添加 · 遵循人设')
        level.locator('.relationship-boundary > summary').click()
        level.get_by_label('搜索已启用的动作').fill('抱')
        expect(level.locator('.boundary-option')).to_have_count(1)
        level.get_by_label('抱抱', exact=True).check()
        expect(level.get_by_role('button', name='移除限制：抱抱', exact=True)).to_be_visible()
        level.locator('.relationship-prompt > summary').click()
        level.get_by_label('档位额外说明', exact=True).fill('一起照料过花园')
        for width, height, palette in [(1440, 1000, 'aurora'), (390, 844, 'cloud')]:
            self.host.set_viewport_size({'width':width,'height':height})
            self.page.evaluate("p=>{document.documentElement.dataset.palette=p;document.documentElement.dataset.theme=p==='cloud'?'light':'dark'}", palette)
            level.scroll_into_view_if_needed()
            self.assertTrue(level.evaluate('n=>n.scrollWidth<=n.clientWidth+1'))
            self.host.screenshot(path=str(self.shots/f'relationship-settings-{width}.png'))
        self.page.locator('#settings-tabs').get_by_role('button', name='自主发言', exact=True).click()
        expect(self.page.get_by_label('每天最多主动联系', exact=True)).to_be_visible()
        expect(self.page.get_by_label('每天最多联系同一个人', exact=True)).to_be_visible()
        self.assertEqual(self.page.get_by_label('每天最多主动联系', exact=True).count(), 1)
        self.page.locator('[data-section-title="主动发言"]').scroll_into_view_if_needed()
        self.host.screenshot(path=str(self.shots/'proactive-contact-settings.png'))
        self.assertEqual(self.errors, [])

    def test_memory_shortcut_cancel_and_all_themes_remain_readable(self):
        self.install_models()
        self.capture_writes()
        self.page.locator('button[data-tab="memories"]').click()
        self.page.locator('#memory-policy').click()
        dialog = self.page.locator('#dialog-body')
        dialog.get_by_label('嵌入模型来源', exact=True).select_option('custom')
        dialog.get_by_label('嵌入服务 API 基址（含 /v1）', exact=True).fill('https://example.invalid/v1')
        self.page.locator('#dialog-cancel').click()
        self.page.locator('#memory-policy').click()
        expect(dialog.get_by_label('嵌入模型来源', exact=True)).to_have_value('provider')
        for width,height in [(1440,1000),(390,844)]:
            self.host.set_viewport_size({'width':width,'height':height})
            for palette in ['nocturne','aurora','rose','cloud','sand','jade']:
                self.page.evaluate("palette=>{document.documentElement.dataset.palette=palette;document.documentElement.dataset.theme=['cloud','sand','jade'].includes(palette)?'light':'dark'}", palette)
                self.page.evaluate("()=>Promise.all(document.getAnimations().filter(a=>a.effect.getTiming().iterations!==Infinity).map(a=>a.finished.catch(()=>{})))")
                expect(dialog.get_by_label('生活记忆嵌入模型', exact=True)).to_be_visible()
                self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'))
                self.host.screenshot(path=str(self.shots/f'memory-models-{palette}-{width}.png'))
        self.assertEqual(self.page.evaluate('policyWrites.length'), 0)
        self.assertEqual(self.errors, [])

    def test_settings_categories_cover_every_section_and_search_finds_embedding(self):
        self.install_models()
        sections = self.page.evaluate("""()=>[...document.querySelectorAll('#settings-form>.card-section')].map(e=>({key:e.dataset.sectionKey,title:e.dataset.sectionTitle,hidden:e.classList.contains('hidden')}))""")
        expected = {'生活记忆', '生活日历与日记', '用户画像'}
        self.assertEqual({row['title'] for row in sections if not row['hidden']}, expected)
        expect(self.page.locator('[data-section-title="生活日历与日记"]')).to_contain_text(
            '世界类型'
        )
        expect(self.page.locator('[data-section-title="生活日历与日记"]')).to_contain_text(
            '把日历与最近日记注入聊天'
        )
        self.page.get_by_role('button', name='生活与作息', exact=True).click()
        expect(self.page.locator('[data-section-title="生活事项"]')).to_be_visible()
        expect(self.page.locator('[data-section-title="后台预算"]')).to_be_visible()
        expect(self.page.locator('[data-section-title="生活记忆"]')).not_to_be_visible()
        self.page.locator('#settings-search').fill('嵌入')
        self.page.locator('.settings-hit').first.click()
        expect(self.page.get_by_label('生活记忆嵌入模型', exact=True)).to_be_visible()
        self.host.screenshot(path=str(self.shots/'memory-model-settings.png'))
        self.assertEqual(self.errors, [])

    def test_thought_status_stays_compact_and_does_not_fabricate_entries(self):
        snapshot = self.install_console()
        self.page.evaluate("""snapshot=>{
          const read=AstrBotPluginPage.apiGet;
          window.thoughtStatus={session:snapshot.session_id,thoughts:[],health:{enabled:true,clock_recent:true,
            last_tick_at:Date.now()/1000,last_plan_at:Date.now()/1000-3600,current:'安静阅读',message:'生活时钟正常',note:'这里只记录实际留下的想法。'}};
          AstrBotPluginPage.apiGet=(name,args)=>name==='thoughts'?Promise.resolve(structuredClone(thoughtStatus)):read(name,args);
          const picker=document.querySelector('#thought-session');picker.add(new Option('合成测试',snapshot.session_id));picker.value=snapshot.session_id;
        }""", snapshot)
        self.page.locator('button[data-tab="thoughts"]').click()
        expect(self.page.locator('#thoughts-health')).to_contain_text('生活时钟正常')
        self.assertLess(self.page.locator('#thoughts-health').bounding_box()['height'], 70)
        expect(self.page.locator('.thought-row')).to_have_count(0)
        self.host.screenshot(path=str(self.shots/'thoughts-health-desktop.png'))
        self.page.evaluate("Object.assign(thoughtStatus.health,{clock_recent:false,message:'暂未检测到近期生活时钟，请检查运行页'})")
        self.page.locator('#thoughts-refresh').click()
        expect(self.page.locator('#thoughts-health')).to_have_attribute('data-ok', 'false')
        self.host.set_viewport_size({'width':390,'height':844})
        self.page.locator('#thoughts-health summary').click()
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'))
        self.host.screenshot(path=str(self.shots/'thoughts-health-phone.png'))
        self.assertEqual(self.errors, [])
