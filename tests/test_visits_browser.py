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

    def test_invite_both_directions_and_preserve_life_scope(self):
        self.assertEqual(self.page.get_by_label('去见哪位朋友', exact=True).count(), 0)
        self.page.get_by_label('这次邀请想一起做什么').fill('一起下完昨天那盘棋')
        self.page.get_by_role('button', name='邀请去做客', exact=True).click()
        self.page.wait_for_function('visitWrites.length===1')
        self.assertEqual(self.page.evaluate('visitWrites[0].payload.direction'), 'outgoing')
        self.assertEqual(self.page.evaluate('visitWrites[0].payload.reason'), '一起下完昨天那盘棋')
        self.assertEqual(self.page.evaluate('visitWrites[0].payload.peer_id'), 'friend')
        self.assertEqual(self.page.evaluate('visitWrites[0].payload.session'), self.page.locator('#status-session').input_value())
        self.page.get_by_role('button', name='邀请她来访', exact=True).click()
        self.page.wait_for_function('visitWrites.length===2')
        self.assertEqual(self.page.evaluate('visitWrites[1].payload.direction'), 'incoming')
        self.assertEqual(self.errors, [])

    def test_pairing_inputs_clear_secrets_after_submit_and_leave(self):
        self.page.locator('.visits-pairing > summary').click()
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
            self.page.locator('.visits-lives').scroll_into_view_if_needed()
            self.host.screenshot(path=str(self.shots / f'visits-lives-{width}.png'))
        self.assertEqual(self.errors, [])

    def test_many_roles_require_one_choice_and_reuse_global_pairing(self):
        self.page.evaluate("""() => {
          visitData.connections[0].actors.push({id:'isla',name:'艾拉',world:'茶室'});
          visitData.peers.push({id:'isla',connection_id:'server',name:'艾拉',world:'茶室',enabled:true});
        }""")
        self.page.locator('#tab-visits').get_by_role('button', name='刷新', exact=True).click()
        self.page.get_by_label('去见哪位朋友', exact=True).select_option('isla')
        self.page.get_by_role('button', name='邀请去做客', exact=True).click()
        self.page.wait_for_function('visitWrites.length===1')
        self.assertEqual(self.page.evaluate('visitWrites[0].payload.peer_id'), 'isla')
        self.page.locator('#status-session').select_option('test:GroupMessage:200')
        expect(self.page.get_by_role('button', name='邀请去做客', exact=True)).to_be_disabled()
        expect(self.page.locator('.visits-peer')).to_have_count(1)
        expect(self.page.get_by_label('新生活', exact=True)).not_to_be_checked()
        self.page.get_by_label('新生活', exact=True).check()
        self.page.get_by_role('button', name='保存参与生活', exact=True).click()
        self.page.wait_for_function('visitWrites.length===2')
        self.assertEqual(self.page.evaluate('visitWrites[1].endpoint'), 'visits/scopes')
        self.assertEqual(self.page.evaluate('visitWrites[1].payload.sessions'),
                         ['test:GroupMessage:100', 'test:GroupMessage:200'])
        self.assertNotIn('session', self.page.evaluate('visitWrites[1].payload'))
        expect(self.page.get_by_role('button', name='邀请去做客', exact=True)).to_be_enabled()
        self.page.get_by_role('button', name='邀请去做客', exact=True).click()
        self.page.wait_for_function('visitWrites.length===3')
        self.assertEqual(self.page.evaluate('visitWrites[2].payload.session'), 'test:GroupMessage:200')
        self.assertEqual(self.page.evaluate('visitWrites[2].payload.peer_id'), 'isla')
        self.assertEqual(self.errors, [])

    def test_global_directory_and_controls_work_without_a_selected_life(self):
        self.page.locator('#status-session').evaluate("""select => {
          const option=document.createElement('option');option.value='';option.textContent='不选择生活';
          select.append(option);select.value='';select.dispatchEvent(new Event('change',{bubbles:true}));
        }""")
        self.page.wait_for_function("visitReads.length>1 && !('session' in visitReads.at(-1))")
        expect(self.page.locator('.visits-peer')).to_have_count(1)
        expect(self.page.get_by_role('button', name='邀请去做客', exact=True)).to_be_disabled()
        self.page.get_by_role('button', name='刷新角色', exact=True).click()
        self.page.wait_for_function('visitWrites.length===1')
        self.assertEqual(self.page.evaluate('visitWrites[0].payload.connection_id'), 'server')
        self.assertNotIn('session', self.page.evaluate('visitWrites[0].payload'))
        self.page.locator('.visits-pairing > summary').click()
        self.page.get_by_role('button', name='生成一次性配对码', exact=True).click()
        expect(self.page.get_by_label('本次配对码', exact=True)).to_have_value('one-use-code')
        self.assertNotIn('session', self.page.evaluate('visitWrites[1].payload'))
        self.assertEqual(self.errors, [])

    def test_scope_draft_survives_refresh_and_shared_life_is_only_one_checkbox(self):
        expect(self.page.locator('.visits-life-choice')).to_have_count(2)
        self.page.get_by_label('新生活', exact=True).check()
        self.page.locator('#tab-visits').get_by_role('button', name='刷新', exact=True).click()
        expect(self.page.get_by_label('新生活', exact=True)).to_be_checked()
        self.page.get_by_label('爱丽丝的共享生活', exact=True).uncheck()
        self.page.get_by_role('button', name='保存参与生活', exact=True).click()
        self.page.wait_for_function('visitWrites.length===1')
        self.assertEqual(self.page.evaluate('visitWrites[0].payload.sessions'), ['test:GroupMessage:200'])
        expect(self.page.get_by_role('button', name='邀请去做客', exact=True)).to_be_disabled()
        self.assertEqual(self.errors, [])

    def test_global_pair_code_finishes_after_switching_life_and_expires(self):
        self.page.evaluate("""() => {
          const write=AstrBotPluginPage.apiPost;
          AstrBotPluginPage.apiPost=(endpoint,payload)=>endpoint==='visits/pair-code'
            ? new Promise(resolve=>{window.finishVisitCode=resolve;visitWrites.push({endpoint,payload:structuredClone(payload)});})
            : write(endpoint,payload);
        }""")
        self.page.locator('.visits-pairing > summary').click()
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
        expect(self.page.get_by_role('button', name='邀请去做客', exact=True)).to_be_disabled()
        self.assertEqual(self.errors, [])

    def test_leaving_during_pair_code_request_keeps_late_secret_hidden(self):
        self.page.evaluate("""() => {
          const write=AstrBotPluginPage.apiPost;
          AstrBotPluginPage.apiPost=(endpoint,payload)=>endpoint==='visits/pair-code'
            ? new Promise(resolve=>{window.finishVisitCode=resolve;}) : write(endpoint,payload);
        }""")
        self.page.locator('.visits-pairing > summary').click()
        self.page.get_by_role('button', name='生成一次性配对码', exact=True).click()
        self.page.wait_for_function('Boolean(window.finishVisitCode)')
        self.page.locator('button[data-tab="status"]').click()
        self.page.locator('button[data-tab="visits"]').click()
        self.page.evaluate("finishVisitCode({code:'late-secret',expires_at:Date.now()/1000+600})")
        expect(self.page.get_by_role('button', name='生成一次性配对码', exact=True)).to_be_enabled()
        expect(self.page.get_by_label('本次配对码', exact=True)).to_have_value('')
        self.assertEqual(self.errors, [])
