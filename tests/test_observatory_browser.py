"""Synthetic visual acceptance for the living observatory and its state controls."""
import unittest

from tests import test_relationships_browser as social
from tests import test_workbench_browser as workbench
from tests.test_generation_browser import expect, sync_playwright


@unittest.skipIf(sync_playwright is None, "Install playwright for visual acceptance")
class ObservatoryBrowserTests(unittest.TestCase):
    setUpClass = classmethod(social.RelationshipsBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(social.RelationshipsBrowserTests.tearDownClass.__func__)
    install_console = workbench.WorkbenchBrowserTests.install_console

    def setUp(self):
        social.RelationshipsBrowserTests.setUp(self)
        self.host.emulate_media(reduced_motion="reduce")
        self.install_console()
        self.page.evaluate("""() => {
          const now = 1792586160;
          Object.assign(numericState, {
            nickname: '澪', nickname_live: '澪', zone_name: '海风小镇', node_name: '窗边书房',
            clock_text: '现在是：2026-10-21 20:36 · 夜晚', mood: '平静而满足',
            activity: {label: '在窗边慢慢读书'}, activity_reason: '想把上次没读完的故事，看完。',
            next_schedule: {time: '22:30', weekday: '周三', actions: '泡一杯热茶，收起手边的书', in_minutes: 114},
            values: {...numericState.values, affect: .42, valence: .76, energy: .68, curiosity: .61},
            life_matters: [{id: 'lm_book', text: '想把喜欢的故事，读到最后',
              motivation: '那些还没走到结尾的人物，让她想在明天继续翻开这本书。',
              next_step: '读完第三章，记下一句喜欢的话', kind: 'project', status: 'working', progress: 40}],
          });
          const read = AstrBotPluginPage.apiGet;
          AstrBotPluginPage.apiGet = (endpoint, params) => endpoint === 'state/history'
            ? Promise.resolve({points: Array.from({length: 50}, (_, i) => ({at: now-21600+i*440,
                affect: .3+Math.sin(i/6)*.09+i*.003, valence: .56+Math.sin(i/8)*.08+i*.003})),
              metrics: {swing_per_hour: .08, peak_arousal: .54, low_minutes: 0}})
            : read(endpoint, params);
          document.getElementById('status-session').dispatchEvent(new Event('change'));
        }""")
        expect(self.page.locator("#hero-name")).to_have_text("澪")
        expect(self.page.locator("#history-canvas")).to_be_visible()

    def appearance(self, palette):
        self.page.locator("#theme-toggle").click()
        self.page.locator(f'[data-appearance="{palette}"]').click()
        self.page.get_by_label("关闭外观设置").click()

    def test_six_worlds_have_readable_live_state_and_unique_vector_resources(self):
        self.host.set_viewport_size({"width": 1600, "height": 1060})
        for palette in ("nocturne", "aurora", "rose", "cloud", "sand", "jade"):
            self.appearance(palette)
            expect(self.page.locator("#hero-kpis")).to_contain_text("窗边书房")
            expect(self.page.locator("#life-focus")).to_contain_text("读完第三章")
            expect(self.page.locator("#scene-clock")).to_contain_text("20:36")
            expect(self.page.locator("#history-canvas")).to_have_attribute("aria-label", "心情趋势，共 50 个记录。最近心潮 0.53，效价 0.69。")
            self.assertTrue(self.page.evaluate("""() => {
              const ids = [...document.querySelectorAll('[id]')].map(node => node.id);
              return ids.length === new Set(ids).size && document.documentElement.scrollWidth <= innerWidth;
            }"""))
            self.assertEqual(self.page.locator(".world-illustration svg").evaluate("e => getComputedStyle(e).animationName"), "none")
            self.host.screenshot(path=str(self.shots / f"observatory-{palette}.png"), animations="disabled")
        self.appearance("nocturne")
        for width in (390, 360):
            self.host.set_viewport_size({"width": width, "height": 844})
            self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
            self.host.screenshot(path=str(self.shots / f"observatory-mobile-{width}.png"), animations="disabled")
        self.page.locator("#theme-toggle").click()
        self.host.screenshot(path=str(self.shots / "observatory-appearances-mobile.png"), animations="disabled")
        self.page.locator("#appearance-dialog").press("Escape")
        expect(self.page.locator("#theme-toggle")).to_be_focused()
        self.assertEqual(self.page.evaluate("numericWrites"), [])
        self.assertEqual(self.page.evaluate("socialWrites"), [])
        self.assertEqual(self.errors, [])

    def test_running_details_are_keyboard_accessible_and_follow_current_snapshot(self):
        self.host.set_viewport_size({"width":1600,"height":1060})
        summary = self.page.locator(".world-details > summary")
        expect(self.page.locator("#status-time")).to_be_hidden()
        summary.focus()
        summary.press("Enter")
        expect(self.page.locator("#status-time")).to_contain_text("20:36")
        self.assertEqual(self.page.locator('.world-details .status-card').evaluate(
            'e => getComputedStyle(e).gridTemplateColumns.split(" ").length'), 3)
        self.assertTrue(self.page.locator('#hero-kpis .kpi').nth(1).locator('.kpi-note').inner_text())
        self.assertTrue(self.page.locator('#hero-kpis .kpi').nth(2).locator('.kpi-note').inner_text())
        expect(self.page.locator('#status-diagnostics')).not_to_be_visible()
        self.assertTrue(self.page.evaluate('''() => {
          const hero=document.querySelector('.hero-card').getBoundingClientRect();
          const body=document.querySelector('.wellbeing-card').getBoundingClientRect();
          const focus=document.querySelector('#life-focus').getBoundingClientRect();
          const history=document.querySelector('.history-card').getBoundingClientRect();
          return Math.abs(hero.y-body.y)<1 && Math.abs(hero.bottom-body.bottom)<1
            && Math.abs(focus.y-history.y)<1;
        }'''))
        self.page.evaluate("""() => {
          numericState.nickname_live = '新名片'; numericState.node_name = '露台';
          numericState.night = {...numericState.night, sleeping: true};
          document.getElementById('status-session').dispatchEvent(new Event('change'));
        }""")
        expect(self.page.locator("#hero-name")).to_have_text("新名片")
        expect(self.page.locator("#hero-kpis")).to_contain_text("露台")
        expect(self.page.locator(".world-window")).to_have_class("world-window is-sleeping")
        expect(self.page.locator("#status-time")).to_be_visible()
        self.assertEqual(self.page.evaluate("numericWrites"), [])
        self.assertEqual(self.errors, [])

    def test_late_logs_cannot_replace_the_new_sessions_diagnostics(self):
        self.page.evaluate("""() => {
          window.pendingStatusLogs = [];
          const read = AstrBotPluginPage.apiGet;
          AstrBotPluginPage.apiGet = (endpoint, params) => endpoint === 'logs'
            ? new Promise(resolve => pendingStatusLogs.push({resolve, session: params.session}))
            : read(endpoint, params);
        }""")
        self.page.locator('#status-refresh').click()
        self.page.wait_for_function('pendingStatusLogs.length === 1')
        self.page.evaluate("""() => {
          const select = document.getElementById('status-session');
          select.add(new Option('另一个角色', 'test:GroupMessage:second'));
          select.value = 'test:GroupMessage:second';
          numericState.nickname_live = '第二个角色';
          select.dispatchEvent(new Event('change'));
        }""")
        self.page.wait_for_function('pendingStatusLogs.length === 2')
        self.page.evaluate("pendingStatusLogs[1].resolve({events:[{world_time:2,event_type:'skip',text:'第二个角色的警告'}]})")
        expect(self.page.locator('#status-warn')).to_contain_text('第二个角色的警告')
        self.page.evaluate("pendingStatusLogs[0].resolve({events:[{world_time:1,event_type:'skip',text:'前一个角色的警告'}]})")
        self.page.evaluate('() => new Promise(resolve => requestAnimationFrame(resolve))')
        expect(self.page.locator('#status-warn')).to_contain_text('第二个角色的警告')
        expect(self.page.locator('#status-warn')).not_to_contain_text('前一个角色')
        self.assertEqual(self.errors, [])

    def test_switching_history_window_during_slow_load_shows_latest_selection(self):
        self.page.evaluate("""() => {
          window.pendingHistories = [];
          const read = AstrBotPluginPage.apiGet;
          AstrBotPluginPage.apiGet = (endpoint, params) => endpoint === 'state/history'
            ? new Promise(resolve => pendingHistories.push({resolve, hours: params.hours, session:params.session}))
            : read(endpoint, params);
        }""")
        self.page.locator('#history-windows button').filter(has_text='6 小时').click()
        self.page.wait_for_function('pendingHistories.length === 1')
        self.page.locator('#history-windows button').filter(has_text='3 天').click()
        self.page.wait_for_function('pendingHistories.length === 2')
        self.page.evaluate("pendingHistories[1].resolve({points:[{at:100,affect:.2,valence:.7},{at:200,affect:.4,valence:.8}]})")
        expect(self.page.locator('#history-canvas')).to_have_attribute('aria-label', '心情趋势，共 2 个记录。最近心潮 0.40，效价 0.80。')
        self.page.evaluate("pendingHistories[0].resolve({points:[{at:100,affect:.1,valence:.1},{at:200,affect:.1,valence:.1}]})")
        self.page.evaluate('() => new Promise(resolve => requestAnimationFrame(resolve))')
        expect(self.page.locator('#history-canvas')).to_have_attribute('aria-label', '心情趋势，共 2 个记录。最近心潮 0.40，效价 0.80。')
        self.assertEqual(self.errors, [])

    def test_refresh_does_not_overwrite_the_name_being_edited(self):
        self.page.locator('.nickname-menu > summary').click()
        nickname = self.page.locator('#status-nickname')
        nickname.fill('正在输入的新名片')
        self.page.evaluate("document.getElementById('status-refresh').click()")
        expect(nickname).to_have_value('正在输入的新名片')
        expect(self.page.locator('#hero-name')).to_have_text('澪')
        self.assertEqual(self.page.evaluate('numericWrites'), [])
        self.assertEqual(self.errors, [])

    def test_clearing_and_reselecting_session_does_not_leave_history_stuck(self):
        self.page.evaluate("""() => {
          window.pendingHistories = [];
          const read = AstrBotPluginPage.apiGet;
          AstrBotPluginPage.apiGet = (endpoint, params) => endpoint === 'state/history'
            ? new Promise(resolve => pendingHistories.push({resolve, session: params.session}))
            : read(endpoint, params);
        }""")
        self.page.locator('#history-windows button').filter(has_text='6 小时').click()
        self.page.wait_for_function('pendingHistories.length === 1')
        self.page.evaluate("""() => {
          const select = document.getElementById('status-session');
          window.previousHistorySession = select.value;
          select.value = '';
          select.dispatchEvent(new Event('change'));
        }""")
        expect(self.page.locator('#history-canvas')).to_be_hidden()
        self.page.evaluate("""() => {
          const select = document.getElementById('status-session');
          select.value = previousHistorySession;
          select.dispatchEvent(new Event('change'));
        }""")
        self.page.wait_for_function('pendingHistories.length === 2')
        self.page.evaluate("pendingHistories[1].resolve({points:[{at:100,affect:.2,valence:.7},{at:200,affect:.4,valence:.8}]})")
        expect(self.page.locator('#history-canvas')).to_have_attribute('aria-label', '心情趋势，共 2 个记录。最近心潮 0.40，效价 0.80。')
        self.page.evaluate("pendingHistories[0].resolve({points:[]})")
        self.page.evaluate('() => new Promise(resolve => requestAnimationFrame(resolve))')
        expect(self.page.locator('#history-canvas')).to_be_visible()
        self.assertEqual(self.errors, [])
