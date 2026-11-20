"""Keep encounter cards behind bounded, independently confirmed goodbyes."""
from __future__ import annotations

import asyncio
import copy
import unittest

from tests import fake_astrbot
fake_astrbot.install()

from core.visit_card import build_visit_card_pages, visit_card_status  # noqa: E402
from core.visits.decisions import VisitDecisionError  # noqa: E402
from core.visits.departure import departure_pending  # noqa: E402
from tests import test_visit_natural_departure_20261120 as fixture  # noqa: E402
from tests import test_visit_record_continuity as browser_fixture  # noqa: E402
from tests.test_generation_browser import expect, sync_playwright  # noqa: E402


class DepartureCardDeliveryTests(unittest.IsolatedAsyncioTestCase):
    host_turn = fixture.NaturalDepartureTests.host_turn
    offered = fixture.NaturalDepartureTests.offered
    ended = fixture.NaturalDepartureTests.ended

    async def asyncSetUp(self):
        await fixture.NaturalDepartureTests.asyncSetUp(self)
        self.rendered, self.sent = [], []
        self.b_actor.record_card_targets = lambda scope, visit: ["qq:GroupMessage:selected"]

        async def render(scope, snapshot):
            self.rendered.append(copy.deepcopy(snapshot))
            return ["isolated-card"]

        async def send(scope, snapshot, session, page):
            self.sent.append((session, page))

        self.b_actor.render_record_card = render
        self.b_actor.send_record_card = send

    async def settle(self, visit_id):
        await self.b._observe_record_card(visit_id)
        task = self.b._record_card_tasks.get(visit_id)
        if task:
            await asyncio.wait_for(asyncio.shield(task), 1)

    async def test_real_dual_goodbyes_are_in_the_first_and_only_sent_card(self):
        visit_id, host, guest, before, _, _ = await self.ended()
        self.assertTrue(departure_pending(host, self.now))
        await self.settle(visit_id)
        self.assertFalse(self.rendered)
        await self.b._notify_end(visit_id)
        self.assertEqual(len(host["departure_farewells"]), 1)
        await self.settle(visit_id)
        self.assertFalse(self.rendered)
        await self.a._notify_end(visit_id)
        self.assertFalse(departure_pending(host, self.now))
        await self.settle(visit_id)
        snapshot = self.rendered[0]
        self.assertEqual(snapshot["log"], host["log"])
        self.assertEqual(snapshot["log"], guest["log"])
        self.assertEqual(snapshot["log"][:len(before)], before)
        self.assertEqual(len(snapshot["log"]), len(before) + 2)
        self.assertTrue(snapshot["farewell_confirmed"])
        self.assertEqual(build_visit_card_pages(snapshot, now=self.now)[0]["status"], "已告别归家")
        self.assertNotIn("quote", str(snapshot.get("departure_progress")))
        self.assertNotIn("candidate", str(snapshot.get("departure_progress")))
        for _ in range(3):
            await self.b._notify_end(visit_id)
            await self.settle(visit_id)
        self.assertEqual(len(self.rendered), 1)
        self.assertEqual(self.sent, [("qq:GroupMessage:selected", "isolated-card")])

    async def test_failed_goodbye_releases_card_with_only_confirmed_facts(self):
        visit_id, host, _, before, _, _ = await self.ended()

        async def unavailable(scope, phase, current):
            raise VisitDecisionError("provider")

        self.b_actor.decide = unavailable
        for delay in (30, 60, 0):
            await self.b._advance_departure(visit_id)
            self.now += delay
        self.assertEqual(host["departure"]["state"], "failed")
        self.assertTrue(host.get("end_pending"))
        await self.settle(visit_id)
        self.assertEqual(self.rendered[0]["log"], before)
        self.assertFalse(self.rendered[0]["farewell_confirmed"])
        self.assertEqual(build_visit_card_pages(self.rendered[0], now=self.now)[0]["status"], "相处已结束")
        self.assertEqual(len(self.sent), 1)

    async def test_pending_goodbye_deadline_cannot_block_card_indefinitely(self):
        visit_id, host, _, before, _, _ = await self.ended()
        host["departure"]["next_at"] = host["departure"]["deadline"] + 1
        await self.settle(visit_id)
        self.assertFalse(self.rendered)
        self.now = host["departure"]["deadline"]
        self.assertTrue(host.get("end_pending"))
        await self.settle(visit_id)
        self.assertEqual(self.rendered[0]["log"], before)
        self.assertFalse(self.rendered[0]["farewell_confirmed"])
        self.assertEqual(len(self.sent), 1)


