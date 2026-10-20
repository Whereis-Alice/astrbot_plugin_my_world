"""Exercise compact workspaces and their controls in Chromium."""

import asyncio
import unittest
from concurrent.futures import ThreadPoolExecutor

from tests import test_generation_browser as generation
from tests.test_generation_browser import expect, sync_playwright


async def console_snapshot():
    from tests.test_engine import EngineTestCase, SESSION

    fixture = EngineTestCase()
    await fixture.asyncSetUp()
    try:
        await fixture.engine.load_state(SESSION, cold_start=False)
        return await fixture.engine.snapshot(SESSION)
    finally:
        await fixture.db.close()
        fixture._tmp.cleanup()


@unittest.skipIf(sync_playwright is None, "Install playwright to run browser tests")
class WorkbenchBrowserTests(unittest.TestCase):
    setUpClass = classmethod(generation.GenerationBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(generation.GenerationBrowserTests.tearDownClass.__func__)
    setUp = generation.GenerationBrowserTests.setUp

    def capture_writes(self):
        self.page.evaluate("""() => {
          const original = AstrBotPluginPage.apiPost;
          window.policyWrites = [];
          AstrBotPluginPage.apiPost = (endpoint, data) => {
            if (endpoint === 'config/world') policyWrites.push(structuredClone(data.world));
            return original(endpoint, data);
          };
        }""")

    def install_console(self):
        with ThreadPoolExecutor(max_workers=1) as executor:
            snapshot = executor.submit(asyncio.run, console_snapshot()).result()
        self.page.evaluate("""data => {
          window.numericState = data;
          window.numericWrites = [];
          const read = AstrBotPluginPage.apiGet, write = AstrBotPluginPage.apiPost;
          AstrBotPluginPage.apiGet = (endpoint, params) => endpoint === 'state'
            ? Promise.resolve(structuredClone(numericState)) : read(endpoint, params);
          AstrBotPluginPage.apiPost = (endpoint, data) => {
            if (endpoint === 'state/action' && data.action === 'set_values') {
              numericWrites.push(structuredClone(data));
              Object.assign(numericState.values, data.values);
              return Promise.resolve({ok: true, applied: data.values});
            }
            return write(endpoint, data);
          };
        }""", snapshot)
        self.page.locator('button[data-tab="status"]').click()
        self.page.evaluate("""data => {
          for (const id of ['status-session','map-session']) {
            const select=document.getElementById(id);
            select.add(new Option('合成测试',data.session_id));select.value=data.session_id;
          }
          document.getElementById('status-session').dispatchEvent(new Event('change'));
        }""", snapshot)
        return snapshot

    def install_dense_map(self):
        snapshot = self.install_console()
        self.page.evaluate("""data => {
          window.mapStates=Array.from({length:24},(_,index)=>({
            session_id:index?`test:GroupMessage:${200+index}`:data.session_id,
            state_key:`life_${index}`,node_id:'lobby',state:'idle',
            activity:{kind:'idle',label:'摆弄小兔子发夹并回想刚读过的长篇故事',detail:'活动完整说明。'.repeat(25)}
          }));
          mapStates.push({session_id:'test:GroupMessage:500',node_id:'bedroom',state:'idle',
            activity:{kind:'action',label:'翻看Galgame攻略本中的长篇剧情分析',detail:'长活动说明'}});
          const read=AstrBotPluginPage.apiGet;
          AstrBotPluginPage.apiGet=async(key,params)=>{
            if(key==='states')return {sessions:structuredClone(mapStates)};
            const config=await read(key,params);
            if(key!=='config')return config;
            const ids=['lobby','bedroom','study','bar','window','kitchen'];
            config.world.nodes.filter(n=>n.zone_id==='home').forEach(n=>{
              const index=ids.indexOf(n.id);n.x=80+(index%3)*160;n.y=100+Math.floor(index/3)*150;
            });
            config.world.zone_edges.push(
              {id:'many_exit_1',from_zone:'home',to_zone:'street',from_node:'lobby',to_node:'street_gate',ticks:5,bidirectional:false},
              {id:'many_exit_2',from_zone:'park',to_zone:'home',from_node:'park_gate',to_node:'lobby',ticks:7,bidirectional:false}
            );
            config.scope='';return config;
          };
          const select=document.getElementById('config-scope');
          select.add(new Option('读取密集地图','dense-map'));select.value='dense-map';
          select.dispatchEvent(new Event('change'));
        }""", snapshot)
        self.page.locator('button[data-tab="map"]').click()
        self.page.locator('#map-show-all').check()
        self.page.locator('.node.here[data-kind="zone"]').dblclick()
        self.page.locator('#map-zoom-fit').click()
        return snapshot

    def assert_map_badges_stay_inside_nodes(self):
        self.assertTrue(self.page.evaluate("""() => {
          const overlap=(a,b)=>a.left<b.right&&a.right>b.left&&a.top<b.bottom&&a.bottom>b.top;
          const nodes=[...document.querySelectorAll('#canvas-nodes .node')];
          return nodes.every(node=>[...node.querySelectorAll('.node-activity,.portal-chip')].every(button=>{
            const r=button.getBoundingClientRect(),n=node.getBoundingClientRect();
            return r.left>=n.left-1&&r.right<=n.right+1&&r.top>=n.top-1&&r.bottom<=n.bottom+1
              &&nodes.filter(other=>other!==node).every(other=>!overlap(r,other.getBoundingClientRect()));
          }));
        }"""))

    def test_fit_uses_actual_toolbar_dock_and_side_panel_bounds(self):
        self.install_dense_map()
        for width,height in [(1440,900),(1024,640),(390,844),(844,390)]:
            self.host.set_viewport_size({'width':width,'height':height})
            for opened in [False,True]:
                collapsed = 'collapsed' in self.page.locator('#map-side').get_attribute('class')
                if collapsed == opened:
                    self.page.locator('#map-side-toggle').click()
                    self.page.locator('#map-side').evaluate('e=>Promise.all(e.getAnimations().map(a=>a.finished.catch(()=>{})))')
                self.page.locator('#map-zoom-fit').click()
                self.page.wait_for_function("() => [...document.querySelectorAll('.node')].length > 0")
                result = self.page.evaluate("""() => {
                  const overlap=(a,b)=>a.left<b.right&&a.right>b.left&&a.top<b.bottom&&a.bottom>b.top;
                  const overlays=[document.querySelector('.map-topbar'),document.querySelector('.map-bottom-tools'),
                    ...(!document.querySelector('#map-side').classList.contains('collapsed')?[document.querySelector('#map-side')]:[])];
                  return [...document.querySelectorAll('.node')].flatMap(node=>{
                    const r=node.getBoundingClientRect();
                    return overlays.filter(panel=>overlap(r,panel.getBoundingClientRect())).map(panel=>[node.dataset.id,panel.className]);
                  });
                }""")
                self.assertEqual(result, [], (width,height,opened,result))
        self.assertEqual(self.errors, [])

    def test_manual_map_zoom_survives_resize_and_node_refresh(self):
        self.install_dense_map()
        self.page.locator('#map-zoom-in').click()
        before = self.page.locator('#canvas-world').get_attribute('style')
        self.host.set_viewport_size({'width':1024,'height':640})
        self.page.locator('#map-show-all').dispatch_event('change')
        self.assertEqual(self.page.locator('#canvas-world').get_attribute('style'), before)
        self.page.locator('#map-zoom-fit').click()
        self.assertNotEqual(self.page.locator('#canvas-world').get_attribute('style'), before)
        self.assertEqual(self.errors, [])

    def test_autonomous_sleep_controls_and_status_details_survive_refresh(self):
        snapshot = self.install_console()
        snapshot['sleep'].update(mode='autonomous', rhythm={
            'decision':'continue','reason':'看完这一章再睡','next_evaluation_at':1800000000},
            history=[{'kind':'sleep','hours':7.5,'ended_at':1799900000,'interrupted':False}])
        self.page.evaluate("""data=>{
          numericState=data; document.getElementById('status-session').dispatchEvent(new Event('change'));
        }""",snapshot)
        details=self.page.locator('.sleep-rhythm-details')
        expect(details).to_contain_text('自主作息')
        details.locator('summary').click()
        expect(details).to_contain_text('7.5 小时')
        self.page.locator('#status-session').dispatch_event('change')
        expect(details).to_have_attribute('open','')
        self.page.locator('button[data-tab="settings"]').click()
        self.page.locator('#settings-tabs').get_by_text('生活与作息',exact=True).click()
        self.page.get_by_text('作息与夜晚',exact=True).click()
        expect(self.page.get_by_text('作息模式',exact=True)).to_be_visible()
        expect(self.page.get_by_text('自主作息偏好',exact=True)).to_be_visible()
        self.assertEqual(self.errors, [])

    def test_many_exits_and_long_activities_do_not_cover_neighboring_places(self):
        self.install_dense_map()
        for width,height in [(1440,900),(390,844)]:
            self.host.set_viewport_size({'width':width,'height':height})
            self.page.locator('#map-zoom-fit').click()
            self.assert_map_badges_stay_inside_nodes()
        exits=self.page.locator('.node[data-id="lobby"] .portal-chip')
        expect(exits).to_have_count(1)
        expect(exits).to_contain_text('4')
        exits.click()
        expect(self.page.locator('.map-exit-row')).to_have_count(4)
        expect(self.page.locator('.map-details-body')).to_contain_text('单向入口')
        expect(self.page.locator('.map-details-body')).to_contain_text('单向出口')
        self.page.locator('.map-exit-row').nth(2).focus()
        self.page.locator('#map-show-all').dispatch_event('change')
        expect(self.page.locator('.map-exit-row').nth(2)).to_be_focused()
        self.page.locator('.map-exit-row').first.click()
        expect(self.page.locator('#dialog')).not_to_be_visible()
        expect(self.page.locator('#map-crumbs')).to_contain_text('商业街')
        self.assertEqual(self.errors, [])

    def test_activity_details_scroll_refresh_and_close_outside_the_scaled_map(self):
        self.install_dense_map()
        self.page.locator('.node[data-id="lobby"] .node-activity').click()
        expect(self.page.locator('.node-activity-row')).to_have_count(24)
        self.host.set_viewport_size({'width':390,'height':844})
        self.assertTrue(self.page.locator('#dialog-card').evaluate("""card=>{
          const r=card.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&r.bottom<=innerHeight;
        }"""))
        self.assertTrue(self.page.locator('#dialog-body').evaluate('e=>e.scrollHeight>e.clientHeight'))
        self.page.locator('#dialog-body').evaluate('e=>e.scrollTop=180')
        self.page.evaluate("mapStates[0].activity.label='刷新后的此刻活动'")
        self.page.locator('button[data-tab="map"]').dispatch_event('click')
        expect(self.page.locator('.map-details-body')).to_contain_text('刷新后的此刻活动')
        self.assertGreaterEqual(self.page.locator('#dialog-body').evaluate('e=>e.scrollTop'),150)
        self.page.locator('#dialog-ok').click()
        expect(self.page.locator('#dialog')).not_to_be_visible()
        self.assert_map_badges_stay_inside_nodes()
        self.assertEqual(self.errors, [])

    def test_numeric_draft_survives_refresh_and_top_button_saves_only_changed_fields(self):
        self.install_console()
        self.page.locator('#values-edit').click()
        slider = self.page.locator('#status-values input[type="range"]').nth(1)
        slider.evaluate("node => {node.value='0.21';node.dispatchEvent(new Event('input',{bubbles:true}));}")
        self.page.locator('#status-refresh').click()
        expect(slider).to_have_value('0.21')
        self.page.locator('#values-edit').click()
        self.page.wait_for_function('numericWrites.length === 1')
        self.assertEqual(self.page.evaluate('numericWrites[0].values'), {'loneliness': 0.21})
        expect(self.page.locator('#values-edit')).to_have_text('编辑数值')
        self.assertEqual(self.errors, [])

    def test_idle_settings_validate_and_action_role_options_are_readable(self):
        self.capture_writes()
        self.page.locator('button[data-tab="actions"]').click()
        self.page.locator('#action-generate-idle').locator('xpath=ancestor::details[1]/summary').click()
        self.page.locator('#action-idle-settings').click()
        self.page.locator('[data-dialog-key="min"]').fill('40')
        self.page.locator('[data-dialog-key="max"]').fill('20')
        self.page.locator('#dialog-ok').click()
        expect(self.page.locator('#dialog')).to_be_visible()
        self.page.locator('[data-dialog-key="max"]').fill('50')
        self.page.locator('[data-dialog-key="enabled"]').uncheck()
        self.page.locator('#dialog-ok').click()
        self.page.locator('#save').click()
        self.page.wait_for_function('policyWrites.length > 0')
        self.assertFalse(self.page.evaluate('policyWrites.at(-1).idle_life.enabled'))
        self.page.locator('.action-card').first.click()
        select = self.page.locator('#action-form label').filter(has_text='生活影响').locator('select')
        select.select_option('interest')
        expect(select.locator('option:checked')).to_contain_text('兴趣')
        self.assertEqual(self.errors, [])

    def test_idle_generator_previews_then_commits_quiet_actions(self):
        self.page.locator('button[data-tab="actions"]').click()
        self.page.locator('#action-generate-idle').locator('xpath=ancestor::details[1]/summary').click()
        self.page.locator('#action-generate-idle').click()
        self.page.locator('#dialog-ok').click()
        self.page.wait_for_function('pendingGeneration.length > 0')
        self.page.evaluate("""() => finishGeneration({actions:[{id:'idle_sample',name:'轻轻哼歌',description:'哼一段旋律',
          idle:true,category:'continuous',scope:'global',llm_level:'template',group:'闲置生活',life_role:'interest',
          enabled:true,target_type:'none',on_complete:{trigger:'none',effects:{}}}],problems:[]})""")
        expect(self.page.locator('#dialog-title')).to_contain_text('闲置动作草稿')
        expect(self.page.locator('.draft-row button').first).to_have_text('全局')
        self.page.locator('#dialog-ok').click()
        expect(self.page.locator('.action-card').filter(has_text='轻轻哼歌')).to_contain_text('闲置生活')
        self.assertEqual(self.errors, [])

    def test_map_shows_quiet_activity_and_full_hover_text_in_both_levels(self):
        snapshot = self.install_console()
        self.page.evaluate("""data => {
          const read = AstrBotPluginPage.apiGet;
          AstrBotPluginPage.apiGet = (endpoint, params) => endpoint === 'states'
            ? Promise.resolve({sessions:[{session_id:data.session_id,node_id:'bedroom',state:'idle',
              activity:{kind:'idle',label:'轻轻哼歌',detail:'哼一段熟悉的旋律，完整说明。'}}]}) : read(endpoint,params);
        }""", snapshot)
        self.page.locator('button[data-tab="map"]').click()
        badge = self.page.locator('.node-activity').first
        expect(badge).to_contain_text('轻轻哼歌')
        expect(badge).to_contain_text('正在')
        expect(badge.locator('.node-activity-icon')).to_have_text('🍃')
        self.assertIn('完整说明', badge.get_attribute('title'))
        self.page.locator('.node.here[data-kind="zone"]').dblclick()
        expect(self.page.locator('.node.here[data-kind="node"] .node-activity')).to_contain_text('轻轻哼歌')
        self.assertEqual(self.errors, [])

    def test_multi_session_activity_and_portals_do_not_hide_sleeping_badge(self):
        snapshot = self.install_console()
        self.page.evaluate("""data => {
          window.mapStates=[{session_id:data.session_id,state_key:data.session_id,node_id:'lobby',state:'sleeping',
            activity:{kind:'action',label:'睡觉',detail:'正式睡眠'}},
            {session_id:'test:GroupMessage:200',state_key:data.session_id,node_id:'lobby',state:'sleeping',activity:{kind:'action',label:'睡觉'}},
            {session_id:'test:GroupMessage:300',state_key:'other',node_id:'lobby',state:'idle',activity:{kind:'idle',label:'轻轻哼歌'}}];
          const read=AstrBotPluginPage.apiGet;
          AstrBotPluginPage.apiGet=(key,params)=>key==='states'?Promise.resolve({sessions:mapStates}):read(key,params);
        }""", snapshot)
        self.page.locator('button[data-tab="map"]').click()
        self.page.locator('#map-show-all').check()
        self.page.locator('#map-zoom-fit').click()
        expect(self.page.locator('.node.here .node-activity').first).to_contain_text('正在睡觉')
        self.page.locator('.node.here[data-kind="zone"]').dblclick()
        self.page.locator('#map-zoom-fit').click()
        badge = self.page.locator('.node[data-id="lobby"] .node-activity')
        expect(badge).to_contain_text('+2')
        self.assertTrue(badge.evaluate("""badge => {
          const r=badge.getBoundingClientRect();
          return [...document.querySelectorAll('.portal-chip')].every(chip => {const c=chip.getBoundingClientRect();return c.bottom<=r.top || c.top>=r.bottom || c.right<=r.left || c.left>=r.right;});
        }"""))
        badge.click()
        expect(self.page.locator('.node-activity-row')).to_have_count(2)
        expect(self.page.locator('.map-details-body')).to_contain_text('200')
        expect(self.page.locator('.map-details-body')).to_contain_text('轻轻哼歌')
        self.host.set_viewport_size({'width':390,'height':844})
        self.page.locator('#map-show-all').dispatch_event('change')
        expect(self.page.locator('#dialog')).to_be_visible()
        self.assertTrue(self.page.locator('.map-details-body').evaluate("""list => {
          const r=list.getBoundingClientRect();
          return r.left>=0 && r.right<=innerWidth;
        }"""))
        self.page.locator('#dialog-close').click()
        self.page.locator('#map-show-all').uncheck()
        expect(self.page.locator('.node[data-id="lobby"] .node-activity')).to_contain_text('睡觉')
        self.assertEqual(self.page.locator('.node[data-id="lobby"] .node-activity-count').count(), 0)
        self.assertEqual(self.errors, [])

    def test_world_smart_routes_only_show_the_hovered_zones_direct_routes(self):
        self.page.locator('button[data-tab="map"]').click()
        self.page.locator('#tab-map [data-label-mode="smart"]').click()
        self.page.locator('#map-side-close').click()
        self.page.locator('#map-zoom-fit').click()
        self.host.mouse.move(0, 0)
        expect(self.page.locator('.edge-path:visible')).to_have_count(0)
        expect(self.page.locator('.edge-pair:visible')).to_have_count(0)
        target=self.page.locator('.node[data-kind="zone"]').first
        identifier=target.get_attribute('data-id')
        target.hover()
        self.assertGreater(self.page.locator('.edge-path:visible').count(), 0)
        for path in self.page.locator('.edge-path:visible').all():
            self.assertIn(identifier,[path.get_attribute('data-from'),path.get_attribute('data-to')])
        expect(self.page.locator('.edge-tick-chip:visible')).to_have_count(0)
        self.host.mouse.move(0, 0)
        expect(self.page.locator('.edge-pair:visible')).to_have_count(0)
        expect(self.page.locator('.edge-path:visible')).to_have_count(0)
        self.page.locator('#tab-map [data-label-mode="all"]').click()
        self.assertGreater(self.page.locator('.edge-pair:visible').count(),0)
        self.assertEqual(self.errors, [])

    def test_selected_life_group_keeps_member_activities_in_both_maps(self):
        snapshot = self.install_console()
        self.page.evaluate("""data => {
          const read=AstrBotPluginPage.apiGet;
          AstrBotPluginPage.apiGet=async (key,params)=>key==='states'?{sessions:[
            {session_id:'test:GroupMessage:200',state_key:data.session_id,node_id:'lobby',state:'sleeping',activity:{kind:'action',label:'睡觉'}},
            {session_id:data.session_id,state_key:data.session_id,node_id:'lobby',state:'sleeping',activity:{kind:'action',label:'睡觉'}},
            {session_id:'test:GroupMessage:300',node_id:'lobby',state:'idle',activity:{kind:'idle',label:'旁人的活动'}}
          ]}:key==='config'?{...await read(key,params),scope:'',sessions:{
            sessions:[{session_id:data.session_id},{session_id:'test:GroupMessage:200'},{session_id:'test:GroupMessage:300'}],
            groups:[{id:'shared_life',name:'共享生活',main_session:data.session_id,sessions:[data.session_id,'test:GroupMessage:200']}]
          }}:read(key,params);
          const select=document.getElementById('config-scope');
          select.add(new Option('重新读取测试配置','refresh-groups'));select.value='refresh-groups';
          select.dispatchEvent(new Event('change'));
        }""", snapshot)
        self.page.locator('button[data-tab="map"]').click()
        self.page.locator('#map-session').select_option('shared_life')
        self.page.locator('#map-show-all').uncheck()
        expect(self.page.locator('.node.here .node-activity')).to_contain_text('睡觉')
        expect(self.page.locator('.node.here.other')).to_have_count(0)
        self.page.locator('.node.here[data-kind="zone"]').dblclick()
        expect(self.page.locator('.node.here .node-activity')).to_contain_text('+1')
        self.page.locator('.node.here .node-activity').click()
        expect(self.page.locator('.node-activity-row')).to_have_count(1)
        expect(self.page.locator('.node-activity-session')).to_contain_text('当前 · ')
        expect(self.page.locator('.map-details-body')).not_to_contain_text('旁人的活动')
        self.assertEqual(self.errors, [])

    def test_event_policy_validates_weights_and_saves_the_new_switch(self):
        self.capture_writes()
        self.page.locator('button[data-tab="life"]').click()
        self.page.locator('[data-life-view="experiences"][role="tab"]').click()
        self.page.get_by_role('button', name='投递与事件设置…', exact=True).click()
        self.page.locator('#event-auto').click()
        expect(self.page.locator('#dialog-title')).to_have_text('自动生成事件')
        for key in ['daily', 'good', 'bad', 'annoying', 'strange']:
            self.page.locator(f'[data-dialog-key="{key}"]').fill('0')
        self.page.locator('#dialog-ok').click()
        expect(self.page.locator('#dialog')).to_be_visible()
        self.page.locator('[data-dialog-key="good"]').fill('10')
        self.page.locator('[data-dialog-key="enabled"]').uncheck()
        self.page.locator('[data-dialog-key="max_per_day"]').fill('4')
        self.page.locator('#dialog-ok').click()
        expect(self.page.locator('#dialog')).to_be_hidden()
        self.page.locator('.life-manager[open] .life-manager-save').click()
        self.page.wait_for_function('policyWrites.length > 0')
        policy = self.page.evaluate('policyWrites.at(-1).events.auto')
        self.assertFalse(policy['enabled'])
        self.assertEqual(policy['max_per_day'], 4)
        self.assertEqual(policy['tone_weights']['good'], 10)
        self.assertEqual(self.errors, [])


    def test_console_menus_and_action_search_leave_space_for_content(self):
        self.page.locator('button[data-tab="status"]').click()
        expect(self.page.locator('#state-tick')).to_be_hidden()
        self.page.locator('.console-toolbar .command-menu').first.locator('summary').click()
        expect(self.page.locator('#state-tick')).to_be_visible()
        expect(self.page.locator('#state-clear-context')).to_be_visible()
        self.page.locator('button[data-tab="actions"]').click()
        self.page.locator('#action-search').fill('想事情')
        expect(self.page.locator('.action-card')).to_have_count(1)
        self.page.locator('.action-card').focus()
        self.page.locator('.action-card').press('Enter')
        expect(self.page.locator('#action-drawer')).to_be_visible()
        self.page.locator('#action-cancel').click()
        self.host.set_viewport_size({'width': 390, 'height': 844})
        menu = self.page.locator('#action-menu')
        self.assertTrue(menu.evaluate('node => node.scrollWidth > node.clientWidth && node.clientHeight < 64'))
        menu.evaluate('node => node.scrollLeft = node.scrollWidth')
        self.assertTrue(menu.evaluate('node => node.lastElementChild.getBoundingClientRect().right <= node.getBoundingClientRect().right + 1'))
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        self.assertEqual(self.errors, [])



    def test_map_commands_and_property_panel_do_not_cover_each_other(self):
        self.page.locator('button[data-tab="map"]').click()
        for width, height in [(1600, 900), (1280, 692), (1024, 640), (390, 844)]:
            self.host.set_viewport_size({'width': width, 'height': height})
            if width < 1100:
                expect(self.page.locator('#map-side')).to_have_class('map-side collapsed')
                self.page.locator('#map-zoom-fit').click(trial=True)
            self.page.locator('#map-zoom-fit').click()
            self.assertTrue(self.page.locator('#canvas').evaluate("""canvas => {
              const stage=canvas.closest('.map-stage'), c=canvas.getBoundingClientRect(), s=stage.getBoundingClientRect();
              return stage.scrollTop === 0 && stage.scrollLeft === 0 && canvas.scrollTop === 0 && canvas.scrollLeft === 0
                && Math.abs(c.top-s.top) <= 1 && Math.abs(c.left-s.left) <= 1;
            }"""), f'canvas remains aligned after focusing fit at {width}')
            self.assertTrue(self.page.locator('.map-topbar').evaluate("""bar => {
              const controls=[...bar.querySelectorAll('button,select,summary')].filter(el=>el.getClientRects().length && !el.closest('.tool-menu-body'));
              return controls.every(el=>{const r=el.getBoundingClientRect();const b=bar.getBoundingClientRect();
                return r.left>=b.left && r.right<=b.right+1 && r.top>=b.top && r.bottom<=b.bottom+1;});
            }"""), f'toolbar bounds at {width}')
            for selector in ['#map-routes', '#zone-add', '#map-side-toggle', '#toolbar-world .tool-menu > summary']:
                self.page.locator(selector).click(trial=True)
            menu = self.page.locator('#toolbar-world .tool-menu')
            menu.locator('summary').click()
            self.page.locator('#map-relayout').click(trial=True)
            menu.locator('summary').click()
            if width > 1100:
                self.assertTrue(self.page.locator('#map-side').evaluate("node => node.getBoundingClientRect().top >= document.querySelector('.map-topbar').getBoundingClientRect().bottom"))
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        self.assertEqual(self.errors, [])

    def test_short_desktop_sidebar_retains_the_first_navigation_item(self):
        self.host.set_viewport_size({'width': 1600, 'height': 640})
        self.page.locator('button[data-tab="presets"]').click()
        self.page.locator('button[data-tab="actions"]').click()
        self.assertTrue(self.page.locator('.sidebar .tabs').evaluate('node => node.scrollHeight <= node.clientHeight + 1 && node.scrollTop === 0'))
        self.assertEqual(self.errors, [])

    def test_many_expanded_schedule_records_retain_full_intrinsic_height(self):
        logs = {'events': [{
            'id': index + 1, 'created_at': 1792230000 + index, 'world_time': index,
            'event_type': 'schedule', 'detail': {
                'run_id': f'run:{index}', 'id': f'plan_{index}', 'time': '20:30',
                'actions': '读书 → 散步', 'notes': ['执行完毕'],
                'steps': [{'event_type': 'action', 'world_time': step,
                           'text': ('完整的过程与结果，不应被列表压扁。' * 25) + f'末尾标记{step}'} for step in range(12)],
            },
        } for index in range(6)], 'types': ['schedule'], 'has_more': False}
        self.page.evaluate("""logs => {
          const original = AstrBotPluginPage.apiGet;
          AstrBotPluginPage.apiGet = (endpoint, params) => endpoint === 'logs' ? Promise.resolve(logs) : original(endpoint, params);
        }""", logs)
        self.page.locator('button[data-tab="life"]').click()
        self.page.locator('[data-life-view="today"][role="tab"]').click()
        self.page.get_by_role('button', name='编辑日程 ↗', exact=True).click()
        self.page.locator('#schedule-session').evaluate("node => {node.add(new Option('测试', 's')); node.value='s'; node.dispatchEvent(new Event('change'));}")
        expect(self.page.locator('.schedule-run-row')).to_have_count(6)
        self.page.locator('.schedule-run-head').first.click()
        row = self.page.locator('.schedule-run-row').first
        self.assertTrue(row.evaluate('node => node.clientHeight >= node.scrollHeight - 2'))
        tail = row.locator('.schedule-run-step-text').last
        tail.scroll_into_view_if_needed()
        self.assertTrue(tail.evaluate("node => {const row=node.closest('.schedule-run-row').getBoundingClientRect(); const tail=node.getBoundingClientRect(); return tail.bottom <= row.bottom;}"))
        expect(tail).to_contain_text('末尾标记11')
        self.host.screenshot(path=str(self.shots / 'schedule-long-record.png'))
        self.assertEqual(self.errors, [])

    def test_route_inspector_sends_the_current_draft_and_explains_one_way_exits(self):
        self.page.evaluate("""() => {
          window.routeRequests = [];
          const original = AstrBotPluginPage.apiPost;
          AstrBotPluginPage.apiPost = (endpoint, data) => {
            if (endpoint !== 'map/inspect') return original(endpoint, data);
            routeRequests.push(structuredClone(data));
            return Promise.resolve({path:[data.start,data.target],ticks:3,tick_seconds:30,reachable:[data.start,data.target],unreachable:[],no_return:[data.target],warnings:[]});
          };
        }""")
        self.page.locator('button[data-tab="map"]').click()
        self.page.locator('#map-routes').click()
        self.page.locator('#dialog-ok').click()
        expect(self.page.locator('.map-route-result')).to_contain_text('无法沿地图返回')
        self.assertGreater(len(self.page.evaluate('routeRequests[0].map.nodes')), 1)
        self.assertEqual(self.errors, [])
