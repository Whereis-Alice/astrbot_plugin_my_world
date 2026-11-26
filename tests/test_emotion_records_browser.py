"""The editor exposes hidden emotions and keeps writes tied to their original session."""
import unittest

from tests import test_generation_browser as generation
from tests.test_generation_browser import expect, sync_playwright


FIRST = "test:GroupMessage:1001"
SECOND = "test:FriendMessage:2002"
OUTSIDE = "test:GroupMessage:3003"

BRIDGE = r"""(() => {
  const get = AstrBotPluginPage.apiGet, post = AstrBotPluginPage.apiPost;
  const first = 'test:GroupMessage:1001', second = 'test:FriendMessage:2002', outside = 'test:GroupMessage:3003';
  const defaults = {status:'active', affects_dialogue:true, current_persona:true, feature_enabled:true, source_enabled:true, revision:'r1'};
  let records = [
    {...defaults, id:'a', text:'我还是很担心没修好的风车', kind:'heart_knot', session:first, source_label:'群 1001「工坊」'},
    {...defaults, id:'b', text:'还没说开的事', kind:'grudge', session:second, source_label:'私聊 2002', user_name:'小满'},
    {...defaults, id:'c', text:'<img src=x>', kind:'heart_knot', session:first, source_label:'群 1001「工坊」',
      archived:true, status:'expired', current_persona:false, affects_dialogue:false},
    {...defaults, id:'d', text:'出门忘了带伞', kind:'heart_knot', session:outside, source_label:'群 3003'},
  ];
  const sessions = {groups:[{id:'shared-life', name:'共同生活', main_session:first, sessions:[first,second]}],
    sessions:[{session_id:first,note:'工坊',enabled:true}, {session_id:second,type:'private',enabled:true},
      {session_id:outside,note:'其他世界',enabled:true}]};
  window.emotionWrites = [];
  window.holdEmotions = false;
  window.releaseEmotions = null;
  AstrBotPluginPage.apiGet = (endpoint, params = {}) => {
    if (endpoint === 'config') return get(endpoint, params).then(config => ({...config, sessions}));
    if (endpoint !== 'thoughts/emotions') return get(endpoint, params);
    const origins = params.session === 'shared-life' ? [first, second] : [params.session];
    const result = {items: structuredClone(records.filter(item => origins.includes(item.session)))};
    if (holdEmotions && params.session === 'shared-life') return new Promise(resolve => {
      window.releaseEmotions = () => resolve(result);
    });
    return Promise.resolve(result);
  };
  AstrBotPluginPage.apiPost = (endpoint, payload) => {
    if (endpoint !== 'thoughts/emotions/update') return post(endpoint, payload);
    emotionWrites.push(structuredClone(payload));
    const row = records.find(row => row.id === payload.id && row.session === payload.source_session);
    if (!row || row.revision !== payload.revision) throw Error('记录已变化');
    const {operation, text} = payload;
    if (operation === 'delete') records = records.filter(item => item !== row);
    if (operation === 'edit') row.text = text;
    if (operation === 'pause') Object.assign(row, {status:'paused', affects_dialogue:false});
    if (operation === 'resume') Object.assign(row, {status:'active', affects_dialogue:true});
    if (operation === 'resolve') Object.assign(row, {status:'resolved', affects_dialogue:false, archived:true});
    row.revision += '_';
    return Promise.resolve({ok:true, item:structuredClone(row)});
  };
})();"""


@unittest.skipIf(sync_playwright is None, "Install playwright to run browser tests")
class EmotionRecordsBrowserTests(unittest.TestCase):
    setUpClass = classmethod(generation.GenerationBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(generation.GenerationBrowserTests.tearDownClass.__func__)

    def setUp(self):
        generation.GenerationBrowserTests.setUp(self)
        self.context.add_init_script(script=BRIDGE)
        self.host.reload()
        self.page = self.host.frame(name="editor")
        expect(self.page.locator("#app")).to_be_visible()
        self.page.locator('button[data-tab="thoughts"]').click()
        self.page.get_by_role("tab", name="未释怀记录").click()
        self.records = self.page.locator("#emotion-records-list")
        expect(self.records.locator(".emotion-record")).to_have_count(3)

    def tearDown(self):
        self.assertEqual(self.errors, [])

    def test_shared_scope_edit_pause_and_archive(self):
        expect(self.records).to_contain_text("<img src=x>")
        expect(self.records.locator("img")).to_have_count(0)
        expect(self.page.locator("#thoughts-clear")).to_be_hidden()
        expect(self.page.locator("#thought-date")).to_be_hidden()
        row = self.records.locator('[data-record-id="b"]')
        row.get_by_role("button", name="编辑内容").click()
        row.get_by_role("textbox", name="记录内容").fill("想等小满有空再聊")
        self.assertEqual(self.page.evaluate("emotionWrites.length"), 0)
        row.get_by_role("button", name="保存内容").click()
        expect(row).to_contain_text("想等小满有空再聊")
        row.get_by_role("button", name="暂停参考").click()
        expect(row).to_contain_text("已暂停")
        row.get_by_role("button", name="恢复参考").click()
        expect(row).to_contain_text("可在相关对话中参考")
        row.get_by_role("button", name="标为已释怀").click()
        expect(row).to_contain_text("已释怀")
        self.page.get_by_role("combobox", name="记录状态").select_option("history")
        expect(self.records.locator(".emotion-record")).to_have_count(2)
        row.get_by_role("button", name="删除记录").click()
        expect(self.page.locator("#dialog-title")).to_have_text("删除未释怀记录？")
        self.page.locator("#dialog-ok").click()
        expect(row).to_have_count(0)
        writes = self.page.evaluate("emotionWrites")
        self.assertEqual([item["operation"] for item in writes], ["edit", "pause", "resume", "resolve", "delete"])
        self.assertTrue(all(item["source_session"] == SECOND and item["session"] == "shared-life" for item in writes))
        self.assertEqual(len({item["revision"] for item in writes}), 5)

    def test_old_scope_cannot_replace_current_records(self):
        self.page.evaluate("holdEmotions = true")
        self.page.locator("#thoughts-refresh").click()
        self.page.wait_for_function("releaseEmotions !== null")
        self.page.locator("#thought-session").select_option(OUTSIDE)
        expect(self.records).to_contain_text("出门忘了带伞")
        self.page.evaluate("releaseEmotions()")
        expect(self.records).to_contain_text("出门忘了带伞")
        expect(self.records).not_to_contain_text("小满")
        self.page.locator("#thought-session").select_option(SECOND)
        expect(self.records).to_contain_text("小满")
        expect(self.records).not_to_contain_text("风车")
        self.assertEqual(self.page.evaluate("emotionWrites"), [])

    def test_keyboard_navigation_includes_records(self):
        records_tab = self.page.get_by_role("tab", name="未释怀记录")
        records_tab.press("ArrowRight")
        inner_tab = self.page.get_by_role("tab", name="心里的话")
        expect(inner_tab).to_be_focused()
        expect(self.page.locator("#thought-date")).to_be_visible()
        inner_tab.press("End")
        expect(records_tab).to_be_focused()
        expect(self.records.locator(".emotion-record")).to_have_count(3)
        self.host.set_viewport_size({"width": 390, "height": 844})
        self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"))
        self.host.screenshot(path=str(self.shots / "emotion-records-mobile.png"))
