"""Relationship cards, explicit apply semantics and the redesigned dossier gallery."""

import copy
import json
import unittest

from core.defaults import default_world
from core.models import parse_world
from tests import test_generation_browser as generation
from tests.test_contacts_browser import CONTACTS_BRIDGE
from tests.test_generation_browser import expect, sync_playwright
from tools.render_screenshots import BRIDGE_JS, build_payload


SOCIAL_BRIDGE = r"""
(() => {
  const original = window.AstrBotPluginPage;
  window.socialPeople = __SOCIAL_PEOPLE__;
  window.socialRules = __SOCIAL_RULES__;
  window.socialWrites = [];
  window.socialReads = [];
  window.socialCatalogReads = [];
  window.socialPending = [];
  window.socialDelay = false;
  window.socialFailure = false;
  const clone = structuredClone;
  const rules = () => ({...clone(socialRules), default_bond: socialRules.default_bond});
  const person = uid => clone(socialPeople.find(item => item.user_id === uid) || {user_id: uid, name: uid, baseline: null, sessions: []});
  window.AstrBotPluginPage = {...original,
    apiGet: async (endpoint, params = {}) => {
      if (endpoint === 'relationships') {
        socialCatalogReads.push(clone(params));
        const query = String(params.q || '');
        const people = socialPeople.filter(item => (!query || [item.name, item.user_id].some(x => x.includes(query)))
          && (params.filter !== 'configured' || item.baseline)
          && (params.filter !== 'unconfigured' || !item.baseline));
        const average = item => item.sessions.length ? item.sessions.reduce((sum, s) => sum + s.affinity, 0) / item.sessions.length : item.baseline?.affinity || 0;
        people.sort((a, b) => {
          const key = item => ({affinity: -average(item), affinity_asc: average(item), sessions: -item.sessions.length,
            name: item.name, recent: -Math.max(0, ...item.sessions.map(s => s.last_seen_at || 0))})[params.sort || 'recent'];
          const x = key(a), y = key(b);
          return (x < y ? -1 : x > y ? 1 : 0) || a.user_id.localeCompare(b.user_id);
        });
        return {people: clone(people), page: 1, pages: 1, total: people.length,
          people_total: socialPeople.length, baselines_total: socialPeople.filter(x => x.baseline).length,
          sessions_total: 2, rules: rules()};
      }
      if (endpoint === 'relationships/person') {
        socialReads.push(clone(params));
        const resolve = () => ({...person(params.user_id), rules: rules()});
        if (socialFailure) throw Error('<img src=x onerror=alert(1)>');
        if (socialDelay) return new Promise(done => socialPending.push({params, resolve: () => done(resolve())}));
        return resolve();
      }
      if (endpoint === 'relationships/rules') return {profile: clone(socialRules), fingerprint: 'initial', actions: [
        {id: 'selfie', name: '自拍'}, {id: 'leg_photo', name: '拍腿的照片'}, {id: 'hug', name: '抱抱'}]};
      if (endpoint === 'profile/contacts') {
        const result = await original.apiGet(endpoint, params);
        result.people = result.people.slice(0, 8).map((item, i) => ({...item,
          name: socialPeople[i].name, digest: ['喜欢在雨天听歌，最近正在练习烘焙。', '一起聊过许多关于星空的小事。', '习惯把计划写下来，偶尔会来分享新发现。'][i % 3],
          profile_preview: [{name: '喜欢', value: ['热茶与旧书', '深夜电台', '猫与散步'][i % 3]},
            {name: '最近', value: ['在学吉他', '准备一次旅行', '种了一盆薄荷'][i % 3]}],
        }));
        return result;
      }
      return original.apiGet(endpoint, params);
    },
    apiPost: async (endpoint, params = {}) => {
      if (endpoint.startsWith('relationships')) {
        socialWrites.push({endpoint, params: clone(params)});
        if (endpoint === 'relationships/rules') {window.socialRules = clone(params.profile); return {ok:true};}
        let current = socialPeople.find(item => item.user_id === params.user_id);
        if (!current) {
          current = {user_id: params.user_id, name: params.user_id, baseline: null, sessions: []}; socialPeople.push(current);
        }
        if (endpoint === 'relationships/baseline') current.baseline = null;
        else if (endpoint === 'relationships/session') {
          Object.assign(current.sessions.find(item => item.session === params.session), {affinity: params.affinity, relations: [...params.relations]});
        } else {
          current.name = params.name || current.name;
          current.baseline = {name: params.name, affinity: params.affinity, relations: [...params.relations], revision: (current.baseline?.revision || 0)+1, updated_at: 1792368000};
          const existing = new Map(current.sessions.map(item => [item.session, item]));
          current.sessions = ['aiocqhttp:GroupMessage:1001', 'aiocqhttp:GroupMessage:2002'].map((sid, i) => ({
            session: sid, label: i ? '朋友的小群' : '日常聊天', level: '朋友', configured: true,
            ...existing.get(sid), affinity: params.affinity, relations: [...params.relations], rules: rules()}));
        }
        return {ok:true, applied_sessions: 2, person: clone(current)};
      }
      return original.apiPost(endpoint, params);
    },
  };
})();
"""


