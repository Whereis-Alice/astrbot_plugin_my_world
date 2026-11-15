"""A technically paused guest still learns real progress through signed polling."""
from __future__ import annotations

import copy
import unittest

from tests import fake_astrbot
fake_astrbot.install()

from core.visits.decisions import VisitDecisionError  # noqa: E402
from core.visits.transport import VisitTransportError  # noqa: E402
from tests import test_visit_format_pause_20261115 as fixture  # noqa: E402
from tests import test_visit_shared_activities_20261113 as activity_fixture  # noqa: E402


class PausedGuestSynchronizationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.FormatPauseTests.asyncSetUp
    host_turn = fixture.FormatPauseTests.host_turn
    offered = fixture.FormatPauseTests.offered

    async def paused_encounter(self, *, return_after=False):
        visit_id, host = await self.host_turn()

        async def propose(scope, phase, current):
            return {"outing": {"place": "窗边", "activity": "观察木盒的细节", "minutes": 30,
                               "pause_minutes": 0, "return_after": return_after},
                    "say": "要一起看看这个木盒吗？", "farewell": "下次再一起看看。"}

        async def accept(scope, phase, current):
            return {"outing_accept": True, "pause_minutes": 0,
                    "return_after": return_after, "farewell": "那我到时候就回去了。",
                    "action_step": activity_fixture.step("仔细观察木盒", 600)}

        self.b_actor.decide = propose
        await self.b._advance(visit_id)
        self.now += self.b.config.poll_seconds
        self.a_actor.decide = accept
        await self.a._advance(visit_id)
        guest = self.a._state["visits"][visit_id]
        started = host["outing"]["started_at"]

        async def continue_observing(scope, phase, current):
            return {"say": "你觉得这个角落的花纹像什么？", "continue_activity": True, "pause_minutes": 0}

        self.b_actor.decide = continue_observing
        self.now = host["outing"]["quiet_until"]
        await self.b.tick()
        self.now = host["next_at"]
        attempts = []

        async def invalid(scope, phase, current):
            attempts.append(phase)
            raise VisitDecisionError("format", format_issue="json_object",
                                     repair_text='{"say":"未执行的私有原稿')

        self.a_actor.decide = invalid
        for delay in (30, 60, 0):
            await self.b.tick()
            await self.a.tick()
            self.now += delay
        self.assertEqual(len(attempts), 3)
        self.assertTrue(guest["model_retry"]["paused"])
        self.assertEqual(host["next_actor"], host["guest_id"])
        self.assertEqual(host["shared_activity"]["action_steps"][host["guest_id"]][0]["status"], "running")
        requests = []
        request = self.a.transport.request

        async def observed(url, packet):
            requests.append(copy.deepcopy(packet))
            return await request(url, packet)

        self.a.transport.request = observed
        return visit_id, host, guest, attempts, requests, started

    async def test_repeated_real_ticks_poll_at_the_original_cadence_without_model_or_lease_extension(self):
        _, host, guest, attempts, requests, _ = await self.paused_encounter()
        calls = copy.deepcopy(guest["calls"])
        log = copy.deepcopy(guest["log"])
        expires = guest["expires_at"]
        await self.b.tick()
        await self.a.tick()
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["payload"]["op"], "sync")
        self.assertNotIn("私有原稿", str(requests[0]))
        next_sync = guest["model_retry"]["sync_at"]
        for moment in (self.now, self.now + 1, next_sync - 1):
            self.now = moment
            await self.b.tick()
            await self.a.tick()
        self.assertEqual(len(requests), 1)
        self.now = next_sync
        await self.b.tick()
        await self.a.tick()
        self.assertEqual(len(requests), 2)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(guest["calls"], calls)
        self.assertEqual(guest["log"], log)
        self.assertTrue(guest["model_retry"]["paused"])
        self.assertEqual(guest["expires_at"], expires)
        self.assertEqual(guest["next_at"], expires)
        self.assertEqual(host["next_actor"], host["guest_id"])

    async def test_actual_host_clock_progress_wakes_the_guest_through_both_servers_ticks(self):
        visit_id, host, guest, attempts, requests, started = await self.paused_encounter()
        expires = guest["expires_at"]
        calls = guest["calls"][guest["guest_id"]]
        before = copy.deepcopy(guest["log"])

        async def corrected(scope, phase, current):
            attempts.append(phase)
            return {"say": "这一步的时间到了，结果还需要确认。", "continue_activity": True}

        self.a_actor.decide = corrected
        self.now = started + 601
        await self.b.tick()
        self.assertEqual(host["shared_activity"]["action_steps"][host["guest_id"]][0]["status"], "elapsed")
        self.assertTrue(guest["model_retry"]["paused"])
        self.assertEqual(host["next_actor"], host["guest_id"])
        await self.a.tick()
        self.assertEqual(len(attempts), 4)
        self.assertEqual([packet["payload"]["op"] for packet in requests], ["sync", "step"])
        self.assertNotIn("model_retry", guest)
        self.assertEqual(guest["calls"][guest["guest_id"]], calls + 1)
        self.assertEqual(guest["expires_at"], expires)
        self.assertEqual(guest["log"][:len(before)], before)
        self.assertEqual(guest["log"], host["log"])
        self.assertEqual(sum(row["text"] == "这一步的时间到了，结果还需要确认。" for row in guest["log"]), 1)
        self.assertNotIn("私有原稿", str(guest["log"]))
        self.assertFalse(guest["shared_activity"]["done"])
        self.assertEqual(guest["shared_activity"]["summary"], "")
        self.assertFalse(self.a_actor.memories)
        self.assertEqual(self.a._state["visits"][visit_id]["status"], "active")

    async def test_paused_poll_retains_existing_network_backoff(self):
        _, _, guest, attempts, requests, _ = await self.paused_encounter()
        expires = guest["expires_at"]

        async def unavailable(url, packet):
            requests.append(copy.deepcopy(packet))
            raise VisitTransportError("暂时离线", category="channel")

        self.a.transport.request = unavailable
        await self.a.tick()
        self.assertEqual(len(requests), 1)
        self.assertEqual(guest["network_failures"], 1)
        retry_at = guest["next_at"]
        self.assertGreater(retry_at, guest["model_retry"]["sync_at"])
        self.now = guest["model_retry"]["sync_at"]
        await self.b.tick()
        await self.a.tick()
        self.assertEqual(len(requests), 1)
        self.now = retry_at
        await self.b.tick()
        await self.a.tick()
        self.assertEqual(len(requests), 2)
        self.assertEqual(guest["network_failures"], 2)
        self.assertEqual(len(attempts), 3)
        self.assertTrue(guest["model_retry"]["paused"])
        self.assertEqual(guest["expires_at"], expires)

    async def test_agreed_return_precedes_the_paused_sync_gate(self):
        _, _, guest, attempts, requests, _ = await self.paused_encounter(return_after=True)
        deadline = guest["outing"]["until"]
        self.assertTrue(guest["outing"]["return_agreed"])
        guest["model_retry"]["sync_at"] = deadline + self.a.config.poll_seconds
        self.now = deadline
        await self.a.tick()
        self.assertEqual(guest["status"], "completed")
        self.assertEqual(guest["ended_at"], deadline)
        self.assertEqual(attempts.count("turn"), 3)
        self.assertFalse(any(packet["payload"]["op"] == "sync" for packet in requests))
        self.assertNotIn("model_retry", guest)


if __name__ == "__main__":
    unittest.main()
