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
    {...defaults, id:'b', text:'还没说开的事', kind:'grudge', session:second, source_label:'私聊 2002', user_name:'小满',
      narrative:'我到约好的地方等了一会儿，还想问问小满后来怎么了。',
      evidence:[{text:'我今天临时有事，没来得及告诉你。<img src=x>', at:1795654800, user_name:'小满'}]},
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
  window.holdEmotionDelete = false;
  window.releaseEmotionDelete = null;
  window.failNextEmotionRead = false;
  AstrBotPluginPage.apiGet = (endpoint, params = {}) => {
    if (endpoint === 'config') return get(endpoint, params).then(config => ({...config, sessions}));
    if (endpoint !== 'thoughts/emotions') return get(endpoint, params);
    if (failNextEmotionRead) {
      failNextEmotionRead = false;
      return Promise.reject(Error('模拟读取中断'));
    }
    const origins = params.session === 'shared-life' ? [first, second] : [params.session];
    const result = {items: structuredClone(records.filter(item => origins.includes(item.session)))};
    if (holdEmotions && params.session === 'shared-life') return new Promise(resolve => {
      window.releaseEmotions = () => resolve(result);
    });
    return Promise.resolve(result);
  };
  AstrBotPluginPage.apiPost = (endpoint, payload) => {
    if (endpoint === 'thoughts/emotions/delete-batch') {
      emotionWrites.push(structuredClone(payload));
      const remove = () => {
        const chosen = payload.items.map(item => records.find(row => row.id === item.id &&
          row.session === item.source_session && row.revision === item.revision));
        if (chosen.includes(undefined)) throw Error('记录已变化');
        records = records.filter(item => !chosen.includes(item));
        return {ok:true, removed:chosen.length};
      };
      if (holdEmotionDelete) return new Promise(resolve => {
        window.releaseEmotionDelete = () => resolve(remove());
      });
      return Promise.resolve(remove());
    }
    if (endpoint !== 'thoughts/emotions/update') return post(endpoint, payload);
    emotionWrites.push(structuredClone(payload));
    const row = records.find(row => row.id === payload.id && row.session === payload.source_session);
    if (!row || row.revision !== payload.revision) throw Error('记录已变化');
    const {operation, text, narrative} = payload;
    if (operation === 'delete') records = records.filter(item => item !== row);
    if (operation === 'edit') Object.assign(row, {text, narrative});
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
        row.get_by_text("查看来龙去脉", exact=True).click()
        expect(row.get_by_text("我到约好的地方等了一会儿，还想问问小满后来怎么了。")).to_be_visible()
        expect(row.locator("blockquote")).to_contain_text("我今天临时有事，没来得及告诉你。<img src=x>")
        expect(row.locator("img")).to_have_count(0)
        row.get_by_role("button", name="编辑内容").click()
        row.get_by_role("textbox", name="简要摘要").fill("想等小满有空再聊")
        row.get_by_role("textbox", name="第一人称叙述").fill("我想等小满有空，再一起说清楚。")
        self.assertEqual(self.page.evaluate("emotionWrites.length"), 0)
        row.get_by_role("button", name="保存内容").click()
        expect(row).to_contain_text("想等小满有空再聊")
        row.get_by_text("查看来龙去脉", exact=True).click()
        expect(row.get_by_text("我想等小满有空，再一起说清楚。")).to_be_visible()
        expect(row.locator("blockquote")).to_contain_text("我今天临时有事")
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
        self.assertEqual(writes[0]["narrative"], "我想等小满有空，再一起说清楚。")
        self.assertNotIn("evidence", writes[0])

    def test_batch_delete_confirms_selected_exact_origins(self):
        self.page.get_by_role("button", name="批量选择", exact=True).click()
        delete = self.page.get_by_role("button", name="删除所选", exact=True)
        expect(delete).to_be_disabled()
        self.page.get_by_role("checkbox", name="全选当前筛选结果").check()
        expect(self.page.locator("#emotion-records-selected")).to_have_text("已选 3 条")
        self.records.locator('[data-record-id="a"]').get_by_role("checkbox").uncheck()
        expect(self.page.locator("#emotion-records-selected")).to_have_text("已选 2 条")
        delete.click()
        expect(self.page.locator("#dialog-title")).to_have_text("删除所选 2 条未释怀记录？")
        self.page.locator("#dialog-cancel").click()
        self.assertEqual(self.page.evaluate("emotionWrites.length"), 0)
        delete.click()
        self.page.locator("#dialog-ok").click()
        expect(self.records.locator(".emotion-record")).to_have_count(1)
        expect(self.records).to_contain_text("风车")
        expect(self.page.locator("#emotion-records-feedback")).to_contain_text("已删除 2 条记录")
        self.assertEqual(self.page.evaluate("emotionWrites"), [{"session": "shared-life", "config_scope": "", "items": [
            {"id": "b", "source_session": SECOND, "revision": "r1"},
            {"id": "c", "source_session": FIRST, "revision": "r1"},
        ]}])

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

    def start_batch_delete(self):
        self.page.get_by_role("button", name="批量选择", exact=True).click()
        self.records.locator('[data-record-id="b"]').get_by_role("checkbox").check()
        self.page.get_by_role("button", name="删除所选", exact=True).click()
        self.page.locator("#dialog-ok").click()

    def test_refresh_and_switch_to_member_wait_for_committed_delete(self):
        self.page.evaluate("holdEmotionDelete = true")
        self.start_batch_delete()
        self.page.wait_for_function("releaseEmotionDelete !== null")
        self.page.locator("#thoughts-refresh").click()
        self.page.locator("#thought-session").select_option(SECOND)
        self.page.evaluate("releaseEmotionDelete()")
        expect(self.page.locator("#emotion-records-feedback")).to_have_text("所选会话暂无未释怀记录。")
        expect(self.records.locator(".emotion-record")).to_have_count(0)
        self.assertEqual(self.page.evaluate("emotionWrites.length"), 1)

    def test_successful_delete_remains_confirmed_when_list_refresh_fails(self):
        self.page.evaluate("failNextEmotionRead = true")
        self.start_batch_delete()
        feedback = self.page.locator("#emotion-records-feedback")
        expect(feedback).to_contain_text("模拟读取中断")
        expect(feedback).to_contain_text("已删除 1 条记录")
        self.page.get_by_role("button", name="重新读取", exact=True).click()
        expect(self.records.locator(".emotion-record")).to_have_count(2)
        expect(feedback).to_contain_text("已删除 1 条记录")

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
