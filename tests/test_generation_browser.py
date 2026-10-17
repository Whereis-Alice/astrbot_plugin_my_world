"""Exercise generation dialogs with a real browser and controlled model replies.

Optional development dependency: playwright and its Chromium browser.
"""

from __future__ import annotations

import http.server
import json
import re
import threading
import unittest

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

from tools.render_screenshots import BRIDGE_JS, Handler, PLUGIN_DIR, build_payload
from core.generator import spring_layout_zones  # noqa: E402


GENERATION_BRIDGE = r"""
(() => {
  const original = window.AstrBotPluginPage.apiPost;
  window.generationCalls = [];
  window.saveCalls = [];
  window.pendingGeneration = [];
  window.layoutCalls = [];
  window.layoutReply = { positions: {}, level: "world" };
  window.AstrBotPluginPage.apiPost = (endpoint, data) => {
    if (endpoint.startsWith("generate/")) {
      // 快照一份：前端后来还会往同一个 draft 里追加区域
      window.generationCalls.push({endpoint, data: JSON.parse(JSON.stringify(data))});
      return new Promise((resolve, reject) => {
        window.pendingGeneration.push({resolve, reject});
      });
    }
    if (endpoint === "map/layout") {
      window.layoutCalls.push({endpoint, data});
      return Promise.resolve(window.layoutReply);
    }
    if (endpoint.startsWith("config/")) window.saveCalls.push(endpoint);
    return original(endpoint, data);
  };
  window.finishGeneration = (result) => window.pendingGeneration.shift().resolve(result);
  window.failGeneration = (message) => window.pendingGeneration.shift().reject(new Error(message));
  // 分块生成：按顺序喂若干块，每块解析成一个区域草稿
  window.finishZoneChunksPayload = async (chunks) => {
    for (const chunk of chunks) {
      while (!window.pendingGeneration.length) {
        await new Promise((resolve) => setTimeout(resolve, 0));
      }
      window.pendingGeneration.shift().resolve(chunk);
    }
  };
  window.finishZoneChunks = async (count, zones = []) => {
    const chunks = [];
    for (let index = 0; index < count; index += 1) {
      const zone = zones[index] || {
        id: `zone_${index + 1}`, name: `区域${index + 1}`, note: "",
        is_home: index === 0,
      };
      chunks.push({
        index: index + 1,
        zone,
        nodes: [],
        edges: [],
        zone_edges: [],
        actions: [],
        problems: [],
      });
    }
    await window.finishZoneChunksPayload(chunks);
  };
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

    def test_thought_and_schedule_history_keep_full_details(self):
        thoughts = [
            {
                "content": f"第 {index + 1} 次心里话",
                "at": 1_760_000_000 + index,
                "world_time": index,
                "node_name": "露台茶会角",
                "source": "聊完一轮",
                "target": "小鲸鱼",
                "quote": "今天风有点凉",
                "reply": "那我给你带件外套",
                "detail": {
                    "env": "露台茶会角，微风",
                    "state": "idle",
                    "mood": "满足",
                    "who": "小鲸鱼",
                    "intent": "顺着聊两句",
                },
            }
            for index in range(45)
        ]
        schedule_logs = {
            "events": [
                {
                    "id": 901,
                    "created_at": 1_760_000_100,
                    "world_time": 44,
                    "event_type": "schedule",
                    "detail": {
                        "run_id": "evening:901:44",
                        "id": "evening",
                        "time": "20:30",
                        "actions": "走到书房 → 上网",
                        "manual": False,
                        "smart": True,
                        "once": False,
                        "note": "看看今天有什么新闻",
                        "notes": ["日程 evening 已触发"],
                        "steps": [
                            {
                                "event_type": "move",
                                "world_time": 43,
                                "text": "从卧室走到书房",
                            },
                            {
                                "event_type": "tool_result",
                                "world_time": 44,
                                "text": "「网页检索」返回：今天有三条科技新闻",
                            },
                        ],
                    },
                }
            ],
            "types": ["schedule"],
            "has_more": False,
        }
        self.context.add_init_script(
            script=(
                "(() => {"
                " const original = window.AstrBotPluginPage.apiGet;"
                f" const thoughts = {json.dumps({'session': 's', 'thoughts': thoughts}, ensure_ascii=False)};"
                f" const logs = {json.dumps(schedule_logs, ensure_ascii=False)};"
                " window.AstrBotPluginPage.apiGet = (endpoint, params) => {"
                "   if (endpoint === 'thoughts') return Promise.resolve(thoughts);"
                "   if (endpoint === 'logs') return Promise.resolve(logs);"
                "   return original(endpoint, params);"
                " };"
                "})();"
            )
        )
        self.host.reload()
        self.page = self.host.frame(name="editor")
        expect(self.page.locator("#app")).to_be_visible()
        self.page.evaluate(
            """() => {
              for (const id of ["thought-session", "schedule-session"]) {
                const select = document.getElementById(id);
                select.innerHTML = '<option value="s">测试会话</option>';
                select.value = "s";
              }
            }"""
        )

        self.page.locator('button[data-tab="thoughts"]').click()
        expect(self.page.locator("#thoughts-reasoning")).to_have_count(0)
        expect(self.page.locator(".thought-row")).to_have_count(40)
        expect(self.page.locator(".thought-row").first).to_contain_text("空闲")
        expect(self.page.locator(".thought-row").first).to_contain_text("小鲸鱼")
        expect(self.page.locator(".thought-row").first).to_contain_text("今天风有点凉")
        self.page.locator(".thought-more").click()
        expect(self.page.locator(".thought-row")).to_have_count(45)
        self.host.screenshot(path=str(self.shots / "thoughts-detailed.png"))

        self.page.locator('button[data-tab="schedules"]').click()
        expect(self.page.locator(".schedule-run-row")).to_have_count(1)
        self.page.locator(".schedule-run-head").click()
        expect(self.page.locator(".schedule-run-step")).to_have_count(2)
        expect(self.page.locator(".schedule-run-row")).to_contain_text("今天有三条科技新闻")
        self.host.screenshot(path=str(self.shots / "schedule-run-details.png"))
        self.assertEqual(self.errors, [])

    def test_event_history_expands_past_the_old_height_cap(self):
        now = 1_760_000_000
        steps = [
            {
                "index": index + 1,
                "at": now + index,
                "desc": f"第 {index + 1} 幕：她做了一个选择",
                "ability": "观察",
                "ability_delta": {"观察": 0.1},
                "result": f"第 {index + 1} 幕的结果仍然完整显示。",
            }
            for index in range(80)
        ]
        events = {
            "abilities": {},
            "ability_day": "",
            "ability_spent_today": {},
            "enabled": True,
            "now": now,
            "world_time": 44,
            "pending_help": {},
            "active_id": "",
            "threads": [
                {
                    "id": "long-thread",
                    "title": "很长的一条线索",
                    "hook": "这件事有很多幕。",
                    "status": "closed",
                    "tier": "daily",
                    "tier_label": "日常",
                    "place_name": "书房",
                    "step_count": len(steps),
                    "opened_at": now - 1000,
                    "updated_at": now,
                    "closed_at": now,
                    "line": "她把这件事记了下来。",
                    "pending_followup": "",
                    "next_step_at": 0,
                    "steps": steps,
                    "can_advance": False,
                    "can_close": False,
                }
            ],
            "open_count": 0,
            "recent_titles": [],
        }
        self.context.add_init_script(
            script=(
                "(() => {"
                " const original = window.AstrBotPluginPage.apiGet;"
                f" const events = {json.dumps(events, ensure_ascii=False)};"
                " window.AstrBotPluginPage.apiGet = (endpoint, params) =>"
                "   endpoint === 'events' ? Promise.resolve(events) : original(endpoint, params);"
                "})();"
            )
        )
        self.host.reload()
        self.page = self.host.frame(name="editor")
        expect(self.page.locator("#app")).to_be_visible()
        self.page.evaluate(
            """() => {
              const select = document.getElementById("status-session");
              select.innerHTML = '<option value="s">测试会话</option>';
              select.value = "s";
            }"""
        )
        self.page.locator('button[data-tab="events"]').click()
        expect(self.page.locator("#event-history")).to_be_enabled()
        self.page.locator("#event-history").click()
        expect(self.page.locator(".event-history-row")).to_have_count(1)
        # 折叠时先不建时间线，避免几十条线索一起拖慢弹窗。
        expect(self.page.locator(".event-step")).to_have_count(0)
        self.page.locator(".event-history-head").click()
        expect(self.page.locator(".event-step")).to_have_count(80)
        panel = self.page.locator(".event-history-row.open .event-history-panel")
        self.assertEqual(panel.evaluate("node => getComputedStyle(node).maxHeight"), "none")
        expect(self.page.locator(".event-step").last).to_contain_text("第 80 幕")
        self.host.screenshot(path=str(self.shots / "event-history-full.png"))
        self.assertEqual(self.errors, [])

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
                expect(self.page.locator(".generation-status")).to_contain_text("正在生成")
                before = self.page.evaluate("generationCalls.length")
                self.page.locator("#dialog-ok").dispatch_event("click")
                self.page.locator("#dialog").press("Enter")
                self.assertEqual(self.page.evaluate("generationCalls.length"), before)
                if key == "zones":
                    self.host.screenshot(path=str(self.shots / "waiting-desktop.png"))
                    self.page.evaluate(
                        "finishZoneChunks(2, [{id: 'garden', name: '花园', note: '休息'}])"
                    )
                else:
                    self.page.evaluate(
                        "result => finishGeneration(result)", {key: [draft], "problems": []}
                    )
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
        self.page.evaluate(
            "finishZoneChunks(2, [{id: 'garden', name: '花园'}])"
        )
        expect(self.page.locator("#dialog-title")).to_contain_text("生成的新区域")
        expect(self.page.locator("#dialog-error")).to_be_empty()

    def test_no_usable_drafts_keeps_parser_reason(self):
        self.open_generator()
        self.page.locator("#dialog-ok").click()
        self.page.evaluate("finishGeneration({zone: null, problems: ['模型输出不是有效 JSON']})")
        expect(self.page.locator("#dialog-error")).to_contain_text("不是有效 JSON")
        expect(self.page.locator("#dialog")).to_be_visible()

    def test_zone_preview_can_be_edited_and_saved_explicitly(self):
        self.open_generator()
        self.page.locator("#dialog-ok").click()
        self.page.evaluate(
            "finishZoneChunks(2, [{id: 'garden', name: '花园', note: '散步'}])"
        )
        expect(self.page.locator("#dialog-title")).to_contain_text("生成的新区域")
        self.page.get_by_label("区域名", exact=True).first.fill("新花园")
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

    def test_generation_failure_keeps_the_form_for_retry(self):
        """服务端说生成失败（含超时）时，弹窗留着、参数还能改、可以再点一次。

        前端不再自己掐 130 秒：等多久由服务端的「生成超时」决定。
        """

        self.open_generator()
        self.page.locator("#dialog-ok").click()
        self.page.evaluate(
            "failGeneration('生成超过 10 分钟还没返回。内容多的时候几分钟是正常的')"
        )
        expect(self.page.locator("#dialog-error")).to_contain_text("10 分钟")
        expect(self.page.locator("#dialog-ok")).to_be_enabled()
        expect(self.page.locator('#dialog-body input[type="number"]').nth(0)).to_be_enabled()

    def test_invalid_count_does_not_call_model(self):
        self.open_generator()
        self.page.locator('#dialog-body input[type="number"]').nth(0).fill("0")
        self.page.locator("#dialog-ok").click()
        expect(self.page.locator("#dialog-error")).to_contain_text("1 到 4")
        self.assertEqual(self.page.evaluate("generationCalls"), [])

    def test_zone_generators_are_chunked_and_carry_context(self):
        """地图生成按区域分块：每块一个短请求，后一块带着前面已经生成的区域。"""

        self.open_generator()
        inputs = self.page.locator('#dialog-body input[type="number"]')
        # 生成数量 / 每个区域的地点 / 每个地点的动作
        expect(inputs).to_have_count(3)
        self.assertEqual(inputs.nth(1).get_attribute("max"), "8")
        inputs.nth(1).fill("4")
        inputs.nth(2).fill("2")
        self.page.locator('#dialog-body textarea').fill("爱丽丝的梦游仙境")
        self.page.locator("#dialog-ok").click()
        self.page.evaluate(
            "finishZoneChunks(2, [{id: 'first_zone', name: '第一块', is_home: true}])"
        )
        calls = self.page.evaluate("generationCalls")
        self.assertEqual({call["endpoint"] for call in calls}, {"generate/zone-chunk"})
        first, second = calls[0]["data"], calls[1]["data"]
        self.assertEqual(first["index"], 0)
        self.assertEqual(first["total"], 2)
        self.assertEqual(first["nodes_per_zone"], 4)
        self.assertEqual(first["actions_per_node"], 2)
        self.assertEqual(first["extra_prompt"], "爱丽丝的梦游仙境")
        self.assertEqual(first["existing"]["zones"], [])
        # 第二块必须带着第一块：模型才知道怎么接、怎么不重名
        self.assertEqual(
            [zone["id"] for zone in second["existing"]["zones"]], ["first_zone"]
        )
        expect(self.page.locator("#dialog-title")).to_contain_text("生成的新区域")
        self.page.locator("#dialog-cancel").click()

        self.open_generator("map", "map-generate-all")
        inputs = self.page.locator('#dialog-body input[type="number"]')
        expect(inputs).to_have_count(3)
        inputs.nth(0).fill("3")
        inputs.nth(1).fill("3")
        inputs.nth(2).fill("0")
        self.page.evaluate("generationCalls.length = 0")
        self.page.locator("#dialog-ok").click()
        self.page.evaluate("finishZoneChunks(3)")
        calls = self.page.evaluate("generationCalls")
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0]["data"]["total"], 3)
        self.assertEqual(calls[0]["data"]["actions_per_node"], 0)
        expect(self.page.locator("#dialog-title")).to_contain_text("一键重建地图")
        self.assertEqual(self.errors, [])

    def test_zone_regenerate_carries_prompt_actions_and_replace(self):
        """区域属性里的「重做这个区域…」：地点数 / 动作数 / 额外提示词 / 替换一起带上。"""

        self.page.locator('button[data-tab="map"]').click()
        self.page.get_by_role("button", name="重做这个区域").click()
        inputs = self.page.locator('#dialog-body input[type="number"]')
        # 地点数 + 每个地点的动作数（「替换 / 追加」是下拉框，「额外提示词」是 textarea）
        expect(inputs).to_have_count(2)
        inputs.nth(0).fill("2")
        inputs.nth(1).fill("1")
        self.page.locator("#dialog-body select").select_option("replace")
        self.page.locator("#dialog-body textarea").fill("安静一点")
        self.page.locator("#dialog-ok").click()

        call = self.page.evaluate("generationCalls").pop()
        self.assertEqual(call["endpoint"], "generate/zone")
        self.assertEqual(call["data"]["count"], 2)
        self.assertEqual(call["data"]["actions_per_node"], 1)
        self.assertEqual(call["data"]["extra_prompt"], "安静一点")
        self.assertTrue(call["data"]["replace"])

        self.page.evaluate(
            """(zoneId) => finishGeneration({
                zone: zoneId,
                nodes: [{id: 'quiet_spot', name: '安静角落', prompt: '没人打扰', x: 10, y: 10}],
                edges: [],
                actions: [{id: 'quiet_sit', name: '坐一会儿', scope: 'node',
                           allowed_nodes: ['quiet_spot'], node_id: 'quiet_spot'}],
                remove_nodes: [],
                problems: [],
            })""",
            call["data"]["zone"],
        )
        expect(self.page.locator("#dialog-title")).to_contain_text("重做")
        self.page.locator("#dialog-ok").click()
        expect(self.page.locator("#dialog")).to_be_hidden()

        config = self.page.evaluate("AstrBotPluginPage.apiGet('config')")
        self.assertTrue(
            [item for item in config["world"]["nodes"] if item["id"] == "quiet_spot"]
        )
        self.assertTrue(
            [item for item in config["world"]["actions"] if item["id"] == "quiet_sit"]
        )
        self.assertEqual(self.errors, [])

    def test_map_tools_can_collapse(self):
        """右下角的天气 + 缩放工具可以收起，给地图让位。"""

        self.page.locator('button[data-tab="map"]').click()
        toggle = self.page.locator("#map-tools-toggle")
        expect(toggle).to_be_visible()
        toggle.click()
        expect(self.page.locator("#map-bottom-tools")).to_have_class(re.compile("collapsed"))
        expect(self.page.locator(".map-zoom")).to_be_hidden()
        toggle.click()
        expect(self.page.locator(".map-zoom")).to_be_visible()

    def test_world_action_generator_lives_in_the_action_library(self):
        self.open_generator("actions", "action-generate-world")
        inputs = self.page.locator('#dialog-body input[type="number"]')
        expect(inputs).to_have_count(1)
        inputs.nth(0).fill("6")
        self.page.locator("#dialog-ok").click()
        action_call = self.page.evaluate("generationCalls").pop()
        self.assertEqual(action_call["endpoint"], "generate/world-actions")
        self.assertEqual(action_call["data"]["count"], 6)
        self.page.evaluate(
            "result => finishGeneration(result)",
            {
                "actions": [
                    {
                        "id": "water_flowers",
                        "node_id": "kitchen",
                        "name": "给花浇水",
                        "description": "给窗台上的花浇水",
                        "scope": "node",
                        "allowed_nodes": ["kitchen"],
                    }
                ],
                "problems": [],
            },
        )
        expect(self.page.locator("#dialog-title")).to_contain_text("生成的动作")
        self.page.locator("#dialog-ok").click()
        expect(self.page.locator("#dialog")).to_be_hidden()
        config = self.page.evaluate("AstrBotPluginPage.apiGet('config')")
        self.assertTrue(
            any(item["id"] == "water_flowers" for item in config["world"]["actions"])
        )
        self.assertEqual(self.errors, [])

    def test_action_filters_and_bulk_bar_are_usable(self):
        self.page.locator('button[data-tab="actions"]').click()
        expect(self.page.locator("#action-grid .action-card").first).to_be_visible()

        # 筛选是胶囊按钮：点一下亮起来、再点一下关掉；「重置」把它们全清掉
        legacy = self.page.locator("#action-filter-legacy")
        legacy.click()
        expect(legacy).to_have_class(re.compile(r"\bon\b"))
        self.page.locator("#action-filter-reset").click()
        expect(legacy).not_to_have_class(re.compile(r"\bon\b"))

        # 批量选择：全选当前结果 → 计数跟着变 → 取消选择清零
        self.page.locator("#action-select-mode").click()
        expect(self.page.locator("#action-bulk")).to_be_visible()
        expect(self.page.locator("#action-bulk-count")).to_contain_text("已选 0 个")
        self.page.locator("#action-bulk-all").click()
        expect(self.page.locator("#action-bulk-count")).not_to_contain_text("已选 0 个")
        self.page.locator("#action-bulk-clear").click()
        expect(self.page.locator("#action-bulk-count")).to_contain_text("已选 0 个")
        self.page.locator("#action-select-mode").click()
        expect(self.page.locator("#action-bulk")).to_be_hidden()
        self.assertEqual(self.errors, [])

    def test_default_actions_can_be_bulk_enabled_and_disabled(self):
        self.page.locator('button[data-tab="actions"]').click()
        advanced = self.page.locator("#tab-actions details.adv-menu")
        if not advanced.evaluate("element => element.open"):
            advanced.locator("summary").click()
        self.page.locator("#action-disable-defaults").click()
        expect(self.page.locator("#dialog-title")).to_contain_text("停用全部默认动作")
        self.page.locator("#dialog-ok").click()
        expect(self.page.locator("#dialog")).to_be_hidden()
        config = self.page.evaluate("AstrBotPluginPage.apiGet('config')")
        by_id = {item["id"]: item for item in config["world"]["actions"]}
        self.assertFalse(by_id["sing"]["enabled"])
        self.assertTrue(by_id["say"]["enabled"])

        if not advanced.evaluate("element => element.open"):
            advanced.locator("summary").click()
        self.page.locator("#action-enable-defaults").click()
        expect(self.page.locator("#dialog-title")).to_contain_text("启用全部默认动作")
        self.page.locator("#dialog-ok").click()
        expect(self.page.locator("#dialog")).to_be_hidden()
        config = self.page.evaluate("AstrBotPluginPage.apiGet('config')")
        by_id = {item["id"]: item for item in config["world"]["actions"]}
        self.assertTrue(by_id["sing"]["enabled"])
        self.assertEqual(self.errors, [])

    def test_map_stage_zoom_labels_pan_and_side_panel(self):
        self.page.locator('button[data-tab="map"]').click()
        expect(self.page.locator("#canvas-nodes .node").first).to_be_visible()
        zoom_before = self.page.locator("#map-zoom-label").inner_text()
        self.page.locator("#map-zoom-in").click()
        self.assertNotEqual(zoom_before, self.page.locator("#map-zoom-label").inner_text())
        self.assertIn(
            "scale(",
            self.page.locator("#canvas-world").get_attribute("style") or "",
        )

        self.page.locator('#tab-map [data-label-mode="ticks"]').click()
        expect(self.page.locator("#canvas-labels .edge-tick-chip").first).to_be_visible()
        self.page.locator('#tab-map [data-label-mode="all"]').click()
        expect(self.page.locator("#canvas-labels .edge-pair").first).to_be_visible()

        canvas = self.page.locator("#canvas")
        box = canvas.bounding_box()
        before = self.page.locator("#canvas-world").get_attribute("style")
        self.host.mouse.move(
            box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.85
        )
        self.host.mouse.down()
        self.host.mouse.move(
            box["x"] + box["width"] * 0.5 + 60,
            box["y"] + box["height"] * 0.85 + 30,
        )
        self.host.mouse.up()
        self.assertNotEqual(before, self.page.locator("#canvas-world").get_attribute("style"))

        side = self.page.locator("#map-side")
        self.assertNotIn("collapsed", side.get_attribute("class") or "")
        self.page.locator("#map-side-close").click()
        self.assertIn("collapsed", side.get_attribute("class") or "")
        self.page.locator("#map-side-toggle").click()
        self.assertNotIn("collapsed", side.get_attribute("class") or "")
        self.assertEqual(self.errors, [])

    def test_map_tab_keeps_scope_controls_and_uses_its_own_session_filter(self):
        self.page.locator('button[data-tab="map"]').click()
        # 地图页把整页让给画布，但「编辑范围 / 自主搭话」这两件事必须留在顶栏里，
        # 不能跟着会话下拉一起消失；地图自己的会话筛选则用工具栏上那一份。
        expect(self.page.locator("#config-scope")).to_be_visible()
        expect(self.page.locator("#proactive-enabled")).to_be_visible()
        expect(self.page.locator(".field-session")).to_be_hidden()
        expect(self.page.locator("#map-session")).to_be_visible()
        stage = self.page.locator("#map-stage").bounding_box()
        self.assertGreater(stage["height"], 850)
        canvas = self.page.locator("#canvas").bounding_box()
        tools = self.page.locator("#map-bottom-tools").bounding_box()
        self.assertGreater(tools["x"], canvas["x"] + canvas["width"] / 2)
        self.assertGreater(tools["y"], canvas["y"] + canvas["height"] / 2)
        self.assertEqual(self.errors, [])

    def test_map_canvas_fills_stage_and_relayout_uses_the_window_shape(self):
        self.page.locator('button[data-tab="map"]').click()
        expect(self.page.locator("#canvas-nodes .node").first).to_be_visible()
        stage = self.page.locator("#map-stage").bounding_box()
        canvas = self.page.locator("#canvas").bounding_box()
        # 旧版给过 .canvas 一个固定 440px 的高度，地图因此永远只占上半屏；
        # 画布要跟着舞台长高，下面的空白才真正还给地图。
        self.assertGreater(canvas["height"], stage["height"] - 8)

        before = self.page.locator('#canvas-nodes .node[data-id="home"]').get_attribute("style") or ""
        self.page.evaluate(
            "window.layoutReply = {positions: {home: [420, 260]}, level: 'world'}"
        )
        self.page.locator("#toolbar-world details.tool-menu summary").click()
        self.page.locator("#map-relayout").click()
        self.page.locator("#dialog-ok").click()
        expect(self.page.locator("#canvas-nodes .node[data-id='home']")).to_be_visible()
        self.host.wait_for_timeout(300)
        after = self.page.locator('#canvas-nodes .node[data-id="home"]').get_attribute("style") or ""
        self.assertIn("left: 420px", after)
        self.assertNotEqual(before, after)
        call = self.page.evaluate("window.layoutCalls.at(-1)")
        self.assertEqual(call["data"]["level"], "world")
        self.assertGreater(call["data"]["aspect"], 0)
        self.assertEqual(self.errors, [])

    def test_spring_layout_with_complex_routes_stays_readable(self):
        base_zones = [
            {"id": "home", "name": "家中", "is_home": True},
            {"id": "street", "name": "商业街"},
            {"id": "school", "name": "学校"},
            {"id": "park", "name": "公园"},
            {"id": "seaside", "name": "海边"},
        ]
        connections = [
            {"from_zone": "home", "to_zone": "street"},
            {"from_zone": "home", "to_zone": "park"},
            {"from_zone": "street", "to_zone": "school"},
            {"from_zone": "street", "to_zone": "park"},
            {"from_zone": "park", "to_zone": "seaside"},
        ]
        positions = spring_layout_zones(base_zones, connections)
        zones = [
            {
                "id": zone["id"],
                "name": zone["name"],
                "note": "",
                "x": positions[zone["id"]][0],
                "y": positions[zone["id"]][1],
                "color": "#7FB2E5",
            }
            for zone in base_zones
        ]
        nodes = [
            {
                "id": f"{zone['id']}_center",
                "zone_id": zone["id"],
                "name": f"{zone['name']}中心",
                "prompt": "",
                "x": 100,
                "y": 60,
            }
            for zone in base_zones
        ]
        zone_edges = [
            {
                "id": f"zp_{index}",
                "from_zone": link["from_zone"],
                "from_node": f"{link['from_zone']}_center",
                "to_zone": link["to_zone"],
                "to_node": f"{link['to_zone']}_center",
                "ticks": 2,
                "bidirectional": True,
            }
            for index, link in enumerate(connections)
        ]
        # 分块生成：一个区域一块，跨区路线挂在它涉及的那一块上
        chunks = [
            {
                "zone": zone,
                "nodes": [node for node in nodes if node["zone_id"] == zone["id"]],
                "edges": [],
                "zone_edges": [
                    link
                    for link in zone_edges
                    if zone["id"] in (link["from_zone"], link["to_zone"])
                ],
                "actions": [],
                "problems": [],
            }
            for zone in zones
        ]
        self.open_generator("map", "map-generate-all")
        self.page.locator('#dialog-body input[type="number"]').nth(0).fill("5")
        self.page.locator("#dialog-ok").click()
        self.page.evaluate("(chunks) => finishZoneChunksPayload(chunks)", chunks)
        expect(self.page.locator("#dialog-title")).to_contain_text("一键重建地图")
        self.page.locator("#dialog-ok").click()
        expect(self.page.locator("#dialog")).to_be_hidden()
        expect(self.page.locator("#canvas-nodes .node")).to_have_count(5)
        first_label = self.page.locator("#canvas-labels text.edge-ticks").first
        self.assertIn("rotate(", first_label.get_attribute("transform") or "")
        # 路线名字是一行「入口 ⟷ 入口」：三段同基线，中间那一段是双向箭头。
        self.assertEqual(first_label.locator("tspan").count(), 3)
        # SVG 里的 tspan 不是 HTMLElement，读文本要用 text_content
        self.assertEqual(first_label.locator("tspan").nth(1).text_content(), " ⟷ ")
        self.host.screenshot(path=str(self.shots / "ring-layout.png"))
        self.assertEqual(self.errors, [])
