"""Unstarted admission and natural refusals keep their original durable meaning."""
from __future__ import annotations

import asyncio
import copy
import threading
import unittest

from tests import fake_astrbot
fake_astrbot.install()

from core.visits.decisions import VisitDecisionError  # noqa: E402 - install the SDK fixture first
from tests import test_visits_model_retry as fixture  # noqa: E402


class VisitDecisionAdmissionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.VisitModelRetryTests.asyncSetUp

    async def pending_proposal(self):
        local = await self.a_actor.describe("life-a")
        peer = self.a._peer("life-a", self.peer)
        visit = self.a._new_visit("life-a", peer, local, "a" * 32, "guest", "喝茶", False)
        visit["proposal_stage"] = "pending"
        self.a._state["visits"][visit["id"]] = visit
        await self.a._save()
        return visit

    async def assert_unstarted_recovery(self, visit, calls):
        self.assertNotIn("model_pending", visit)
        self.assertEqual(visit["calls"], calls)
        self.assertFalse(self.a_actor.calls)
        self.assertFalse(self.b._state["visits"])
        self.assertEqual(visit["log"], [])
        self.assertNotIn("arrived_at", visit)
        self.assertEqual(visit["model_retry"]["category"], "state")
        stored = await self.a_db.call("kv_get", "world_visits.v1", {})
        self.assertNotIn("model_pending", stored["visits"][visit["id"]])
        self.assertEqual(stored["visits"][visit["id"]]["calls"], calls)
        self.now = visit["model_retry"]["next_at"]
        await self.a._advance(visit["id"])
        self.assertEqual(len(self.a_actor.calls), 1)
        self.assertEqual(sum(visit["calls"].values()), 1)
        self.assertEqual(visit["proposal_stage"], "sent")
        self.assertEqual(sum(row["kind"] == "invitation" for row in visit["log"]), 1)
        self.assertEqual(len(self.b._state["visits"]), 1)
        self.assertNotIn("arrived_at", visit)

    async def test_admission_write_failure_releases_unstarted_call(self):
        await self.assert_admission_write_failure(committed=False)

    async def test_admission_committed_then_failed_still_releases_unstarted_call(self):
        await self.assert_admission_write_failure(committed=True)

    async def assert_admission_write_failure(self, *, committed):
        visit = await self.pending_proposal()
        calls = copy.deepcopy(visit["calls"])
        original = self.a_db.call
        failed = False

        async def write(operation, key, value):
            nonlocal failed
            if operation == "kv_set" and visit.get("model_pending") and not failed:
                failed = True
                if committed:
                    await original(operation, key, value)
                raise OSError("temporary journal failure")
            return await original(operation, key, value)

        self.a_db.call = write
        with self.assertRaises(OSError):
            await self.a._decide(visit["id"], "propose")
        self.assertTrue(failed)
        await self.assert_unstarted_recovery(visit, calls)

    async def test_cancellation_after_admission_commit_releases_unstarted_call(self):
        visit = await self.pending_proposal()
        calls = copy.deepcopy(visit["calls"])
        original = self.a_db.call
        entered = asyncio.Event()
        failed = False

        async def write(operation, key, value):
            nonlocal failed
            if operation == "kv_set" and visit.get("model_pending") and not failed:
                failed = True
                await original(operation, key, value)
                entered.set()
                await asyncio.Future()
            return await original(operation, key, value)

        self.a_db.call = write
        task = asyncio.create_task(self.a._decide(visit["id"], "propose"))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await self.assert_unstarted_recovery(visit, calls)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_repeated_admission_failures_stop_without_spending_model_calls(self):
        visit = await self.pending_proposal()
        original = self.a_db.call

        async def write(operation, key, value):
            if operation == "kv_set" and visit.get("model_pending"):
                raise OSError("temporary journal failure")
            return await original(operation, key, value)

        self.a_db.call = write
        for attempt in range(3):
            with self.assertRaises(OSError):
                await self.a._decide(visit["id"], "propose")
            self.assertFalse(self.a_actor.calls)
            self.assertEqual(sum(visit["calls"].values()), 0)
            self.assertNotIn("model_pending", visit)
            if attempt < 2:
                self.assertEqual(visit["model_retry"]["attempts"], attempt + 1)
                self.now = visit["model_retry"]["next_at"]
        self.assertEqual(visit["status"], "interrupted")
        self.assertNotIn("model_retry", visit)
        self.assertNotIn("arrived_at", visit)
        self.assertFalse(self.b._state["visits"])

    async def test_repeated_cancellation_waits_for_actual_sqlite_refund_save(self):
        visit = await self.pending_proposal()
        calls = copy.deepcopy(visit["calls"])
        original = self.a_db.raw.kv_set
        loop = asyncio.get_running_loop()
        entered, release = asyncio.Event(), threading.Event()
        writes = 0

        def write(key, value):
            nonlocal writes
            writes += 1
            if writes == 1:
                original(key, value)
                raise OSError("committed admission returned an error")
            if writes == 2:
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(3):
                    raise RuntimeError("test refund write was not released")
            return original(key, value)

        self.a_db.raw.kv_set = write
        task = asyncio.create_task(self.a._decide(visit["id"], "propose"))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                self.assertTrue(self.a._lock.locked())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.a_db.raw.kv_set = original
            await self.assert_unstarted_recovery(visit, calls)
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.a_db.raw.kv_set = original

    async def test_admission_failure_after_close_does_not_rewrite_old_journal(self):
        visit = await self.pending_proposal()
        original = self.a_db.call
        stored = None

        async def write(operation, key, value):
            nonlocal stored
            await original(operation, key, value)
            if operation == "kv_set" and visit.get("model_pending"):
                stored = copy.deepcopy(value)
                await self.a.close()
                raise OSError("shutdown crossed an admission write")

        self.a_db.call = write
        with self.assertRaises(OSError):
            await self.a._decide(visit["id"], "propose")
        self.a_db.call = original
        saved = await original("kv_get", "world_visits.v1", {})
        self.assertEqual(saved, stored)
        self.assertIn("model_pending", visit)
        self.assertFalse(self.a_actor.calls)
        self.assertFalse(self.b._state["visits"])


class VisitNaturalRefusalTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.VisitModelRetryTests.asyncSetUp
    offered = fixture.VisitModelRetryTests.offered

    async def saved_refusal(self, *, paid_candidate=False, gate=None, leave=False, accept=False, omit_accept=False):
        visit_id, visit = await self.offered()
        original = self.b_actor.decide

        async def refuse(scope, phase, snapshot):
            result = {**await original(scope, phase, snapshot), "accept": accept or leave, "leave": leave,
                      "say": "我还想把这本书看完，明天下午再约，好吗？"}
            if omit_accept:
                result.pop("accept")
            if paid_candidate:
                raise VisitDecisionError("state", candidate=result)
            return result

        self.b_actor.decide = refuse
        await self.b._decide(visit_id, "receive")
        describe = self.b_actor.describe

        async def changed(scope):
            return {**await describe(scope), **(gate or {"ready": False})}

        self.b_actor.describe = changed
        return visit_id, visit

    async def assert_natural_refusal(self, visit):
        self.assertEqual(visit["status"], "declined")
        self.assertEqual(visit["end_reason"], "我还想把这本书看完，明天下午再约，好吗？")
        self.assertEqual(visit["log"][-1]["kind"], "contact_reply")
        self.assertEqual(visit["log"][-1]["text"], visit["end_reason"])
        self.assertEqual(len(self.b_actor.calls), 1)
        self.assertEqual(visit["calls"][self.b._local_id(visit)], 1)
        self.assertNotIn("arrived_at", visit)
        self.assertFalse(self.b_actor.memories)

    async def test_saved_refusal_is_not_replaced_by_current_busy_status(self):
        visit_id, visit = await self.saved_refusal()
        await self.b._advance(visit_id)
        await self.assert_natural_refusal(visit)

    async def test_paid_refusal_resumes_without_another_model_when_currently_busy(self):
        visit_id, visit = await self.saved_refusal(paid_candidate=True)
        self.now = visit["model_retry"]["next_at"]
        await self.b._advance(visit_id)
        await self.assert_natural_refusal(visit)

    async def test_saved_refusal_preserves_words_after_reception_closes(self):
        visit_id, visit = await self.saved_refusal(gate={"reception_open": False})
        await self.b._advance(visit_id)
        await self.assert_natural_refusal(visit)

    async def test_paid_refusal_preserves_words_when_human_chat_is_busy(self):
        visit_id, visit = await self.saved_refusal(paid_candidate=True, gate={"chat_busy": True})
        self.now = visit["model_retry"]["next_at"]
        await self.b._advance(visit_id)
        await self.assert_natural_refusal(visit)

    async def test_leave_choice_also_preserves_words_without_arriving(self):
        visit_id, visit = await self.saved_refusal(paid_candidate=True, leave=True)
        self.now = visit["model_retry"]["next_at"]
        await self.b._advance(visit_id)
        await self.assert_natural_refusal(visit)

    async def test_refusal_from_previous_identity_is_still_discarded(self):
        visit_id, visit = await self.saved_refusal(paid_candidate=True, gate={"persona_key": "another-persona"})
        self.now = visit["model_retry"]["next_at"]
        await self.b._advance(visit_id)
        self.assertEqual(visit["status"], "interrupted")
        self.assertFalse(any(row["text"].startswith("我还想把这本书看完") for row in visit["log"]))
        self.assertNotIn("arrived_at", visit)
        self.assertEqual(len(self.b_actor.calls), 1)

    async def test_natural_refusal_reaches_caller_as_the_original_phone_reply(self):
        visit_id, visit = await self.saved_refusal(paid_candidate=True)
        self.now = visit["model_retry"]["next_at"]
        await self.b._advance(visit_id)
        await self.assert_natural_refusal(visit)
        await self.a._advance(visit_id)
        caller = self.a._state["visits"][visit_id]
        self.assertEqual(caller["status"], "declined")
        self.assertEqual(caller["end_reason"], visit["end_reason"])
        self.assertNotIn("arrived_at", caller)
        self.assertFalse(self.a_actor.memories)

    async def test_admission_failure_keeps_paid_refusal_and_its_exact_call_count(self):
        visit_id, visit = await self.saved_refusal(paid_candidate=True)
        calls = copy.deepcopy(visit["calls"])
        candidate = copy.deepcopy(visit["model_retry"]["candidate"])
        original = self.b_db.call
        failed = False

        async def write(operation, key, value):
            nonlocal failed
            if operation == "kv_set" and visit.get("model_pending") and not failed:
                failed = True
                await original(operation, key, value)
                raise OSError("paid candidate verification admission failed")
            return await original(operation, key, value)

        self.b_db.call = write
        self.now = visit["model_retry"]["next_at"]
        with self.assertRaises(OSError):
            await self.b._decide(visit_id, "receive")
        self.assertEqual(visit["calls"], calls)
        self.assertEqual(visit["model_retry"]["candidate"], candidate)
        self.assertNotIn("model_pending", visit)
        saved = await self.b_db.call("kv_get", "world_visits.v1", {})
        self.assertEqual(saved["visits"][visit_id]["calls"], calls)
        self.assertEqual(saved["visits"][visit_id]["model_retry"]["candidate"], candidate)
        self.now = visit["model_retry"]["next_at"]
        await self.b._advance(visit_id)
        await self.assert_natural_refusal(visit)

    async def assert_accepting_candidate_gate(self, *, gate, omit_accept=False):
        visit_id, visit = await self.saved_refusal(paid_candidate=True, gate=gate,
                                                   accept=True, omit_accept=omit_accept)
        self.now = visit["model_retry"]["next_at"]
        await self.b._advance(visit_id)
        self.assertNotIn("arrived_at", visit)
        self.assertEqual(len(self.b_actor.calls), 1)
        if gate.get("ready") is False:
            self.assertEqual(visit["status"], "offered")
            self.assertEqual(visit["model_retry"]["category"], "life_busy")
        else:
            self.assertEqual(visit["status"], "declined")
            self.assertEqual(visit["log"][-1]["kind"], "contact_status")

    async def test_accepting_candidate_still_waits_for_current_readiness(self):
        await self.assert_accepting_candidate_gate(gate={"ready": False})

    async def test_accepting_candidate_still_obeys_reception_hours(self):
        await self.assert_accepting_candidate_gate(gate={"reception_open": False})

    async def test_accepting_candidate_still_obeys_current_human_chat(self):
        await self.assert_accepting_candidate_gate(gate={"chat_busy": True})

    async def test_candidate_without_accept_still_waits_for_readiness(self):
        await self.assert_accepting_candidate_gate(gate={"ready": False}, omit_accept=True)

    async def test_candidate_without_accept_still_obeys_reception_hours(self):
        await self.assert_accepting_candidate_gate(gate={"reception_open": False}, omit_accept=True)

    async def test_candidate_without_accept_still_obeys_current_human_chat(self):
        await self.assert_accepting_candidate_gate(gate={"chat_busy": True}, omit_accept=True)
