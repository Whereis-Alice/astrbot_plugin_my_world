"""The call ledger in the real editor: filtering, privacy, empty/error states and layout."""

import json
import unittest

from tests import test_generation_browser as generation
from tests.test_generation_browser import expect, sync_playwright
from tools.render_screenshots import BRIDGE_JS, build_payload


CALLS_BRIDGE = r"""
(() => {
  const original = AstrBotPluginPage.apiGet;
  window.callReads = [];
  window.callMode = 'normal';
  window.callsPending = [];
  const now = Date.now() / 1000;
  const usage = {available: true, partial: false, input: 1420, input_other: 1020, input_cached: 400, output: 286, total: 1706};
  const purposes = ['plan','chat','helper','consolidate','vision','search'];
  window.callRows = purposes.map((purpose, i) => ({id: i+1, started_at: now-i*1250, duration_ms: 1630+i*430,
    session_id: 'test:GroupMessage:100', session_label: '星空与旅行 · 100', purpose,
    reason: ['自主决策需要选择接下来做什么', '收到用户消息，由 AstrBot 原生 Agent 生成最终回复',
      '动作执行前补全工具参数', '睡眠到点，整理今天的记忆与画像', '收到图片，需要理解画面内容', '搜索结果返回后决定是否继续'][i],
    chain: i === 0 ? ['生活时钟推进','自主决策需要选择接下来做什么'] : [],
    provider_id: i % 2 ? 'chat-provider' : 'utility-provider', model: i % 2 ? 'chat-model' : 'utility-model',
    coverage: i === 1 ? 'native_final' : 'direct', attempt: i === 2 ? 2 : 1,
    retry_reason: i === 2 ? '采样参数不兼容，退回只传温度' : '', trace_id: 'trace-' + i, response_id: 'response-' + i,
    status: i === 4 ? 'error' : i === 5 ? 'cancelled' : 'success',
    usage: i < 4 ? usage : {available: false, partial: false, input: null, output: null, total: null, input_cached: null},
    error: i === 4 ? 'Provider 请求失败，请检查模型配置' : ''}));
  AstrBotPluginPage.apiGet = async (endpoint, params) => {
    if (endpoint !== 'model-calls') return original(endpoint, params);
    callReads.push(structuredClone(params));
    if (callMode === 'error') throw new Error('临时读取失败');
    if (callMode === 'delayed') return new Promise(resolve => callsPending.push(resolve));
    let rows = structuredClone(callMode === 'empty' ? [] : callRows);
    for (const [key, field] of [['status','status'],['purpose','purpose'],['provider','provider_id'],['session','session_id']])
      if (params[key]) rows = rows.filter(row => row[field] === params[key]);
    if (params.q) rows = rows.filter(row => [row.reason,row.model,row.session_id].some(text => text.includes(params.q)));
    const all = rows;
    if (callMode === 'pages') rows = rows.slice((params.page-1)*2, params.page*2);
    return {items: rows, page: params.page, pages: callMode === 'pages' ? 3 : 1, hours: params.hours,
      summary: {calls: all.length, errors: all.filter(r=>r.status==='error').length, cancelled: all.filter(r=>r.status==='cancelled').length,
        reported: all.filter(r=>r.usage.available).length, input_reported: 4, output_reported: 4, cached_reported: 4,
        total: 6824, input: 5680, output: 1144, cached: 1600, duration_ms: 2705, final_only: 1},
      series: Array.from({length: 24}, (_,i)=>({at: Math.floor(now/3600)*3600-i*3600,calls: i%4+1,tokens: (i%5)*465})),
      facets: {sessions: ['test:GroupMessage:100'], purposes, providers: ['chat-provider','utility-provider']},
      session_labels: {'test:GroupMessage:100': '星空与旅行 · 100'}};
  };
})();
"""


