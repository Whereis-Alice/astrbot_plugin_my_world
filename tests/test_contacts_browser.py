"""Real browser regressions for address-book layout and asynchronous selection."""

import json
import unittest

from tests import test_generation_browser as generation
from tests.test_generation_browser import expect, sync_playwright
from tools.render_screenshots import BRIDGE_JS, build_payload


CONTACTS_BRIDGE = r"""
(() => {
  const original = window.AstrBotPluginPage;
  const clone = structuredClone;
  window.contactPending = [];
  window.delayContacts = false;
  window.failContact = false;
  window.contactCalls = [];
  window.contactWrites = [];
  window.contactHistory = [];
  const detail = async (params) => {
    const data = await original.apiGet('profile/contact', params);
    data.user_id = params.user_id;
    data.identity.display_name = `朋友 ${params.user_id}`;
    data.person.name = data.identity.display_name;
    data.person.level = {...data.person.level, name: '信任'};
    data.relationship = {base_level: '亲密', effective_level: '信任', affinity_min: -100, affinity_max: 100,
      reasons: ['她还在生气，暂时比平常疏远；关系本身没有解除'],
      progress: {enabled: true, talk_days: 4, next: {bond: '朋友', min_talk_days: 5,
        min_affinity: 15, days_left: 1, affinity_left: 2}}};
    data.miss = {value: 0.9, reason: '自主搭话已暂停', will_reach_out: false};
    data.bonds = [{id: 1, type: '朋友', status: 'current', since: 1792230000,
      evidence: '<img src=x onerror=alert(1)> 是一段原话', asserted_by: '管理员设置'}];
    data.person.digest = '一起看过电影，也记得上次约定的事。';
    data.weaver.fields = Array.from({length: 27}, (_, index) => ({
      name: `资料 ${index}`, value: `喜欢安静的地方和热茶 ${params.user_id}，会把聊天里的小事记在心里。`,
      updated_at: '2026-10-18 08:00', evidence: '本人的明确自述', locked: index === 0,
      record_key: index === 0 ? params.user_id : `${params.session}_${params.user_id}`, shared: index === 0,
    }));
    data.facts = Array.from({length: 42}, (_, index) => ({
      id: index + 1, kind: '近况', text: `共同经历 ${index}：约好周末去散步`,
      evidence: '周末一起去散步吧', status: 'active', last_confirmed_at: 1792230000,
    }));
    data.model_context = '当前说话人的资料预览';
    return data;
  };
  window.AstrBotPluginPage = {...original,
    apiPost: async (endpoint, params = {}) => {
      if (endpoint === 'profile/weaver/field') window.contactWrites.push(clone(params));
      return original.apiPost(endpoint, params);
    },
    apiGet: async (endpoint, params = {}) => {
      if (endpoint === 'profile/weaver/history') {
        window.contactHistory.push(clone(params));
        return {items: []};
      }
      if (endpoint === 'profile/contacts') {
        const data = await original.apiGet(endpoint, params);
        const first = data.people[0];
        data.people = Array.from({length: 66}, (_, i) => ({...clone(first),
          user_id: `${10001 + i}`, name: `朋友 ${10001 + i}`, last_seen_at: 1792230000 - i,
        }));
        return data;
      }
      if (endpoint === 'profile/contact') {
        window.contactCalls.push(clone(params));
        if (window.failContact) throw new Error('<img src=x onerror=alert(1)>');
        if (window.delayContacts) return new Promise(resolve =>
          window.contactPending.push({params, resolve: async () => resolve(await detail(params))}));
        return detail(params);
      }
      return original.apiGet(endpoint, params);
    },
  };
})();
"""


