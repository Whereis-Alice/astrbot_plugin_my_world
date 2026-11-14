"""Paid output survives reload cancellation without replaying inference or facts."""
from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace
import unittest

from tests import test_visits_lifecycle_races as fixture
from core.visits import VisitService
from core.visits.decisions import record_decision_candidate
from core.visits.persistence import KEY


class VisitCancelledCandidateTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.VisitLifecycleRaceTests.asyncSetUp
    pair = fixture.VisitLifecycleRaceTests.pair
    exchange = fixture.VisitLifecycleRaceTests.exchange

    async def reload_service(self):
        await self.b.close()
        resumed = VisitService(self.b.db, self.b.config, self.b.actor_port,
                               clock=lambda: self.now, rng=lambda: .9)
        self.addAsyncCleanup(resumed.close)
        await resumed._load()
        return resumed

    async def pending_choice(self, *, generated=True):
        _, visit = self.pair(arrived=True)
        visit["next_actor"] = visit["host_id"]
        await self.b._save()
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.b_actor.decide

        async def decide(scope, phase, snapshot):
            result = await original(scope, phase, snapshot)
            if generated:
                record_decision_candidate(result)
            entered.set()
            await release.wait()
            return result

        self.b_actor.decide = decide
        task = asyncio.create_task(self.b._decide(visit["id"], "turn"))
        self.addAsyncCleanup(self.stop_task, task, release)
        await asyncio.wait_for(entered.wait(), 1)
        return visit, task

    @staticmethod
    async def stop_task(task, release):
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def cancel(self, task):
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_actual_adapter_candidate_survives_reload_without_another_model_call(self):
        from tests import test_visits_actor as actor_fixture
        from tests import test_engine as engine_fixture

        _, visit = self.pair(arrived=True)
        visit["next_actor"] = visit["host_id"]
        before_log, before_private = copy.deepcopy(visit["log"]), copy.deepcopy(visit["private"])
        before_calls = copy.deepcopy(visit["calls"])
        self.node_id = "study"
        await actor_fixture.VisitActorTests.asyncSetUp(self)
        self.llm.default_reply = '{"say":"茶还温着，我们接着聊。","thought":"想听她说完。"}'
        describe, actual_decide = self.b_actor.describe, self.port.decide
        generate = self.engine._budgeted_generate
        generated = False
        verifying, release = asyncio.Event(), asyncio.Event()

        async def describe_model(scope):
            card = await describe(scope)
            if generated:
                verifying.set()
                await release.wait()
            return {**card, "place": "客厅", "reception_open": True}

        async def model_for_service(scope, phase, snapshot):
            return await actual_decide(engine_fixture.SESSION, phase, snapshot)

        async def generate_then_verify(*args, **kwargs):
            nonlocal generated
            reply = await generate(*args, **kwargs)
            generated = True
            return reply

        self.port.describe = describe_model
        self.engine._budgeted_generate = generate_then_verify
        self.b.actor_port = SimpleNamespace(describe=describe_model, decide=model_for_service,
                                           remember=self.b_actor.remember)
        task = asyncio.create_task(self.b._decide(visit["id"], "turn"))
        self.addAsyncCleanup(self.stop_task, task, release)
        await asyncio.wait_for(verifying.wait(), 2)
        await self.cancel(task)
        candidate = copy.deepcopy(visit["model_retry"]["candidate"])
        self.assertEqual(candidate["say"], "茶还温着，我们接着聊。")
        self.assertEqual(candidate["thought"], "想听她说完。")
        self.assertTrue(visit["model_retry"]["waiting"])
        self.assertNotIn("model_pending", visit)
        self.assertEqual(visit["calls"][visit["host_id"]], before_calls[visit["host_id"]] + 1)
        self.assertEqual(visit["log"], before_log)
        self.assertEqual(visit["private"], before_private)
        self.assertNotIn("candidate", self.b._wire(visit))
        stored = await self.b.db.call("kv_get", KEY, {})
        self.assertEqual(stored["visits"][visit["id"]]["model_retry"]["candidate"], candidate)
        release.set()
        resumed = await self.reload_service()
        current = resumed._state["visits"][visit["id"]]
        self.assertEqual(current["status"], "active")
        self.now = current["model_retry"]["next_at"]
        await resumed._advance(visit["id"])
        await resumed._advance(visit["id"])
        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual(current["calls"], visit["calls"])
        self.assertEqual(sum(row["text"] == candidate["say"] for row in current["log"]), 1)
        self.assertEqual(current["private"].get("thought"), candidate["thought"])

    async def test_returned_candidate_survives_cancellation_while_waiting_for_journal_lock(self):
        _, visit = self.pair(arrived=True)
        visit["next_actor"] = visit["host_id"]
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.b_actor.decide
        blocker = None

        async def hold_lock():
            async with self.b._lock:
                entered.set()
                await release.wait()

        async def decide(scope, phase, snapshot):
            nonlocal blocker
            result = await original(scope, phase, snapshot)
            record_decision_candidate(result)
            blocker = asyncio.create_task(hold_lock())
            await entered.wait()
            return result

        self.b_actor.decide = decide
        task = asyncio.create_task(self.b._decide(visit["id"], "turn"))
        self.addAsyncCleanup(self.stop_task, task, release)
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await blocker
        self.assertNotIn("model_pending", visit)
        self.assertEqual(visit["model_retry"]["candidate"]["say"], "host：turn")
        self.assertEqual(visit["turns"][visit["host_id"]], 0)

    async def test_repeated_cancellation_waits_for_the_owned_candidate_write(self):
        visit, task = await self.pending_choice()
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.b.db.call

        async def save(operation, key, value=None):
            if operation == "kv_set" and (visit.get("model_retry") or {}).get("candidate"):
                entered.set()
                await release.wait()
            return await original(operation, key, value)

        self.b.db.call = save
        task.cancel()
        await asyncio.wait_for(entered.wait(), 1)
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.b.db.call = original
        stored = await original("kv_get", KEY, {})
        self.assertNotIn("model_pending", stored["visits"][visit["id"]])
        self.assertEqual(stored["visits"][visit["id"]]["model_retry"]["candidate"]["say"], "host：turn")

    async def test_a_closed_instance_does_not_write_its_cancelled_candidate(self):
        visit, task = await self.pending_choice()
        stored = await self.b.db.call("kv_get", KEY, {})
        await self.b.close()
        await self.cancel(task)
        self.assertEqual(await self.b.db.call("kv_get", KEY, {}), stored)
        self.assertIn("model_pending", visit)
        self.assertNotIn("model_retry", visit)

    async def test_confirmed_goodbye_wins_over_a_cancelled_candidate(self):
        visit, task = await self.pending_choice()
        async with self.b._lock:
            await self.b._finish(visit, "completed", "已确认告别")
        stored = await self.b.db.call("kv_get", KEY, {})
        await self.cancel(task)
        self.assertEqual(await self.b.db.call("kv_get", KEY, {}), stored)
        self.assertEqual(visit["status"], "completed")
        self.assertNotIn("model_retry", visit)

    async def test_a_new_confirmed_phase_discards_the_cancelled_old_candidate(self):
        visit, task = await self.pending_choice()
        async with self.b._lock:
            self.b._append(visit, visit["guest_id"], "say", "刚刚确认的新话题")
            await self.b._save()
        confirmed = copy.deepcopy(visit["log"])
        await self.cancel(task)
        self.assertEqual(visit["status"], "active")
        self.assertNotIn("model_pending", visit)
        self.assertNotIn("model_retry", visit)
        self.assertEqual(visit["log"], confirmed)
        self.assertEqual(len(self.b_actor.calls), 1)

    async def test_an_unknown_inflight_request_keeps_the_existing_reload_policy(self):
        visit, task = await self.pending_choice(generated=False)
        calls, log = copy.deepcopy(visit["calls"]), copy.deepcopy(visit["log"])
        await self.cancel(task)
        self.assertIn("model_pending", visit)
        self.assertNotIn("model_retry", visit)
        resumed = await self.reload_service()
        current = resumed._state["visits"][visit["id"]]
        self.assertEqual(current["status"], "interrupted")
        self.assertEqual(current["calls"], calls)
        self.assertEqual(current["log"], log)
        self.assertEqual(len(self.b_actor.calls), 1)


if __name__ == "__main__":
    unittest.main()
