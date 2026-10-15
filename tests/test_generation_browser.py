"""Exercise generation dialogs with a real browser and controlled model replies.

Optional development dependency: playwright and its Chromium browser.
"""

from __future__ import annotations

import http.server
import json
import threading
import unittest

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

from tools.render_screenshots import BRIDGE_JS, Handler, PLUGIN_DIR, build_payload


GENERATION_BRIDGE = r"""
(() => {
  const original = window.AstrBotPluginPage.apiPost;
  window.generationCalls = [];
  window.saveCalls = [];
  window.pendingGeneration = [];
  window.AstrBotPluginPage.apiPost = (endpoint, data) => {
    if (endpoint.startsWith("generate/")) {
      window.generationCalls.push({endpoint, data});
      return new Promise((resolve, reject) => {
        window.pendingGeneration.push({resolve, reject});
      });
    }
    if (endpoint.startsWith("config/")) window.saveCalls.push(endpoint);
    return original(endpoint, data);
  };
  window.finishGeneration = (result) => window.pendingGeneration.shift().resolve(result);
  window.failGeneration = (message) => window.pendingGeneration.shift().reject(new Error(message));
})();
"""


@unittest.skipIf(sync_playwright is None, "Install playwright to run browser regression tests")
class GenerationBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch()
        except Exception as exc:
            cls.pw.stop()
            if "Executable doesn't exist" in str(exc):
                raise unittest.SkipTest("Run playwright install chromium") from exc
            raise
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/index.html"
        cls.shots = PLUGIN_DIR / ".pytest_cache" / "generation-ui"
        cls.shots.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1440, "height": 1000})
        self.addCleanup(self.context.close)
        script = BRIDGE_JS.replace("__PAYLOAD__", json.dumps(build_payload(), ensure_ascii=False))
        self.context.add_init_script(script=script + GENERATION_BRIDGE)
        self.host = self.context.new_page()
        self.errors = []
        self.host.on("pageerror", lambda error: self.errors.append(str(error)))
        self.host.route("**/harness.html", lambda route: route.fulfill(
            content_type="text/html",
            body='<body style="margin:0"><iframe name="editor" src="./index.html" '
            'style="width:100vw;height:100vh;border:0"></iframe></body>',
        ))
        self.host.goto(self.url.replace("index.html", "harness.html"))
        self.page = self.host.frame(name="editor")
        expect(self.page.locator("#app")).to_be_visible()

    def open_generator(self, tab="map", button="zone-generate"):
        self.page.locator(f'button[data-tab="{tab}"]').click()
        self.page.locator(f"#{button}").click()

    def test_success_preserves_each_preview_and_prevents_duplicate_requests(self):
        for tab, button, key, draft, title in (
            ("map", "zone-generate", "zones", {"id": "garden", "name": "花园", "note": "休息"}, "生成的新区域"),
            ("schedules", "schedule-generate", "schedules", {"id": "walk", "time": "08:00", "note": "散步", "action_chain": []}, "生成的日程"),
            ("events", "event-generate", "events", {"title": "下雨了", "text": "窗外下起小雨。"}, "生成的事件"),
        ):
            with self.subTest(kind=key):
                self.open_generator(tab, button)
                self.page.locator("#dialog-ok").click()
                expect(self.page.locator("#dialog-ok")).to_be_disabled()
                expect(self.page.locator(".generation-status")).to_contain_text("正在生成草稿")
                before = self.page.evaluate("generationCalls.length")
                self.page.locator("#dialog-ok").dispatch_event("click")
                self.page.locator("#dialog").press("Enter")
                self.assertEqual(self.page.evaluate("generationCalls.length"), before)
                if key == "zones":
                    self.host.screenshot(path=str(self.shots / "waiting-desktop.png"))
                self.page.evaluate("result => finishGeneration(result)", {key: [draft], "problems": []})
                expect(self.page.locator("#dialog-title")).to_contain_text(title)
                expect(self.page.locator("#dialog")).to_be_visible()
                expect(self.page.locator("#dialog-ok")).to_be_enabled()
                self.assertEqual(self.page.evaluate("saveCalls"), [])
                self.page.locator("#dialog-cancel").click()
        self.assertEqual(self.errors, [])

    def test_failure_stays_visible_and_retry_can_open_preview(self):
        self.open_generator()
        self.page.locator("#dialog-ok").click()
        self.page.evaluate("failGeneration('内容生成失败：HTTP 429，额度不足，请检查内容生成模型')")
        expect(self.page.locator("#dialog-error")).to_contain_text("HTTP 429")
        expect(self.page.locator("#dialog-ok")).to_be_enabled()
        self.host.set_viewport_size({"width": 390, "height": 844})
        self.host.screenshot(path=str(self.shots / "failure-mobile.png"))
        box = self.page.locator("#dialog-card").bounding_box()
        self.assertGreaterEqual(box["x"], 0)
        self.assertLessEqual(box["x"] + box["width"], 391)
        self.assertTrue(self.page.locator("#dialog-body").evaluate(
            "body => body.scrollWidth <= body.clientWidth"
        ))
        self.page.locator("#dialog-ok").click()
        self.page.evaluate("finishGeneration({zones: [{id: 'garden', name: '花园'}], problems: []})")
        expect(self.page.locator("#dialog-title")).to_contain_text("生成的新区域")
        expect(self.page.locator("#dialog-error")).to_be_empty()

    def test_no_usable_drafts_keeps_parser_reason(self):
        self.open_generator()
        self.page.locator("#dialog-ok").click()
        self.page.evaluate("finishGeneration({zones: [], problems: ['模型输出不是有效 JSON']})")
        expect(self.page.locator("#dialog-error")).to_contain_text("不是有效 JSON")
        expect(self.page.locator("#dialog")).to_be_visible()

    def test_zone_preview_can_be_edited_and_saved_explicitly(self):
        self.open_generator()
        self.page.locator("#dialog-ok").click()
        self.page.evaluate("finishGeneration({zones: [{id: 'garden', name: '花园', note: '散步'}]})")
        expect(self.page.locator("#dialog-title")).to_contain_text("生成的新区域")
        self.page.get_by_label("区域名", exact=True).fill("新花园")
        self.page.locator("#dialog-ok").click()
        expect(self.page.locator("#dialog")).to_be_hidden()
        config = self.page.evaluate("AstrBotPluginPage.apiGet('config')")
        saved = [zone for zone in config["world"]["zones"] if zone["id"] == "garden"]
        self.assertEqual(saved[0]["name"], "新花园")

    def test_late_result_does_not_replace_a_new_dialog(self):
        self.open_generator()
        self.page.locator("#dialog-ok").click()
        self.page.locator("#dialog-cancel").click()
        self.open_generator("schedules", "schedule-generate")
        self.page.evaluate("finishGeneration({zones: [{id: 'late', name: '迟到区域'}]})")
        expect(self.page.locator("#dialog-title")).to_have_text("生成日程")
        expect(self.page.locator("#dialog-ok")).to_have_text("开始生成")

    def test_timeout_and_late_result_keep_retry_available(self):
        self.host.clock.install()
        self.open_generator()
        self.page.locator("#dialog-ok").click()
        self.host.clock.fast_forward(131000)
        expect(self.page.locator("#dialog-error")).to_contain_text("超时")
        expect(self.page.locator("#dialog-ok")).to_be_enabled()
        self.page.evaluate("finishGeneration({zones: [{id: 'late', name: '迟到区域'}]})")
        expect(self.page.locator("#dialog-title")).to_have_text("生成新区域")

    def test_invalid_count_does_not_call_model(self):
        self.open_generator()
        self.page.locator('#dialog-body input[type="number"]').fill("0")
        self.page.locator("#dialog-ok").click()
        expect(self.page.locator("#dialog-error")).to_contain_text("1 到 4")
        self.assertEqual(self.page.evaluate("generationCalls"), [])
