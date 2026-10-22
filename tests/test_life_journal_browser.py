"""Journal settings, retained vocabulary and responsive views in real Chromium."""
import unittest

from tests import test_generation_browser as generation
from tests import test_workbench_browser as workbench


@unittest.skipIf(generation.sync_playwright is None, "Install playwright")
class LifeJournalBrowserTests(unittest.TestCase):
    setUpClass = classmethod(generation.GenerationBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(generation.GenerationBrowserTests.tearDownClass.__func__)
    setUp = generation.GenerationBrowserTests.setUp
    capture_writes = workbench.WorkbenchBrowserTests.capture_writes

    def test_life_settings_save_independently_and_keep_vocabulary(self):
        expect = generation.expect
        self.capture_writes()
        self.page.locator('button[data-tab="memories"]').click()
        expect(self.page.locator('#memory-vocabulary')).to_be_visible()
        self.page.locator('#memory-policy').click()
        expect(self.page.locator('#dialog-title')).to_have_text('生活回忆设置')
        self.assertEqual(self.page.locator('[data-dialog-key="automatic"] option').all_text_contents(),
                         ['根据地点与话题自动回想', '只在主动查询时回想'])
        self.assertEqual(self.page.locator('[data-dialog-key="mode"], [data-dialog-key="summary"]').count(), 0)
        self.page.locator('[data-dialog-key="automatic"]').select_option('manual')
        self.page.locator('[data-dialog-key="sleep_review"]').uncheck()
        self.page.locator('[data-dialog-key="chars"]').fill('800')
        self.page.locator('#dialog-ok').click()
        self.page.locator('#save').click()
        self.page.wait_for_function('policyWrites.length > 0')
        world = self.page.evaluate('policyWrites.at(-1)')
        self.assertEqual(world['memory']['automatic_recall'], 'manual')
        self.assertEqual(world['memory']['recall_max_chars'], 800)
        self.assertFalse(world['memory']['sleep_review'])
        self.assertTrue(world['profile']['enabled'])
        self.assertTrue(world['profile']['consolidate_enabled'])
        self.assertNotIn('dialogue_summary', world['memory'])
        self.assertNotIn('chat_overflow', world['context'])
        self.assertEqual(self.errors, [])

    def test_journal_contacts_and_relationships_fit_six_themes_and_mobile(self):
        self.page.add_style_tag(content="*,*::before,*::after{animation-duration:0s!important;transition-duration:0s!important}")
        self.page.evaluate("""() => {
          const read = AstrBotPluginPage.apiGet;
          // Memory records belong to a selected session; the base fixture has none.
          const session = document.getElementById('memory-session');
          session.add(new Option('合成日记会话', 'test:GroupMessage:journal'));
          session.value = 'test:GroupMessage:journal';
          const rows = [
            {id:1, content:'在书房修好了风车模型，试转时叶片终于平稳了。', type:'scene', node_id:'study', created_at:1792411200, weight:.7, tier:'raw', source:'runtime'},
            {id:2, content:'这次只读完了第一章，还想找时间接着看。', type:'inner', node_id:'study', created_at:1792411300, weight:.5, tier:'raw', source:'runtime'},
            {id:3, content:'做了个梦：风车带着我飞过很高的云。', type:'dream', node_id:'', created_at:1792411400, weight:.25, tier:'gist', source:'dream'},
          ];
          AstrBotPluginPage.apiGet = (endpoint, params) => {
            if (endpoint === 'memories') return Promise.resolve({memories:rows});
            if (endpoint === 'memories/stats') return Promise.resolve({total:3, by_type:{scene:1, inner:1, dream:1}, avg_weight:.48});
            return read(endpoint, params);
          };
        }""")
        for theme in ['nocturne', 'aurora', 'rose', 'cloud', 'sand', 'jade']:
            self.page.locator('#theme-toggle').click()
            self.page.locator(f'[data-appearance="{theme}"]').click()
            self.page.locator('.appearance-close').click()
            for width, height in [(1440, 1000), (390, 844)]:
                self.host.set_viewport_size({'width': width, 'height': height})
                for tab in ['memories', 'contacts', 'relationships']:
                    self.page.locator(f'button[data-tab="{tab}"]').click()
                    self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (theme, width, tab))
                self.page.locator('button[data-tab="memories"]').click()
                generation.expect(self.page.locator('#memory-list .memory-card')).to_have_count(3)
                self.assertNotIn('undefined', self.page.locator('#memory-list').inner_text())
                self.assertNotIn('NaN', self.page.locator('#memory-list').inner_text())
                if width == 390:
                    self.assertGreater(self.page.locator('.memory-copy').first.bounding_box()['width'], 270)
                self.host.screenshot(path=str(self.shots / f'life-journal-{theme}-{width}.png'))
        self.assertEqual(self.page.locator('#memory-type option[value="interaction"], #memory-type option[value="relation"]').count(), 0)
        self.assertEqual(self.page.locator('#memory-type option[value="dream"]').count(), 1)
        self.page.locator('#memory-list').get_by_role('button', name='编辑', exact=True).first.click()
        self.assertEqual(self.page.locator('[data-dialog-key="type"] option').evaluate_all('(rows) => rows.map(row => row.value)'),
                         ['scene', 'dream', 'inner', 'event'])
        self.assertEqual(self.errors, [])
