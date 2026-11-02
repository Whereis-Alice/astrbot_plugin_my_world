"""Memory cards and their detail share the API's confirmed encounter location."""
import unittest

from tests import test_memory_details_browser as details
from tests.test_generation_browser import expect, sync_playwright


@unittest.skipIf(sync_playwright is None, "Install playwright for browser verification")
class MemoryLocationsBrowserTests(unittest.TestCase):
    setUpClass = classmethod(details.MemoryDetailsBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(details.MemoryDetailsBrowserTests.tearDownClass.__func__)
    setUp = details.MemoryDetailsBrowserTests.setUp
    tearDown = details.MemoryDetailsBrowserTests.tearDown
    open_page = details.MemoryDetailsBrowserTests.open_page

    def encounter(self, location=None):
        self.page.evaluate('''(location) => {
          const reply = recordReply;
          window.recordReply = (key, params) => {
            const result = reply(key, params);
            if (key === 'memories') result.memories[0] = {...result.memories[0], source:'life_event',
              node_id:'bedroom', context:JSON.stringify({kind:'world_visit',peer_world:'港湾'}),
              location};
            return result;
          };
        }''', location)
        self.open_page("memories")

    def test_cards_and_detail_show_confirmed_route_instead_of_home(self):
        self.encounter({"kind": "visit", "label": "港湾 · 港口步道 → 灯塔", "stops": [
            {"place": "港口步道", "activity": "一起看潮汐"}, {"place": "灯塔", "activity": "看灯光"}]})
        card = self.page.locator("#memory-list")
        expect(card).to_contain_text("港湾 · 港口步道 → 灯塔")
        expect(card).not_to_contain_text("卧室")
        card.get_by_role("button", name="详情", exact=True).click()
        body = self.page.locator("#dialog-body")
        expect(body).to_contain_text("港湾 · 港口步道 → 灯塔")
        expect(body).to_contain_text("港口步道 · 一起看潮汐")
        expect(body).not_to_contain_text("bedroom")

    def test_older_api_reply_does_not_treat_home_as_a_visit_location(self):
        self.encounter()
        card = self.page.locator("#memory-list")
        expect(card).to_contain_text("串门 · 具体地点未记")
        expect(card).not_to_contain_text("卧室")
        card.get_by_role("button", name="详情", exact=True).click()
        expect(self.page.locator("#dialog-body")).to_contain_text("串门 · 具体地点未记")