class DepartureCardStatusTests(unittest.TestCase):
    def test_old_completed_and_unconfirmed_planned_goodbyes_stay_neutral(self):
        value = {"status": "completed", "host_id": "host", "guest_id": "guest", "log": [],
                 "outing": {"return_agreed": True, "until": 100,
                            "farewells": {"host": "再见。", "guest": "下次见。"}}}
        self.assertEqual(visit_card_status(value, now=100), "相处已结束")
        value["departure_progress"] = {"state": "pending", "deadline": 200}
        self.assertEqual(visit_card_status(value, now=100), "相处已结束，等待告别")
        self.assertEqual(visit_card_status(value, now=200), "相处已结束")
        value["departure_progress"]["state"] = "failed"
        self.assertEqual(visit_card_status(value, now=100), "相处已结束")
        value.pop("departure_progress")
        value["log"] = [{"seq": i, "actor_id": actor, "kind": "say", "text": words, "at": 100}
                        for i, (actor, words) in enumerate(value["outing"]["farewells"].items(), 1)]
        self.assertEqual(visit_card_status(value, now=100), "已告别归家")


@unittest.skipIf(sync_playwright is None, "Install playwright for browser verification")
class DeparturePageStatusTests(unittest.TestCase):
    setUpClass = browser_fixture.VisitRecordContinuityTests.__dict__["setUpClass"]
    tearDownClass = browser_fixture.VisitRecordContinuityTests.__dict__["tearDownClass"]
    setUp = browser_fixture.VisitRecordContinuityTests.setUp
    visit = staticmethod(browser_fixture.VisitRecordContinuityTests.visit)
    refresh = browser_fixture.VisitRecordContinuityTests.refresh
    seed_history = browser_fixture.VisitRecordContinuityTests.seed_history
    open_record = browser_fixture.VisitRecordContinuityTests.open_record

    def test_page_waits_for_real_receipts_and_shows_legacy_end_neutrally(self):
        now = self.page.evaluate("Date.now()/1000")
        value = self.visit("closing", [], host_id="host", guest_id="guest",
                           ended_at=now - 5, departure_progress={"state": "pending", "deadline": now + 300})
        self.seed_history(value, self.visit("legacy-completed", []))
        closing = self.open_record("closing")
        expect(closing.locator(".visits-record-ending")).to_contain_text("相处已结束，等待告别")
        legacy = self.open_record("legacy-completed")
        expect(legacy.locator(".visits-record-ending")).to_contain_text("相处已结束")
        expect(legacy.locator(".visits-record-ending")).not_to_contain_text("已告别归家")
        value["departure_progress"]["state"] = "done"
        value["log"] = [{"seq": i, "actor_id": actor, "kind": "say", "text": words, "at": now}
                        for i, (actor, words) in enumerate((("host", "纸边下次接着看。"),
                                                            ("guest", "那就下次见。")), 1)]
        value["departure_farewells"] = {row["actor_id"]: {"id": "quote-" + row["actor_id"],
            "actor_id": row["actor_id"], "text": row["text"], "at": row["at"], "log_seq": row["seq"]}
            for row in value["log"]}
        self.seed_history(value)
        closing = self.open_record("closing")
        expect(closing.locator(".visits-record-ending")).to_contain_text("已告别归家")
        expect(closing.locator(".visits-full-transcript").get_by_text("纸边下次接着看。", exact=True)).to_have_count(1)
        expect(closing.locator(".visits-full-transcript").get_by_text("那就下次见。", exact=True)).to_have_count(1)
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
