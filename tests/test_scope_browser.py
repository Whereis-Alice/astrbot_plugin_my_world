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
  const fixed = ['admin_ids', 'proactive_chat_paused', 'proactive_session_whitelist', 'life_share_session_whitelist', 'wizard_done', 'schema_version'];
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
        {session_id: 'test:PrivateMessage:300', note: '私聊主人', enabled: true},
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
        if (data.whitelist) config.world.proactive_session_whitelist = data.whitelist;
        if (data.life_share_whitelist) config.world.life_share_session_whitelist = data.life_share_whitelist;
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
    def test_global_whitelist_has_one_immediate_entry_and_cannot_be_scoped(self):
        self.page.locator('button[data-tab="settings"]').click()
        self.page.locator('#settings-tabs').get_by_text('自主发言', exact=True).click()
        allow = self.page.get_by_label('允许主动发言：Test A', exact=True)
        expect(allow).not_to_be_checked()
        allow.check()
        self.page.get_by_role('button', name='应用全局白名单', exact=True).click()
        expect(self.page.locator('.proactive-access-foot')).to_contain_text('已生效 · 主动发言 1 处 · 可分享生活 0 处')
        saved = self.page.evaluate('scopeWrites.at(-1)')
        self.assertEqual(saved['endpoint'], 'config/proactive')
        self.assertEqual(saved['data']['whitelist'], ['test:GroupMessage:100'])
        self.assertEqual(saved['data']['life_share_whitelist'], [])
        self.page.get_by_label('允许主动发言：私聊主人', exact=True).check()
        self.page.get_by_label('允许生活分享：私聊主人', exact=True).check()
        self.page.get_by_role('button', name='应用全局白名单', exact=True).click()
        expect(self.page.locator('.proactive-access-foot')).to_contain_text('可分享生活 1 处')
        saved = self.page.evaluate('scopeWrites.at(-1).data')
        self.assertEqual(saved['life_share_whitelist'], ['test:PrivateMessage:300'])
        self.assertEqual(saved['whitelist'], ['test:GroupMessage:100', 'test:PrivateMessage:300'])
        self.host.screenshot(path=str(self.shots/'life-share-whitelist-desktop.png'))
        self.host.set_viewport_size({'width':390,'height':844})
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'))
        self.host.screenshot(path=str(self.shots/'life-share-whitelist-mobile.png'))
        self.host.set_viewport_size({'width':1440,'height':1000})
        self.page.locator('#config-scope').select_option('test:GroupMessage:200')
        expect(self.page.get_by_role('button', name='应用全局白名单', exact=True)).to_be_disabled()
        expect(self.page.get_by_label('允许生活分享：私聊主人', exact=True)).to_be_disabled()
        expect(self.page.get_by_label('允许生活分享：私聊主人', exact=True)).to_be_checked()
        expect(self.page.locator('.proactive-access-note')).to_contain_text('独立配置不能扩大名单')
        self.assertEqual(self.errors, [])
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
        self.page.locator('button[data-tab="life"]').click()
        self.page.locator('[data-life-view="today"][role="tab"]').click()
        self.page.get_by_role('button', name='编辑日程 ↗', exact=True).click()
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
