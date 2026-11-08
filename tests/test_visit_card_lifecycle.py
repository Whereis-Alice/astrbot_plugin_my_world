"""Long-card delivery preserves confirmed history and exact-session receipts."""
from __future__ import annotations

import asyncio
import copy
import json
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from tests import test_visits_lifecycle_races as fixture
from core.visits import VisitService
from core.visits.persistence import KEY
from core.visits.record_cards import MAX_RECORD_CARD_FAILURES, RecordCardRetryableError


class VisitCardLifecycleTests(unittest.IsolatedAsyncioTestCase):
    pair = fixture.VisitLifecycleRaceTests.pair
    exchange = fixture.VisitLifecycleRaceTests.exchange

    async def asyncSetUp(self):
        await fixture.VisitLifecycleRaceTests.asyncSetUp(self)
        self.targets = ["qq:GroupMessage:selected"]
        self.rendered, self.sent = [], []
        self.pages = ["file:///card-1.png", "file:///card-2.png"]
        self.a_actor.record_card_targets = lambda scope, visit: list(self.targets)

        async def render(scope, snapshot):
            self.assertFalse(self.a._lock.locked())
            self.rendered.append(copy.deepcopy(snapshot))
            return list(self.pages)

        async def send(scope, snapshot, session, page):
            self.assertFalse(self.a._lock.locked())
            saved = self.a.db.state[KEY]["visits"][snapshot["id"]]["record_card"]
            self.assertEqual(saved["claim"]["session"], session)
            self.sent.append((session, page))

        self.a_actor.render_record_card = render
        self.a_actor.send_record_card = send
        self.released = []
        self.a_actor.release_record_card = lambda pages: self.released.append(list(pages))

    async def finished(self, *, arrived=True, notify=False):
        visit, _ = self.pair(arrived=arrived)
        await self.a._finish(visit, "completed", "已经确认的告别", notify=notify)
        return visit

    async def settle(self, visit, service=None):
        service = service or self.a
        await service._observe_record_card(visit["id"])
        task = service._record_card_tasks.get(visit["id"])
        if task:
            await asyncio.wait_for(asyncio.shield(task), 1)
        await asyncio.sleep(0)

    async def reload(self):
        service = VisitService(self.a.db, self.a.config, self.a_actor, clock=lambda: self.now)
        self.addAsyncCleanup(service.close)
        await service._load()
        return service

    async def test_existing_terminal_history_is_not_sent_when_feature_activates(self):
        visit = await self.finished()
        self.a.db.state[KEY]["visits"][visit["id"]].pop("record_card")
        resumed = await self.reload()
        await self.settle(visit, resumed)
        card = resumed._state["visits"][visit["id"]]["record_card"]
        self.assertEqual((card["state"], card["reason"]), ("skipped", "historical"))
        self.assertEqual(self.rendered, [])
        self.assertEqual(self.sent, [])

    async def test_missing_feature_and_unarrived_calls_never_render(self):
        visit = await self.finished(arrived=False)
        await self.settle(visit)
        self.assertEqual(visit["record_card"]["reason"], "not_arrived")
        self.targets = []
        arrived = await self.finished()
        await self.settle(arrived)
        self.assertEqual(arrived["record_card"]["reason"], "disabled")
        self.targets = ["qq:GroupMessage:newly-enabled"]
        await self.settle(arrived)
        self.assertEqual(self.rendered, [])

    async def test_recovery_save_cannot_queue_historical_endings_later_in_journal(self):
        visit = await self.finished()
        saved = self.a.db.state[KEY]
        historical = saved["visits"][visit["id"]]
        historical.pop("record_card")
        interrupted = copy.deepcopy(historical)
        interrupted.update(id="b" * 32, status="active", model_pending="interrupted-inference")
        interrupted.pop("ended_at", None)
        saved["visits"] = {interrupted["id"]: interrupted, historical["id"]: historical}
        resumed = await self.reload()
        history = resumed._state["visits"][historical["id"]]["record_card"]
        recovered = resumed._state["visits"][interrupted["id"]]
        self.assertEqual((history["state"], history["reason"]), ("skipped", "historical"))
        self.assertEqual((recovered["status"], recovered["record_card"]["state"]), ("interrupted", "pending"))

    async def test_legacy_public_rows_keep_composite_fields_without_private_details(self):
        visit, _ = self.pair(arrived=True)
        visit["log"] = [
            {"actor_id": visit["host_id"], "say": "旧版对白", "action": "旧版打算", "result": "确认的结果",
             "speaker": "友邻", "thought": "PRIVATE-SECRET", "decision": {"thought": "HIDDEN"}},
            {"kind": "action", "text": "旧版公开行动"},
            {"kind": "outcome", "text": "旧版公开结果"},
            {"kind": "scene", "text": "旧版共同场景"},
        ]
        await self.a._finish(visit, "completed", notify=False)
        await self.settle(visit)
        rows = self.rendered[0]["log"]
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["say"], "旧版对白")
        self.assertEqual(rows[0]["result"], "确认的结果")
        self.assertNotIn("PRIVATE-SECRET", json.dumps(rows))

    async def test_freezes_exact_targets_and_rechecks_removed_sessions(self):
        self.targets = ["qq:GroupMessage:selected", "qq:GroupMessage:removed", "qq:GroupMessage:selected"]
        visit = await self.finished()
        self.targets = ["qq:GroupMessage:selected", "qq:GroupMessage:new"]
        await self.settle(visit)
        self.assertEqual(self.sent, [("qq:GroupMessage:selected", page) for page in self.pages])
        self.assertEqual(set(visit["record_card"]["targets"]), {"qq:GroupMessage:selected", "qq:GroupMessage:removed"})
        self.assertEqual(visit["record_card"]["targets"]["qq:GroupMessage:removed"]["state"], "skipped")

    async def test_renders_complete_public_history_instead_of_wire_tail(self):
        visit, _ = self.pair(arrived=True)
        visit["manual"] = True
        visit["log"] = []
        for index in range(230):
            self.a._append(visit, visit["host_id"], "say", "第%d条已确认发言" % index)
        visit["private"] = {"thought": "PRIVATE-SECRET"}
        visit["local"]["persona_key"] = "PERSONA-SECRET"
        visit["action_plans"] = {"hidden": "PLAN-SECRET"}
        visit["outbox"] = {"secret": "AUTH-SECRET"}
        self.a._append(visit, visit["host_id"], "thought", "THOUGHT-SECRET")
        visit["log"][0]["private"] = "ROW-SECRET"
        visit["outing"] = {"route": [{"place": str(index), "activity": "喝茶", "started_at": self.now,
                                       "until": self.now + 60, "thought": "ROUTE-SECRET"} for index in range(75)],
                           "farewells": {"hidden": "FAREWELL-SECRET"}}
        await self.a._finish(visit, "completed", "确认告别", notify=False)
        await self.settle(visit)
        snapshot = self.rendered[0]
        self.assertEqual(len(snapshot["log"]), 230)
        self.assertEqual(len(snapshot["outing"]["route"]), 75)
        self.assertEqual(snapshot["log"][0]["text"], "第0条已确认发言")
        self.assertNotIn("SECRET", json.dumps(snapshot))
        self.assertNotIn("record_card", snapshot)
        self.assertEqual(visit["record_card"]["state"], "done")
        self.assertEqual(self.released, [self.pages])
        self.assertNotIn("record_card", self.a._wire(visit))

    async def test_waits_for_goodbye_confirmation_and_includes_late_facts(self):
        visit = await self.finished(notify=True)
        await self.settle(visit)
        self.assertFalse(self.rendered)
        self.a._append(visit, visit["host_id"], "say", "最后确认的一句道别")
        visit.pop("end_pending")
        await self.a._save()
        await self.settle(visit)
        self.assertEqual(self.rendered[0]["log"][-1]["text"], "最后确认的一句道别")

    async def test_slow_render_keeps_tick_free_and_single_flight(self):
        visit = await self.finished()
        newer = self.a._new_visit("life-a", self.peer, self.local, "b" * 32,
                                  "guest", "下一次联系", False)
        newer["status"] = "offered"
        self.a._state["visits"][newer["id"]] = newer
        entered, release = asyncio.Event(), asyncio.Event()
        self.addCleanup(release.set)
        original = self.a_actor.render_record_card

        async def blocked(scope, snapshot):
            entered.set()
            await release.wait()
            return await original(scope, snapshot)

        self.a_actor.render_record_card = blocked
        advanced = []

        async def advance(visit_id):
            advanced.append(visit_id)

        self.a._advance, self.a._automatic = advance, AsyncMock()
        await asyncio.wait_for(self.a.tick(), 1)
        await asyncio.wait_for(entered.wait(), 1)
        task = self.a._record_card_tasks[visit["id"]]
        self.assertFalse(self.a._tick_lock.locked())
        for _ in range(3):
            await asyncio.wait_for(self.a.tick(), 1)
        self.assertEqual(advanced, [newer["id"]] * 4)
        self.assertIs(self.a._record_card_tasks[visit["id"]], task)
        release.set()
        await asyncio.wait_for(asyncio.shield(task), 1)
        self.assertEqual(len(self.rendered), 1)

    async def test_retry_backoff_survives_reload_and_is_bounded(self):
        visit = await self.finished()
        self.a_actor.render_record_card = AsyncMock(side_effect=RuntimeError("renderer unavailable"))
        await self.settle(visit)
        card = visit["record_card"]
        self.assertEqual(card["failures"], 1)
        resumed = await self.reload()
        await self.settle(visit, resumed)
        self.assertEqual(self.a_actor.render_record_card.await_count, 1)
        for _ in range(MAX_RECORD_CARD_FAILURES - 1):
            current = resumed._state["visits"][visit["id"]]
            self.now = current["record_card"]["next_at"]
            await self.settle(visit, resumed)
        current = resumed._state["visits"][visit["id"]]
        self.assertEqual(current["record_card"]["state"], "failed")
        self.assertEqual(self.a_actor.render_record_card.await_count, MAX_RECORD_CARD_FAILURES)
        self.assertFalse(self.sent)

    async def test_definite_pre_send_failure_resumes_after_acknowledged_pages(self):
        visit = await self.finished()
        original = self.a_actor.send_record_card
        failed = False

        async def flaky(scope, snapshot, session, page):
            nonlocal failed
            if page == self.pages[1] and not failed:
                failed = True
                raise RecordCardRetryableError("adapter missing before send")
            await original(scope, snapshot, session, page)

        self.a_actor.send_record_card = flaky
        await self.settle(visit)
        self.assertEqual(self.sent, [(self.targets[0], self.pages[0])])
        self.assertEqual(visit["record_card"]["targets"][self.targets[0]]["next_page"], 1)
        self.now = visit["record_card"]["next_at"]
        resumed = await self.reload()
        await self.settle(visit, resumed)
        self.assertEqual(self.sent, [(self.targets[0], page) for page in self.pages])
        self.assertEqual(resumed._state["visits"][visit["id"]]["record_card"]["state"], "done")

    async def test_ambiguous_send_is_not_replayed_and_other_target_still_receives(self):
        self.targets.append("qq:GroupMessage:other")
        visit = await self.finished()
        original = self.a_actor.send_record_card

        async def ambiguous(scope, snapshot, session, page):
            if session == self.targets[0]:
                raise RuntimeError("platform may already have accepted")
            await original(scope, snapshot, session, page)

        self.a_actor.send_record_card = ambiguous
        await self.settle(visit)
        self.assertEqual(self.sent, [(self.targets[1], page) for page in self.pages])
        self.assertEqual(visit["record_card"]["targets"][self.targets[0]]["state"], "uncertain")
        resumed = await self.reload()
        await self.settle(visit, resumed)
        self.assertEqual(len(self.sent), 2)

    async def test_cancelled_send_retains_claim_and_reload_does_not_duplicate(self):
        visit = await self.finished()
        entered = asyncio.Event()
        original = self.a_actor.send_record_card

        async def interrupted(scope, snapshot, session, page):
            if page == self.pages[1]:
                entered.set()
                await asyncio.Event().wait()
            await original(scope, snapshot, session, page)

        self.a_actor.send_record_card = interrupted
        await self.a._observe_record_card(visit["id"])
        await asyncio.wait_for(entered.wait(), 1)
        await self.a.close()
        self.assertEqual(self.released, [self.pages])
        self.assertEqual(len(self.sent), 1)
        self.assertFalse(self.a._record_card_tasks)
        self.assertEqual(self.a.db.state[KEY]["visits"][visit["id"]]["record_card"]["claim"]["page"], 1)
        self.a_actor.send_record_card = original
        resumed = await self.reload()
        await self.settle(visit, resumed)
        card = resumed._state["visits"][visit["id"]]["record_card"]
        self.assertEqual(card["state"], "uncertain")
        self.assertEqual(len(self.sent), 1)

    async def test_send_timeout_is_ambiguous_and_releases_images(self):
        visit = await self.finished()

        async def blocked(*args):
            await asyncio.Event().wait()

        self.a_actor.send_record_card = blocked
        with patch("core.visits.record_cards.RECORD_CARD_SEND_TIMEOUT", .01):
            await self.settle(visit)
        self.assertEqual(visit["record_card"]["state"], "uncertain")
        self.assertEqual(self.released, [self.pages])
        self.assertFalse(self.a._record_card_tasks)

    async def test_claim_must_be_persisted_before_any_send(self):
        visit = await self.finished()
        original, failed = self.a.db.call, False

        async def flaky(operation, key, value):
            nonlocal failed
            card = value.get("visits", {}).get(visit["id"], {}).get("record_card", {}) if isinstance(value, dict) else {}
            if operation == "kv_set" and card.get("claim") and not failed:
                failed = True
                raise OSError("storage unavailable before send")
            return await original(operation, key, value)

        self.a.db.call = flaky
        await self.settle(visit)
        self.assertEqual(self.sent, [])
        self.assertNotIn("claim", visit["record_card"])
        self.now = visit["record_card"]["next_at"]
        await self.settle(visit)
        self.assertEqual(len(self.sent), 2)

    async def test_late_fact_discards_render_before_first_delivery(self):
        visit = await self.finished()
        original = self.a_actor.render_record_card

        async def changed(scope, snapshot):
            pages = await original(scope, snapshot)
            self.a._append(visit, visit["host_id"], "say", "渲染期间确认的道别")
            self.a_actor.render_record_card = original
            return pages

        self.a_actor.render_record_card = changed
        await self.settle(visit)
        self.assertFalse(self.sent)
        await self.settle(visit)
        self.assertEqual(self.rendered[-1]["log"][-1]["text"], "渲染期间确认的道别")
        self.assertEqual(len(self.sent), 2)

    async def test_changed_pagination_after_partial_delivery_is_not_mixed(self):
        visit = await self.finished()
        original = self.a_actor.send_record_card

        async def partial(scope, snapshot, session, page):
            if page == self.pages[1]:
                raise RecordCardRetryableError("not sent")
            await original(scope, snapshot, session, page)

        self.a_actor.send_record_card = partial
        await self.settle(visit)
        self.pages.append("file:///card-3.png")
        self.now = visit["record_card"]["next_at"]
        self.a_actor.send_record_card = original
        await self.settle(visit)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(visit["record_card"]["state"], "failed")

    async def test_pending_cards_survive_history_trim_without_blocking_invites(self):
        self.a.config = replace(self.a.config, history_keep=1)
        first = await self.finished()
        first["remembered"] = True
        second = copy.deepcopy(first)
        second.update(id="b" * 32, ended_at=self.now + 1)
        second.pop("record_card")
        self.a._state["visits"][second["id"]] = second
        await self.a._save()
        self.assertIn(first["id"], self.a._state["visits"])
        self.assertFalse(self.a._settlement_pending(first))
        self.assertFalse(self.a._settlement_backlog_full("life-a"))
        await self.settle(first)
        self.assertNotIn(first["id"], self.a._state["visits"])

    async def test_direct_protocol_terminal_transition_is_admitted_on_save(self):
        visit, _ = self.pair(arrived=True)
        visit.update(status="completed", ended_at=self.now)
        await self.a._save()
        self.assertEqual(visit["record_card"]["state"], "pending")
        await self.settle(visit)
        self.assertEqual(visit["record_card"]["state"], "done")
