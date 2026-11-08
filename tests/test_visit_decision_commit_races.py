"""Human continuation queued at the save boundary supersedes an old choice."""
from __future__ import annotations

import asyncio
import copy
import unittest
from unittest.mock import patch

from tests import test_visits_model_retry as fixture


class VisitDecisionCommitRaceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.VisitModelRetryTests.asyncSetUp
    offered = fixture.VisitModelRetryTests.offered
    host_turn = fixture.VisitModelRetryTests.host_turn

    def queue_continuation_after_decision(self, service, phase):
        original = service._decision_still_current
        tasks = []

        async def current(visit, current_phase, cursor):
            result = await original(visit, current_phase, cursor)
            if (result and current_phase == phase and not tasks
                    and visit.get("decision_cursor") == cursor):
                async def continue_visit():
                    async with service._lock:
                        service._mark_manual_request(visit, "请继续相处")
                        await service._save()
                tasks.append(asyncio.create_task(continue_visit()))
                # The lock is still held by _decide, so the human operation
                # queues before the caller can reacquire its commit lock.
                await asyncio.sleep(0)
            return result

        return patch.object(service, "_decision_still_current", side_effect=current), tasks

    async def test_receive_cannot_apply_old_acceptance_after_human_continuation(self):
        visit_id, visit = await self.offered()
        before = copy.deepcopy(visit["log"])
        guard, tasks = self.queue_continuation_after_decision(self.b, "receive")
        with guard:
            await self.b._advance(visit_id)
            await asyncio.gather(*tasks)
        self.assertTrue(tasks)
        self.assertTrue(visit["manual_continuation"])
        self.assertEqual(visit["status"], "offered")
        self.assertNotIn("arrived_at", visit)
        self.assertEqual(visit["log"], before)
        self.assertEqual(len(self.b_actor.calls), 1)

    async def test_host_cannot_apply_old_goodbye_after_human_continuation(self):
        visit_id, visit = await self.host_turn()
        visit["event_stage"] = "skipped"
        before = copy.deepcopy(visit["log"])
        original = self.b_actor.decide

        async def goodbye(scope, phase, snapshot):
            return {**await original(scope, phase, snapshot), "say": "我先回去了。", "leave": True}

        guard, tasks = self.queue_continuation_after_decision(self.b, "turn")
        with patch.object(self.b_actor, "decide", side_effect=goodbye), guard:
            await self.b._advance(visit_id)
            await asyncio.gather(*tasks)
        self.assertTrue(tasks)
        self.assertTrue(visit["manual_continuation"])
        self.assertEqual(visit["status"], "active")
        self.assertNotIn("ended_at", visit)
        self.assertEqual(visit["log"], before)

    async def test_scene_cannot_apply_old_event_after_human_continuation(self):
        visit_id, visit = await self.host_turn()
        before = copy.deepcopy(visit["log"])
        event_stage = visit["event_stage"]
        guard, tasks = self.queue_continuation_after_decision(self.b, "scene")
        with guard:
            await self.b._scene(visit_id, "event_offer")
            await asyncio.gather(*tasks)
        self.assertTrue(tasks)
        self.assertTrue(visit["manual_continuation"])
        self.assertEqual(visit["status"], "active")
        self.assertEqual(visit["event_stage"], event_stage)
        self.assertEqual(visit["log"], before)
