"""Deterministic final-review races without network calls or a live model."""
from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace
import unittest

from tests import fake_astrbot
fake_astrbot.install()

from core.visits.flow import VisitFlow  # noqa: E402
from core.visits.persistence import VisitJournal  # noqa: E402


class MemoryActor:
    def __init__(self):
        self.decisions = []
        self.memories = []
        self.decide_hook = None
        self.remember_hook = None
        self.review = {"reflection": "刚才各自慢慢做事的时间，我想留在心里。"}

    async def decide(self, scope, phase, snapshot):
        self.decisions.append(copy.deepcopy(snapshot))
        if self.decide_hook:
            await self.decide_hook()
        return copy.deepcopy(self.review)

    async def remember(self, scope, snapshot):
        if self.remember_hook:
            await self.remember_hook()
        self.memories.append(copy.deepcopy(snapshot))


class MemoryJournal(VisitJournal):
    _retain_private = staticmethod(VisitFlow._retain_private)

    def __init__(self, actor, clock, visit):
        super().__init__(None, SimpleNamespace(timeout_seconds=2), actor, clock, lambda: .9)
        self._state = {"visits": {visit["id"]: visit}}
        self.saved = copy.deepcopy(self._state)
        self.fail_saves = 0

    async def _save(self):
        if self.fail_saves:
            self.fail_saves -= 1
            raise OSError("simulated durable marker write failure")
        self.saved = copy.deepcopy(self._state)


class FinishReviewRaceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 1000.0
        self.visit = {"id": "finish-race", "scope": "life-isla", "status": "completed",
                      "created_at": 800.0, "arrived_at": 900.0, "ended_at": 990.0,
                      "end_reason": "已经告别", "local_experience_seq": 1,
                      "private": {}, "private_moments": [], "shared_activity": {},
                      "log": [{"seq": 1, "actor_id": "host:alice", "kind": "say",
                               "text": "今天先到这里。", "at": 989.0}]}
        self.actor = MemoryActor()
        self.journal = MemoryJournal(self.actor, lambda: self.now, self.visit)

    def add_late_fact(self):
        self.visit["log"].append({"seq": len(self.visit["log"]) + 1, "actor_id": "host:alice",
                                  "kind": "say", "text": "最后一句迟到的告别原话。", "at": 990.0})
        self.visit["local_experience_seq"] += 1

    async def test_failed_claim_does_not_pay_for_obsolete_review_before_fresh_retry(self):
        # Queue a real state update between snapshot acquisition and review claim.
        await self.journal._lock.acquire()
        worker = asyncio.create_task(self.journal._write_memory(self.visit["id"]))
        await asyncio.sleep(0)

        async def update():
            async with self.journal._lock:
                self.add_late_fact()
                await self.journal._save()

        updater = asyncio.create_task(update())
        await asyncio.sleep(0)
        self.journal._lock.release()
        await asyncio.gather(worker, updater)
        await self.journal._write_memory(self.visit["id"])
        self.assertEqual([row["local_experience_seq"] for row in self.actor.decisions], [2],
                         "A claim rejected by a changed snapshot must not execute a stale paid review.")

    async def test_review_waits_for_durable_claim_when_marker_save_fails(self):
        self.journal.fail_saves = 1
        await self.journal._write_memory(self.visit["id"])
        self.assertFalse(self.actor.decisions,
                         "A swallowed journal failure must not admit an ambiguously repeatable paid review.")
        self.assertNotIn("finish_review_attempted", self.visit)
        self.assertGreater(self.visit["remember_retry_at"], self.now)
        await self.journal._write_memory(self.visit["id"])
        self.assertFalse(self.actor.decisions, "A failed durable claim must use bounded settlement backoff.")
        self.now = self.visit["remember_retry_at"]
        await self.journal._write_memory(self.visit["id"])
        self.assertEqual(len(self.actor.decisions), 1)
        self.assertTrue(self.visit.get("remembered"))

    async def test_paid_review_survives_late_facts_and_failed_first_memory_write(self):
        entered, release = asyncio.Event(), asyncio.Event()
        self.addCleanup(release.set)

        async def blocked_review():
            entered.set()
            await release.wait()

        writes = 0

        async def fail_first_write():
            nonlocal writes
            writes += 1
            if writes == 1:
                raise OSError("simulated memory write failure after completed review")

        self.actor.decide_hook = blocked_review
        self.actor.remember_hook = fail_first_write
        worker = asyncio.create_task(self.journal._write_memory(self.visit["id"]))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            async with self.journal._lock:
                self.add_late_fact()
                await self.journal._save()
            release.set()
            await worker
        finally:
            release.set()
            if not worker.done():
                await worker
        self.now = self.visit.get("remember_retry_at", self.now)
        await self.journal._write_memory(self.visit["id"])
        self.assertEqual(len(self.actor.decisions), 1, "A completed paid review must not be regenerated.")
        self.assertTrue(self.visit.get("remembered"))
        self.assertEqual(len(self.actor.memories[-1]["log"]), 2)
        self.assertIn("慢慢做事", str(self.actor.memories[-1].get("private_moments", [])),
                      "The finished review must survive a confirmed log extension and retry.")
        moment = self.actor.memories[-1]["private_moments"][-1]
        self.assertEqual(moment["log_seq"], 1, "Its source is the reviewed prefix, not the later facts.")

    async def test_finished_review_appends_its_origin_without_overwriting_newer_private_feeling(self):
        entered, release = asyncio.Event(), asyncio.Event()
        self.addCleanup(release.set)

        async def blocked_review():
            entered.set()
            await release.wait()

        self.actor.decide_hook = blocked_review
        self.actor.review["promise"] = "下次想继续刚才那一步。"
        worker = asyncio.create_task(self.journal._write_memory(self.visit["id"]))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            async with self.journal._lock:
                self.add_late_fact()
                self.journal._retain_private(self.visit, {"reflection": "这句新的告别让我很安心。",
                                                         "promise": "后来我更想和她一起看花。"}, self.now)
                await self.journal._save()
            self.now += 1
            release.set()
            await worker
        finally:
            release.set()
            if not worker.done():
                await worker
        self.assertEqual(self.visit["private"]["reflection"], "这句新的告别让我很安心。")
        self.assertEqual(self.visit["private"]["promise"], "后来我更想和她一起看花。")
        moments = self.visit["private_moments"]
        self.assertEqual([row["seq"] for row in moments], [1, 2])
        self.assertEqual([row["log_seq"] for row in moments], [2, 1])
        self.assertEqual([row["at"] for row in moments], [1000.0, 1001.0])
        self.assertIn("慢慢做事", moments[-1]["reflection"])
        self.assertEqual(moments[-1]["promise"], "下次想继续刚才那一步。")
        self.assertTrue(self.visit.get("remembered"))

    async def test_finished_review_is_not_attached_to_a_changed_persona(self):
        self.visit["local"] = {"actor_id": "isla", "persona_key": "original-isla"}
        entered, release = asyncio.Event(), asyncio.Event()
        self.addCleanup(release.set)

        async def blocked_review():
            entered.set()
            await release.wait()

        self.actor.decide_hook = blocked_review
        worker = asyncio.create_task(self.journal._write_memory(self.visit["id"]))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            async with self.journal._lock:
                self.visit["local"]["persona_key"] = "another-persona"
                await self.journal._save()
            release.set()
            await worker
        finally:
            release.set()
            if not worker.done():
                await worker
        self.assertFalse(self.visit["private_moments"])
        self.assertFalse(self.actor.memories, "One persona's review cannot be stored under another persona.")
        self.assertNotIn("remembered", self.visit)

    async def test_worker_does_not_confirm_stale_end_time_as_current_memory(self):
        self.visit["finish_review_attempted"] = True
        entered, release = asyncio.Event(), asyncio.Event()
        self.addCleanup(release.set)

        async def blocked_write():
            entered.set()
            await release.wait()

        self.actor.remember_hook = blocked_write
        worker = asyncio.create_task(self.journal._write_memory(self.visit["id"]))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            async with self.journal._lock:
                self.visit["ended_at"] = 991.0
                await self.journal._save()
            release.set()
            await worker
        finally:
            release.set()
            if not worker.done():
                await worker
        self.assertNotIn("remembered", self.visit,
                         "An older worker cannot settle a changed confirmed end time.")
        self.actor.remember_hook = None
        await self.journal._write_memory(self.visit["id"])
        self.assertTrue(self.visit.get("remembered"))
        self.assertEqual(self.actor.memories[-1]["ended_at"], 991.0)

    async def test_worker_confirmation_guards_actual_arrival_and_memory_ownership(self):
        cases = (("arrived_at", 901.0), ("scope", "another-life"),
                 ("local", {"actor_id": "isla", "persona_key": "another-persona"}),
                 ("local", {"actor_id": "another-actor", "persona_key": "original-isla"}),
                 ("remote", {"id": "another-peer"}), ("outing", {"stage": 2}))
        for field, value in cases:
            with self.subTest(field=field, value=value):
                visit = copy.deepcopy(self.visit)
                visit.update(finish_review_attempted=True,
                             local={"actor_id": "isla", "persona_key": "original-isla"},
                             remote={"id": "host:alice"})
                actor = MemoryActor()
                journal = MemoryJournal(actor, lambda: self.now, visit)
                entered, release = asyncio.Event(), asyncio.Event()

                async def blocked_write():
                    entered.set()
                    await release.wait()

                actor.remember_hook = blocked_write
                worker = asyncio.create_task(journal._write_memory(visit["id"]))
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    async with journal._lock:
                        visit[field] = copy.deepcopy(value)
                        await journal._save()
                    release.set()
                    await worker
                finally:
                    release.set()
                    if not worker.done():
                        await worker
                self.assertNotIn("remembered", visit, "An old worker cannot acknowledge new time or ownership.")

    async def test_cancelled_unknown_review_is_not_repaid_after_restart(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def unknown_review():
            entered.set()
            await release.wait()

        self.actor.decide_hook = unknown_review
        worker = asyncio.create_task(self.journal._write_memory(self.visit["id"]))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            worker.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await worker
        finally:
            release.set()
            if not worker.done():
                await worker
        restored = copy.deepcopy(self.journal.saved["visits"][self.visit["id"]])
        self.assertTrue(restored["finish_review_attempted"])
        resumed_actor = MemoryActor()
        resumed = MemoryJournal(resumed_actor, lambda: self.now, restored)
        await resumed._write_memory(restored["id"])
        self.assertFalse(resumed_actor.decisions)
        self.assertTrue(restored.get("remembered"))
        self.assertEqual(resumed_actor.memories[-1]["log"], self.visit["log"])


if __name__ == "__main__":
    unittest.main()
