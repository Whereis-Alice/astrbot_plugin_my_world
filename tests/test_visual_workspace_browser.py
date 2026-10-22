"""Space budgets, appearance persistence and every module's responsive shell."""
import unittest

from tests.test_generation_browser import expect, sync_playwright
from tests import test_relationships_browser as social
from tests import test_workbench_browser as workbench


@unittest.skipIf(sync_playwright is None, "Install playwright to run browser tests")
class VisualWorkspaceTests(unittest.TestCase):
    setUpClass = classmethod(social.RelationshipsBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(social.RelationshipsBrowserTests.tearDownClass.__func__)
    install_console = workbench.WorkbenchBrowserTests.install_console

    def setUp(self):
        social.RelationshipsBrowserTests.setUp(self)
        self.host.emulate_media(reduced_motion="reduce")

    def appearance(self, palette):
        self.page.locator("#theme-toggle").click()
        self.page.locator(f'[data-appearance="{palette}"]').click()
        self.page.get_by_label("关闭外观设置").click()

    def test_people_content_starts_near_top_and_pagination_uses_toolbar(self):
        self.host.set_viewport_size({"width": 1600, "height": 870})
        for tab, grid in [("contacts", "#contacts-list"), ("relationships", "#relationships-grid")]:
            self.page.locator(f'#tabs [data-tab="{tab}"]').click()
            expect(self.page.locator(grid).locator("button").first).to_be_visible()
            bounds = self.page.locator(grid).bounding_box()
            self.assertLess(bounds["y"], 130)
            self.assertGreater(bounds["height"], 720)
        self.assertEqual(self.page.locator(".people-toolbar #relationships-pagination").count(), 1)
        self.page.evaluate('''() => {
          const read = AstrBotPluginPage.apiGet;
          AstrBotPluginPage.apiGet = async (url, params) => {
            const result = await read(url, params);
            if (url === 'relationships') {
              const page = params.page || 1;
              return {...result, people: result.people.slice((page-1)*2, page*2), page, pages:4};
            }
            return result;
          };
        }''')
        self.page.locator('#tabs [data-tab="contacts"]').click()
        self.page.locator('#tabs [data-tab="relationships"]').click()
        expect(self.page.locator(".social-card")).to_have_count(2)
        expect(self.page.locator("#relationships-page")).to_have_text("1 / 4")
        self.page.get_by_label("下一页", exact=True).click()
        expect(self.page.locator("#relationships-page")).to_have_text("2 / 4")
        expect(self.page.locator(".social-card").first).to_have_attribute("data-qq", "10003")
        self.assertEqual(self.page.evaluate("socialWrites"), [])
        self.assertEqual(self.errors, [])

    def test_six_appearances_are_local_persistent_and_keyboard_accessible(self):
        self.host.set_viewport_size({"width": 1600, "height": 900})
        for palette in ("nocturne", "aurora", "rose", "cloud", "sand", "jade"):
            self.appearance(palette)
            self.assertEqual(self.page.evaluate("localStorage.getItem('mw-appearance')"), palette)
            self.assertEqual(self.page.evaluate("document.documentElement.dataset.palette"), palette)
            tokens = self.page.evaluate('''() => {
              const style = getComputedStyle(document.documentElement);
              return Object.fromEntries(['--text','--text-2','--muted','--bg','--panel'].map(key => [key, style.getPropertyValue(key).trim()]));
            }''')
            def luminance(color):
                values = [int(color[i:i+2], 16)/255 for i in (1, 3, 5)]
                return sum((value/12.92 if value <= .04045 else ((value+.055)/1.055)**2.4)*weight
                           for value, weight in zip(values, (.2126, .7152, .0722)))
            for foreground in ("--text", "--text-2", "--muted"):
                for background in ("--bg", "--panel"):
                    low, high = sorted((luminance(tokens[foreground]), luminance(tokens[background])))
                    self.assertGreaterEqual((high+.05)/(low+.05), 4.5, f"Low contrast: {palette} {foreground}")
            self.host.screenshot(path=str(self.shots / f"atelier-relationships-{palette}.png"), animations="disabled")
            self.page.locator('#tabs [data-tab="contacts"]').click()
            expect(self.page.locator(".contact-card").first).to_be_visible()
            self.host.screenshot(path=str(self.shots / f"atelier-contacts-{palette}.png"), animations="disabled")
            self.page.locator('#tabs [data-tab="relationships"]').click()
        self.host.reload()
        self.page = self.host.frame(name="editor")
        expect(self.page.locator("#app")).to_be_visible()
        self.assertEqual(self.page.evaluate("document.documentElement.dataset.palette"), "jade")
        self.page.locator("#theme-toggle").click()
        expect(self.page.locator("#appearance-dialog")).to_be_visible()
        self.page.locator("#appearance-dialog").press("Escape")
        expect(self.page.locator("#appearance-dialog")).not_to_be_visible()
        expect(self.page.locator("#theme-toggle")).to_be_focused()
        self.assertEqual(self.page.evaluate("socialWrites"), [])
        self.assertEqual(self.errors, [])

    def test_every_module_fits_desktop_tablet_and_phone(self):
        self.install_console()
        for width, height in [(1600, 900), (1024, 768), (390, 844), (360, 780)]:
            self.host.set_viewport_size({"width": width, "height": height})
            for tab in ("status", "life", "thoughts", "contacts", "relationships", "memories", "logs", "ticks", "debug", "map", "actions", "sessions", "settings", "presets"):
                self.page.locator(f'#tabs [data-tab="{tab}"]').click()
                expect(self.page.locator(f"#tab-{tab}")).to_be_visible()
                self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), f"Overflow: {tab} {width}")
                if width == 1024:
                    self.assertLessEqual(self.page.locator(".sidebar").bounding_box()["width"], 70)
                if tab in ("contacts", "relationships"):
                    grid = self.page.locator("#contacts-list" if tab == "contacts" else "#relationships-grid")
                    self.assertGreater(grid.bounding_box()["height"], height * .55)
                    card = grid.locator("button").first
                    if card.count():
                        bounds = card.bounding_box()
                        footer = card.locator(".people-card-foot").bounding_box()
                        self.assertLessEqual(footer["y"] + footer["height"], bounds["y"] + bounds["height"] - 8, f"Clipped footer: {tab} {width}")
                if width in (1600, 390):
                    self.host.screenshot(path=str(self.shots / f"atelier-{tab}-{width}.png"), animations="disabled")
            if width == 390:
                self.page.locator(".topbar-context > summary").click()
                expect(self.page.locator(".topbar-context #config-scope")).to_be_visible()
                self.page.locator(".topbar-context").press("Escape")
                self.assertFalse(self.page.locator(".topbar-context").evaluate("node => node.open"))
                self.page.locator("#theme-toggle").click()
                for option in self.page.locator(".appearance-option").all():
                    bounds = option.bounding_box()
                    text = option.locator("small").bounding_box()
                    self.assertGreater(bounds["height"], 110)
                    self.assertLessEqual(text["y"] + text["height"], bounds["y"] + bounds["height"] - 5)
                self.host.screenshot(path=str(self.shots / "atelier-theme-picker-mobile.png"), animations="disabled")
                self.page.get_by_label("关闭外观设置").click()
        self.assertEqual(self.errors, [])

    def test_open_session_picker_themes_long_labels_and_real_selection(self):
        self.install_console()
        session = self.page.locator("#status-session")
        self.page.evaluate('''() => {
          window.pickerReads = [];
          const read = AstrBotPluginPage.apiGet;
          AstrBotPluginPage.apiGet = (url, params) => {
            if (url === 'state' && params.session === document.getElementById('status-session').value
                && pickerSelectionChanged) pickerReads.push(params.session);
            return read(url, params);
          };
          const select = document.getElementById('status-session');
          window.pickerSelectionChanged = false;
          select.addEventListener('change', () => { pickerSelectionChanged = true; }, {capture:true});
          select.options[1].textContent = '朋友的小群 · 一起讨论星空与旅行的很长很长的群名称 · 2002';
          for (let index = 0; index < 24; index++) select.add(new Option(`群聊 · ${3000+index}`, `test:GroupMessage:${3000+index}`));
        }''')
        if not self.page.evaluate("CSS.supports('appearance', 'base-select')"):
            self.skipTest("This browser uses the native picker fallback")
        for palette in ("nocturne", "aurora", "rose", "cloud", "sand", "jade"):
            self.appearance(palette)
            original = session.input_value()
            session.click()
            self.assertTrue(session.evaluate("node => node.matches(':open')"))
            styles = session.evaluate('''node => {
              const picker = getComputedStyle(node, '::picker(select)');
              return {height: parseFloat(picker.height), background: picker.backgroundColor,
                text: picker.color, expectedText: getComputedStyle(node).color};
            }''')
            self.assertLessEqual(styles["height"], 360)
            self.assertNotEqual(styles["background"], "rgba(0, 0, 0, 0)")
            self.assertEqual(styles["text"], styles["expectedText"])
            self.host.screenshot(path=str(self.shots / f"session-picker-{palette}.png"), animations="disabled")
            session.press("Escape")
            self.assertEqual(session.input_value(), original)
        self.assertEqual(self.page.evaluate("pickerReads"), [])
        session.click()
        session.locator("option").nth(1).click()
        expect(session).to_have_value("aiocqhttp:GroupMessage:2002")
        self.assertIn("aiocqhttp:GroupMessage:2002", self.page.evaluate("pickerReads"))
        self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"))
        session.click()
        self.host.keyboard.press("Home")
        self.host.keyboard.press("Enter")
        expect(session).to_have_value("aiocqhttp:GroupMessage:1001")
        self.assertIn("aiocqhttp:GroupMessage:1001", self.page.evaluate("pickerReads"))
        self.host.set_viewport_size({"width": 390, "height": 844})
        self.page.locator(".topbar-context > summary").click()
        menu = self.page.locator(".topbar-context .command-menu-body").bounding_box()
        self.assertGreaterEqual(menu["x"], 10)
        self.assertLessEqual(menu["x"] + menu["width"], 380)
        session.click()
        self.host.screenshot(path=str(self.shots / "session-picker-mobile.png"), animations="disabled")
        label = session.locator("option").nth(1).bounding_box()
        self.assertGreaterEqual(label["x"], 0)
        self.assertLessEqual(label["x"] + label["width"], 390)
        self.assertGreater(label["height"], 32)
        session.press("Escape")
        self.assertTrue(self.page.locator(".topbar-context").evaluate("node => node.open"))
        self.assertFalse(session.evaluate("node => node.matches(':open')"))
        session.click()
        session.locator("option").nth(1).click()
        self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"))
        self.host.screenshot(path=str(self.shots / "session-picker-mobile-selected.png"), animations="disabled")
        self.host.keyboard.press("Escape")
        self.assertFalse(self.page.locator(".topbar-context").evaluate("node => node.open"))
        self.assertEqual(self.errors, [])
