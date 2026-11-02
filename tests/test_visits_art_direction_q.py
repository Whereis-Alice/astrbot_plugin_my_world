"""The visits page makes confirmed shared life visible without a wall of controls."""
import unittest

from tests import test_visits_browser as visits
from tests.test_generation_browser import expect, sync_playwright


@unittest.skipIf(sync_playwright is None, "Install playwright for browser verification")
class VisitsArtDirectionTests(unittest.TestCase):
    setUpClass = classmethod(visits.VisitsBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(visits.VisitsBrowserTests.tearDownClass.__func__)
    setUp = visits.VisitsBrowserTests.setUp
    open_phone = visits.VisitsBrowserTests.open_phone

    def refresh(self):
        self.page.locator('#tab-visits').get_by_role('button', name='刷新', exact=True).click()

    def appearance(self, palette):
        self.page.locator('#theme-toggle').click()
        self.page.locator(f'[data-appearance="{palette}"]').click()
        self.page.get_by_label('关闭外观设置').click()

    def outing(self):
        self.page.evaluate("""() => {
          visitData.connections[0].alias='小世界'; visitData.connections[0].name='小世界';
          visitData.connections.push({id:'forest',alias:'小世界',actors:[{id:'leaf',name:'莉耶芙',world:'圣森'}]});
          visitData.active={id:'walk',status:'active',arrived_at:1793580600,observed_at:1793583000,
            local:{id:'ours',name:'艾拉'},remote:{id:'friend',name:'爱丽丝',world:'港湾'},
            scene:{place:'旧厨房',activity:'之前一起做饼干'},
            outing:{version:1,stage:1,place:'河岸花园',activity:'沿着花径散步，寻找喜欢的花',
              started_at:1793581200,until:1793584800,
              route:[{place:'港湾茶室',activity:'一起喝茶',started_at:1793580600,until:1793581200},
                     {place:'河岸花园',activity:'沿着花径散步，寻找喜欢的花',started_at:1793581200,until:1793584800}]},
            outing_offer:{place:'未答应的海岛',activity:'还没决定是否去看海',minutes:30},
            log:[{actor_id:'ours',kind:'say',text:'这条小路上开了好多花。'},
                 {actor_id:'friend',kind:'say',text:'我们慢慢走吧。'},
                 {actor_id:'friend',kind:'activity',text:'开始相处：预制的旧句子'}]};
        }""")
        self.refresh()

    def test_idle_page_uses_character_names_and_a_short_home_state(self):
        self.page.evaluate("""() => {
          visitData.connections[0].alias='小世界'; visitData.connections[0].name='小世界';
          visitData.connections.push({id:'forest',alias:'小世界',actors:[{id:'leaf',name:'莉耶芙',world:'圣森'}]});
        }""")
        self.refresh()
        self.assertEqual(self.page.locator('.visits-peer h4').all_text_contents(), ['爱丽丝', '莉耶芙'])
        expect(self.page.locator('.visits-notice')).not_to_be_visible()
        expect(self.page.locator('.visits-current')).to_have_class('visits-current card is-home')
        expect(self.page.locator('.visits-scene-art')).to_have_count(0)
        self.assertLess(self.page.locator('.visits-current').bounding_box()['height'], 42)
        self.assertLess(self.page.locator('.visits-heading').bounding_box()['height'], 52)
        expect(self.page.locator('.visits-settings-body')).not_to_be_visible()
        expect(self.page.locator('.visits-diagnostics')).not_to_be_visible()
        self.open_phone()
        expect(self.page.locator('.visits-phone-head h3')).to_have_text('打给 爱丽丝')
        self.page.get_by_role('button', name='收起', exact=True).click()
        for width, height in ((1440, 1000), (390, 844)):
            self.host.set_viewport_size({'width': width, 'height': height})
            self.assertEqual(self.page.locator('.visits-peer').first.evaluate(
                'node => getComputedStyle(node).marginBottom'), '0px')
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'))
            self.host.screenshot(path=str(self.shots / f'visits-journal-home-{width}.png'), animations='disabled')
        self.assertEqual(self.errors, [])

    def test_confirmed_activity_is_visible_with_real_route_and_time_in_six_themes(self):
        self.outing()
        expect(self.page.locator('.visits-scene-copy h4')).to_have_text('河岸花园')
        expect(self.page.locator('.visits-scene-activity')).to_have_text('沿着花径散步，寻找喜欢的花')
        expect(self.page.locator('.visits-route li')).to_have_count(2)
        expect(self.page.locator('.visits-route [aria-current="step"]')).to_contain_text('河岸花园')
        expect(self.page.get_by_role('progressbar', name='当前共同活动的时间')).to_have_attribute('aria-valuenow', '50')
        expect(self.page.locator('.visits-current')).not_to_contain_text('未答应的海岛')
        expect(self.page.locator('.visits-current')).not_to_contain_text('开始相处：')
        colors = set()
        for palette in ('nocturne', 'aurora', 'rose', 'cloud', 'sand', 'jade'):
            self.appearance(palette)
            colors.add(self.page.locator('.visits-art-land').evaluate('node => getComputedStyle(node).fill'))
            for width, height in ((1440, 1000), (390, 844)):
                self.host.set_viewport_size({'width': width, 'height': height})
                self.page.locator('.visits-current').scroll_into_view_if_needed()
                expect(self.page.locator('.visits-scene-art')).to_be_visible()
                self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'))
                self.host.screenshot(path=str(self.shots / f'visits-journal-{palette}-{width}.png'), animations='disabled')
        self.assertEqual(len(colors), 6)
        self.assertEqual(self.errors, [])

    def test_a_long_confirmed_route_keeps_the_present_stage_and_past_activity_in_history(self):
        self.outing()
        self.page.evaluate("""() => {
          const outing=visitData.active.outing;
          outing.route=Array.from({length:10},(_,index)=>({place:`已到过的地方${index}`,
            activity:`共同活动${index}`,started_at:1793581200+index*60,until:1793581260+index*60}));
          Object.assign(outing,outing.route[9],{stage:9});
        }""")
        self.refresh()
        expect(self.page.locator('.visits-route li')).to_have_count(8)
        expect(self.page.locator('.visits-route [aria-current="step"]')).to_contain_text('已到过的地方9')
        self.page.evaluate("""() => {
          visitData.history=[{...visitData.active,status:'completed'}];
          visitData.active=null;
        }""")
        self.refresh()
        card = self.page.locator('.visits-history > details')
        expect(card.locator('summary')).to_contain_text('爱丽丝 · 已到过的地方9')
        card.locator('summary').click()
        expect(card.locator('.visits-scene-copy h4')).to_have_text('已到过的地方9')
        expect(card.locator('.visits-route li')).to_have_count(8)
        expect(card.locator('[aria-current="step"]')).to_have_count(0)
        expect(card.get_by_role('progressbar')).to_have_count(0)
        self.assertEqual(self.errors, [])

    def test_invitation_plan_does_not_draw_an_unconfirmed_shared_place(self):
        self.page.evaluate("""() => {
          visitData.active={id:'waiting',status:'inviting',scene:{place:'海岛',activity:'看潮汐'},
            outing_offer:{place:'海岛',activity:'看潮汐',minutes:30},log:[]};
        }""")
        self.refresh()
        expect(self.page.locator('.visits-shared-scene')).to_have_count(0)
        expect(self.page.locator('.visits-scene-art')).to_have_count(0)
        expect(self.page.locator('.visits-scene-plan')).to_contain_text('约见打算')
        self.open_phone()
        expect(self.page.get_by_role('button', name='去做客', exact=True)).to_be_enabled()
        self.assertEqual(self.errors, [])

    def test_failed_contacts_and_automatic_budget_notes_stay_out_of_keepsakes(self):
        self.page.evaluate("""() => {
          visitData.history=[{id:'met',status:'completed',arrived_at:1793580600,
            remote:{id:'friend',name:'爱丽丝'},log:[{actor_id:'friend',kind:'say',text:'下次还来这条小路。'}],
            end_reason:'本次相见的预算已用完'}];
          visitData.diagnostics=[{id:'failed',status:'declined',remote:{id:'friend',name:'爱丽丝'},
            end_reason:'今日串门模型预算已用完，稍后再约'}];
          visitData.automatic_budget={allowed:false,reason:'本方今日自动联系额度已用完'};
        }""")
        self.refresh()
        expect(self.page.locator('.visits-history > details')).to_have_count(1)
        expect(self.page.locator('.visits-history')).not_to_contain_text('预算已用完')
        expect(self.page.locator('.visits-history')).not_to_contain_text('告别原因')
        expect(self.page.locator('.visits-diagnostics-body')).not_to_be_visible()
        self.page.locator('.visits-diagnostics > summary').click()
        expect(self.page.locator('.visits-budget-note')).to_contain_text('本方')
        card = self.page.locator('.visits-diagnostics-body > details')
        expect(card).to_have_count(1)
        card.locator('summary').click()
        expect(card).to_contain_text('今日串门模型预算已用完')
        self.assertEqual(self.errors, [])

    def test_arrived_before_joint_confirmation_does_not_draw_the_hosts_plan_as_current_life(self):
        self.page.evaluate("""() => {
          visitData.active={id:'arrived',status:'active',arrived_at:1793580600,outing_version:1,
            remote:{id:'friend',name:'爱丽丝'},scene:{place:'未答应的海岛',activity:'还没决定是否去看海'},
            outing_offer:{place:'未答应的海岛',activity:'还没决定是否去看海',minutes:30},log:[]};
        }""")
        self.refresh()
        expect(self.page.locator('.visits-shared-scene')).to_have_count(0)
        expect(self.page.locator('.visits-scene-art')).to_have_count(0)
        expect(self.page.locator('.visits-current')).to_contain_text('还在商量')
        expect(self.page.locator('.visits-current')).not_to_contain_text('未答应的海岛')
        self.assertEqual(self.errors, [])

    def test_paged_route_uses_the_global_stage_without_losing_the_current_place(self):
        self.outing()
        self.page.evaluate("""() => {
          Object.assign(visitData.active.outing,{stage:65,route_offset:64,route_count:66});
        }""")
        self.refresh()
        expect(self.page.locator('.visits-route [aria-current="step"]')).to_contain_text('河岸花园')
        expect(self.page.locator('.visits-route li')).to_have_count(2)
        expect(self.page.locator('.visits-scene-copy h4')).to_have_text('河岸花园')
        self.assertEqual(self.errors, [])

    def test_dialogue_actions_and_experience_share_one_moment_with_two_distinct_sides(self):
        self.outing()
        self.page.evaluate("""() => {
          visitData.active.log=[
            {seq:1,actor_id:'ours',kind:'say',text:'这朵白色的小花……很像茶室窗边的那一盆。',at:1793583000},
            {seq:2,actor_id:'ours',kind:'action_plan',text:'蹲在花径旁，伸手轻轻托住低垂的叶子。',at:1793583000.2},
            {seq:3,actor_id:'ours',kind:'experience',text:'风把花香带了过来，刚才的紧张慢慢散了。',at:1793583000.3},
            {seq:4,actor_id:'friend',kind:'say',text:'那下次，我们就带一小盆回去。你来选吧。',at:1793583060},
            {seq:5,actor_id:'friend',kind:'action_plan',text:'拿出随身的小本子，把白花的样子画在页角。',at:1793583060.1}];
        }""")
        self.refresh()
        moments = self.page.locator('.visits-current .visits-moment')
        expect(moments).to_have_count(2)
        expect(moments.nth(0)).to_have_class('visits-moment is-local')
        expect(moments.nth(1)).to_have_class('visits-moment is-remote')
        expect(moments.nth(0).locator('strong')).to_have_text('艾拉')
        expect(moments.nth(0).locator('.visits-log-say')).to_contain_text('白色的小花')
        expect(moments.nth(0).locator('.visits-log-action')).to_contain_text('低垂的叶子')
        expect(moments.nth(0).locator('.visits-log-experience')).to_contain_text('紧张慢慢散了')
        self.assertNotEqual(moments.nth(0).locator('.visits-log-say').evaluate(
            'node => getComputedStyle(node).color'), moments.nth(0).locator('.visits-log-action').evaluate(
            'node => getComputedStyle(node).color'))
        for palette in ('nocturne', 'aurora', 'rose', 'cloud', 'sand', 'jade'):
            self.appearance(palette)
            for width, height in ((1440, 1000), (390, 844)):
                self.host.set_viewport_size({'width': width, 'height': height})
                self.page.locator('.visits-current').scroll_into_view_if_needed()
                self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'))
                left, right = moments.nth(0).bounding_box(), moments.nth(1).bounding_box()
                self.assertGreater(right['x'], left['x'])
                self.host.screenshot(path=str(self.shots / f'visits-moments-{palette}-{width}.png'), animations='disabled')
        self.assertEqual(self.errors, [])

    def test_recent_moments_expand_without_losing_earlier_facts_on_refresh(self):
        self.outing()
        self.page.evaluate("""() => {
          visitData.active.log=Array.from({length:60},(_,index)=>({seq:index+1,actor_id:Math.floor(index/3)%2?'friend':'ours',
            kind:['say','action_plan','experience'][index%3],text:`记忆${index+1}`,at:1793581200+Math.floor(index/3)*60}));
        }""")
        self.refresh()
        expect(self.page.locator('.visits-current .visits-moment:visible')).to_have_count(8)
        earlier = self.page.locator('.visits-current .visits-earlier-moments')
        expect(earlier.locator('summary')).to_have_text('更早的片刻 · 12')
        earlier.locator('summary').click()
        expect(self.page.locator('.visits-current .visits-moment:visible')).to_have_count(20)
        expect(earlier).to_contain_text('记忆1')
        self.refresh()
        expect(self.page.locator('.visits-current .visits-moment:visible')).to_have_count(20)
        self.assertEqual(self.errors, [])

    def test_earlier_pages_merge_by_sequence_and_ignore_a_late_reply_after_life_switch(self):
        self.outing()
        self.page.evaluate("""() => {
          const rows=Array.from({length:288},(_,index)=>({seq:index+1,actor_id:Math.floor(index/3)%2?'friend':'ours',
            kind:['say','action_plan','experience'][index%3],text:`完整记忆${index+1}`,at:1793581200+Math.floor(index/3)*60}));
          Object.assign(visitData.active,{log:rows.slice(96),log_offset:96,log_count:288});
          const write=AstrBotPluginPage.apiPost;
          AstrBotPluginPage.apiPost=(endpoint,payload)=>{
            if(endpoint!=='visits/record')return write(endpoint,payload);
            visitWrites.push({endpoint,payload:structuredClone(payload)});
            return Promise.resolve({id:'walk',log:rows.slice(0,96),log_offset:0,log_count:288,next_before:0,has_more:false});
          };
        }""")
        self.refresh()
        earlier = self.page.locator('.visits-current .visits-earlier-moments')
        earlier.locator('summary').click()
        self.page.get_by_role('button', name='继续往前翻', exact=True).click()
        expect(self.page.locator('.visits-current .visits-moment:visible')).to_have_count(96)
        expect(self.page.locator('.visits-current')).to_contain_text('完整记忆1')
        self.assertEqual(self.page.evaluate('visitWrites.at(-1).payload.before'), 97)
        expect(self.page.get_by_role('button', name='继续往前翻', exact=True)).to_have_count(0)
        self.refresh()
        expect(self.page.locator('.visits-current .visits-moment:visible')).to_have_count(96)
        self.page.evaluate("""() => {
          AstrBotPluginPage.apiPost=(endpoint,payload)=>endpoint==='visits/record'
            ? new Promise(resolve=>window.finishOldRecord=resolve) : Promise.resolve({ok:true});
        }""")
        # A different visit drops the previous visit's loaded pages before waiting.
        self.page.evaluate("() => {visitData.active.id='second';}")
        self.refresh()
        earlier.locator('summary').click()
        self.page.get_by_role('button', name='继续往前翻', exact=True).click()
        self.page.locator('#status-session').select_option('test:GroupMessage:200')
        self.page.wait_for_function("visitReads.at(-1).session==='test:GroupMessage:200'")
        self.page.evaluate("""() => finishOldRecord({id:'second',log:[{seq:1,actor_id:'ours',kind:'say',text:'旧生活专属的过往'}],next_before:0})""")
        self.page.wait_for_timeout(100)
        expect(self.page.locator('.visits-current')).not_to_contain_text('旧生活专属的过往')
        self.assertEqual(self.errors, [])
