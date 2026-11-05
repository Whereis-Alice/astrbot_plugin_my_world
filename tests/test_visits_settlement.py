"""Completed trips survive trimming and a failing memory store independently."""
import asyncio
import copy
from dataclasses import replace
import unittest

from tests import test_visits_protocol as protocol
from core.visits import VisitService
from core.visits.persistence import KEY


class VisitSettlementTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = protocol.VisitProtocolTests.asyncSetUp
    advance = protocol.VisitProtocolTests.advance

    async def completed_pending(self, direction="incoming"):
        pending = await self.a.invite("life-a", self.peer, direction=direction)
        for _ in range(24):
            self.now += 65
            await self.a.tick()
            visit = self.a._state["visits"][pending["id"]]
            if visit["status"] == "completed":
                return visit
            await self.b.tick()
        self.fail("The real two-server trip did not finish")

    async def test_new_rejection_cannot_trim_unwritten_memory_or_unsent_farewell(self):
        self.a.config = replace(self.a.config, history_keep=1)
        visit = await self.completed_pending()
        old = visit["id"]
        self.assertNotIn(old, self.a_actor.memories)
        self.assertTrue(visit.get("end_pending"))
        self.a_actor.accept = False
        self.now += 1
        await self.a.invite("life-a", self.peer)
        self.assertIn(old, self.a._state["visits"])
        await self.a.tick()
        self.assertIn(old, self.a_actor.memories)
        self.assertIn(old, self.a._state["visits"])
        self.assertNotIn("end_pending", self.a._state["visits"][old])
        self.assertEqual(self.b._state["visits"][old]["status"], "completed")

    async def test_already_written_memory_still_retains_pending_farewell(self):
        self.a.config = replace(self.a.config, history_keep=1)
        visit = await self.completed_pending()
        old = visit["id"]
        await self.a._remember(old)
        self.assertTrue(visit["remembered"])
        self.a_actor.accept = False
        self.now += 1
        await self.a.invite("life-a", self.peer)
        self.assertIn(old, self.a._state["visits"])
        await self.a.tick()
        self.assertIn(old, self.a._state["visits"])
        self.assertNotIn("end_pending", self.a._state["visits"][old])

    async def test_one_memory_failure_does_not_block_the_next_real_trip(self):
        visit = await self.completed_pending()
        old = visit["id"]
        remember = self.a_actor.remember
        failed_calls = []

        async def failing(scope, snapshot):
            if snapshot["id"] == old:
                failed_calls.append(self.now)
                raise RuntimeError("isolated memory backend failure")
            await remember(scope, snapshot)

        self.a_actor.remember = failing
        # End the old shared visit before calling again; its independent memory
        # failure can remain pending without making the friend falsely available.
        await self.a._notify_end(old)
        self.assertIsNone(self.b.active("life-b"))
        self.assertNotIn(old, self.a_actor.memories)
        pending = await self.a.invite("life-a", self.peer)
        await self.advance()
        newer = self.a._state["visits"][pending["id"]]
        self.assertEqual(newer["status"], "completed")
        self.assertIn(newer["id"], self.a_actor.memories)
        self.assertNotIn(old, self.a_actor.memories)
        self.assertLess(len(failed_calls), 10)
        self.assertEqual(visit["remember_error"], "RuntimeError")
        self.a_actor.remember = remember
        self.now = visit["remember_retry_at"]
        await self.a.tick()
        self.assertIn(old, self.a_actor.memories)

    async def test_retry_deadline_survives_reload_and_does_not_spin(self):
        visit = await self.completed_pending()
        attempts = []
        remember = self.a_actor.remember

        async def failing(scope, snapshot):
            attempts.append(self.now)
            raise RuntimeError("backend unavailable")

        self.a_actor.remember = failing
        await self.a._remember(visit["id"])
        retry_at = visit["remember_retry_at"]
        await self.a.close()
        resumed = VisitService(self.a_db, replace(self.a.config, listen_port=0), self.a_actor,
                               clock=lambda: self.now)
        self.addAsyncCleanup(resumed.close)
        await resumed.start()
        self.assertEqual(resumed._state["visits"][visit["id"]]["remember_retry_at"], retry_at)
        for _ in range(3):
            await resumed._remember(visit["id"])
        self.assertEqual(len(attempts), 1)
        self.a_actor.remember = remember
        self.now = retry_at
        await resumed._remember(visit["id"])
        self.assertTrue(resumed._state["visits"][visit["id"]]["remembered"])

    async def test_duplicate_write_is_single_flight_and_cancellation_allows_retry(self):
        visit = await self.completed_pending()
        entered, release = asyncio.Event(), asyncio.Event()
        attempts = []
        remember = self.a_actor.remember

        async def blocked(scope, snapshot):
            attempts.append(snapshot["id"])
            entered.set()
            await release.wait()
            await remember(scope, snapshot)

        self.a_actor.remember = blocked
        task = asyncio.create_task(self.a._remember(visit["id"]))
        await entered.wait()
        await self.a._remember(visit["id"])
        self.assertEqual(len(attempts), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertNotIn("remember_retry_at", visit)
        release.set()
        await self.a._remember(visit["id"])
        self.assertEqual(len(attempts), 2)
        self.assertIn(visit["id"], self.a_actor.memories)

    async def test_late_confirmed_facts_do_not_inherit_old_memory_backoff(self):
        visit = await self.completed_pending(direction="outgoing")
        await self.a._notify_end(visit["id"])
        remote = self.b._state["visits"][visit["id"]]
        self.b._append(remote, remote["host_id"], "say", "最后补上一句已确认的告别")
        visit.update(remember_failures=7, remember_error="RuntimeError", remember_retry_at=self.now + 3600)
        await self.a._save()
        await self.b._send(remote["id"], {"op": "end", "visit": self.b._wire(remote)})
        self.assertNotIn("remember_retry_at", visit)
        await self.a._remember(visit["id"])
        self.assertIn("最后补上一句", str(self.a_actor.memories[visit["id"]]["log"]))

    async def test_storage_backlog_blocks_only_new_invites_without_spending_daily_chance(self):
        template = await self.completed_pending()
        await self.a._notify_end(template["id"])
        for index in range(32):
            pending = copy.deepcopy(template)
            pending["id"] = f"pending-{index}"
            self.a._state["visits"][pending["id"]] = pending
        self.a.config = replace(self.a.config, auto_enabled=True)
        before = copy.deepcopy(self.a._state["draws"])
        with self.assertRaisesRegex(ValueError, "收尾"):
            await self.a.invite("life-a", self.peer, automatic=True)
        self.assertEqual(self.a._state["draws"], before)
        # The other server also cannot consume a model call or reserve a trip here.
        response = await self.b.invite("life-b", next(iter(self.b._state["peers"])))
        self.assertEqual(response["status"], "declined")
        self.assertIsNone(self.a.active("life-a"))
        for visit in self.a._state["visits"].values():
            visit.pop("end_pending", None)
            visit["remembered"] = True
        await self.a._save()
        self.a_actor.accept = False
        await self.a.invite("life-a", self.peer, automatic=True)
        self.assertEqual(self.a._state["draws"]["life-a"]["count"], 1)
        stored = await self.a_db.call("kv_get", KEY)
        self.assertLessEqual(len(stored["visits"]), self.a.config.history_keep + 1)

    async def settlement_save_failure(self, fail_memory):
        old = await self.completed_pending()
        await self.a._notify_end(old["id"])
        pending = await self.a.invite("life-a", self.peer)
        remember, db_call = self.a_actor.remember, self.a_db.call
        lost = []

        async def remembering(scope, snapshot):
            if fail_memory and snapshot["id"] == old["id"]:
                raise RuntimeError("memory unavailable")
            await remember(scope, snapshot)

        async def lose_one_ack(method, *args, **kwargs):
            if method == "kv_set" and not lost:
                lost.append(method)
                raise OSError("one failed settlement journal save")
            return await db_call(method, *args, **kwargs)

        self.a_actor.remember, self.a_db.call = remembering, lose_one_ack
        await self.advance()
        self.assertEqual(len(lost), 1)
        self.assertEqual(self.a._state["visits"][pending["id"]]["status"], "completed")
        self.assertIn(pending["id"], self.a_actor.memories)
        self.assertNotIn(old["id"], self.a._remembering)

    async def test_failure_to_save_memory_backoff_cannot_abort_other_trip(self):
        await self.settlement_save_failure(fail_memory=True)

    async def test_failure_to_save_written_memory_receipt_cannot_abort_other_trip(self):
        await self.settlement_save_failure(fail_memory=False)
