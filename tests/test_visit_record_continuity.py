"""Completed keepsakes retain their ending, and refreshes do not conceal missing pages."""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import unittest

from tests import test_visit_journal_reading_z as reading
from tests.test_generation_browser import expect, sync_playwright


@unittest.skipUnless(shutil.which("node"), "Node is required for pure record cursor verification")
class RecordCursorTests(unittest.TestCase):
    def test_cursor_fills_refresh_gaps_without_discarding_a_cached_prefix(self):
        module = (Path(__file__).resolve().parents[1] / "pages/world_editor/visit-records.js").as_uri()
        script = "import {recordCursor} from " + repr(module) + ";\n" + r'''
import assert from 'node:assert/strict';
const rows=(start,end)=>Array.from({length:end-start+1},(_,i)=>({seq:start+i}));
assert.equal(recordCursor(rows(98,289)),97);
assert.equal(recordCursor([...rows(1,192),...rows(205,396)]),204);
assert.equal(recordCursor([...rows(1,396),{seq:396}]),0);
assert.equal(recordCursor([...rows(1,20),...rows(25,30),...rows(35,50)]),34);
assert.equal(recordCursor([{seq:1,kind:'activity',text:'开始相处：旧标记'},...rows(2,10)]),0);
assert.equal(recordCursor([], {next_before:96,log_offset:192}),96);
assert.equal(recordCursor([], {log_offset:192}),192);
assert.equal(recordCursor([], {next_before:-1,log_offset:'192'}),0);
'''
        result = subprocess.run([shutil.which("node"), "--input-type=module"], input=script,
                                text=True, encoding="utf-8", capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


@unittest.skipIf(sync_playwright is None, "Install playwright for browser verification")
class VisitRecordContinuityTests(unittest.TestCase):
    setUpClass = reading.VisitJournalReadingTests.__dict__["setUpClass"]
    tearDownClass = reading.VisitJournalReadingTests.__dict__["tearDownClass"]
    setUp = reading.VisitJournalReadingTests.setUp
    visit = staticmethod(reading.VisitJournalReadingTests.visit)
    refresh = reading.VisitJournalReadingTests.refresh
    seed_history = reading.VisitJournalReadingTests.seed_history
    open_record = reading.VisitJournalReadingTests.open_record

    def test_an_arrived_but_interrupted_visit_keeps_its_saved_ending_after_dialogue(self):
        reason = '<img src=x onerror="window.injected=true">\n暂时无法继续确认对方的状态。'
        self.seed_history(self.visit("interrupted-arrival", [
            {"seq": 1, "actor_id": "away:alice", "kind": "say", "text": "一起看看窗边的花吧。"},
        ], status="interrupted", end_reason=reason))
        card = self.open_record("interrupted-arrival")
        expect(card.locator(".visits-record-ending")).to_contain_text("这次暂时中断")
        expect(card.locator(".visits-record-ending-reason")).to_have_text(reason)
        expect(card.locator(".visits-record-ending time")).not_to_have_text("")
        expect(card.locator(".visits-log-say")).to_have_text("一起看看窗边的花吧。")
        expect(card.locator(".visits-record-ending img, .visits-record-ending .visits-log-head")).to_have_count(0)
        expect(card.locator(".visits-record-ending")).to_have_attribute("data-status", "interrupted")
        self.refresh()
        expect(card.locator(".visits-record-ending-reason:visible")).to_have_text(reason)
        for width in (390, 1440):
            self.host.set_viewport_size({"width": width, "height": 1100})
            card.scroll_into_view_if_needed()
            self.assertLessEqual(card.bounding_box()["width"], width)
            self.host.screenshot(path=str(self.shots / f"visit-record-ending-{width}.png"))
        self.assertEqual(self.errors, [])

    def test_saved_farewell_is_not_repeated_and_missing_legacy_reason_is_explicit(self):
        self.seed_history(self.visit("farewell", [
            {"seq": 1, "actor_id": "home:isla", "kind": "say", "text": "下次再一起喝茶。"},
        ], end_reason="下次再一起喝茶。"), self.visit("legacy-ending", [], end_reason=""))
        farewell = self.open_record("farewell")
        expect(farewell.locator(".visits-record-ending")).to_contain_text("已告别归家")
        expect(farewell.locator(".visits-record-ending-reason")).to_have_count(0)
        expect(farewell.locator(".visits-visit-body").get_by_text("下次再一起喝茶。", exact=True)).to_have_count(1)
        legacy = self.open_record("legacy-ending")
        expect(legacy.locator(".visits-record-ending-reason")).to_have_text("这份旧记录没有保存结束说明。")
        self.assertEqual(self.errors, [])

    def test_refresh_recovers_a_gap_between_an_old_prefix_and_the_new_wire_tail(self):
        self.page.evaluate("""() => {
          window.completeRows=Array.from({length:396},(_,index)=>({seq:index+1,
            actor_id:'away:alice',kind:'say',text:`连续记录 ${index+1}`}));
          const write=AstrBotPluginPage.apiPost;
          AstrBotPluginPage.apiPost=(endpoint,payload)=>{
            if(endpoint!=='visits/record')return write(endpoint,payload);
            visitWrites.push({endpoint,payload:structuredClone(payload)});
            const end=payload.before,start=Math.max(0,end-96);
            return Promise.resolve({id:'refresh-gap',log:completeRows.slice(start,end),next_before:start});
          };
        }""")
        self.seed_history(self.visit("refresh-gap", self.page.evaluate("completeRows.slice(0,192)")))
        card = self.open_record("refresh-gap")
        expect(card.get_by_role("button", name="继续往前翻", exact=True)).to_have_count(0)
        self.page.evaluate("""() => {
          visitData.history[0].log=completeRows.slice(204);
          visitData.history[0].log_count=396;visitData.history[0].log_offset=204;
        }""")
        self.refresh()
        expect(card.locator(".visits-log-say")).to_have_count(384)
        expect(card.locator(".visits-record-gap")).to_have_text("这里还有一段未加载的经过，继续往前翻可补齐。")
        card.get_by_role("button", name="继续往前翻", exact=True).click()
        expect(card.locator(".visits-log-say")).to_have_count(396)
        self.assertEqual(self.page.evaluate("visitWrites.map(item=>item.payload.before)"), [204])
        self.assertEqual(card.locator(".visits-log-say").all_text_contents(),
                         [f"连续记录 {index}" for index in range(1, 397)])
        expect(card.get_by_role("button", name="继续往前翻", exact=True)).to_have_count(0)
        self.assertEqual(self.errors, [])

    def test_formal_record_wins_over_an_empty_or_short_legacy_projection(self):
        for legacy in ([], [{"speaker": "旧来源", "say": "旧局部投影。"}]):
            with self.subTest(legacy=legacy):
                self.seed_history(self.visit("formal", [
                    {"seq": 1, "actor_id": "away:alice", "kind": "say", "text": "正式保存的第一句。"},
                    {"seq": 2, "actor_id": "home:isla", "kind": "say", "text": "正式保存的后续。"},
                ], transcript=legacy))
                card = self.open_record("formal")
                self.assertEqual(card.locator(".visits-log-say").all_text_contents(),
                                 ["正式保存的第一句。", "正式保存的后续。"])
                expect(card).not_to_contain_text("旧局部投影。")
        self.assertEqual(self.errors, [])

    def test_an_empty_page_keeps_the_missing_records_reachable_for_retry(self):
        self.page.evaluate("""() => {
          const write=AstrBotPluginPage.apiPost;
          AstrBotPluginPage.apiPost=(endpoint,payload)=>endpoint==='visits/record'
            ?Promise.resolve({id:'empty-page',log:[],next_before:0}):write(endpoint,payload);
        }""")
        self.seed_history(self.visit("empty-page", [
            {"seq": 3, "actor_id": "away:alice", "kind": "say", "text": "后来的记录仍可见。"},
        ], log_offset=2, log_count=3))
        card = self.open_record("empty-page")
        card.get_by_role("button", name="继续往前翻", exact=True).click()
        expect(card.get_by_role("button", name="继续往前翻", exact=True)).to_be_enabled()
        expect(card.locator(".visits-log-say")).to_have_text("后来的记录仍可见。")
        self.assertEqual(self.errors, [])

    def test_live_records_keep_the_real_wait_and_next_speaker_visible(self):
        active = self.visit("waiting-live", [
            {"seq": 1, "actor_id": "away:alice", "kind": "say", "text": "我给你留了一杯茶。"},
        ], status="active", ended_at=0, observed_at=1793657450,
            progress={"kind": "delivery_wait", "reason": "联系暂未送达，正在重试", "next_at": 1793657500})
        self.page.evaluate("visit => {visitData.active=visit;visitData.history=[];}", active)
        self.refresh()
        note = self.page.locator(".visits-current .visits-record-progress")
        expect(note.locator(".visits-progress-reason")).to_have_text("联系暂未送达，正在重试")
        expect(note.locator(".visits-progress-time")).to_contain_text("继续")
        expect(self.page.locator(".visits-current .visits-record-ending")).to_have_count(0)
        self.page.evaluate("""() => {
          visitData.active.progress={kind:'next_turn',reason:'下一步轮到她回应',actor_id:'home:isla'};
        }""")
        self.refresh()
        expect(note.locator(".visits-progress-reason")).to_have_text("接下来由艾拉回应")
        expect(note.locator(".visits-progress-time")).to_have_count(0)
        expect(self.page.locator(".visits-current .visits-log-say")).to_have_text("我给你留了一杯茶。")
        self.assertEqual(self.errors, [])