@unittest.skipIf(sync_playwright is None, "Install playwright to run browser tests")
class ModelCallsBrowserTests(unittest.TestCase):
    setUpClass = classmethod(generation.GenerationBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(generation.GenerationBrowserTests.tearDownClass.__func__)

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1600, "height": 900}, reduced_motion="reduce")
        self.addCleanup(self.context.close)
        script = BRIDGE_JS.replace("__PAYLOAD__", json.dumps(build_payload(), ensure_ascii=False))
        self.context.add_init_script(script=script + CALLS_BRIDGE)
        self.host = self.context.new_page()
        self.errors = []
        self.host.on("pageerror", lambda error: self.errors.append(str(error)))
        self.host.route("**/harness.html", lambda route: route.fulfill(content_type="text/html",
            body='<iframe name="editor" src="./index.html" style="position:fixed;inset:0;width:100%;height:100%;border:0"></iframe>'))
        self.host.goto(self.url.replace("index.html", "harness.html"))
        self.page = self.host.frame(name="editor")
        expect(self.page.locator("#app")).to_be_visible()
        self.page.locator('[data-tab="model-calls"]').click()
        expect(self.page.locator(".calls-row")).to_have_count(6)

    def test_filters_details_pagination_unknown_usage_and_log_entry(self):
        expect(self.page.locator(".calls-metric-accent strong")).to_have_text("6,824")
        rows = self.page.locator(".calls-row")
        rows.nth(1).locator("summary").click()
        expect(rows.nth(1).locator(".calls-coverage-note")).to_contain_text("中间轮次")
        rows.nth(0).locator("summary").click()
        expect(rows.nth(0).locator(".calls-trigger-path")).to_contain_text("生活时钟推进")
        expect(rows.nth(4).locator(".calls-tokens")).to_contain_text("用量未提供")
        expect(rows.nth(4).locator(".calls-tokens strong")).to_have_text("—")
        self.page.locator(".calls-filter-menu > summary").click()
        self.page.locator("#calls-status").select_option("error")
        expect(rows).to_have_count(1)
        expect(self.page.locator("#calls-filter-count")).to_have_text("1")
        self.assertEqual(self.page.evaluate("callReads.at(-1).config_scope"), "")
        self.page.locator("#calls-status").select_option("")
        self.page.locator(".calls-filter-menu").press("Escape")
        self.page.evaluate("callMode = 'pages'")
        self.page.locator("#calls-refresh").click()
        expect(rows).to_have_count(2)
        self.page.locator("#calls-next").click()
        expect(self.page.locator("#calls-page")).to_have_text("2 / 3")
        expect(rows.first).to_contain_text("参数补全")
        self.page.locator('[data-tab="logs"]').click()
        self.page.locator("#log-model-calls").click()
        expect(self.page.locator("#tab-model-calls")).to_be_visible()
        self.assertEqual(self.errors, [])

    def test_untrusted_text_empty_error_and_stale_responses(self):
        self.page.evaluate("callRows[0].reason = '<img src=x onerror=alert(1)>'; callRows[0].model = 'VeryLongModelName'.repeat(15)")
        self.page.locator("#calls-refresh").click()
        expect(self.page.locator(".calls-row").first).to_contain_text("<img src=x onerror=alert(1)>")
        self.assertEqual(self.page.locator("#calls-list img").count(), 0)
        self.page.evaluate("callMode = 'error'")
        self.page.locator("#calls-refresh").click()
        expect(self.page.locator("#calls-error-state")).to_contain_text("临时读取失败")
        expect(self.page.locator(".calls-row")).to_have_count(6)
        self.page.evaluate("callMode = 'empty'")
        self.page.locator("#calls-refresh").click()
        expect(self.page.locator(".calls-empty")).to_contain_text("这里还没有调用记录")
        self.page.evaluate("callMode = 'delayed'")
        self.page.locator("#calls-refresh").click()
        self.page.locator("#calls-period").select_option("168")
        self.page.wait_for_function("callsPending.length === 2")
        self.page.evaluate("callsPending[1]({items: [], summary: {calls: 0}, hours:168, page:1, pages:1, facets:{}})")
        expect(self.page.locator("#calls-coverage")).to_contain_text("最近 7 天")
        self.page.evaluate("callsPending[0]({items: [], summary: {calls: 99}, hours:24, page:1, pages:1, facets:{}})")
        expect(self.page.locator("#calls-total")).to_have_text("0 条记录")
        self.assertEqual(self.errors, [])

    def test_six_themes_responsive_layout_and_short_sidebar(self):
        for width, height in [(1600,900),(1024,768),(390,844),(360,780)]:
            self.host.set_viewport_size({"width":width,"height":height})
            self.host.screenshot(path=str(self.shots / f"model-calls-overview-{width}.png"), animations="disabled")
            self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), f"Overflow {width}")
            self.assertLess(self.page.locator(".calls-ledger").bounding_box()["y"], 500, f"Ledger pushed down at {width}px")
            expect(self.page.locator("#save")).not_to_be_visible()
            expect(self.page.locator(".topbar-context")).not_to_be_visible()
            self.page.locator(".calls-row").first.locator("summary").click()
            self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"))
            self.host.screenshot(path=str(self.shots / f"model-calls-{width}.png"), animations="disabled")
            self.page.locator(".calls-row").first.locator("summary").click()
        self.host.set_viewport_size({"width":1600,"height":900})
        for theme in ("nocturne","aurora","rose","cloud","sand","jade"):
            self.page.locator("#theme-toggle").click()
            self.page.locator(f'[data-appearance="{theme}"]').click()
            self.page.get_by_label("关闭外观设置").click()
            self.host.screenshot(path=str(self.shots / f"model-calls-{theme}.png"), animations="disabled")
        self.host.set_viewport_size({"width":1600,"height":640})
        self.page.locator('[data-tab="presets"]').click()
        self.page.locator('[data-tab="model-calls"]').click()
        self.assertTrue(self.page.locator('.sidebar .tabs').evaluate('node => node.scrollHeight <= node.clientHeight + 1 && node.scrollTop === 0'))
        self.assertEqual(self.errors, [])

    def test_auto_refresh_waits_while_reading_details_and_when_another_page_is_open(self):
        self.host.clock.install()
        self.page.locator(".calls-filter-menu > summary").click()
        self.page.locator("#calls-auto").check()
        self.page.locator(".calls-filter-menu").press("Escape")
        before = self.page.evaluate("callReads.length")
        self.host.clock.fast_forward(15000)
        self.page.wait_for_function("count => callReads.length === count + 1", arg=before)
        expect(self.page.locator("#calls-list")).not_to_have_attribute("aria-busy", "true")
        self.page.locator(".calls-row").first.locator("summary").click()
        before = self.page.evaluate("callReads.length")
        self.host.clock.fast_forward(15000)
        self.assertEqual(self.page.evaluate("callReads.length"), before)
        self.page.locator('[data-tab="logs"]').click()
        self.host.clock.fast_forward(15000)
        self.assertEqual(self.page.evaluate("callReads.length"), before)
        self.assertEqual(self.errors, [])
