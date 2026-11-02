"""Real browser acceptance for invitations, safe rendering and pairing secret lifetime."""

import unittest

from tests import test_generation_browser as generation
from tests.test_generation_browser import expect, sync_playwright


@unittest.skipIf(sync_playwright is None, "Install playwright for browser verification")
class VisitsBrowserTests(unittest.TestCase):
    setUpClass = classmethod(generation.GenerationBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(generation.GenerationBrowserTests.tearDownClass.__func__)

    def setUp(self):
        # The general visual harness has no worlds. Add a real selectable life
        # before boot so this test exercises the ordinary scope picker.
        payload = generation.build_payload()
        payload["config"]["sessions"]["sessions"] = [
            {"session_id": "test:GroupMessage:100", "enabled": True},
            {"session_id": "test:GroupMessage:200", "enabled": True},
        ]
        from unittest.mock import patch
        with patch.object(generation, "build_payload", return_value=payload):
            generation.GenerationBrowserTests.setUp(self)
        self.page.evaluate("""() => {
          const read=AstrBotPluginPage.apiGet, write=AstrBotPluginPage.apiPost;
          window.visitWrites=[]; window.visitReads=[];
          window.visitData={enabled:true,listener:{host:'0.0.0.0',port:38471},
            connections:[{id:'server',alias:'港湾的爱丽丝',url:'https://friend.example',enabled:true,
              actors:[{id:'friend',name:'爱丽丝',world:'港湾世界'}]}],
            lives:[{scope:'test:GroupMessage:100',label:'爱丽丝的共享生活',enabled:true},
              {scope:'test:GroupMessage:200',label:'新生活',enabled:false}],
            enabled_scopes:['test:GroupMessage:100'],scope_enabled:true,
            peers:[{id:'friend',connection_id:'server',name:'爱丽丝',alias:'港湾的爱丽丝',world:'港湾世界',enabled:true}],
            active:null,history:[]};
          AstrBotPluginPage.apiGet=(endpoint,payload)=>{
            if(endpoint==='visits'){
              visitReads.push(structuredClone(payload));
              const result=structuredClone(visitData);
              result.scope_enabled=result.enabled_scopes.includes(payload.session);
              if(!result.scope_enabled)result.peers=[];
              return Promise.resolve(result);
            }
            return read(endpoint,payload);
          };
          AstrBotPluginPage.apiPost=(endpoint,payload)=>{
            if(!endpoint.startsWith('visits/'))return write(endpoint,payload);
            visitWrites.push({endpoint,payload:structuredClone(payload)});
            if(endpoint==='visits/pair-code')return Promise.resolve({code:'one-use-code',expires_at:Date.now()/1000+600,url:'https://ours.example'});
            if(endpoint==='visits/scopes'){
              visitData.enabled_scopes=payload.sessions;
              visitData.lives.forEach(life=>life.enabled=payload.sessions.includes(life.scope));
            }
            return Promise.resolve({ok:true});
          };
        }""")
        self.page.locator('button[data-tab="visits"]').click()
        expect(self.page.locator('.visits-peer')).to_have_count(1)

    def open_phone(self):
        if not self.page.locator('.visits-phone').is_visible():
            self.page.get_by_role('button', name='打电话', exact=True).first.click()

    def open_settings(self):
        if not self.page.locator('.visits-settings').evaluate('node => node.open'):
            self.page.locator('.visits-settings > summary').click()

    def open_pairing(self):
        self.open_settings()
        self.page.locator('.visits-pairing > summary').click()

    def test_directory_is_compact_and_configuration_and_phone_are_collapsed(self):
        expect(self.page.locator('.visits-phone')).not_to_be_visible()
        expect(self.page.locator('.visits-lives')).not_to_be_visible()
        expect(self.page.get_by_label('这次邀请想一起做什么')).to_have_count(0)
        expect(self.page.locator('.visits-peer input, .visits-peer select')).to_have_count(0)
        self.assertLess(self.page.locator('.visits-peer').bounding_box()['height'], 160)
        self.open_phone()
        expect(self.page.locator('.visits-peer.selected')).to_have_count(1)
        expect(self.page.get_by_role('button', name='打电话', exact=True)).to_have_attribute('aria-expanded', 'true')
        self.assertEqual(self.page.evaluate('visitWrites.length'), 0)
        self.page.get_by_role('button', name='收起', exact=True).click()
        expect(self.page.locator('.visits-phone')).not_to_be_visible()
        expect(self.page.get_by_role('button', name='打电话', exact=True)).to_have_attribute('aria-expanded', 'false')
        self.assertEqual(self.errors, [])

    def test_pending_contact_is_not_displayed_as_an_arrived_visit(self):
        self.open_phone()
        for status in ('inviting', 'offered'):
            with self.subTest(status=status):
                self.page.evaluate("""status => {
                  visitData.active={id:'waiting',status,created_at:1761990000,
                    remote:{id:'friend',name:'爱丽丝'},log:[],contact_error:'对方串门服务暂时不可用（HTTP 503）'};
                }""", status)
                self.page.locator('#tab-visits').get_by_role('button', name='刷新', exact=True).click()
                expect(self.page.locator('.visits-phone-hint')).to_have_text('邀请还在等待决定，双方答应后才会相见。')
                expect(self.page.get_by_role('button', name='取消邀请', exact=True)).to_be_enabled()
                expect(self.page.locator('.visits-channel-status')).to_have_text('对方串门服务暂时不可用（HTTP 503）')
                if status == 'offered':
                    expect(self.page.locator('.visits-current h3')).to_contain_text('等待接待决定')
        self.page.evaluate("""() => {
          visitData.active.status='active';visitData.active.arrived_at=1761990100;delete visitData.active.contact_error;
        }""")
        self.page.locator('#tab-visits').get_by_role('button', name='刷新', exact=True).click()
        expect(self.page.get_by_role('button', name='告别归家', exact=True)).to_be_enabled()
        expect(self.page.locator('.visits-phone-hint')).to_have_text('她们正在相见。新的安排会接着这次经历考虑。')
        expect(self.page.locator('.visits-channel-status')).to_have_count(0)
        self.assertEqual(self.errors, [])

    def test_reading_history_survives_refresh_but_does_not_follow_a_life_switch(self):
        self.page.evaluate("""() => {
          visitData.history=[{id:'past-call',status:'completed',created_at:1761990000,arrived_at:1761990000,
            remote:{id:'friend',name:'爱丽丝'},end_reason:'告别归家',
            log:[{actor_id:'friend',kind:'say',text:'下次一起喝茶。'}]}];
        }""")
        refresh = self.page.locator('#tab-visits').get_by_role('button', name='刷新', exact=True)
        refresh.click()
        card = self.page.locator('.visits-history details[data-visit-id="past-call"]')
        expect(card.locator('summary .visits-history-time')).not_to_have_text('')
        card.locator('summary').click()
        expect(card.locator('.visits-visit-body')).to_be_visible()
        refresh.click()
        expect(card.locator('.visits-visit-body')).to_be_visible()
        self.page.locator('#status-session').select_option('test:GroupMessage:200')
        self.page.wait_for_function("visitReads.at(-1).session==='test:GroupMessage:200'")
        expect(card.locator('.visits-visit-body')).not_to_be_visible()
        self.assertEqual(self.errors, [])

    def test_invite_both_directions_and_preserve_life_scope(self):
        self.open_phone()
        self.assertEqual(self.page.get_by_label('去见哪位朋友', exact=True).count(), 0)
        self.page.get_by_label('这次邀请想一起做什么').fill('一起下完昨天那盘棋')
        self.page.get_by_role('button', name='去做客', exact=True).click()
        self.page.wait_for_function('visitWrites.length===1')
        self.assertEqual(self.page.evaluate('visitWrites[0].payload.direction'), 'outgoing')
        self.assertEqual(self.page.evaluate('visitWrites[0].payload.reason'), '一起下完昨天那盘棋')
        self.assertEqual(self.page.evaluate('visitWrites[0].payload.peer_id'), 'friend')
        self.assertEqual(self.page.evaluate('visitWrites[0].payload.session'), self.page.locator('#status-session').input_value())
        self.page.get_by_role('button', name='请她来', exact=True).click()
        self.page.wait_for_function('visitWrites.length===2')
        self.assertEqual(self.page.evaluate('visitWrites[1].payload.direction'), 'incoming')
        self.assertEqual(self.errors, [])

    def test_pairing_inputs_clear_secrets_after_submit_and_leave(self):
        self.open_pairing()
        self.page.get_by_role('button', name='生成一次性配对码', exact=True).click()
        expect(self.page.get_by_label('本次配对码', exact=True)).to_have_value('one-use-code')
        self.page.get_by_label('对方串门地址', exact=True).fill('https://friend.example')
        self.page.get_by_label('对方的一次性配对码', exact=True).fill('friend-code')
        self.page.get_by_label('给这个友邻的称呼', exact=True).fill('另一个世界')
        self.page.get_by_role('button', name='确认配对', exact=True).click()
        self.page.wait_for_function('visitWrites.length===2')
        self.assertEqual(self.page.evaluate('visitWrites[1].payload.code'), 'friend-code')
        self.assertFalse(self.page.evaluate("visitWrites.some(item=>'session' in item.payload)"))
        expect(self.page.get_by_label('对方的一次性配对码', exact=True)).to_have_value('')
        self.page.locator('button[data-tab="status"]').click()
        expect(self.page.get_by_label('本次配对码', exact=True)).to_have_value('')
        self.assertEqual(self.errors, [])

    def test_remote_text_is_plain_text_and_phone_stays_inside_viewport(self):
        self.page.evaluate("""() => {
          visitData.connections[0].alias='<img src=x onerror="window.injected=true">';
          visitData.connections[0].catalog_error='<img src=x onerror="window.injected=true">';
          visitData.lives[0].label='<script>window.injected=true</script>';
          visitData.active={id:'trip',peer_name:'港湾',status:'visiting',scene:{place:'厨房',activity:'做饼干'},
            transcript:[{speaker:'港湾的爱丽丝',say:'<script>window.injected=true</script>',action:'一起揉面'}]};
        }""")
        self.page.locator('#tab-visits').get_by_role('button', name='刷新', exact=True).click()
        expect(self.page.locator('.visits-transcript')).to_contain_text('<script>window.injected=true</script>')
        self.assertFalse(self.page.evaluate('Boolean(window.injected)'))
        self.assertEqual(self.page.locator('#tab-visits img').count(), 0)
        for width, height in [(1440, 1000), (390, 844)]:
            self.host.set_viewport_size({'width':width, 'height':height})
            expect(self.page.locator('.visits-peer')).to_be_visible()
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'))
            self.host.screenshot(path=str(self.shots / f'visits-{width}.png'))
            self.open_settings()
            self.page.locator('.visits-lives').scroll_into_view_if_needed()
            self.host.screenshot(path=str(self.shots / f'visits-lives-{width}.png'))
        self.assertEqual(self.errors, [])

    def test_successful_meetings_and_failed_contacts_have_separate_records(self):
        self.page.evaluate("""() => {
          visitData.history=[
            {id:'declined',peer_name:'港湾',status:'declined',reason:'一起做饼干',end_reason:'她正在休息，今晚不方便。'},
            {id:'interrupted',peer_name:'茶室',status:'interrupted',end_reason:'对方暂时无法连接。'},
            {id:'completed',peer_name:'森林',status:'completed',arrived_at:1793500200,
             end_reason:'已经约好下次再见，先回家了。',log:[{actor_id:'friend',kind:'say',text:'下次再一起喝茶。'}]},
            {id:'legacy',peer_name:'旧友邻',status:'failed',end_reason:'   '}
          ];
        }""")
        self.page.locator('#tab-visits').get_by_role('button', name='刷新', exact=True).click()
        cards = self.page.locator('.visits-history > details')
        expect(cards).to_have_count(1)
        cards.locator('summary').click()
        expect(cards).to_contain_text('下次再一起喝茶。')
        expect(cards.locator('.visits-end-reason')).to_have_count(0)
        self.page.locator('.visits-diagnostics > summary').click()
        failed = self.page.locator('.visits-diagnostics-body > details')
        expect(failed).to_have_count(3)
        for index, reason in enumerate(['她正在休息，今晚不方便。', '对方暂时无法连接。',
                                        '这条联系没有留下具体说明。']):
            card = failed.nth(index)
            card.locator('summary').click()
            expect(card.locator('.visits-end-reason p')).to_have_text(reason)
            expect(card.locator('.visits-end-reason strong')).to_have_count(0)
        expect(failed.first).to_contain_text('一起做饼干')
        self.assertEqual(self.errors, [])

    def test_history_end_reason_is_plain_text_and_wraps_on_phone(self):
        reason = '<img src=x onerror="window.injected=true">\n' + '未确认的远端说明' * 45
        self.page.evaluate("""reason => {
          visitData.history=[{id:'ended',peer_name:'港湾',status:'interrupted',end_reason:reason}];
        }""", reason)
        self.page.locator('#tab-visits').get_by_role('button', name='刷新', exact=True).click()
        expect(self.page.locator('.visits-history > details')).to_have_count(0)
        self.page.locator('.visits-diagnostics > summary').click()
        card = self.page.locator('.visits-diagnostics-body > details')
        expect(card).to_have_count(1)
        card.locator('summary').click()
        expect(card.locator('.visits-end-reason p')).to_have_text(reason)
        self.assertFalse(self.page.evaluate('Boolean(window.injected)'))
        self.assertEqual(card.locator('img').count(), 0)
        for width, height in [(1440, 1000), (390, 844)]:
            self.host.set_viewport_size({'width':width, 'height':height})
            card.scroll_into_view_if_needed()
            expect(card.locator('.visits-end-reason')).to_be_visible()
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'))
            self.host.screenshot(path=str(self.shots / f'visits-end-reason-{width}.png'))
        self.assertEqual(self.errors, [])

    def test_phone_reply_and_availability_are_distinct_and_keep_actor_sources(self):
        self.page.evaluate("""() => {
          visitData.connections.push(
            {id:'tea',alias:'茶室',enabled:true,actors:[{id:'isla',name:'艾拉',world:'窗边的茶室'}]},
            {id:'forest',alias:'森林小屋',enabled:true,actors:[{id:'leaf',name:'莉耶芙',world:'圣森'}]});
          visitData.active={id:'trip',peer_name:'港湾的爱丽丝',status:'visiting',
            local:{id:'ours',name:'总羽爱乃'},remote:{id:'friend',name:'爱丽丝'},
            scene:{place:'港湾的厨房',activity:'一起做饼干'},created_at:1793500200,
            log:[{actor_id:'friend',kind:'contact_reply',text:'好呀，带上昨天那盘棋吧。',at:1793500200},
              {actor_id:'ours',kind:'say',text:'那我过来了。',at:1793500260},
              {actor_id:'friend',kind:'say',text:'先尝一块刚出炉的饼干。',at:1793500320}]};
          visitData.history=[{id:'earlier',peer_name:'茶室的艾拉',status:'declined',
            reason:'一起喝杯茶',end_reason:'对方正在休息，暂时不方便。',
            local:{id:'ours',name:'总羽爱乃'},remote:{id:'isla',name:'艾拉'},
            log:[{actor_id:'isla',kind:'contact_status',text:'正在休息，暂时不方便。',at:1793499900}]}];
        }""")
        self.page.locator('#tab-visits').get_by_role('button', name='刷新', exact=True).click()
        reply = self.page.locator('.visits-current .visits-contact-reply')
        expect(reply.locator('strong')).to_have_text('爱丽丝')
        expect(reply.locator('.visits-log-kind')).to_have_text('电话答复')
        expect(reply.locator('p')).to_have_text('好呀，带上昨天那盘棋吧。')
        ordinary = self.page.locator('.visits-current .visits-transcript li:not(.visits-contact-reply)')
        expect(ordinary.nth(0).locator('strong')).to_have_text('总羽爱乃')
        expect(ordinary.nth(1).locator('strong')).to_have_text('爱丽丝')
        expect(self.page.locator('.visits-history > details')).to_have_count(0)
        self.page.locator('.visits-diagnostics > summary').click()
        card = self.page.locator('.visits-diagnostics-body > details')
        card.locator('summary').click()
        status = card.locator('.visits-contact-status')
        expect(status.locator('strong')).to_have_text('对方当前状态')
        expect(status.locator('.visits-log-kind')).to_have_text('通话前状态')
        expect(status.locator('p')).to_have_text('正在休息，暂时不方便。')
        self.assertEqual(status.locator('.visits-contact-reply').count(), 0)
        for width, height in [(1440, 1000), (390, 844)]:
            self.host.set_viewport_size({'width':width, 'height':height})
            self.page.locator('.visits-heading').scroll_into_view_if_needed()
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'))
            self.host.screenshot(path=str(self.shots / f'visits-redesign-{width}.png'))
            if width == 390:
                self.page.locator('.visits-current').scroll_into_view_if_needed()
                self.host.screenshot(path=str(self.shots / f'visits-records-{width}.png'))
            self.open_phone()
            self.page.locator('.visits-phone').scroll_into_view_if_needed()
            expect(self.page.locator('.visits-phone')).to_be_visible()
            self.host.screenshot(path=str(self.shots / f'visits-phone-{width}.png'))
            self.page.get_by_role('button', name='收起', exact=True).click()
        self.assertEqual(self.errors, [])

    def test_many_roles_require_one_choice_and_reuse_global_pairing(self):
        self.page.evaluate("""() => {
          visitData.connections[0].actors.push({id:'isla',name:'艾拉',world:'茶室'});
          visitData.peers.push({id:'isla',connection_id:'server',name:'艾拉',world:'茶室',enabled:true});
        }""")
        self.page.locator('#tab-visits').get_by_role('button', name='刷新', exact=True).click()
        self.open_phone()
        self.page.get_by_label('去见哪位朋友', exact=True).select_option('isla')
        self.page.get_by_role('button', name='去做客', exact=True).click()
        self.page.wait_for_function('visitWrites.length===1')
        self.assertEqual(self.page.evaluate('visitWrites[0].payload.peer_id'), 'isla')
        self.page.locator('#status-session').select_option('test:GroupMessage:200')
        expect(self.page.get_by_role('button', name='去做客', exact=True)).to_be_disabled()
        expect(self.page.locator('.visits-peer')).to_have_count(1)
        self.open_settings()
        expect(self.page.get_by_label('新生活', exact=True)).not_to_be_checked()
        self.page.get_by_label('新生活', exact=True).check()
        self.page.get_by_role('button', name='保存参与生活', exact=True).click()
        self.page.wait_for_function('visitWrites.length===2')
        self.assertEqual(self.page.evaluate('visitWrites[1].endpoint'), 'visits/scopes')
        self.assertEqual(self.page.evaluate('visitWrites[1].payload.sessions'),
                         ['test:GroupMessage:100', 'test:GroupMessage:200'])
        self.assertNotIn('session', self.page.evaluate('visitWrites[1].payload'))
        expect(self.page.get_by_role('button', name='去做客', exact=True)).to_be_enabled()
        self.page.get_by_role('button', name='去做客', exact=True).click()
        self.page.wait_for_function('visitWrites.length===3')
        self.assertEqual(self.page.evaluate('visitWrites[2].payload.session'), 'test:GroupMessage:200')
        self.assertEqual(self.page.evaluate('visitWrites[2].payload.peer_id'), 'isla')
        self.assertEqual(self.errors, [])

    def test_call_transcript_keeps_local_remote_and_ambiguous_statuses_distinct(self):
        self.page.evaluate("""() => {
          visitData.active={id:'trip',status:'inviting',
            local:{id:'home:same',actor_id:'same',name:'爱丽丝'},
            remote:{id:'away:same',actor_id:'same',name:'爱丽丝'},
            log:[{actor_id:'home:same',kind:'contact_status',text:'本方手头有事'},
              {actor_id:'away:same',kind:'contact_status',text:'对方正在休息'},
              {actor_id:'same',kind:'contact_status',text:'旧来源不明确'},
              {actor_id:'home:same',kind:'contact_reply',text:'我先收好手里的书'},
              {actor_id:'away:same',speaker:'其他人',kind:'contact_reply',text:'我晚点再来'}]};
        }""")
        self.page.locator('#tab-visits').get_by_role('button', name='刷新', exact=True).click()
        self.assertEqual(self.page.locator('.visits-current .visits-log-head strong').all_text_contents(),
                         ['本方当前状态', '对方当前状态', '来源未确认的状态', '我（爱丽丝）', '对方（爱丽丝）'])
        self.assertEqual(self.errors, [])

    def test_global_directory_and_controls_work_without_a_selected_life(self):
        self.open_phone()
        self.page.locator('#status-session').evaluate("""select => {
          const option=document.createElement('option');option.value='';option.textContent='不选择生活';
          select.append(option);select.value='';select.dispatchEvent(new Event('change',{bubbles:true}));
        }""")
        self.page.wait_for_function("visitReads.length>1 && !('session' in visitReads.at(-1))")
        expect(self.page.locator('.visits-peer')).to_have_count(1)
        expect(self.page.get_by_role('button', name='去做客', exact=True)).to_be_disabled()
        self.open_settings()
        self.page.get_by_role('button', name='刷新角色', exact=True).click()
        self.page.wait_for_function('visitWrites.length===1')
        self.assertEqual(self.page.evaluate('visitWrites[0].payload.connection_id'), 'server')
        self.assertNotIn('session', self.page.evaluate('visitWrites[0].payload'))
        self.open_pairing()
        self.page.get_by_role('button', name='生成一次性配对码', exact=True).click()
        expect(self.page.get_by_label('本次配对码', exact=True)).to_have_value('one-use-code')
        self.assertNotIn('session', self.page.evaluate('visitWrites[1].payload'))
        self.assertEqual(self.errors, [])

    def test_scope_draft_survives_refresh_and_shared_life_is_only_one_checkbox(self):
        self.open_phone()
        self.open_settings()
        expect(self.page.locator('.visits-life-choice')).to_have_count(2)
        self.page.get_by_label('新生活', exact=True).check()
        self.page.locator('#tab-visits').get_by_role('button', name='刷新', exact=True).click()
        expect(self.page.get_by_label('新生活', exact=True)).to_be_checked()
        self.page.get_by_label('爱丽丝的共享生活', exact=True).uncheck()
        self.page.get_by_role('button', name='保存参与生活', exact=True).click()
        self.page.wait_for_function('visitWrites.length===1')
        self.assertEqual(self.page.evaluate('visitWrites[0].payload.sessions'), ['test:GroupMessage:200'])
        expect(self.page.get_by_role('button', name='去做客', exact=True)).to_be_disabled()
        self.assertEqual(self.errors, [])

    def test_global_pair_code_finishes_after_switching_life_and_expires(self):
        self.page.evaluate("""() => {
          const write=AstrBotPluginPage.apiPost;
          AstrBotPluginPage.apiPost=(endpoint,payload)=>endpoint==='visits/pair-code'
            ? new Promise(resolve=>{window.finishVisitCode=resolve;visitWrites.push({endpoint,payload:structuredClone(payload)});})
            : write(endpoint,payload);
        }""")
        self.open_pairing()
        self.page.get_by_label('对方的一次性配对码', exact=True).fill('keep-this-draft')
        self.page.get_by_role('button', name='生成一次性配对码', exact=True).click()
        self.page.wait_for_function('Boolean(window.finishVisitCode)')
        self.page.get_by_label('对方的一次性配对码', exact=True).fill('keep-this-draft')
        self.page.locator('#status-session').select_option('test:GroupMessage:200')
        expect(self.page.get_by_label('对方的一次性配对码', exact=True)).to_have_value('keep-this-draft')
        self.page.evaluate("finishVisitCode({code:'after-switch',expires_at:Date.now()/1000+1.5})")
        expect(self.page.get_by_label('本次配对码', exact=True)).to_have_value('after-switch')
        self.assertNotIn('session', self.page.evaluate('visitWrites[0].payload'))
        expect(self.page.get_by_label('本次配对码', exact=True)).to_have_value('', timeout=5000)
        expect(self.page.get_by_label('对方的一次性配对码', exact=True)).to_have_value('')
        self.assertEqual(self.errors, [])

    def test_late_old_life_read_cannot_replace_current_life_or_global_changes(self):
        self.open_phone()
        self.page.evaluate("""() => {
          const read=AstrBotPluginPage.apiGet;
          window.pendingVisitReads=[];
          AstrBotPluginPage.apiGet=(endpoint,payload)=>endpoint==='visits'
            ? new Promise(resolve=>pendingVisitReads.push({resolve,payload:structuredClone(payload)}))
            : read(endpoint,payload);
        }""")
        self.page.locator('#tab-visits').get_by_role('button', name='刷新', exact=True).click()
        self.page.wait_for_function('pendingVisitReads.length===1')
        self.page.locator('#status-session').select_option('test:GroupMessage:200')
        self.page.wait_for_function('pendingVisitReads.length===2')
        self.page.evaluate("""() => {
          const fresh=structuredClone(visitData);fresh.scope_enabled=false;fresh.peers=[];
          fresh.connections[0].alias='更新后的全局友邻';pendingVisitReads[1].resolve(fresh);
        }""")
        expect(self.page.locator('.visits-peer h4')).to_have_text('更新后的全局友邻')
        self.page.evaluate("""() => {
          const old=structuredClone(visitData);old.scope_enabled=true;
          old.active={id:'old-trip',peer_name:'旧生活的朋友',status:'visiting',transcript:[]};
          pendingVisitReads[0].resolve(old);
        }""")
        expect(self.page.locator('.visits-peer h4')).to_have_text('更新后的全局友邻')
        expect(self.page.locator('.visits-current')).not_to_contain_text('旧生活的朋友')
        expect(self.page.get_by_role('button', name='去做客', exact=True)).to_be_disabled()
        self.assertEqual(self.errors, [])

    def test_leaving_during_pair_code_request_keeps_late_secret_hidden(self):
        self.page.evaluate("""() => {
          const write=AstrBotPluginPage.apiPost;
          AstrBotPluginPage.apiPost=(endpoint,payload)=>endpoint==='visits/pair-code'
            ? new Promise(resolve=>{window.finishVisitCode=resolve;}) : write(endpoint,payload);
        }""")
        self.open_pairing()
        self.page.get_by_role('button', name='生成一次性配对码', exact=True).click()
        self.page.wait_for_function('Boolean(window.finishVisitCode)')
        self.page.locator('button[data-tab="status"]').click()
        self.page.locator('button[data-tab="visits"]').click()
        self.page.evaluate("finishVisitCode({code:'late-secret',expires_at:Date.now()/1000+600})")
        expect(self.page.get_by_role('button', name='生成一次性配对码', exact=True)).to_be_enabled()
        expect(self.page.get_by_label('本次配对码', exact=True)).to_have_value('')
        self.assertEqual(self.errors, [])