@unittest.skipIf(sync_playwright is None, "Install playwright to run browser tests")
class ContactsBrowserTests(unittest.TestCase):
    setUpClass = classmethod(generation.GenerationBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(generation.GenerationBrowserTests.tearDownClass.__func__)

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1827, "height": 870})
        self.addCleanup(self.context.close)
        payload = build_payload()
        payload["config"]["sessions"]["sessions"] = [
            {"session_id": "aiocqhttp:GroupMessage:1001", "enabled": True, "note": "日常聊天"},
            {"session_id": "aiocqhttp:GroupMessage:2002", "enabled": True},
            {"session_id": "aiocqhttp:GroupMessage:3003", "enabled": True},
            {"session_id": "aiocqhttp:FriendMessage:4004", "type": "private", "enabled": True},
        ]
        payload["config"]["session_names"] = {
            "aiocqhttp:GroupMessage:1001": "平台上的群名",
            "aiocqhttp:GroupMessage:2002": "另一个群",
        }
        script = BRIDGE_JS.replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False))
        self.context.add_init_script(script=script + CONTACTS_BRIDGE)
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
        self.page.locator('button[data-tab="contacts"]').click()
        expect(self.page.locator(".contact-card")).to_have_count(66)
        expect(self.page.locator("#contact-drawer")).to_be_hidden()
        if self.page.evaluate("document.documentElement.dataset.theme") != "dark":
            self.page.locator("#theme-toggle").click()
            self.page.locator('[data-appearance="nocturne"]').click()
            self.page.get_by_label("关闭外观设置").click()

    def open_person(self, uid="10001"):
        self.page.locator(f'[data-user="{uid}"]').click()
        expect(self.page.locator(".weaver-field-row")).to_have_count(27)

    def test_session_picker_shows_names_and_numbers_while_preserving_identity(self):
        picker = self.page.locator("#contacts-session")
        self.assertEqual(picker.locator("option").all_text_contents(), [
            "日常聊天 · 1001", "另一个群 · 2002", "群聊 · 3003", "私聊 · 4004",
        ])
        picker.select_option("aiocqhttp:GroupMessage:2002")
        expect(picker).to_have_attribute("title", "另一个群 · 2002")
        self.open_person()
        self.assertEqual(self.page.evaluate("contactCalls.at(-1).session"), "aiocqhttp:GroupMessage:2002")
        self.page.locator("#contacts-back").click()
        self.host.set_viewport_size({"width": 390, "height": 844})
        self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
        self.assertEqual(self.errors, [])

    def test_card_gallery_and_fixed_dossier_headers_at_short_height(self):
        for height in (870, 640):
            self.host.set_viewport_size({"width": 1827, "height": height})
            self.assertGreaterEqual(self.page.locator(".contact-card").first.bounding_box()["height"], 258)
            self.assertTrue(self.page.locator("#contacts-list").evaluate("node => node.clientHeight > 200"))
            self.open_person()
            tabs = self.page.locator("#contacts-detail-tabs")
            before = tabs.bounding_box()
            self.page.locator("#contacts-detail").evaluate("node => node.scrollTop = node.scrollHeight")
            after = tabs.bounding_box()
            self.assertEqual([after[k] for k in ("y", "height", "width")], [before[k] for k in ("y", "height", "width")])
            self.assertTrue(self.page.evaluate("document.documentElement.scrollHeight <= innerHeight + 1"))
            self.page.locator("#contacts-back").click()
            expect(self.page.locator('[data-user="10001"]')).to_be_focused()
        self.host.set_viewport_size({"width": 1827, "height": 870})
        self.host.screenshot(path=str(self.shots / "contacts-desktop.png"))
        self.assertEqual(self.errors, [])

    def test_directory_reads_only_on_demand_and_dossier_traps_focus(self):
        self.assertEqual(self.page.evaluate("contactCalls"), [])
        self.page.locator("#contacts-search").fill("10002")
        expect(self.page.locator(".contact-card")).to_have_count(1)
        self.assertEqual(self.page.evaluate("contactCalls"), [])
        self.open_person("10002")
        expect(self.page.locator("#contacts-open-relationship")).to_be_visible()
        self.assertEqual(self.page.locator("#contact-tab-relation").count(), 0)
        self.page.locator("#contacts-back").focus()
        self.page.locator("#contacts-back").press("Shift+Tab")
        self.assertTrue(self.page.locator("#contact-drawer").evaluate("node => node.contains(document.activeElement)"))
        self.page.locator("#contact-drawer").press("Escape")
        expect(self.page.locator("#contact-drawer")).to_be_hidden()
        expect(self.page.locator('[data-user="10002"]')).to_be_focused()
        self.assertEqual(self.errors, [])

    def test_detailed_tiers_preserve_relationship_policy_and_remap_boundaries(self):
        result = self.page.evaluate('''async () => {
          const {applyDetailedLevels} = await import('./relationship-preset.js');
          const profile = {levels: [{min_affinity: -100, max_affinity: 0},
            {min_affinity: 0, max_affinity: 100}], bonds: [{name: '朋友', floor: 1, cap: 1,
              auto_form: false, allow_boundary_crossing: true}]};
          applyDetailedLevels(profile);
          return profile;
        }''')
        self.assertEqual(len(result['levels']), 10)
        self.assertEqual(result['bonds'][0]['floor'], 2)
        self.assertEqual(result['bonds'][0]['cap'], 9)
        self.assertFalse(result['bonds'][0]['auto_form'])
        self.assertTrue(result['bonds'][0]['allow_boundary_crossing'])

    def test_relationship_settings_expose_manual_default_and_apply_tier_draft(self):
        self.page.locator('button[data-tab="settings"]').click()
        self.page.locator('#settings-tabs').get_by_role('button', name='记忆与上下文').click()
        def fold(title):
            return self.page.locator('details.fold-editor').filter(
                has=self.page.locator('summary', has_text=title))
        bonds = fold('关系表')
        bonds.locator(':scope > summary').click()
        rows = bonds.locator('.list-row').filter(has=self.page.locator('input[placeholder="名称"]'))
        romance = next(row for row in rows.all() if row.locator('input').first.input_value() == '男友')
        expect(romance.get_by_label('自然形成', exact=True)).not_to_be_checked()
        expect(romance.get_by_label('允许越过上限', exact=True)).not_to_be_checked()
        romance.get_by_label('自然形成', exact=True).check()
        levels = fold('亲密度分级')
        levels.locator(':scope > summary').click()
        levels.get_by_role('button', name='使用十档相处方案').click()
        expect(self.page.locator('#dialog-hint')).to_contain_text('戒备 → 疏远 → 礼貌')
        self.page.locator('#dialog-ok').click()
        expect(fold('亲密度分级').locator('.fold-note')).to_contain_text('10 档')
        bonds = fold('关系表')
        bonds.locator(':scope > summary').click()
        rows = bonds.locator('.list-row').filter(has=self.page.locator('input[placeholder="名称"]'))
        romance = next(row for row in rows.all() if row.locator('input').first.input_value() == '男友')
        expect(romance.get_by_label('自然形成', exact=True)).to_be_checked()
        self.assertEqual(self.errors, [])

    def test_selection_and_session_responses_cannot_overwrite_newer_selection(self):
        self.page.evaluate("delayContacts = true")
        self.page.evaluate("document.querySelector('[data-user=\"10002\"]').click()")
        self.page.evaluate("document.querySelector('[data-user=\"10003\"]').click()")
        self.page.evaluate("contactPending[1].resolve()")
        expect(self.page.locator('#contacts-title')).to_have_text('朋友 10003')
        expect(self.page.locator('.weaver-field-row').first).to_contain_text('10003')
        self.page.evaluate("contactPending[0].resolve()")
        expect(self.page.locator('#contacts-subtitle')).to_contain_text('10003')
        self.page.evaluate("document.querySelector('[data-user=\"10004\"]').click()")
        self.page.locator('#contacts-back').click()
        self.page.locator('#contacts-session').select_option('aiocqhttp:GroupMessage:2002')
        expect(self.page.locator('#contact-drawer')).to_be_hidden()
        self.page.locator('[data-user="10001"]').click()
        self.page.wait_for_function('contactPending.length === 4')
        self.page.evaluate('contactPending[3].resolve()')
        expect(self.page.locator('#contacts-title')).to_have_text('朋友 10001')
        self.page.evaluate('contactPending[2].resolve()')
        expect(self.page.locator('#contacts-subtitle')).to_contain_text('10001')
        self.assertEqual(self.page.evaluate('contactCalls.at(-1).session'), 'aiocqhttp:GroupMessage:2002')
        self.assertEqual(self.errors, [])

    def test_failed_detail_is_text_and_retry_recovers(self):
        self.page.evaluate("failContact = true")
        self.page.locator('[data-user="10002"]').click()
        expect(self.page.locator('#contacts-detail')).to_contain_text('<img src=x')
        self.assertEqual(self.page.locator('#contacts-detail img').count(), 0)
        self.page.evaluate("failContact = false")
        self.page.locator('#contacts-detail').get_by_role('button', name='重试').click()
        expect(self.page.locator('.weaver-field-row')).to_have_count(27)
        self.assertEqual(self.errors, [])

    def test_shared_field_operations_target_the_record_being_displayed(self):
        self.open_person()
        row = self.page.locator('.weaver-field-row').first
        expect(row).to_contain_text('全局共享')
        row.get_by_role('button', name='解锁', exact=True).click()
        self.page.wait_for_function('contactWrites.length === 1')
        self.assertEqual(self.page.evaluate('contactWrites[0].key'), '10001')
        self.page.locator('.weaver-field-row').first.get_by_role('button', name='历史', exact=True).click()
        self.page.wait_for_function('contactHistory.length === 1')
        self.assertEqual(self.page.evaluate('contactHistory[0].key'), '10001')
        self.assertEqual(self.errors, [])

    def test_mobile_select_back_and_light_theme(self):
        self.host.set_viewport_size({"width": 390, "height": 844})
        self.page.locator('#theme-toggle').click()
        self.page.locator('[data-appearance="cloud"]').click()
        self.page.get_by_label("关闭外观设置").click()
        self.page.locator('[data-user="10002"]').click()
        expect(self.page.locator('#contacts-back')).to_be_visible()
        expect(self.page.locator('#contact-tab-profile')).to_be_visible()
        self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
        self.host.screenshot(path=str(self.shots / "contacts-mobile.png"))
        geometry = self.page.evaluate("""() => {
          const rect = selector => document.querySelector(selector).getBoundingClientRect().toJSON();
          return {topbar: rect('.topbar'), main: rect('main'), title: rect('#contacts-title'),
            tabs: rect('#contacts-detail-tabs'), body: rect('#contacts-detail'), height: innerHeight};
        }""")
        self.assertLessEqual(geometry['topbar']['bottom'], geometry['main']['top'] + 1)
        self.assertGreater(geometry['title']['width'], 120)
        self.assertLess(geometry['title']['height'], 55)
        self.assertLess(geometry['tabs']['bottom'], geometry['height'] - 140)
        self.assertGreater(geometry['body']['height'], 140)
        self.assertLessEqual(geometry['body']['bottom'], geometry['height'])
        expect(self.page.locator('[data-contact-page="profile"]')).to_be_visible()
        self.host.screenshot(path=str(self.shots / "contacts-mobile.png"))
        self.page.locator('#contacts-back').click()
        expect(self.page.locator('#contacts-list')).to_be_visible()
        self.assertEqual(self.errors, [])

    def test_about_person_folds_and_retains_preference_after_selection_and_edit(self):
        self.open_person()
        section = self.page.locator('.contact-weaver-section')
        section.locator(':scope > summary').click()
        expect(section).not_to_have_attribute('open', '')
        expect(self.page.locator('.weaver-field-row').first).to_be_hidden()
        self.page.locator('#contacts-back').click()
        self.open_person("10002")
        expect(self.page.locator('#contacts-title')).to_contain_text('10002')
        expect(section).not_to_have_attribute('open', '')
        count = self.page.evaluate('contactCalls.length')
        self.page.evaluate("document.querySelector('#contacts-refresh').click()")
        self.page.wait_for_function('count => contactCalls.length > count', arg=count)
        expect(section).not_to_have_attribute('open', '')
        section.locator('[data-act="weaver-add"]').click()
        expect(self.page.locator('#dialog')).to_be_visible()
        expect(section).not_to_have_attribute('open', '')
        self.page.locator('#dialog-cancel').click()
        section.locator(':scope > summary').click()
        expect(self.page.locator('.weaver-field-row').first).to_be_visible()
        self.assertEqual(self.errors, [])

    def test_rail_icons_do_not_move_when_activated(self):
        self.page.locator('#sidebar-toggle').click()
        button = self.page.locator('button[data-tab="contacts"]')
        def style():
            return button.locator('.nav-icon').bounding_box()
        before = style()
        self.page.locator('button[data-tab="status"]').click()
        self.assertEqual(style(), before)
        button.click()
        self.assertEqual(style(), before)
        self.assertEqual(before['width'], 18)
        self.host.screenshot(path=str(self.shots / "contacts-rail.png"))
        self.assertEqual(self.errors, [])

    def test_sidebar_manual_and_automatic_rails_share_one_centerline(self):
        def geometry(width, collapsed, expected_width):
            self.host.set_viewport_size({"width": width, "height": 900})
            is_collapsed = self.page.locator('#app').evaluate("node => node.classList.contains('side-rail')")
            if collapsed != is_collapsed:
                self.page.locator('#sidebar-toggle').click()
            self.assertEqual(self.page.locator('.sidebar').bounding_box()['width'], expected_width)
            icons = self.page.locator('#tabs .nav-icon').evaluate_all("nodes => nodes.map(n => {const b=n.getBoundingClientRect(); return [b.x+b.width/2,b.width,b.height]})")
            center = icons[0][0]
            for x, w, h in icons:
                self.assertAlmostEqual(x, center, delta=.1)
                self.assertEqual((w, h), (18, 18))
            logo = self.page.locator('.brand-logo').bounding_box()
            self.assertAlmostEqual(logo['x'] + logo['width']/2, center, delta=.1)
            widths = [self.page.locator(s).bounding_box()['width'] for s in ('.sidebar .brand', '.sidebar .tabs', '.sidebar-foot')]
            self.assertLess(max(widths) - min(widths), .1)
            for button in self.page.locator('#tabs button').all():
                self.assertEqual(button.bounding_box()['width'], widths[0])
            return self.page.locator('.sidebar .tabs').bounding_box()['width']
        geometry(1600, False, 208)
        manual = geometry(1600, True, 64)
        self.host.screenshot(path=str(self.shots / 'sidebar-manual-rail.png'))
        self.page.evaluate("localStorage.removeItem('mw-sidebar-rail')")
        self.host.set_viewport_size({"width": 1024, "height": 900})
        self.host.reload()
        self.page = self.host.frame(name='editor')
        expect(self.page.locator('#app')).to_have_class('app side-rail')
        self.assertEqual(manual, geometry(1024, True, 64))
        geometry(1024, False, 208)
        expect(self.page.locator('#sidebar-toggle')).to_have_attribute('aria-expanded', 'true')
        self.host.reload()
        self.page = self.host.frame(name='editor')
        expect(self.page.locator('.sidebar')).to_have_css('width', '208px')
        geometry(1600, False, 208)
        self.host.screenshot(path=str(self.shots / 'sidebar-expanded.png'))
        self.assertEqual(self.errors, [])
