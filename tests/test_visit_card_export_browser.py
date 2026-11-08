"""Per-record card preview and downloads stay local and scoped to the chosen life."""
import base64
import os
from pathlib import Path
import unittest

from tests import test_visits_browser as visits_browser
from tests.test_generation_browser import expect, sync_playwright


@unittest.skipIf(sync_playwright is None, "Install playwright for browser verification")
class VisitCardExportBrowserTests(unittest.TestCase):
    setUpClass = classmethod(visits_browser.VisitsBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(visits_browser.VisitsBrowserTests.tearDownClass.__func__)

    def setUp(self):
        visits_browser.VisitsBrowserTests.setUp(self)
        sample = Path(os.environ.get("MY_WORLD_CARD_PREVIEW", "__no_card_preview__"))
        png = base64.b64encode(sample.read_bytes()).decode() if sample.exists() else (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZrNwAAAAASUVORK5CYII=")
        self.page.evaluate("""dataUrl => {
          visitData.enabled=false;visitData.scope_enabled=false;
          visitData.history=[{id:'past-call',status:'completed',created_at:1761990000,arrived_at:1761990000,
            local:{id:'home:isla',name:'艾拉'},remote:{id:'friend',name:'爱丽丝'},end_reason:'告别归家',
            log:[{actor_id:'friend',kind:'say',text:'下次一起喝茶。'}]}];
          window.cardExportStarts=[];window.cardExportReads=[];window.cardExportDownloads=[];
          window.cardExportMode='pending';window.cardPageMode='ok';window.cardDownloadMode='ok';window.cardPageCount=1;
          window.exportJob=()=>({job_id:'job-one',state:cardExportMode==='defer'?'pending':cardExportMode,
            filename:'my-world-visit-past-call.png',pages:cardExportMode==='ready'?
              Array.from({length:cardPageCount},(_,index)=>({index,filename:`visit-${index+1}.png`,mime:'image/png',width:1350,height:4428})):[],
            error:'生成暂时失败，请重试'});
          const read=AstrBotPluginPage.apiGet,write=AstrBotPluginPage.apiPost;
          AstrBotPluginPage.apiPost=(endpoint,payload)=>{
            if(endpoint!=='visits/card-export')return write(endpoint,payload);
            cardExportStarts.push(structuredClone(payload));
            if(cardExportMode==='defer')return new Promise(resolve=>window.resolveCardStart=resolve);
            return Promise.resolve(exportJob());
          };
          AstrBotPluginPage.apiGet=(endpoint,payload)=>{
            if(endpoint==='visits/card-export-status'){cardExportReads.push({endpoint,payload:structuredClone(payload)});return Promise.resolve(exportJob());}
            if(endpoint==='visits/card-export-page'){
              cardExportReads.push({endpoint,payload:structuredClone(payload)});
              if(cardPageMode==='fail')return Promise.reject(new Error('预览加载失败，请重试'));
              return Promise.resolve({index:payload.page,filename:`visit-${payload.page+1}.png`,mime:'image/png',
                data_url:cardPageMode==='unsafe'?'https://untrusted.example/private.png':dataUrl});
            }
            return read(endpoint,payload);
          };
          AstrBotPluginPage.download=(endpoint,payload,filename)=>{
            cardExportDownloads.push({endpoint,payload:structuredClone(payload),filename});
            return cardDownloadMode==='fail'?Promise.reject(new Error('下载暂时失败，请重试')):Promise.resolve();
          };
        }""", "data:image/png;base64," + png)
        self.refresh()
        self.record().locator('summary').click()
        expect(self.panel()).to_be_visible()

    def refresh(self):
        self.page.locator('#tab-visits').get_by_role('button', name='刷新', exact=True).click()

    def record(self):
        return self.page.locator('.visits-history details[data-visit-id="past-call"]')

    def panel(self):
        return self.record().locator('.visits-export')

    def generate(self):
        return self.panel().locator('.visits-export-generate')

    def ready(self):
        self.page.evaluate("cardExportMode='ready'")
        self.generate().click()
        expect(self.panel().locator('img')).to_be_visible()

    def test_pending_survives_refresh_and_only_downloads_after_user_clicks(self):
        self.generate().click()
        expect(self.generate()).to_be_disabled()
        self.assertEqual(self.page.evaluate('cardExportStarts.length'), 1)
        self.refresh()
        expect(self.generate()).to_be_disabled()
        expect(self.panel()).to_contain_text('正在整理完整记录')
        self.page.evaluate("cardExportMode='ready'")
        expect(self.panel().locator('img')).to_be_visible()
        self.assertEqual(self.page.evaluate('cardExportDownloads.length'), 0)
        self.assertEqual(self.page.evaluate("cardExportReads.filter(item=>item.endpoint==='visits/card-export-page').map(item=>item.payload.page)"), [0])
        expect(self.panel().locator('img')).to_have_attribute('alt', '本次相见的完整长卡片')
        self.assertEqual(self.panel().get_by_role('button', name='上一张', exact=True).count(), 0)
        self.assertEqual(self.panel().get_by_role('button', name='下一张', exact=True).count(), 0)
        self.assertEqual(self.panel().get_by_role('button', name='下载全部 ZIP', exact=True).count(), 0)
        self.panel().get_by_role('button', name='下载图片', exact=True).click()
        self.page.wait_for_function('cardExportDownloads.length===1')
        downloads = self.page.evaluate('cardExportDownloads')
        self.assertEqual(downloads[0], {'endpoint': 'visits/card-export-download', 'filename': 'visit-1.png',
                                      'payload': {'config_scope': '', 'session': 'test:GroupMessage:100',
                                                  'visit_id': 'past-call', 'job_id': 'job-one', 'page': 0}})
        self.assertEqual(len(downloads), 1)
        self.assertEqual(self.page.evaluate('visitWrites.length'), 0)
        self.assertEqual(self.errors, [])

    def test_ready_preview_and_zoom_stay_inside_desktop_and_mobile(self):
        self.ready()
        expect(self.panel()).to_contain_text('不发送到 QQ')
        expect(self.panel()).to_contain_text('完整记录生成一张长图')
        expect(self.panel()).to_contain_text('已生成完整长图')
        for width, height in [(1440, 1000), (390, 844)]:
            self.host.set_viewport_size({'width': width, 'height': height})
            self.panel().scroll_into_view_if_needed()
            self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'))
            self.host.screenshot(path=str(self.shots / f'visit-card-export-single-{width}.png'))
        self.panel().get_by_role('button', name='放大预览', exact=True).click()
        expect(self.panel().get_by_role('button', name='适应宽度', exact=True)).to_have_attribute('aria-pressed', 'true')
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'))
        self.assertGreater(self.panel().locator('.visits-export-preview').evaluate('node=>node.scrollWidth-node.clientWidth'), 100)
        self.panel().get_by_role('button', name='适应宽度', exact=True).click()
        expect(self.panel().get_by_role('button', name='放大预览', exact=True)).to_be_visible()
        self.assertEqual(self.errors, [])

    def test_generate_preview_and_download_failures_are_recoverable(self):
        self.page.evaluate("cardExportMode='failed'")
        self.generate().click()
        expect(self.panel()).to_contain_text('生成暂时失败，请重试')
        expect(self.generate()).to_be_enabled()
        self.page.evaluate("cardExportMode='ready';cardPageMode='fail'")
        self.generate().click()
        expect(self.panel()).to_contain_text('预览加载失败，请重试')
        self.page.evaluate("cardPageMode='ok'")
        self.panel().get_by_role('button', name='重试预览', exact=True).click()
        expect(self.panel().locator('img')).to_be_visible()
        self.page.evaluate("cardDownloadMode='fail'")
        self.panel().get_by_role('button', name='下载图片', exact=True).click()
        expect(self.panel()).to_contain_text('下载暂时失败，请重试')
        expect(self.panel().get_by_role('button', name='下载图片', exact=True)).to_be_enabled()
        self.assertEqual(self.page.evaluate('visitWrites.length'), 0)
        self.assertEqual(self.errors, [])

    def test_switching_life_discards_a_late_generation_result(self):
        self.page.evaluate("cardExportMode='defer'")
        self.generate().click()
        self.page.locator('#status-session').select_option('test:GroupMessage:200')
        self.page.wait_for_function("visitReads.at(-1).session==='test:GroupMessage:200'")
        self.page.evaluate("cardExportMode='ready';resolveCardStart(exportJob())")
        self.page.locator('#status-session').select_option('test:GroupMessage:100')
        self.page.wait_for_function("visitReads.at(-1).session==='test:GroupMessage:100'")
        self.record().locator('summary').click()
        expect(self.generate()).to_have_text('生成卡片')
        self.assertEqual(self.panel().locator('img').count(), 0)
        self.assertEqual(self.page.evaluate('cardExportReads.length'), 0)
        self.assertEqual(self.errors, [])

    def test_unsafe_image_url_is_rejected_and_active_record_has_no_export(self):
        self.page.evaluate("cardExportMode='ready';cardPageMode='unsafe'")
        self.generate().click()
        expect(self.panel()).to_contain_text('预览图片暂时不可用，请重试')
        self.assertEqual(self.panel().locator('img').count(), 0)
        self.page.evaluate("visitData.active={...visitData.history[0],id:'active-call',status:'active'}")
        self.refresh()
        self.assertEqual(self.page.locator('.visits-current .visits-export').count(), 0)
        self.assertEqual(self.errors, [])

    def test_legacy_multiple_images_are_not_silently_truncated(self):
        self.page.evaluate("cardExportMode='ready';cardPageCount=3")
        self.generate().click()
        expect(self.panel()).to_contain_text('卡片结果不是完整长图，请重新生成')
        self.assertEqual(self.panel().locator('img').count(), 0)
        self.assertEqual(self.page.evaluate('cardExportReads.length'), 0)
        expect(self.generate()).to_be_enabled()
        self.page.evaluate('cardPageCount=1')
        self.generate().click()
        expect(self.panel().locator('img')).to_be_visible()
        self.assertEqual(self.page.evaluate('cardExportStarts.length'), 2)
        self.assertEqual(self.page.evaluate('visitWrites.length'), 0)
        self.assertEqual(self.errors, [])
