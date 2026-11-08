"""Manual card snapshots read complete historical facts without sending or saving."""
from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
import json
import unittest
from unittest.mock import AsyncMock

from tests import test_visits_lifecycle_races as fixture


class VisitCardSnapshotTests(unittest.IsolatedAsyncioTestCase):
    pair = fixture.VisitLifecycleRaceTests.pair
    exchange = fixture.VisitLifecycleRaceTests.exchange

    async def asyncSetUp(self):
        await fixture.VisitLifecycleRaceTests.asyncSetUp(self)
        self.visit, _ = self.pair(arrived=True)
        self.visit.update(status="completed", ended_at=self.now,
                          private={"private-owner": {"thought": "PRIVATE-THOUGHT"}},
                          decision={"thought": "PRIVATE-DECISION"},
                          record_card={"state": "skipped", "reason": "historical"})
        self.visit["local"]["persona_key"] = "OLD-PERSONA-KEY"
        self.visit["log"] = [{"seq": index, "actor_id": self.visit["guest_id"],
                              "kind": "say", "text": f"确认的第{index}句", "thought": "PRIVATE-ROW"}
                             for index in range(1, 601)]
        self.visit["outing"] = {"place": "最后的茶室", "activity": "喝茶", "private": "PRIVATE-ROUTE",
                                "route": [{"place": f"地点{index}", "activity": f"确认活动{index}",
                                           "private": "PRIVATE-STOP"} for index in range(101)]}
        self.a.actor_port.render_record_card = AsyncMock(side_effect=AssertionError("must not render"))
        self.a.actor_port.send_record_card = AsyncMock(side_effect=AssertionError("must not send"))
        self.a.actor_port.remember = AsyncMock(side_effect=AssertionError("must not remember"))

    async def test_full_history_and_route_are_allowlisted_independent_copies(self):
        original = copy.deepcopy(self.visit)
        snapshot = await self.a.record_card_snapshot("life-a", self.visit["id"])
        self.assertEqual([row["seq"] for row in snapshot["log"]], list(range(1, 601)))
        self.assertEqual(len(snapshot["outing"]["route"]), 101)
        self.assertNotIn("PRIVATE-", json.dumps(snapshot))
        self.assertNotIn("OLD-PERSONA-KEY", json.dumps(snapshot))
        self.assertNotIn("record_card", snapshot)
        self.assertNotIn("log_prefix", snapshot)
        self.assertNotIn("log_offset", snapshot)
        snapshot["log"][0]["text"] = "edited preview"
        snapshot["outing"]["route"][0]["place"] = "edited route"
        self.assertEqual(self.visit, original)

    async def test_disabled_transport_and_historical_persona_need_no_delivery_permission(self):
        self.a.config = replace(self.a.config, enabled=False, auto_enabled=False)
        self.a._state["enabled_scopes"] = []
        self.a.actor_port.record_card_targets = lambda *_: []
        original_state = copy.deepcopy(self.a._state)
        original_db = copy.deepcopy(self.a.db.state)
        snapshot = await self.a.record_card_snapshot("life-a", self.visit["id"])
        self.assertEqual(len(snapshot["log"]), 600)
        self.assertEqual(self.a._state, original_state)
        self.assertEqual(self.a.db.state, original_db)
        self.a.actor_port.render_record_card.assert_not_awaited()
        self.a.actor_port.send_record_card.assert_not_awaited()
        self.a.actor_port.remember.assert_not_awaited()
        self.assertEqual(self.a._record_card_tasks, {})

    async def test_exact_current_owner_or_its_valid_alias_is_required(self):
        self.a.actor_port.canonical_scope = lambda scope: "life-a" if scope in {"life-a", "shared-alias"} else scope
        snapshot = await self.a.record_card_snapshot("shared-alias", self.visit["id"])
        self.assertEqual(snapshot["scope"], "life-a")
        for scope, visit_id in (("life-b", self.visit["id"]), ("life-a", "missing")):
            with self.subTest(scope=scope, visit_id=visit_id), self.assertRaisesRegex(ValueError, "不属于"):
                await self.a.record_card_snapshot(scope, visit_id)
        self.a.actor_port.canonical_scope = lambda scope: "life-new" if scope == "life-a" else scope
        with self.assertRaisesRegex(ValueError, "不属于"):
            await self.a.record_card_snapshot("life-a", self.visit["id"])

    async def test_owner_is_rechecked_after_waiting_for_journal_lock(self):
        owner = {"selected": "life-a", "life-a": "life-a"}
        self.a.actor_port.canonical_scope = lambda scope: owner.get(scope, scope)
        await self.a._lock.acquire()
        pending = asyncio.create_task(self.a.record_card_snapshot("selected", self.visit["id"]))
        await asyncio.sleep(0)
        owner["selected"] = "life-other"
        self.a._lock.release()
        with self.assertRaisesRegex(ValueError, "不属于"):
            await pending

    async def test_active_and_invalid_selections_are_rejected(self):
        self.visit["status"] = "active"
        with self.assertRaisesRegex(ValueError, "相见结束"):
            await self.a.record_card_snapshot("life-a", self.visit["id"])
        for scope, visit_id in ((None, self.visit["id"]), ([], self.visit["id"]), ("", self.visit["id"]),
                                ("life-a", None), ("life-a", []), ("life-a", "")):
            with self.subTest(scope=scope, visit_id=visit_id), self.assertRaises(ValueError):
                await self.a.record_card_snapshot(scope, visit_id)
