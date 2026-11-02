"""Read long visit journals using the service's row-boundary cursor contract."""

import unittest

from tests import test_visits_browser as visits
from tests.test_generation_browser import expect, sync_playwright


@unittest.skipIf(sync_playwright is None, "Install playwright for browser verification")
class VisitsJournalReviewTests(unittest.TestCase):
    setUpClass = classmethod(visits.VisitsBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(visits.VisitsBrowserTests.tearDownClass.__func__)
    setUp = visits.VisitsBrowserTests.setUp

    def refresh(self):
        self.page.locator('#tab-visits').get_by_role('button', name='刷新', exact=True).click()

    def journal(self, count=481, *, offset=True, hidden_tail=False):
        self.page.evaluate("""({count,offset,hiddenTail}) => {
          window.journalRows=Array.from({length:count},(_,index)=>({seq:index+1,
            actor_id:index%2?'friend':'ours',kind:hiddenTail&&index>=count-192?'activity':'say',
            text:hiddenTail&&index>=count-192?'开始相处：旧版预制文字':`片刻 ${index+1}`,
            at:1793581200+index*60}));
          const start=Math.max(0,count-192);
          visitData.active={id:'long-journal',status:'active',arrived_at:1793580600,
            local:{id:'ours',name:'艾拉'},remote:{id:'friend',name:'爱丽丝'},
            log:journalRows.slice(start),log_count:count};
          if(offset)visitData.active.log_offset=start;
          const write=AstrBotPluginPage.apiPost;
          // Match public_record: before is the exclusive array end, not seq.
          AstrBotPluginPage.apiPost=(endpoint,payload)=>{
            if(endpoint!=='visits/record')return write(endpoint,payload);
            visitWrites.push({endpoint,payload:structuredClone(payload)});
            const end=payload.before?Math.min(payload.before,journalRows.length):journalRows.length;
            const start=Math.max(0,end-96);
            return Promise.resolve({id:'long-journal',log:journalRows.slice(start,end),
              log_offset:start,log_count:journalRows.length,next_before:start,has_more:start>0});
          };
        }""", {"count": count, "offset": offset, "hiddenTail": hidden_tail})
        self.refresh()
        self.page.locator('.visits-current .visits-earlier-moments > summary').click()

    def test_real_row_boundaries_read_every_page_once_and_survive_refresh(self):
        self.journal()
        expect(self.page.locator('.visits-current .visits-moment:visible')).to_have_count(192)
        for count in (288, 384, 480, 481):
            self.page.get_by_role('button', name='继续往前翻', exact=True).click()
            expect(self.page.locator('.visits-current .visits-moment:visible')).to_have_count(count)
        self.assertEqual(self.page.evaluate('visitWrites.map(item=>item.payload.before)'), [289, 193, 97, 1])
        self.assertEqual(self.page.locator('.visits-current .visits-log-say').all_text_contents(),
                         [f"片刻 {index}" for index in range(1, 482)])
        expect(self.page.get_by_role('button', name='继续往前翻', exact=True)).to_have_count(0)
        self.refresh()
        expect(self.page.locator('.visits-current .visits-moment:visible')).to_have_count(481)
        self.assertEqual(self.page.locator('.visits-current .visits-log-say').all_text_contents(),
                         [f"片刻 {index}" for index in range(1, 482)])
        self.assertEqual(self.errors, [])

    def test_legacy_overview_without_offset_starts_before_first_visible_sequence(self):
        self.journal(288, offset=False)
        self.page.get_by_role('button', name='继续往前翻', exact=True).click()
        expect(self.page.locator('.visits-current .visits-moment:visible')).to_have_count(288)
        self.assertEqual(self.page.evaluate('visitWrites.at(-1).payload.before'), 96)
        expect(self.page.get_by_role('button', name='继续往前翻', exact=True)).to_have_count(0)
        self.assertEqual(self.errors, [])

    def test_repeated_boundary_row_does_not_duplicate_a_moment(self):
        self.journal(288)
        self.page.evaluate("""() => {
          const read=AstrBotPluginPage.apiPost;
          AstrBotPluginPage.apiPost=async(endpoint,payload)=>{
            const result=await read(endpoint,payload);
            if(endpoint==='visits/record')result.log.push(structuredClone(result.log.at(-1)));
            return result;
          };
        }""")
        self.page.get_by_role('button', name='继续往前翻', exact=True).click()
        expect(self.page.locator('.visits-current .visits-moment:visible')).to_have_count(288)
        self.assertEqual(self.page.locator('.visits-current .visits-log-say').all_text_contents(),
                         [f"片刻 {index}" for index in range(1, 289)])
        self.assertEqual(self.errors, [])

    def test_a_filtered_latest_page_uses_its_row_offset_to_find_real_earlier_facts(self):
        self.journal(288, hidden_tail=True)
        expect(self.page.locator('.visits-current .visits-moment:visible')).to_have_count(0)
        self.page.get_by_role('button', name='继续往前翻', exact=True).click()
        expect(self.page.locator('.visits-current .visits-moment:visible')).to_have_count(96)
        self.assertEqual(self.page.evaluate('visitWrites.at(-1).payload.before'), 96)
        self.assertEqual(self.page.locator('.visits-current .visits-log-say').all_text_contents(),
                         [f"片刻 {index}" for index in range(1, 97)])
        expect(self.page.get_by_role('button', name='继续往前翻', exact=True)).to_have_count(0)
        expect(self.page.locator('.visits-current')).not_to_contain_text('旧版预制文字')
        self.assertEqual(self.errors, [])

    def test_invalid_page_keeps_its_cursor_available_for_retry(self):
        self.journal(288)
        self.page.evaluate("""() => {
          const read=AstrBotPluginPage.apiPost;
          let invalid=true;
          AstrBotPluginPage.apiPost=async(endpoint,payload)=>{
            const result=await read(endpoint,payload);
            if(endpoint==='visits/record'&&invalid){
              invalid=false;
              result.log.push({seq:payload.before+1,actor_id:'ours',kind:'say',text:'无效分页的文字'});
            }
            return result;
          };
        }""")
        previous = self.page.get_by_role('button', name='继续往前翻', exact=True)
        previous.click()
        expect(previous).to_be_enabled()
        expect(self.page.locator('.visits-current .visits-moment:visible')).to_have_count(192)
        expect(self.page.locator('.visits-current')).not_to_contain_text('无效分页的文字')
        previous.click()
        expect(self.page.locator('.visits-current .visits-moment:visible')).to_have_count(288)
        self.assertEqual(self.page.evaluate('visitWrites.map(item=>item.payload.before)'), [96, 96])
        self.assertEqual(self.errors, [])

    def test_refresh_during_paging_preserves_new_facts_and_the_waiting_page(self):
        self.journal(288)
        self.page.evaluate("""() => {
          const read=AstrBotPluginPage.apiPost;
          AstrBotPluginPage.apiPost=async(endpoint,payload)=>{
            const result=await read(endpoint,payload);
            if(endpoint==='visits/record')return new Promise(resolve=>{
              window.finishJournalPage=()=>resolve(result);
            });
            return result;
          };
        }""")
        self.page.get_by_role('button', name='继续往前翻', exact=True).click()
        self.page.wait_for_function('Boolean(window.finishJournalPage)')
        self.page.evaluate("""() => {
          journalRows.push({seq:289,actor_id:'friend',kind:'say',text:'片刻 289',at:1793600000});
          Object.assign(visitData.active,{log:journalRows.slice(-192),log_offset:97,log_count:289});
        }""")
        self.refresh()
        expect(self.page.get_by_role('button', name='正在翻页…', exact=True)).to_be_disabled()
        self.page.evaluate('finishJournalPage()')
        expect(self.page.locator('.visits-current .visits-moment:visible')).to_have_count(289)
        self.assertEqual(self.page.locator('.visits-current .visits-log-say').all_text_contents(),
                         [f"片刻 {index}" for index in range(1, 290)])
        self.assertEqual(self.errors, [])

    def test_a_late_page_cannot_follow_the_reader_to_another_life(self):
        self.journal(288)
        self.page.evaluate("""() => {
          const read=AstrBotPluginPage.apiPost;
          AstrBotPluginPage.apiPost=async(endpoint,payload)=>{
            const result=await read(endpoint,payload);
            if(endpoint==='visits/record')return new Promise(resolve=>{
              window.finishJournalPage=()=>resolve(result);
            });
            return result;
          };
        }""")
        self.page.get_by_role('button', name='继续往前翻', exact=True).click()
        self.page.wait_for_function('Boolean(window.finishJournalPage)')
        self.page.evaluate("""() => {
          visitData.enabled_scopes.push('test:GroupMessage:200');
          visitData.active={id:'another-life',status:'active',arrived_at:1793580600,
            local:{id:'other-local',name:'莉耶芙'},remote:{id:'friend',name:'爱丽丝'},
            log:[{seq:1,actor_id:'other-local',kind:'say',text:'这份生活的片刻'}],log_offset:0};
        }""")
        self.page.locator('#status-session').select_option('test:GroupMessage:200')
        expect(self.page.locator('.visits-current .visits-log-say')).to_have_text('这份生活的片刻')
        self.page.evaluate('finishJournalPage()')
        expect(self.page.locator('.visits-current .visits-log-say')).to_have_text('这份生活的片刻')
        expect(self.page.locator('.visits-current')).not_to_contain_text('片刻 1')
        self.assertEqual(self.errors, [])

    def test_pair_code_expiry_keeps_live_journal_refreshing_until_the_reader_leaves(self):
        self.host.clock.install()
        self.journal(24)
        self.page.evaluate("""() => {
          const write=AstrBotPluginPage.apiPost;
          AstrBotPluginPage.apiPost=(endpoint,payload)=>endpoint==='visits/pair-code'
            ? Promise.resolve({code:'expires-soon',expires_at:Date.now()/1000+1})
            : write(endpoint,payload);
        }""")
        self.page.locator('.visits-settings > summary').click()
        self.page.locator('.visits-pairing > summary').click()
        self.page.get_by_role('button', name='生成一次性配对码', exact=True).click()
        expect(self.page.get_by_label('本次配对码', exact=True)).to_have_value('expires-soon')
        self.host.clock.fast_forward(2000)
        expect(self.page.get_by_label('本次配对码', exact=True)).to_have_value('')
        before = self.page.evaluate('visitReads.length')
        self.host.clock.fast_forward(15000)
        self.assertEqual(self.page.evaluate('visitReads.length'), before + 1)
        self.page.locator('button[data-tab="status"]').click()
        before = self.page.evaluate('visitReads.length')
        self.host.clock.fast_forward(15000)
        self.assertEqual(self.page.evaluate('visitReads.length'), before)
        self.assertEqual(self.errors, [])