def social_people():
    rows = []
    profile = parse_world(default_world())[0].profile.model_dump(mode="json")
    for index, name in enumerate(["林间", "小满", "南星", "遥", "秋山", "知夏", "小北", "枫岚"]):
        baseline = {"name": name, "affinity": [42, 65, 30, 0][index % 4],
                    "relations": ["主人", "朋友"] if index == 0 else ["朋友"],
                    "revision": 1, "updated_at": 1792368000} if index % 3 != 2 else None
        value = baseline["affinity"] if baseline else 12
        rows.append({"user_id": str(10001 + index), "name": name, "baseline": baseline,
                     "sessions": [{"session": sid, "label": label, "affinity": value + (2 if i else 0),
                         "relations": copy.deepcopy(baseline["relations"] if baseline else ["群友"]),
                         "level": "朋友", "configured": True, "rules": profile}
                         for i, (sid, label) in enumerate([("aiocqhttp:GroupMessage:1001", "日常聊天"),
                                                          ("aiocqhttp:GroupMessage:2002", "朋友的小群")])]})
    return rows


@unittest.skipIf(sync_playwright is None, "Install playwright to run browser tests")
class RelationshipsBrowserTests(unittest.TestCase):
    setUpClass = classmethod(generation.GenerationBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(generation.GenerationBrowserTests.tearDownClass.__func__)

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1600, "height": 1000})
        self.addCleanup(self.context.close)
        payload = build_payload()
        payload["config"]["sessions"]["sessions"] = [
            {"session_id": "aiocqhttp:GroupMessage:1001", "enabled": True, "note": "日常聊天"},
            {"session_id": "aiocqhttp:GroupMessage:2002", "enabled": True, "note": "朋友的小群"}]
        script = BRIDGE_JS.replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False))
        social = SOCIAL_BRIDGE.replace("__SOCIAL_PEOPLE__", json.dumps(social_people(), ensure_ascii=False))
        social = social.replace("__SOCIAL_RULES__", json.dumps(parse_world(default_world())[0].profile.model_dump(mode="json"), ensure_ascii=False))
        self.context.add_init_script(script=script + CONTACTS_BRIDGE + social)
        self.host = self.context.new_page()
        self.errors = []
        self.host.on("pageerror", lambda error: self.errors.append(str(error)))
        self.host.route("**/harness.html", lambda route: route.fulfill(content_type="text/html",
            body='<iframe name="editor" src="./index.html" style="position:fixed;inset:0;width:100%;height:100%;border:0"></iframe>'))
        self.host.goto(self.url.replace("index.html", "harness.html"))
        self.page = self.host.frame(name="editor")
        expect(self.page.locator("#app")).to_be_visible()
        self.page.locator('button[data-tab="relationships"]').click()
        expect(self.page.locator(".social-card")).to_have_count(8)

    def test_card_wall_reserves_the_view_for_people_and_search_is_read_only(self):
        self.assertEqual(self.page.evaluate("socialWrites"), [])
        grid = self.page.locator("#relationships-grid").bounding_box()
        self.assertGreater(grid["height"], 650)
        card = self.page.locator(".social-card").first
        self.assertGreaterEqual(card.bounding_box()["height"], 300)
        for selector in (".people-avatar", ".people-card-name", ".social-score", ".people-card-foot"):
            child = card.locator(selector).bounding_box()
            self.assertGreater(child["height"], 0)
            self.assertGreaterEqual(child["y"], card.bounding_box()["y"])
            self.assertLessEqual(child["y"] + child["height"], card.bounding_box()["y"] + card.bounding_box()["height"])
        self.host.screenshot(path=str(self.shots / "social-desktop.png"))
        self.page.locator("#relationships-search").fill("10001")
        expect(self.page.locator(".social-card")).to_have_count(1)
        expect(self.page.locator(".social-card")).to_contain_text("统一起点好感")
        self.assertEqual(self.page.evaluate("socialWrites"), [])
        self.assertEqual(self.errors, [])

    def test_unified_editor_submits_qq_affinity_and_coexisting_identities_together(self):
        self.page.locator('[data-qq="10001"]').click()
        editor = self.page.locator(".social-start-section")
        editor.get_by_label("统一起点好感", exact=True).fill("51")
        self.assertEqual(self.page.evaluate("socialWrites.length"), 0)
        editor.get_by_role("button", name="保存并应用到所有会话", exact=True).click()
        self.page.wait_for_function("socialWrites.length === 1")
        posted = self.page.evaluate("socialWrites[0]")
        self.assertEqual(posted["endpoint"], "relationships/apply")
        self.assertEqual(posted["params"]["user_id"], "10001")
        self.assertEqual(posted["params"]["affinity"], 51)
        self.assertCountEqual(posted["params"]["relations"], ["主人", "朋友"])
        self.assertEqual(posted["params"]["revision"], 1)
        self.assertEqual(posted["params"]["config_scope"], "")
        expect(self.page.locator(".social-scope-score")).to_have_text(["51", "51"])
        self.host.screenshot(path=str(self.shots / "social-detail-desktop.png"))
        self.assertEqual(self.errors, [])

    def test_affinity_preview_explains_caps_and_accepts_precise_values(self):
        self.page.locator('#relationships-add').click()
        self.page.get_by_label('QQ 号', exact=True).fill('987654321')
        self.page.get_by_role('button', name='继续设置 →', exact=True).click()
        editor = self.page.locator('.social-start-section')
        number = editor.get_by_label('统一起点好感', exact=True)
        preview = editor.locator('.social-distance-preview')
        reason = editor.locator('.social-distance-reason')
        number.fill('31.15')
        expect(preview).to_have_text('预计相处档位 · 客气')
        expect(reason).to_contain_text('好感对应「朋友」')
        expect(reason).to_contain_text('默认关系「陌生人」上限')
        editor.get_by_role('button', name='朋友', exact=True).click()
        expect(preview).to_have_text('预计相处档位 · 朋友')
        slider = editor.get_by_label('拖动调整好感')
        slider.focus()
        slider.press('End')
        expect(number).to_have_value('100')
        expect(reason).to_contain_text('关系「朋友」上限')
        slider.press('Home')
        expect(preview).to_have_text('预计相处档位 · 敌意')
        number.fill('')
        expect(preview).to_have_text('等待有效好感值')
        number.fill('31.15')
        self.host.screenshot(path=str(self.shots / 'affinity-distance-explanation.png'))
        editor.get_by_role('button', name='保存并应用到所有会话', exact=True).click()
        self.page.wait_for_function('socialWrites.length === 1')
        self.assertEqual(self.page.evaluate('socialWrites[0].params.affinity'), 31.15)
        self.assertEqual(self.errors, [])

    def test_distance_preview_matches_backend_for_boundaries_and_identity_limits(self):
        profile = parse_world(default_world())[0].profile
        cases = []
        for crossing in (False, True):
            config = profile.model_copy(deep=True)
            config.bond_by_name('朋友').allow_boundary_crossing = crossing
            for names in ([], ['陌生人'], ['朋友'], ['主人'], ['朋友', '主人'], ['朋友', '讨厌的人'], ['敌人']):
                for value in (-100, -40, -10, 0, 9.9, 10, 30, 31.15, 55, 80, 100):
                    expected = config.level_for(value, cap=config.bond_cap(names), floor=config.bond_floor(names)).name
                    cases.append({'rules': config.model_dump(mode='json'), 'names': names, 'value': value, 'expected': expected})
        results = self.page.evaluate('''async cases => {
          const {distancePreview} = await import('./people-ui.js');
          return cases.map(c => distancePreview(c.rules, c.value, c.names).name);
        }''', cases)
        self.assertEqual(results, [c['expected'] for c in cases])

    def test_sort_menu_orders_current_affinity_and_restores_preference(self):
        menu = self.page.locator('#relationships-sort')
        menu.locator('summary').click()
        menu.get_by_role('radio', name='好感 · 从高到低', exact=True).check()
        expect(menu).not_to_have_attribute('open', '')
        expect(self.page.locator('.social-card').first).to_have_attribute('data-qq', '10002')
        expect(self.page.locator('.social-card').first.locator('.social-sort-value')).to_have_text('平均好感 · 66')
        menu.locator('summary').click()
        menu.get_by_role('radio', name='好感 · 从低到高', exact=True).check()
        expect(self.page.locator('.social-card').first).to_have_attribute('data-qq', '10004')
        self.assertEqual(self.page.evaluate('socialCatalogReads.at(-1).sort'), 'affinity_asc')
        self.assertEqual(self.page.evaluate('socialCatalogReads.at(-1).page'), 1)
        menu.locator('summary').click()
        self.host.screenshot(path=str(self.shots / 'relationships-sort-desktop.png'))
        self.host.reload()
        self.page = self.host.frame(name='editor')
        menu = self.page.locator('#relationships-sort')
        self.page.locator('#tabs [data-tab="relationships"]').click()
        expect(self.page.locator('.social-card').first).to_have_attribute('data-qq', '10004')
        menu.locator('summary').click()
        expect(menu.get_by_role('radio', name='好感 · 从低到高', exact=True)).to_be_checked()
        self.host.set_viewport_size({'width': 390, 'height': 844})
        bounds = menu.locator('.people-sort-options').bounding_box()
        self.assertGreaterEqual(bounds['x'], 0)
        self.assertLessEqual(bounds['x'] + bounds['width'], 390)
        self.assertGreater(bounds['y'], menu.locator('summary').bounding_box()['y'])
        self.assertTrue(menu.locator('.people-sort-options').evaluate('''panel => {
          const b = panel.getBoundingClientRect();
          return panel.contains(document.elementFromPoint(b.right - 35, b.top + 25));
        }'''))
        self.host.screenshot(path=str(self.shots / 'relationships-sort-mobile.png'))
        self.assertEqual(self.page.evaluate('socialWrites'), [])
        self.assertEqual(self.errors, [])

    def test_contacts_sort_by_affinity_messages_and_profile(self):
        self.page.evaluate('''() => {
          const original = AstrBotPluginPage.apiGet;
          AstrBotPluginPage.apiGet = async (url, params) => {
            const result = await original(url, params);
            if (url !== 'profile/contacts') return result;
            return {...result, people: result.people.slice(0, 4).map((item, i) => ({...item,
              affinity: [10, -10, 50, 10][i], message_count: [1, 4, 2, 3][i],
              weaver: {...item.weaver, completeness: [10, 80, 30, 60][i]}}))};
          };
        }''')
        self.page.locator('#tabs [data-tab="contacts"]').click()
        expect(self.page.locator('.contact-card')).to_have_count(4)
        menu = self.page.locator('#contacts-sort')
        for label, uid in [('好感 · 从高到低', '10003'), ('好感 · 从低到高', '10002'),
                           ('交谈次数 · 从多到少', '10002'), ('画像完整度 · 从高到低', '10002')]:
            menu.locator('summary').click()
            menu.get_by_role('radio', name=label, exact=True).check()
            expect(self.page.locator('.contact-card').first).to_have_attribute('data-user', uid)
            expect(menu).not_to_have_attribute('open', '')
        menu.locator('summary').click()
        self.host.screenshot(path=str(self.shots / 'contacts-sort-desktop.png'))
        self.assertEqual(self.page.evaluate('contactWrites'), [])
        self.assertEqual(self.errors, [])

    def test_local_editor_does_not_change_starting_point_or_another_conversation(self):
        self.page.locator('[data-qq="10001"]').click()
        card = self.page.locator(".social-scope-card").last
        card.locator("summary").first.click()
        card.get_by_label("此会话好感", exact=True).fill("-12")
        card.get_by_role("button", name="保存此会话", exact=True).click()
        self.page.wait_for_function("socialWrites.length === 1")
        posted = self.page.evaluate("socialWrites[0]")
        self.assertEqual(posted["endpoint"], "relationships/session")
        self.assertEqual(posted["params"]["session"], "aiocqhttp:GroupMessage:2002")
        self.assertEqual(self.page.evaluate("socialPeople[0].baseline.affinity"), 42)
        self.assertEqual(self.page.evaluate("socialPeople[0].sessions.map(x=>x.affinity)"), [42, -12])
        self.assertEqual(self.errors, [])

    def test_unknown_person_can_be_configured_before_first_contact(self):
        self.page.locator("#relationships-add").click()
        self.page.get_by_label("QQ 号", exact=True).fill("987654321")
        self.page.get_by_role("button", name="继续设置 →", exact=True).click()
        editor = self.page.locator(".social-start-section")
        editor.get_by_label("统一起点好感", exact=True).fill("20")
        editor.get_by_role("button", name="主人", exact=True).click()
        editor.get_by_role("button", name="保存并应用到所有会话", exact=True).click()
        self.page.wait_for_function("socialWrites.length === 1")
        self.assertEqual(self.page.evaluate("socialWrites[0].params.user_id"), "987654321")
        self.assertEqual(self.page.evaluate("socialWrites[0].params.relations"), ["主人"])
        self.assertEqual(self.errors, [])

    def test_outdated_details_cannot_repaint_the_new_selection(self):
        self.page.evaluate("socialDelay = true")
        self.page.evaluate("document.querySelector('[data-qq=\"10001\"]').click()")
        self.page.evaluate("document.querySelector('[data-qq=\"10002\"]').click()")
        self.page.wait_for_function("socialPending.length === 2")
        self.page.evaluate("socialPending[1].resolve()")
        expect(self.page.locator("#relationship-title")).to_have_text("小满")
        self.page.evaluate("socialPending[0].resolve()")
        expect(self.page.locator("#relationship-title")).to_have_text("小满")
        self.assertEqual(self.errors, [])

    def test_evidence_and_temporary_feelings_remain_visible_without_html_execution(self):
        self.page.evaluate("""() => {
          const get = AstrBotPluginPage.apiGet;
          AstrBotPluginPage.apiGet = async (endpoint, params) => {
            const data = await get(endpoint, params);
            if (endpoint === 'profile/contact') data.relationship.trend = {
              label: '近期相处较融洽', count: 4,
              explanation: '近14天的直接交谈观察，不改变好感、身份或权限。',
              evidence: [{at: Date.now()/1000, reason: '<img src=x onerror=alert(1)> 认真听她讲完了一件自己的事'}]
            };
            return data;
          };
        }""")
        self.page.locator('[data-qq="10001"]').click()
        card = self.page.locator(".social-scope-card").first
        card.locator("summary").first.click()
        card.get_by_role("button", name="查看关系依据与变化记录", exact=True).click()
        overview = card.locator(".contact-relationship-overview")
        expect(overview).to_contain_text("4 个不同日期")
        expect(overview).to_contain_text("暂时比平常疏远")
        history = card.locator(".contact-relationship-history")
        expect(history).to_contain_text("<img src=x")
        self.assertEqual(history.locator("img").count(), 0)
        trend = overview.locator('.contact-trend')
        trend.locator('summary').click()
        expect(trend).to_contain_text('近期相处较融洽 · 4 条依据')
        expect(trend).to_contain_text('不改变好感、身份或权限')
        expect(trend).to_contain_text('<img src=x')
        self.assertEqual(trend.locator('img').count(), 0)
        expect(card.get_by_label("她还在意的事")).to_be_visible()
        self.assertEqual(self.page.evaluate("socialWrites"), [])
        for width, height in [(1600, 1000), (390, 844)]:
            self.host.set_viewport_size({'width': width, 'height': height})
            trend.scroll_into_view_if_needed()
            self.assertTrue(trend.evaluate('node => node.scrollWidth <= node.clientWidth + 1'))
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'))
            self.host.screenshot(path=str(self.shots / f'relationship-trend-{width}.png'))
        self.assertEqual(self.errors, [])

    def test_saving_one_form_warns_before_discarding_another_form_draft(self):
        self.page.locator('[data-qq="10001"]').click()
        self.page.get_by_label("统一起点好感", exact=True).fill("55")
        card = self.page.locator(".social-scope-card").first
        card.locator("summary").first.click()
        card.get_by_label("此会话好感", exact=True).fill("33")
        card.get_by_role("button", name="保存此会话", exact=True).click()
        expect(self.page.locator("#dialog-title")).to_have_text("保存并放弃其他未保存修改？")
        self.page.locator("#dialog-cancel").click()
        self.assertEqual(self.page.evaluate("socialWrites"), [])
        expect(self.page.get_by_label("统一起点好感", exact=True)).to_have_value("55")
        card.get_by_role("button", name="保存此会话", exact=True).click()
        self.page.locator("#dialog-ok").click()
        self.page.wait_for_function("socialWrites.length === 1")
        self.assertEqual(self.page.evaluate("socialWrites[0].endpoint"), "relationships/session")
        self.assertEqual(self.errors, [])

    def test_rules_templates_default_to_manual_and_save_only_when_applied(self):
        self.page.locator(".people-management > summary").click()
        self.page.locator("#relationships-rules").click()
        self.page.get_by_text("从身份模板添加", exact=True).click()
        self.page.get_by_role("button", name="同学 ＋", exact=True).click()
        card = self.page.locator(".rule-card").filter(has=self.page.locator("summary strong", has_text="同学"))
        card.locator("summary").click()
        expect(card.get_by_label("允许自然形成", exact=True)).not_to_be_checked()
        expect(card.get_by_label("允许越过通常上限", exact=True)).not_to_be_checked()
        self.assertEqual(self.page.evaluate("socialWrites.length"), 0)
        self.page.locator("#dialog-ok").click()
        self.page.wait_for_function("socialWrites.length === 1")
        added = self.page.evaluate("socialWrites[0].params.profile.bonds.find(x=>x.name==='同学')")
        self.assertFalse(added["auto_form"])
        self.assertEqual(added["group"], "")
        self.assertEqual(self.errors, [])

    def test_gallery_dossier_and_mobile_relationships_have_no_horizontal_overflow(self):
        self.page.locator('button[data-tab="contacts"]').click()
        expect(self.page.locator(".contact-card")).to_have_count(8)
        expect(self.page.locator("#contact-drawer")).to_be_hidden()
        self.host.screenshot(path=str(self.shots / "people-gallery-desktop.png"))
        self.page.locator('[data-user="10001"]').click()
        expect(self.page.locator(".weaver-field-row")).to_have_count(27)
        expect(self.page.locator("#contacts-open-relationship")).to_be_visible()
        self.assertEqual(self.page.locator("#contact-tab-relation").count(), 0)
        self.host.screenshot(path=str(self.shots / "people-dossier-desktop.png"))
        self.page.locator("#contacts-back").click()
        for width, theme in [(390, "dark"), (390, "light"), (768, "light")]:
            self.host.set_viewport_size({"width": width, "height": 844})
            self.page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
            self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
            self.host.screenshot(path=str(self.shots / f'people-gallery-{width}-{theme}.png'))
            self.page.locator('button[data-tab="relationships"]').click()
            self.host.screenshot(path=str(self.shots / f'social-{width}-{theme}.png'))
            self.page.locator('[data-qq="10001"]').click()
            expect(self.page.get_by_label("统一起点好感", exact=True)).to_be_visible()
            self.assertTrue(self.page.locator("#relationship-drawer").evaluate("node=>node.scrollWidth <= node.clientWidth + 1"))
            self.host.screenshot(path=str(self.shots / f'social-detail-{width}-{theme}.png'))
            self.page.locator("#relationship-close").click()
            self.page.locator('button[data-tab="contacts"]').click()
        self.assertEqual(self.errors, [])
