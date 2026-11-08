"""Meeting keepsakes retain confirmed evidence and remain safe to render."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from html.parser import HTMLParser

import pytest

from core.visit_card import build_visit_card_pages, render_visit_card_html


def visit(log=None, **extra):
    value = {
        "id": "keepsake", "status": "completed", "created_at": 1793656800,
        "arrived_at": 1793657400, "ended_at": 1793660400,
        "local": {"id": "home:isla", "actor_id": "isla", "name": "艾拉"},
        "remote": {"id": "away:alice", "actor_id": "alice", "name": "爱丽丝"},
        "scene": {"place": "港湾茶室", "activity": "一起喝茶"},
        "log": [] if log is None else log,
    }
    value.update(extra)
    return value


def entries(pages):
    return [row for page in pages for row in page["entries"] if row["category"] != "route"]


def test_dialogue_intention_and_feelings_share_a_moment_but_results_remain_neutral():
    pages = build_visit_card_pages(visit([
        {"seq": 1, "actor_id": "home:isla", "kind": "arrival", "text": "双方接受邀请，开始相处。"},
        {"seq": 2, "actor_id": "home:isla", "kind": "say", "at": 1793657400, "text": "我去拿茶杯。"},
        {"seq": 3, "actor_id": "home:isla", "kind": "action_plan", "at": 1793657400.2, "text": "打算拿两只杯子。"},
        {"seq": 4, "actor_id": "home:isla", "kind": "experience", "at": 1793657400.3, "text": "听见她的笑声，放松了一点。"},
        {"seq": 5, "actor_id": "home:isla", "kind": "result", "text": "茶杯已经摆好。"},
        {"seq": 6, "actor_id": "away:alice", "kind": "future_checkpoint", "say": "旧字段也不能变成台词。"},
    ]))
    rows = entries(pages)
    assert [row["category"] for row in rows] == ["chronicle", "speech", "result", "chronicle"]
    assert rows[1]["speaker"] == "艾拉"
    assert [part["type"] for part in rows[1]["parts"]] == ["speech", "intent", "experience"]
    assert rows[-1]["parts"][0]["type"] == "chronicle"
    html = render_visit_card_html(pages[0])
    parser = _TreeParser()
    parser.feed(html)
    shared = [node for node in parser.sections if "shared" in node["classes"]]
    assert all("speaker" not in node["child_classes"] for node in shared)


def test_continuations_require_consecutive_confirmed_sources():
    logs = [
        {"seq": 1, "actor_id": "home:isla", "kind": "say", "at": 50, "text": "第一句。"},
        {"seq": 3, "actor_id": "home:isla", "kind": "experience", "at": 50, "text": "中间有缺行。"},
        {"seq": 4, "actor_id": "home:isla", "kind": "say", "at": 51, "text": "第二句。"},
        {"seq": 5, "actor_id": "home:isla", "kind": "action_plan", "at": 55, "text": "已过了几秒。"},
        {"seq": 6, "actor_id": "away:alice", "kind": "experience", "at": 55, "text": "这是另一个人的感受。"},
    ]
    assert len(entries(build_visit_card_pages(visit(logs)))) == 5


def test_stock_arrival_remains_a_neutral_milestone_without_system_wording():
    page = build_visit_card_pages(visit([{
        "seq": 1, "actor_id": "home:isla", "kind": "arrival", "text": "双方接受邀请，开始在东道主世界相处。",
    }]))[0]
    assert page["entries"][0]["tag"] == "相见"
    assert not page["entries"][0]["parts"]
    assert "双方接受邀请" not in render_visit_card_html(page)


def test_actor_ids_are_exact_and_same_names_are_disambiguated():
    local = {"id": "home:same", "actor_id": "same", "name": "爱丽丝"}
    remote = {"id": "away:same", "actor_id": "same", "name": "爱丽丝"}
    logs = [{"actor_id": actor, "kind": "say", "text": f"记录{index}"}
            for index, actor in enumerate(("home:same", "away:same", "same", "missing", ""))]
    logs[-1]["speaker"] = "爱丽丝"
    rows = entries(build_visit_card_pages(visit(logs, local=local, remote=remote)))
    assert [row["side"] for row in rows] == ["local", "remote", "unknown", "unknown", "unknown"]
    assert [row["speaker"] for row in rows[:2]] == ["本方 · 爱丽丝", "对方 · 爱丽丝"]
    assert rows[-1]["speaker"] == "来源未确认 · 爱丽丝"
    local.pop("id")
    raw = visit(logs[:2], local=local, remote=remote, role="host", host_id="home:same", guest_id="away:same")
    assert [row["side"] for row in entries(build_visit_card_pages(raw))] == ["local", "remote"]
    assert entries(build_visit_card_pages(visit(logs[:1], local="malformed")))[0]["side"] == "unknown"


def test_complete_transcript_and_route_stay_in_one_card_without_mutation():
    logs = [{"seq": index, "actor_id": "home:isla", "kind": "say", "text": f"片刻{index}：" + "记下这一天。" * 65}
            for index in range(1, 57)]
    source = visit(logs, outing={"route": [{"place": f"地点{i}", "activity": f"活动{i}"} for i in range(13)]})
    original = deepcopy(source)
    pages = build_visit_card_pages(source, max_page_height=120)
    assert len(pages) == 1
    assert source == original
    assert [row["seq"] for row in entries(pages)] == list(range(1, 57))
    assert [part["text"] for row in entries(pages) for part in row["parts"]] == [row["text"] for row in logs]
    assert [stop["place"] for page in pages for row in page["entries"] if row["category"] == "route"
            for stop in row["stops"]] == [f"地点{i}" for i in range(13)]
    assert pages[0]["page"] == pages[0]["pages"] == 1
    assert pages[0]["first"] and pages[0]["last"]
    html = render_visit_card_html(pages[0])
    assert "未完" not in html and "接着上一张" not in html and "page-number" not in html


def test_oversized_legacy_paragraph_remains_whole_without_losing_characters():
    paragraph = "这一页很长。Mixed text, \n" * 900
    rows = entries(build_visit_card_pages(visit([{"kind": "say", "text": paragraph}])))
    assert len(rows) == 1
    assert rows[0]["parts"][0]["text"] == paragraph.strip()
    assert not rows[0]["continued"]


def test_ongoing_and_unaccepted_offers_cannot_be_presented_as_a_shared_trip():
    assert build_visit_card_pages(visit(status="active")) == []
    assert build_visit_card_pages(visit(status=["completed"])) == []
    declined = visit(status="declined", arrived_at=0,
                     outing_offer={"place": "还没去过的海边", "activity": "等待确认的散步"})
    page = build_visit_card_pages(declined)[0]
    assert page["title"] == "一次约见的记录"
    assert not page["activity"]
    assert "还没去过" not in render_visit_card_html(page)
    assert "没有留下逐句经过" in render_visit_card_html(page)


def test_world_timezone_and_arrival_date_are_explicit():
    at = datetime(2026, 11, 4, 23, 30, tzinfo=timezone.utc).timestamp()
    page = build_visit_card_pages(visit(arrived_at=at, ended_at=at + 3600), timezone="Asia/Shanghai")[0]
    assert page["date"] == "2026.11.05"
    assert page["interval"] == "07:30 — 08:30"
    assert page["duration"] == "1 小时 0 分钟"
    invalid = build_visit_card_pages(visit(arrived_at=float("nan"), ended_at=True), timezone="invalid/zone")[0]
    assert invalid["date"] == "日期未记"
    assert invalid["end_time"] == ""


class _TreeParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.sections = []
        self.section = None

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        classes = dict(attrs).get("class", "").split()
        if tag == "section" and "entry" in classes:
            self.section = {"classes": classes, "child_classes": []}
            self.sections.append(self.section)
        elif self.section:
            self.section["child_classes"].extend(classes)

    def handle_endtag(self, tag):
        if tag == "section":
            self.section = None


def test_all_record_text_is_escaped_and_never_compiled_as_a_template():
    attack = '<img src="https://bad.example/a" onerror="window.injected=true"><script>alert(1)</script>{{ 7*7 }}'
    source = visit([{"kind": 'other" onmouseover="alert(1)', "text": attack}],
                   local={"name": attack}, scene={"place": attack, "activity": attack}, end_reason=attack,
                   outing={"route": [{"place": attack, "activity": attack}]})
    html = render_visit_card_html(build_visit_card_pages(source)[0])
    parser = _TreeParser()
    parser.feed(html)
    assert not {"img", "script", "a", "link", "iframe"}.intersection(parser.tags)
    assert "&lt;img" in html and "&lt;script&gt;" in html
    assert "{{ 7*7 }}" in html
    assert "onmouseover" not in " ".join(parser.sections[0]["classes"])


def test_real_browser_layout_wraps_long_tokens_and_retains_full_page():
    playwright = pytest.importorskip("playwright.sync_api")
    page_data = build_visit_card_pages(visit([
        {"seq": 1, "actor_id": "home:isla", "kind": "say", "text": "这是很长的链接：" + "abcdefghijklmnopqrstuvwxyz" * 20},
        {"seq": 2, "actor_id": "away:alice", "kind": "say", "text": "把这一天好好留在这里。"},
    ]))[0]
    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True)
        except playwright.Error as exc:
            if "Executable doesn't exist" in str(exc):
                pytest.skip("Install Chromium for browser layout verification")
            raise
        try:
            page = browser.new_page(viewport={"width": 900, "height": 1000})
            requests = []
            page.on("request", lambda request: requests.append(request.url))
            page.set_content(render_visit_card_html(page_data))
            assert page.locator("body").evaluate("node => node.scrollWidth") == 900
            assert page.locator(".card").bounding_box()["width"] == 900
            assert page.locator(".speech").count() == 2
            assert page.locator(".ending").bounding_box()["y"] > page.locator(".entry").last.bounding_box()["y"]
            assert not requests
        finally:
            browser.close()


def test_real_browser_activity_is_compact_and_long_card_keeps_one_dark_theme():
    playwright = pytest.importorskip("playwright.sync_api")
    logs = [
        {"seq": 1, "kind": "arrival", "text": "双方接受邀请，开始在东道主世界相处。"},
        {"seq": 2, "kind": "activity", "text": "喝热红茶搭配茶点闲聊"},
        {"seq": 3, "kind": "event", "text": "窗外的雨声渐渐小了。"},
        {"seq": 4, "kind": "result", "text": "两个人已经把茶点吃完。"},
    ]
    logs += [{"seq": index, "actor_id": "home:isla" if index % 2 else "away:alice",
              "kind": "say", "at": 1793657400 + index * 30,
              "text": f"第{index}个片刻。" + "还想再陪你坐一会儿，听听窗外的雨声。" * 3}
             for index in range(5, 85)]
    cards = build_visit_card_pages(visit(logs))
    assert len(cards) == 1
    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(headless=True)
        except playwright.Error as exc:
            if "Executable doesn't exist" in str(exc):
                pytest.skip("Install Chromium for browser layout verification")
            raise
        try:
            page = browser.new_page(viewport={"width": 900, "height": 1000})
            page.set_content(render_visit_card_html(cards[0]))
            activity = page.locator(".kind-activity .entry-body")
            assert activity.bounding_box()["height"] < 150
            assert page.locator(".kind-activity").evaluate("node => getComputedStyle(node).whiteSpace") == "normal"
            assert page.locator(".kind-result").evaluate("node => getComputedStyle(node).fontSize") == "16px"
            assert page.locator(".entry").count() == len(logs)
            assert page.locator(".card").bounding_box()["height"] > 5200
            backgrounds = [page.locator(selector).evaluate("node => getComputedStyle(node).backgroundColor")
                           for selector in ("body", ".card", ".cover", ".content")]
            assert backgrounds == ["rgb(39, 43, 72)"] * 4
            assert page.locator(".part.speech").first.evaluate("node => getComputedStyle(node).fontSize") == "25px"
            last_entry = page.locator(".entry").last.bounding_box()
            ending = page.locator(".ending").bounding_box()
            card = page.locator(".card").bounding_box()
            assert ending["y"] >= last_entry["y"] + last_entry["height"]
            assert ending["y"] + ending["height"] <= card["height"]
            assert page.locator("body").evaluate("node => node.scrollWidth") == 900
            assert not page.locator(".continued-note,.page-number,.folio").count()
        finally:
            browser.close()
