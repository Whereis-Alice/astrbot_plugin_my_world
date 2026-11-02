"""Readers see attributed dialogue and complete, navigable visit keepsakes."""
from __future__ import annotations

from datetime import datetime, timezone
import unittest

from tests import test_visits_browser as visits
from tests.test_generation_browser import expect, sync_playwright


@unittest.skipIf(sync_playwright is None, "Install playwright for browser verification")
class VisitJournalReadingTests(unittest.TestCase):
    setUpClass = classmethod(visits.VisitsBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(visits.VisitsBrowserTests.tearDownClass.__func__)
    setUp = visits.VisitsBrowserTests.setUp

    @staticmethod
    def visit(identifier, logs, **extra):
        result = {
            "id": identifier, "status": "completed", "created_at": 1793656800,
            "arrived_at": 1793657400, "ended_at": 1793660400,
            "local": {"id": "home:isla", "actor_id": "isla", "name": "艾拉"},
            "remote": {"id": "away:alice", "actor_id": "alice", "name": "爱丽丝"},
            "scene": {"place": "港湾茶室", "activity": "一起喝茶"}, "log": logs,
        }
        result.update(extra)
        return result

    def refresh(self):
        self.page.locator("#tab-visits").get_by_role("button", name="刷新", exact=True).click()

    def seed_history(self, *records):
        self.page.evaluate("""records => {
          visitData.active=null; visitData.history=records;
        }""", list(records))
        self.refresh()

    def open_record(self, identifier):
        card = self.page.locator(f'.visits-history > details[data-visit-id="{identifier}"]')
        expect(card).to_have_count(1)
        if not card.evaluate("node => node.open"):
            card.locator(":scope > summary").click()
        expect(card.locator(".visits-visit-body")).to_be_visible()
        return card

    def test_arrival_is_a_neutral_meeting_before_the_actual_character_line(self):
        self.seed_history(self.visit("arrival", [
            {"seq": 1, "actor_id": "home:isla", "kind": "arrival", "at": 1793657400,
             "text": "双方接受邀请，开始在东道主世界相处。"},
            {"seq": 2, "actor_id": "home:isla", "kind": "say", "at": 1793657401,
             "text": "我去拿两只茶杯。"},
        ]))
        card = self.open_record("arrival")
        arrival = card.locator('.visits-moment[data-record-kind="arrival"]')
        expect(arrival.get_by_text("相见", exact=True)).to_be_visible()
        expect(arrival.locator(".visits-log-head, .visits-log-avatar, .visits-log-say")).to_have_count(0)
        expect(arrival).not_to_contain_text("艾拉")
        expect(arrival).not_to_contain_text("双方接受邀请")
        speech = card.locator('.visits-moment[data-record-kind="say"]')
        expect(speech.locator(".visits-log-head strong")).to_have_text("艾拉")
        expect(speech.locator(".visits-log-say")).to_have_text("我去拿两只茶杯。")
        self.assertEqual(card.locator(".visits-moment").evaluate_all(
            "rows => rows.map(row => row.dataset.recordKind)"), ["arrival", "say"])
        self.assertEqual(self.errors, [])

    def test_shared_activity_event_result_and_unknown_kind_never_become_speech(self):
        self.seed_history(self.visit("shared", [
            {"seq": 1, "actor_id": "home:isla", "kind": "activity", "at": 1793657400,
             "text": "在河岸长椅一起折纸。"},
            {"seq": 2, "actor_id": "home:isla", "kind": "event", "at": 1793657410,
             "text": "风把一张蓝色的纸吹到了长椅下面。"},
            {"seq": 3, "actor_id": "home:isla", "kind": "result", "at": 1793657420,
             "text": "蓝纸找回来了，两只纸鹤也立稳了。"},
            {"seq": 4, "actor_id": "home:isla", "kind": "future_checkpoint", "at": 1793657430,
             "text": "未知记录仍保留这段经过。", "say": "旧附加字段也不能变成台词。"},
        ]))
        card = self.open_record("shared")
        for kind, text in (
            ("activity", "在河岸长椅一起折纸。"),
            ("event", "风把一张蓝色的纸吹到了长椅下面。"),
            ("result", "蓝纸找回来了，两只纸鹤也立稳了。"),
            ("future_checkpoint", "未知记录仍保留这段经过。"),
        ):
            with self.subTest(kind=kind):
                moment = card.locator(f'.visits-moment[data-record-kind="{kind}"]')
                expect(moment).to_contain_text(text)
                expect(moment.locator(".visits-log-say, .visits-log-head, .visits-log-avatar")).to_have_count(0)
        expect(card.locator('.visits-moment[data-record-kind="result"] .visits-log-result')).to_have_text(
            "蓝纸找回来了，两只纸鹤也立稳了。")
        expect(card).to_contain_text("旧附加字段也不能变成台词。")
        self.assertEqual(card.locator(".visits-moment").evaluate_all(
            "rows => rows.map(row => row.dataset.recordKind)"),
            ["activity", "event", "result", "future_checkpoint"])
        self.assertEqual(self.errors, [])

    def test_action_intention_and_feeling_stay_attributed_but_result_is_independent(self):
        self.seed_history(self.visit("choice", [
            {"seq": 1, "actor_id": "home:isla", "kind": "say", "at": 1793657400,
             "text": "我想给你折一只纸鹤。"},
            {"seq": 2, "actor_id": "home:isla", "kind": "action_plan", "at": 1793657400.2,
             "text": "打算从包里拿一张纸。"},
            {"seq": 3, "actor_id": "home:isla", "kind": "experience", "at": 1793657400.3,
             "text": "她的笑声让我放松了。"},
            {"seq": 4, "actor_id": "home:isla", "kind": "result", "at": 1793657410,
             "text": "纸鹤已经放在窗边。"},
            {"seq": 5, "actor_id": "away:alice", "kind": "say", "at": 1793657420,
             "text": "那我选蓝色的纸。"},
        ]))
        card = self.open_record("choice")
        moments = card.locator(".visits-moment")
        expect(moments).to_have_count(3)
        first = moments.nth(0)
        expect(first.locator(".visits-log-head strong")).to_have_text("艾拉")
        expect(first.locator(".visits-log-say")).to_have_text("我想给你折一只纸鹤。")
        expect(first.locator(".visits-log-action")).to_have_text("打算从包里拿一张纸。")
        expect(first.locator(".visits-log-action")).to_have_attribute(
            "aria-label", "当时的行动打算：打算从包里拿一张纸。")
        expect(first.locator(".visits-log-experience")).to_have_text("她的笑声让我放松了。")
        expect(first.locator(".visits-log-experience")).to_have_attribute(
            "aria-label", "当时的感受：她的笑声让我放松了。")
        expect(moments.nth(1).locator(".visits-log-result")).to_have_text("纸鹤已经放在窗边。")
        expect(moments.nth(1).locator(".visits-log-head, .visits-log-avatar")).to_have_count(0)
        expect(moments.nth(2).locator(".visits-log-head strong")).to_have_text("爱丽丝")
        self.assertEqual(card.locator(".visits-log-say").all_text_contents(),
                         ["我想给你折一只纸鹤。", "那我选蓝色的纸。"])
        self.assertEqual(self.errors, [])

    def test_same_names_and_incomplete_actor_sources_are_never_guessed(self):
        self.seed_history(self.visit("same-name", [
            {"seq": 1, "actor_id": "home:same", "kind": "say", "text": "我把点心摆到中间。"},
            {"seq": 2, "actor_id": "away:same", "kind": "say", "text": "那我来倒茶。"},
            {"seq": 3, "actor_id": "same", "kind": "say", "text": "这句话只保存了旧短号。"},
            {"seq": 4, "actor_id": "someone-else", "speaker": "爱丽丝", "kind": "say",
             "text": "署名不能替代来源。"},
            {"seq": 5, "speaker": "爱丽丝", "kind": "say", "text": "只有名字的旧记录。"},
        ], local={"id": "home:same", "actor_id": "same", "name": "爱丽丝"},
            remote={"id": "away:same", "actor_id": "same", "name": "爱丽丝"}))
        card = self.open_record("same-name")
        heads = card.locator(".visits-log-head strong")
        expect(heads.nth(0)).to_have_text("我（爱丽丝）")
        expect(heads.nth(1)).to_have_text("对方（爱丽丝）")
        for index, text in enumerate(("这句话只保存了旧短号。", "署名不能替代来源。", "只有名字的旧记录。"), 2):
            with self.subTest(source=index):
                moment = card.locator(".visits-moment").nth(index)
                expect(moment.locator(".visits-log-head strong")).to_contain_text("来源未确认")
                expect(moment.locator(".visits-log-avatar")).to_have_text("·")
                expect(moment.locator(".visits-log-say")).to_have_text(text)
                self.assertIsNone(moment.get_attribute("data-actor-id"))
        self.assertEqual(self.errors, [])

    def test_character_and_shared_malicious_text_remains_visible_plain_text(self):
        speech = '<img src=x onerror="window.visitJournalInjected=true">\n还是一起喝茶吧。'
        event = '<script>window.visitJournalInjected=true</script><a href="https://bad.example">纸条</a>'
        self.page.evaluate("window.visitJournalInjected=false")
        self.seed_history(self.visit("plain-text", [
            {"seq": 1, "actor_id": "away:alice", "kind": "say", "text": speech},
            {"seq": 2, "actor_id": "home:isla", "kind": "event", "text": event},
        ]))
        card = self.open_record("plain-text")
        expect(card.locator(".visits-log-say")).to_have_text(speech)
        expect(card.locator(".visits-log-chronicle")).to_have_text(event)
        expect(card.locator(".visits-transcript img, .visits-transcript script, .visits-transcript a")).to_have_count(0)
        self.assertFalse(self.page.evaluate("window.visitJournalInjected"))
        self.assertEqual(self.errors, [])

    def test_open_history_uses_arrival_date_and_all_loaded_lines_in_chronological_order(self):
        created = int(datetime(2026, 11, 1, 12, tzinfo=timezone.utc).timestamp())
        started = int(datetime(2026, 11, 2, 12, tzinfo=timezone.utc).timestamp())
        arrived = int(datetime(2026, 11, 3, 12, tzinfo=timezone.utc).timestamp())
        logs = [{"seq": index, "actor_id": "home:isla" if index % 2 else "away:alice",
                 "kind": "say", "at": arrived + index * 30, "text": f"那天的片刻 {index}"}
                for index in range(1, 21)]
        self.seed_history(self.visit("cross-day", logs, created_at=created, started_at=started,
                                     arrived_at=arrived, ended_at=arrived + 3600))
        expect(self.page.locator(".visits-history .visits-moment")).to_have_count(0)
        card = self.open_record("cross-day")
        expected_day = self.page.evaluate("at => String(new Date(at*1000).getDate()).padStart(2,'0')", arrived)
        previous_day = self.page.evaluate("at => String(new Date(at*1000).getDate()).padStart(2,'0')", started)
        self.assertNotEqual(expected_day, previous_day)
        expect(card.locator(".visits-entry-date b")).to_have_text(expected_day)
        expect(card.locator(".visits-entry-copy .visits-history-time")).not_to_have_text("")
        expect(card.locator(".visits-log-say:visible")).to_have_count(20)
        self.assertEqual(card.locator(".visits-log-say").all_text_contents(),
                         [f"那天的片刻 {index}" for index in range(1, 21)])
        expect(card.locator(".visits-earlier-moments")).to_have_count(0)
        self.refresh()
        expect(card.locator(".visits-log-say:visible")).to_have_count(20)
        self.assertEqual(card.locator(".visits-log-say").all_text_contents(),
                         [f"那天的片刻 {index}" for index in range(1, 21)])
        self.assertEqual(self.errors, [])

    def test_paged_history_reads_every_earlier_line_and_preserves_refresh_increment(self):
        self.page.evaluate("""() => {
          window.readingRows=Array.from({length:289},(_,index)=>({seq:index+1,
            actor_id:index%2?'away:alice':'home:isla',kind:'say',
            text:`旧片刻 ${index+1}`,at:1793657400+index*30}));
          const write=AstrBotPluginPage.apiPost, read=AstrBotPluginPage.apiGet;
          AstrBotPluginPage.apiPost=(endpoint,payload)=>{
            if(endpoint!=='visits/record')return write(endpoint,payload);
            visitWrites.push({endpoint,payload:structuredClone(payload)});
            const end=payload.before?Math.min(payload.before,readingRows.length):readingRows.length;
            const start=Math.max(0,end-96), log=readingRows.slice(start,end);
            if(log.length)log.push(structuredClone(log.at(-1)));
            return Promise.resolve({id:'paged',log,next_before:start,log_offset:start,
              log_count:readingRows.length,has_more:start>0});
          };
          AstrBotPluginPage.apiGet=(endpoint,payload)=>read(endpoint,payload).then(result=>{
            if(endpoint==='visits'&&payload.session==='test:GroupMessage:200'){
              result.history=[{id:'paged',status:'completed',arrived_at:1793657400,
                local:{id:'home:isla',name:'艾拉'},remote:{id:'away:alice',name:'爱丽丝'},
                log:[{seq:1,actor_id:'away:alice',kind:'say',text:'这是另一份生活的记录。'}]}];
            }
            return result;
          });
        }""")
        logs = self.page.evaluate("readingRows.slice(97)")
        self.seed_history(self.visit("paged", logs, log_offset=97, log_count=289))
        card = self.open_record("paged")
        expect(card.locator(".visits-log-say:visible")).to_have_count(192)
        for count in (288, 289):
            card.get_by_role("button", name="继续往前翻", exact=True).click()
            expect(card.locator(".visits-log-say:visible")).to_have_count(count)
        self.assertEqual(self.page.evaluate("visitWrites.map(item=>item.payload.before)"), [97, 1])
        self.assertEqual(self.page.evaluate("visitWrites.map(item=>item.payload.session)"),
                         ["test:GroupMessage:100", "test:GroupMessage:100"])
        self.assertEqual(card.locator(".visits-log-say").all_text_contents(),
                         [f"旧片刻 {index}" for index in range(1, 290)])
        expect(card.get_by_role("button", name="继续往前翻", exact=True)).to_have_count(0)
        self.page.evaluate("""() => {
          readingRows.push({seq:290,actor_id:'away:alice',kind:'say',text:'刷新后又留下一个片刻。'});
          visitData.history[0].log=readingRows.slice(-192);visitData.history[0].log_count=290;
        }""")
        self.refresh()
        expect(card.locator(".visits-log-say:visible")).to_have_count(290)
        self.assertEqual(card.locator(".visits-log-say").all_text_contents(),
                         [f"旧片刻 {index}" for index in range(1, 290)] + ["刷新后又留下一个片刻。"])
        self.page.locator("#status-session").select_option("test:GroupMessage:200")
        expect(card.locator(".visits-entry-preview")).to_have_text("这是另一份生活的记录。")
        expect(card.locator(".visits-visit-body")).not_to_be_visible()
        card = self.open_record("paged")
        expect(card.locator(".visits-log-say")).to_have_text("这是另一份生活的记录。")
        expect(card).not_to_contain_text("旧片刻")
        self.assertEqual(self.errors, [])

    def test_entries_after_twenty_remain_reachable_and_reset_only_for_another_life(self):
        self.page.evaluate("""() => {
          visitData.history=Array.from({length:45},(_,index)=>({id:`note-${index+1}`,
            status:'completed',arrived_at:1793657400-index*3600,
            local:{id:'home:isla',name:'艾拉'},remote:{id:'away:alice',name:'爱丽丝'},
            log:[{seq:1,actor_id:'away:alice',kind:'say',text:`札记正文 ${index+1}`}]}));
          const read=AstrBotPluginPage.apiGet;
          AstrBotPluginPage.apiGet=(endpoint,payload)=>read(endpoint,payload).then(result=>{
            if(endpoint==='visits'&&payload.session==='test:GroupMessage:200'){
              result.history=Array.from({length:25},(_,index)=>({id:`other-${index+1}`,
                status:'completed',arrived_at:1793657400-index*3600,
                remote:{id:'away:alice',name:'爱丽丝'},
                log:[{seq:1,actor_id:'away:alice',kind:'say',text:`另一份生活 ${index+1}`}]}));
            }
            return result;
          });
        }""")
        self.refresh()
        entries = self.page.locator(".visits-history > details[data-visit-id]")
        expect(entries).to_have_count(20)
        expect(self.page.locator(".visits-history .visits-moment")).to_have_count(0)
        for count in (40, 45):
            self.page.get_by_role("button", name="继续翻阅", exact=True).click()
            expect(entries).to_have_count(count)
        expect(self.page.get_by_role("button", name="继续翻阅", exact=True)).to_have_count(0)
        card = self.open_record("note-45")
        expect(card.locator(".visits-log-say")).to_have_text("札记正文 45")
        self.page.evaluate("""() => {
          visitData.history.push({id:'note-46',status:'completed',arrived_at:1793660400,
            remote:{id:'away:alice',name:'爱丽丝'},
            log:[{seq:1,actor_id:'away:alice',kind:'say',text:'后来补入的一篇札记。'}]});
        }""")
        self.refresh()
        expect(entries).to_have_count(46)
        expect(card.locator(".visits-log-say:visible")).to_have_text("札记正文 45")
        expect(self.open_record("note-46").locator(".visits-log-say")).to_have_text("后来补入的一篇札记。")
        self.page.locator("#status-session").select_option("test:GroupMessage:200")
        expect(entries).to_have_count(20)
        expect(self.page.locator('.visits-history > details[data-visit-id="note-45"]')).to_have_count(0)
        expect(self.page.locator(".visits-history .visits-moment")).to_have_count(0)
        self.page.get_by_role("button", name="继续翻阅", exact=True).click()
        expect(entries).to_have_count(25)
        expect(self.open_record("other-25").locator(".visits-log-say")).to_have_text("另一份生活 25")
        self.assertEqual(self.errors, [])

    def test_old_more_button_cannot_expand_a_pending_refresh_or_another_life(self):
        records = [self.visit(f"delayed-{index}", [
            {"seq": 1, "actor_id": "away:alice", "kind": "say", "text": f"刷新前札记 {index}"},
        ]) for index in range(1, 46)]
        self.seed_history(*records)
        entries = self.page.locator(".visits-history > details[data-visit-id]")
        expect(entries).to_have_count(20)
        first = self.open_record("delayed-1")
        expect(first.locator(".visits-log-say")).to_have_text("刷新前札记 1")
        self.page.evaluate("""() => {
          const read=AstrBotPluginPage.apiGet;
          window.pendingHistoryMore=document.querySelector('.visits-history-more');
          window.holdHistoryRefresh=true;window.finishHistoryRefresh=null;
          AstrBotPluginPage.apiGet=(endpoint,payload)=>{
            let response=read(endpoint,payload);
            if(endpoint==='visits'&&payload.session==='test:GroupMessage:200'){
              response=response.then(result=>{
                result.history=result.history.map((visit,index)=>({...visit,id:`new-life-${index+1}`,
                  log:[{seq:1,actor_id:'away:alice',kind:'say',text:`新的生活札记 ${index+1}`}]}));
                return result;
              });
            }
            if(endpoint==='visits'&&window.holdHistoryRefresh){
              window.holdHistoryRefresh=false;
              return new Promise(resolve=>{window.finishHistoryRefresh=()=>response.then(resolve);});
            }
            return response;
          };
        }""")
        self.refresh()
        self.page.wait_for_function("typeof finishHistoryRefresh==='function'")
        expect(self.page.get_by_role("button", name="继续翻阅", exact=True)).to_be_disabled()
        self.page.evaluate("""() => {
          pendingHistoryMore.click();
          pendingHistoryMore.dispatchEvent(new MouseEvent('click',{bubbles:true}));
        }""")
        expect(entries).to_have_count(20)
        expect(first.locator(".visits-log-say")).to_have_text("刷新前札记 1")
        self.page.evaluate("finishHistoryRefresh()")
        expect(self.page.get_by_role("button", name="继续翻阅", exact=True)).to_be_enabled()
        expect(entries).to_have_count(20)
        expect(first.locator(".visits-log-say:visible")).to_have_text("刷新前札记 1")
        self.page.get_by_role("button", name="继续翻阅", exact=True).click()
        expect(entries).to_have_count(40)
        self.page.evaluate("window.oldLifeMore=document.querySelector('.visits-history-more')")
        self.page.locator("#status-session").select_option("test:GroupMessage:200")
        expect(self.page.locator('.visits-history > details[data-visit-id="new-life-1"] .visits-entry-preview')).to_have_text(
            "新的生活札记 1")
        expect(entries).to_have_count(20)
        self.page.evaluate("oldLifeMore.dispatchEvent(new MouseEvent('click',{bubbles:true}))")
        expect(entries).to_have_count(20)
        expect(self.page.locator(".visits-history .visits-moment")).to_have_count(0)
        self.page.get_by_role("button", name="继续翻阅", exact=True).click()
        expect(entries).to_have_count(40)
        expect(self.open_record("new-life-40").locator(".visits-log-say")).to_have_text("新的生活札记 40")
        self.assertEqual(self.errors, [])
