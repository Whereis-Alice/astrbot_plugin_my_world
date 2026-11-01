"""Final settlement retains the actual reason for an already confirmed visit."""
from __future__ import annotations

import asyncio
import copy
import unittest

from tests import test_visits_lifecycle_races as fixture
from core.visits import VisitService
from core.visits.decisions import VisitDecisionError


class VisitEndReasonReviewTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.VisitLifecycleRaceTests.asyncSetUp
    pair = fixture.VisitLifecycleRaceTests.pair
    exchange = fixture.VisitLifecycleRaceTests.exchange

    async def assert_settled_reason(self, guest, host, reason, status):
        await self.a._notify_end(guest["id"])
        self.assertEqual(guest["status"], status)
        self.assertEqual(host["status"], status)
        self.assertEqual(guest["end_reason"], reason)
        self.assertEqual(host["end_reason"], reason)
        await self.a._remember(guest["id"])
        await self.b._remember(host["id"])
        self.assertEqual(self.a_actor.memories[guest["id"]]["end_reason"], reason)
        self.assertEqual(self.b_actor.memories[host["id"]]["end_reason"], reason)

    async def test_three_failed_guest_thoughts_keep_the_real_failure_in_both_memories(self):
        guest, host = self.pair(arrived=True)

        async def failing(scope, phase, visit):
            raise VisitDecisionError("timeout")

        self.a_actor.decide = failing
        for attempt in range(3):
            await self.a._decide(guest["id"], "turn")
            if attempt < 2:
                self.now = guest["model_retry"]["next_at"]
        self.assertEqual(guest["status"], "interrupted")
        await self.assert_settled_reason(guest, host, str(VisitDecisionError("timeout")), "interrupted")

    async def test_explicit_return_home_retains_the_human_request(self):
        guest, host = self.pair(arrived=True)
        result = await self.a.return_home("life-a")
        self.assertEqual(result["status"], "interrupted")
        await self.assert_settled_reason(guest, host, "真人要求告别归家", "interrupted")

    async def test_completed_guest_goodbye_retains_its_reason(self):
        guest, host = self.pair(arrived=True)
        reason = "想回家照顾窗边的花，下次再来喝茶"
        await self.a._finish(guest, "completed", reason)
        await self.assert_settled_reason(guest, host, reason, "completed")

    async def test_empty_guest_reason_keeps_the_existing_fallback(self):
        guest, host = self.pair(arrived=True)
        await self.a._finish(guest, "interrupted")
        await self.assert_settled_reason(guest, host, "对方已告别或结束本次联系", "interrupted")

    async def test_legacy_goodbye_without_snapshot_keeps_the_existing_fallback(self):
        guest, host = self.pair(arrived=True)
        async with self.b._lock:
            await self.b._inbox(self.reverse, self.remote, {"op": "end", "id": guest["id"]})
        self.assertEqual(host["status"], "interrupted")
        self.assertEqual(host["end_reason"], "对方已告别或结束本次联系")

    async def test_late_guest_reason_does_not_rewrite_a_settled_host_reason(self):
        guest, host = self.pair(arrived=True)
        reason = "约好的相处已结束，双方告别归家"
        await self.b._finish(host, "completed", reason, notify=False)
        await self.a._finish(guest, "interrupted", "迟到的本地联系异常")
        await self.assert_settled_reason(guest, host, reason, "completed")

    async def reload_host(self, visit):
        resumed = VisitService(self.b.db, self.b.config, self.b_actor, clock=lambda: self.now)
        self.addAsyncCleanup(resumed.close)
        await resumed._load()
        return resumed._state["visits"][visit["id"]]

    async def test_guest_goodbye_discards_an_inflight_scene_without_changing_settled_memory(self):
        guest, host = self.pair(arrived=True)
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.b_actor.decide

        async def blocked(scope, phase, snapshot):
            entered.set()
            await release.wait()
            return await original(scope, phase, snapshot)

        self.b_actor.decide = blocked
        task = asyncio.create_task(self.b._scene(host["id"], "event_offer"))
        await entered.wait()
        try:
            async with self.b._lock:
                await self.b._inbox(self.reverse, self.remote, {
                    "op": "step", "id": host["id"], "seq": len(host["log"]), "calls": 1,
                    "decision": {"say": "今天先回家，下次见。", "leave": True},
                })
                await self.b._save()
        finally:
            release.set()
            await task
        self.assertEqual(host["status"], "completed")
        self.assertNotIn("model_pending", host)
        self.assertNotIn("model_retry", host)
        self.assertNotIn("state_retry", host)
        self.assertFalse(any(row["kind"] == "event" for row in host["log"]))
        await self.b._remember(host["id"])
        reloaded = await self.reload_host(host)
        self.assertEqual(reloaded["status"], "completed")
        self.assertEqual(reloaded["end_reason"], "访客已告别归家")
        self.assertEqual(reloaded["log"], self.b_actor.memories[guest["id"]]["log"])
        self.assertEqual(reloaded["status"], self.b_actor.memories[guest["id"]]["status"])
        self.assertTrue(reloaded["remembered"])

    async def test_legacy_terminal_model_markers_do_not_rewrite_final_facts_or_memory(self):
        for status in ("completed", "interrupted", "expired"):
            with self.subTest(status=status):
                _, host = self.pair(arrived=True)
                reason = "已经确认的原始结束原因：" + status
                await self.b._finish(host, status, reason, notify=False)
                await self.b._remember(host["id"])
                host.update(model_pending="stale-inference", model_retry={"phase": "scene"},
                            state_retry={"attempts": 1, "next_at": self.now + 30},
                            proposal_stage="pending")
                await self.b._save()
                memory = copy.deepcopy(self.b_actor.memories[host["id"]])
                reloaded = await self.reload_host(host)
                self.assertEqual(reloaded["status"], status)
                self.assertEqual(reloaded["end_reason"], reason)
                self.assertEqual(reloaded["log"], memory["log"])
                self.assertTrue(reloaded["remembered"])
                self.assertEqual(reloaded["proposal_stage"], "done")
                for key in ("model_pending", "model_retry", "state_retry"):
                    self.assertNotIn(key, reloaded)
                self.assertEqual(self.b_actor.memories[host["id"]], memory)

    async def test_legacy_terminal_proposal_marker_cannot_reopen_completed_visit(self):
        _, host = self.pair(arrived=True)
        await self.b._finish(host, "completed", "相处完成，双方已告别", notify=False)
        host["proposal_stage"] = "pending"
        await self.b._save()
        reloaded = await self.reload_host(host)
        self.assertEqual(reloaded["status"], "completed")
        self.assertEqual(reloaded["end_reason"], host["end_reason"])
        self.assertEqual(reloaded["proposal_stage"], "done")

    def arm_waits(self, visit):
        visit.update(model_pending="stale-inference", model_retry={"phase": "scene"},
                     state_retry={"attempts": 1, "next_at": self.now + 30})

    def assert_no_decision_waits(self, visit):
        for key in ("model_pending", "model_retry", "state_retry"):
            self.assertNotIn(key, visit)

    async def test_received_host_goodbye_clears_local_model_and_state_waits(self):
        guest, host = self.pair(arrived=True)
        await self.b._finish(host, "completed", "相处已结束，下次见", notify=False)
        self.arm_waits(guest)
        async with self.a._lock:
            await self.a._inbox(self.peer, self.local, {
                "op": "end", "id": guest["id"], "visit": self.b._wire(host),
            })
        self.assertEqual(guest["status"], "completed")
        self.assertEqual(guest["end_reason"], host["end_reason"])
        self.assert_no_decision_waits(guest)

    async def test_host_sync_of_guest_terminal_clears_waits_and_preserves_pending_goodbye(self):
        guest, host = self.pair(arrived=True)
        await self.a._finish(guest, "interrupted", "需要先处理家里的事", notify=False)
        self.arm_waits(host)
        notice = {"attempts": 1, "next_at": self.now + 60}
        outgoing = {"message_id": "pending-goodbye", "payload": {"op": "end"}}
        host.update(end_pending=copy.deepcopy(notice), outbox=copy.deepcopy(outgoing))
        async with self.b._lock:
            await self.b._inbox(self.reverse, self.remote, {
                "op": "sync", "id": host["id"], "visit": self.a._wire(guest), "calls": 1,
            })
        self.assertEqual(host["status"], "interrupted")
        self.assertEqual(host["end_reason"], guest["end_reason"])
        self.assert_no_decision_waits(host)
        self.assertEqual(host["end_pending"], notice)
        self.assertEqual(host["outbox"], outgoing)

    async def test_guest_sync_of_host_terminal_preserves_queued_goodbye(self):
        guest, host = self.pair(arrived=True)
        await self.b._finish(host, "completed", "双方告别归家", notify=False)
        self.arm_waits(guest)
        notice = {"attempts": 1, "next_at": self.now + 60}
        outgoing = {"message_id": "pending-goodbye", "payload": {"op": "end"}}
        guest.update(end_pending=copy.deepcopy(notice), outbox=copy.deepcopy(outgoing))
        self.a._synchronize(guest, self.b._wire(host), guest["host_id"])
        self.assertEqual(guest["status"], "completed")
        self.assert_no_decision_waits(guest)
        self.assertEqual(guest["end_pending"], notice)
        self.assertEqual(guest["outbox"], outgoing)
