"""Independent recovery checks: bounded I/O, exact ownership and isolated failures."""
from __future__ import annotations

import asyncio
import copy
import unittest

from tests import fake_astrbot
fake_astrbot.install()

from core.visit_chat_bridge import VisitChatOwner, reconcile_visit_chat, record_visit_chat  # noqa: E402
from tests import test_visit_chat_bridge_shared_life_20261113 as fixture  # noqa: E402


class ReconcileReviewTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixture.VisitChatBridgeTests.setUp
    owner = fixture.VisitChatBridgeTests.owner

    async def saved_note(self):
        await record_visit_chat(self.engine, self.owner(), kind="read", text="这句确实读到过", source_id="saved")
        self.wakes.clear()

    async def test_failed_reads_keep_the_thirty_second_retry_bound(self):
        await self.saved_note()
        reads = []

        async def broken(op, key, value=None):
            reads.append(op)
            raise OSError("temporary journal read failure")

        self.engine.db.call = broken
        for _ in range(3):
            await reconcile_visit_chat(self.engine)
        self.assertEqual(reads, ["kv_get"])
        self.clock += 29
        await reconcile_visit_chat(self.engine)
        self.assertEqual(len(reads), 1)
        self.clock += 1
        await reconcile_visit_chat(self.engine)
        self.assertEqual(len(reads), 2)

    async def test_concurrent_passes_use_one_read_and_recheck_changed_persona(self):
        await self.saved_note()
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.engine.db.call
        reads = []

        async def blocked(op, key, value=None):
            reads.append(op)
            entered.set()
            await release.wait()
            return await original(op, key, value)

        self.engine.db.call = blocked
        task = asyncio.create_task(reconcile_visit_chat(self.engine))
        await asyncio.wait_for(entered.wait(), 1)
        try:
            await asyncio.wait_for(reconcile_visit_chat(self.engine), 1)
            self.assertEqual(reads, ["kv_get"])
            self.visit["local"]["persona_key"] = "new-persona"
        finally:
            release.set()
            await task
        self.assertEqual(self.wakes, [])

    async def test_end_time_changed_during_read_excludes_late_note(self):
        await self.saved_note()
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.engine.db.call

        async def blocked(op, key, value=None):
            entered.set()
            await release.wait()
            return await original(op, key, value)

        self.engine.db.call = blocked
        task = asyncio.create_task(reconcile_visit_chat(self.engine))
        await asyncio.wait_for(entered.wait(), 1)
        self.visit.update(status="completed", ended_at=1090.)
        release.set()
        await task
        self.assertEqual(self.wakes, [])

    async def test_old_failed_wake_does_not_starve_another_life(self):
        await self.saved_note()
        other = copy.deepcopy(self.visit)
        other.update(id="other-trip", scope="test:GroupMessage:2", guest_id="another:actor")
        other["local"] = {"actor_id": "actor", "persona_key": "another-persona"}
        self.visits[other["id"]] = other
        owner = VisitChatOwner(other["id"], other["scope"], other["guest_id"], "another-persona",
                               other["scope"], 1000., self.clock)
        await record_visit_chat(self.engine, owner, kind="read", text="另一份生活真正读到的消息", source_id="other")
        self.wakes.clear()

        async def wake(**payload):
            if payload["visit_id"] == self.visit["id"]:
                raise OSError("one visit notification failed")
            self.wakes.append(payload)
            other["local_experience_seq"] = payload["seq"]
            return True

        self.service.wake_local_experience = wake
        await reconcile_visit_chat(self.engine)
        self.assertEqual([row["visit_id"] for row in self.wakes], [other["id"]])
        self.assertEqual(other["local_experience_seq"], 1)

    async def test_cancelling_older_pass_does_not_clear_newer_claim(self):
        await self.saved_note()
        entered = asyncio.Event()
        original = self.engine.db.call
        reads = []

        async def first_blocked(op, key, value=None):
            reads.append(op)
            if len(reads) == 1:
                entered.set()
                await asyncio.Future()
            return await original(op, key, value)

        self.engine.db.call = first_blocked
        older = asyncio.create_task(reconcile_visit_chat(self.engine))
        await asyncio.wait_for(entered.wait(), 1)
        self.clock += 31
        await reconcile_visit_chat(self.engine)
        newer_claim = self.engine._visit_chat_reconciled_at
        older.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await older
        self.assertEqual(self.engine._visit_chat_reconciled_at, newer_claim)
        await reconcile_visit_chat(self.engine)
        self.assertEqual(reads, ["kv_get", "kv_get"])

    async def test_reload_recovers_exact_old_receipt_once_and_keeps_peer_payload_private(self):
        await self.saved_note()
        self.visit.update(status="completed", ended_at=1110., remembered=True)

        async def recovered(**payload):
            self.wakes.append(payload)
            self.visit["local_experience_seq"] = payload["seq"]
            self.visit.pop("remembered", None)
            return True

        self.service.wake_local_experience = recovered
        await reconcile_visit_chat(self.engine)
        self.assertEqual(len(self.wakes), 1)
        self.assertNotIn("text", self.wakes[0])
        self.assertEqual(self.wakes[0]["persona_key"], "my-persona")
        self.assertNotIn("remembered", self.visit)
        self.clock += 31
        await reconcile_visit_chat(self.engine)
        self.assertEqual(len(self.wakes), 1)


if __name__ == "__main__":
    unittest.main()
