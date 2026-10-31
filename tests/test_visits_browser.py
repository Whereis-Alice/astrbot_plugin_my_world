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
        payload["config"]["sessions"]["sessions"] = [{"session_id": "test:GroupMessage:100", "enabled": True}]
        from unittest.mock import patch
        with patch.object(generation, "build_payload", return_value=payload):
            generation.GenerationBrowserTests.setUp(self)
        self.page.evaluate("""() => {
          const read=AstrBotPluginPage.apiGet, write=AstrBotPluginPage.apiPost;
          window.visitWrites=[]; window.visitReads=[];
          window.visitData={enabled:true,listener:{host:'0.0.0.0',port:38471},
            peers:[{id:'friend',alias:'港湾的爱丽丝',url:'https://friend.example',world:'港湾世界'}],
            active:null,history:[]};
          AstrBotPluginPage.apiGet=(endpoint,payload)=>{
            if(endpoint==='visits'){visitReads.push(structuredClone(payload));return Promise.resolve(structuredClone(visitData));}
            return read(endpoint,payload);
          };
          AstrBotPluginPage.apiPost=(endpoint,payload)=>{
            if(!endpoint.startsWith('visits/'))return write(endpoint,payload);
            visitWrites.push({endpoint,payload:structuredClone(payload)});
            if(endpoint==='visits/pair-code')return Promise.resolve({code:'one-use-code',expires_at:Date.now()/1000+600,url:'https://ours.example'});
            return Promise.resolve({ok:true});
          };
        }""")
        self.page.locator('button[data-tab="visits"]').click()
        expect(self.page.locator('.visits-peer')).to_have_count(1)

    def test_invite_both_directions_and_preserve_life_scope(self):
        self.page.get_by_label('这次邀请想一起做什么').fill('一起下完昨天那盘棋')
        self.page.get_by_role('button', name='邀请去做客', exact=True).click()
        self.page.wait_for_function('visitWrites.length===1')
        self.assertEqual(self.page.evaluate('visitWrites[0].payload.direction'), 'outgoing')
        self.assertEqual(self.page.evaluate('visitWrites[0].payload.reason'), '一起下完昨天那盘棋')
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
        expect(self.page.get_by_label('对方的一次性配对码', exact=True)).to_have_value('')
        self.page.locator('button[data-tab="status"]').click()
        expect(self.page.get_by_label('本次配对码', exact=True)).to_have_value('')
        self.assertEqual(self.errors, [])

    def test_remote_text_is_plain_text_and_phone_stays_inside_viewport(self):
        self.page.evaluate("""() => {
          visitData.peers[0].alias='<img src=x onerror="window.injected=true">';
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
        self.assertEqual(self.errors, [])
