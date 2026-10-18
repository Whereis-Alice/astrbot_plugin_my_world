"""Exercise configuration inheritance and pause controls in Chromium."""

import json
import unittest

from tests import test_generation_browser as _generation
from tests.test_generation_browser import expect, sync_playwright
from tools.render_screenshots import BRIDGE_JS, build_payload


SCOPE_BRIDGE = r"""
(() => {
  const original = window.AstrBotPluginPage;
  window.scopeWrites = [];
  const local = {};
  const mapKeys = ['zones', 'zone_edges', 'nodes', 'edges'];
  const actionKeys = ['actions', 'purged_actions', 'idle_life'];
  const fixed = ['admin_ids', 'proactive_chat_paused', 'wizard_done', 'schema_version'];
  const pick = (world, block) => Object.fromEntries(Object.entries(world).filter(([key]) =>
    block === 'map' ? mapKeys.includes(key) : block === 'actions' ? actionKeys.includes(key)
    : ![...mapKeys, ...actionKeys, ...fixed].includes(key)));
  window.AstrBotPluginPage = {...original,
    apiGet: async (endpoint, params = {}) => {
      const data = await original.apiGet(endpoint, params);
      if (endpoint !== 'config') return data;
      data.sessions.sessions = [
        {session_id: 'test:GroupMessage:100', note: 'Test A', enabled: true},
        {session_id: 'test:GroupMessage:200', note: 'Test B', enabled: true},
      ];
      data.sessions.groups = [];
      data.global_config = structuredClone({world: data.world, schedules: data.schedules});
      data.scope = params.config_scope || '';
      data.scope_blocks = Object.keys(local[data.scope] || {});
      for (const [block, value] of Object.entries(local[data.scope] || {})) {
        if (block === 'schedules') data.schedules = structuredClone(value);
        else Object.assign(data.world, structuredClone(value));
      }
      return data;
    },
    apiPost: async (endpoint, data) => {
      scopeWrites.push({endpoint, data: structuredClone(data)});
      if (endpoint === 'config/scope') {
        local[data.config_scope] = Object.fromEntries(data.blocks.map(block =>
          [block, structuredClone(block === 'schedules' ? data.schedules : pick(data.world, block))]));
        return {ok: true};
      }
      if (endpoint === 'config/proactive') {
        const config = await original.apiGet('config');
        config.world.proactive_chat_paused = !data.enabled;
        await original.apiPost('config/world', {world: config.world});
        return {ok: true};
      }
      return original.apiPost(endpoint, data);
    },
  };
})();
"""


@unittest.skipIf(sync_playwright is None, "Install playwright to run browser tests")
class ScopeBrowserTests(unittest.TestCase):
    setUpClass = classmethod(_generation.GenerationBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(_generation.GenerationBrowserTests.tearDownClass.__func__)

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1440, "height": 1000})
        self.addCleanup(self.context.close)
        script = BRIDGE_JS.replace("__PAYLOAD__", json.dumps(build_payload(), ensure_ascii=False))
        self.context.add_init_script(script=script + SCOPE_BRIDGE)
        self.host = self.context.new_page()
        self.errors = []
        self.host.on("pageerror", lambda error: self.errors.append(str(error)))
        self.host.route("**/harness.html", lambda route: route.fulfill(
            content_type="text/html", body='<iframe name="editor" src="./index.html" '
            'style="position:fixed;inset:0;width:100%;height:100%;border:0"></iframe>',
        ))
        self.host.goto(self.url.replace("index.html", "harness.html"))
        self.page = self.host.frame(name="editor")
        expect(self.page.locator("#app")).to_be_visible()

    def select_local(self, tab="settings"):
        self.page.locator(f'button[data-tab="{tab}"]').click()
        self.page.locator("#config-scope").select_option("test:GroupMessage:100")
        expect(self.page.locator("#scope-edit-label")).to_be_visible()

    def test_local_save_and_restore_never_write_global_files(self):
        self.select_local()
        self.assertTrue(self.page.locator("#tab-settings").evaluate("node => node.inert"))
        self.page.locator("#scope-edit").check()
        self.page.get_by_label("世界名称").fill("Local World")
        self.page.locator("#save").click()
        expect(self.page.locator("#save-state")).to_contain_text("会话配置已保存")
        writes = self.page.evaluate("scopeWrites")
        self.assertEqual([row["endpoint"] for row in writes], ["config/scope"])
        self.assertEqual(writes[0]["data"]["blocks"], ["settings"])
        self.page.get_by_label("世界名称").fill("Unsaved rename")
        self.page.get_by_label("世界名称").press("Tab")
        self.page.locator("#config-scope").select_option("test:GroupMessage:200")
        expect(self.page.locator("#dialog-title")).to_have_text("切换编辑范围")
        self.page.locator("#dialog-ok").click()
        expect(self.page.locator("#config-scope")).to_have_value("test:GroupMessage:200")
        self.assertNotEqual(self.page.get_by_label("世界名称").input_value(), "Local World")
        self.page.locator("#config-scope").select_option("test:GroupMessage:100")
        expect(self.page.get_by_label("世界名称")).to_have_value("Local World")
        self.page.locator("#scope-edit").uncheck()
        self.page.locator("#dialog-ok").click()
        self.page.locator("#save").click()
        expect(self.page.locator("#save-state")).to_contain_text("会话配置已保存")
        self.assertEqual(self.page.evaluate("scopeWrites.at(-1).data.blocks"), [])
        self.assertEqual(self.errors, [])

    def test_unsaved_switch_can_cancel_or_discard_and_new_blocks_start_inherited(self):
        self.select_local("actions")
        self.page.locator("#scope-edit").check()
        self.page.locator("#config-scope").select_option("")
        expect(self.page.locator("#dialog-title")).to_have_text("切换编辑范围")
        self.page.locator("#dialog-cancel").click()
        expect(self.page.locator("#config-scope")).to_have_value("test:GroupMessage:100")
        self.page.locator('button[data-tab="schedules"]').click()
        expect(self.page.locator("#scope-edit")).not_to_be_checked()
        self.page.locator("#config-scope").select_option("")
        self.page.locator("#dialog-ok").click()
        expect(self.page.locator("#config-scope")).to_have_value("")
        self.assertEqual(self.page.evaluate("scopeWrites"), [])
        self.assertEqual(self.errors, [])

    def test_global_pause_is_immediate_and_mobile_bar_fits(self):
        # 「编辑范围 / 自主搭话」常驻顶栏（地图页也一样，不再单占一行），
        # 这里顺带检查顶栏在移动端不会横向溢出。
        self.select_local("settings")
        self.page.locator("#proactive-enabled").uncheck()
        expect(self.page.locator("#proactive-state")).to_have_text("已暂停")
        self.assertEqual(self.page.evaluate("scopeWrites.at(-1).endpoint"), "config/proactive")
        self.assertFalse(self.page.evaluate("scopeWrites.at(-1).data.enabled"))
        self.host.screenshot(path=str(self.shots / "scope-desktop.png"))
        self.host.set_viewport_size({"width": 390, "height": 844})
        expect(self.page.locator("#proactive-state")).to_have_text("已暂停")
        self.host.screenshot(path=str(self.shots / "scope-mobile.png"))
        self.assertTrue(self.page.locator(".topbar").evaluate("node => node.scrollWidth <= node.clientWidth"))
        self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
        self.assertEqual(self.errors, [])
