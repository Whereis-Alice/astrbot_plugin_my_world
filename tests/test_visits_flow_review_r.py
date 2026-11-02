"""Confirmed outing decisions through the real signed channel and shared clocks."""
from __future__ import annotations

import copy
import asyncio
from dataclasses import replace
import json
import unittest

from core.visits import VisitService
from core.visits.outings import pending_outing_requests
from tests import test_visits_outings_q as outing_fixture


class OutingFlowReviewTests(unittest.IsolatedAsyncioTestCase):
    make_world = outing_fixture.OutingClockTests.make_world
    exhaust = outing_fixture.OutingClockTests.exhaust
    advance = outing_fixture.OutingClockTests.advance
    guest_reply = outing_fixture.OutingClockTests.guest_reply
    host_reply = outing_fixture.OutingClockTests.host_reply
    start_trip = outing_fixture.OutingClockTests.start_trip

    async def asyncSetUp(self):
        await outing_fixture.OutingClockTests.asyncSetUp(self)

    async def restart(self, world):
        previous = world.service
        config = replace(previous.config, listen_port=previous.transport.bound_port)
        await previous.close()
        world.service = VisitService(world.db, config, world.port, clock=world.clock.now, rng=lambda: 0)
        world.engine.visits = world.service
        world.plugin.visits = world.service
        self.addAsyncCleanup(world.service.close)
        await world.service.start()
        if world is self.a:
            original = world.service.transport.request

            async def request(url, body):
                self.operations.append(body["payload"]["op"])
                return await original(url, body)

            world.service.transport.request = request

    async def test_mid_activity_guest_farewell_does_not_create_a_new_shared_pause(self):
        visit_id = await self.start_trip(minutes=45, pause=3)
        self.b.llm.default_reply = json.dumps({"say": "稍后去看那边的花吗？",
            "continue_activity": True, "pause_minutes": 10}, ensure_ascii=False)
        self.advance(181)
        await self.b.service._advance(visit_id)
        host = self.b.service._state["visits"][visit_id]
        previous = copy.deepcopy(host["outing"])
        self.a.llm.default_reply = json.dumps({"say": "我先回去了，下次再一起。",
            "leave": True, "pause_minutes": 0}, ensure_ascii=False)
        self.advance(self.cfg.poll_seconds + 1)
        await self.a.service._advance(visit_id)
        guest = self.a.service._state["visits"][visit_id]
        self.assertEqual(host["status"], "completed")
        self.assertEqual(guest["status"], "completed")
        self.assertEqual(host["log"], guest["log"])
        self.assertEqual(host["outing"], previous)
        self.assertEqual(guest["outing"], previous)
        self.assertEqual(host["end_reason"], "我先回去了，下次再一起。")

    async def test_automatic_host_budget_restores_after_midnight_and_real_reload(self):
        visit_id = await self.start_trip(manual=False, minutes=5, pause=0)
        await self.exhaust(self.b)
        self.advance(301)
        await self.b.service._advance(visit_id)
        hold = self.b.service._state["visits"][visit_id]["budget_holds"]
        reset = max(row["reset_at"] for row in hold.values())
        await self.restart(self.a)
        await self.restart(self.b)
        before = (len(self.a.llm.calls), len(self.b.llm.calls), len(self.operations))
        self.advance(60)
        await self.a.service.tick()
        await self.b.service.tick()
        self.assertEqual((len(self.a.llm.calls), len(self.b.llm.calls), len(self.operations)), before)
        self.advance(reset - self.a.clock.now() + 1)
        self.b.llm.default_reply = self.host_reply(place="山顶", minutes=45, pause=3)
        self.a.llm.default_reply = self.guest_reply(pause=3)
        # The guest wakes first while the host still holds yesterday's checkpoint.
        await self.a.service.tick()
        await self.b.service.tick()
        self.advance(self.cfg.poll_seconds + 1)
        await self.a.service.tick()
        host = self.b.service._state["visits"][visit_id]
        guest = self.a.service._state["visits"][visit_id]
        self.assertEqual(host["status"], "active")
        self.assertEqual(guest["status"], "active")
        self.assertEqual(host["outing"], guest["outing"])
        self.assertEqual(host["outing"]["place"], "山顶")
        self.assertEqual(len(host["outing"]["route"]), 2)
        self.assertFalse(host.get("budget_holds"))
        self.assertFalse(guest.get("budget_holds"))
        self.assertEqual((len(self.a.llm.calls), len(self.b.llm.calls)), (before[0] + 1, before[1] + 1))

    async def test_human_request_during_quiet_is_shared_and_discussed_without_rewriting_the_plan(self):
        visit_id = await self.start_trip(minutes=120, pause=120)
        host = self.b.service._state["visits"][visit_id]
        guest = self.a.service._state["visits"][visit_id]
        previous = copy.deepcopy(host["outing"])
        before = (len(self.a.llm.calls), len(self.b.llm.calls))
        result = await self.a.service.invite(self.scope, self.peer_id, manual=True, reason="一起去花园看花吧")
        self.assertEqual(result["id"], visit_id)
        self.assertEqual(host["outing"], previous)
        self.assertEqual(guest["outing"], previous)
        self.assertEqual(host["outing_requests"], guest["outing_requests"])
        self.assertEqual(list(pending_outing_requests(host)), [guest["guest_id"]])
        self.assertEqual((len(self.a.llm.calls), len(self.b.llm.calls)), before)
        self.b.llm.default_reply = self.host_reply(place="花园", minutes=45, pause=3)
        self.a.llm.default_reply = self.guest_reply(pause=3)
        await self.b.service._advance(visit_id)
        self.advance(self.cfg.poll_seconds + 1)
        await self.a.service._advance(visit_id)
        self.assertEqual(host["outing"], guest["outing"])
        self.assertEqual(host["outing"]["place"], "花园")
        self.assertEqual(len(host["outing"]["route"]), 2)
        self.assertFalse(pending_outing_requests(host))
        self.assertFalse(pending_outing_requests(guest))
        self.assertEqual((len(self.a.llm.calls), len(self.b.llm.calls)), (before[0] + 1, before[1] + 1))
        after = (len(self.a.llm.calls), len(self.b.llm.calls), len(self.operations))
        self.advance(60)
        await self.a.service.tick()
        await self.b.service.tick()
        self.assertEqual((len(self.a.llm.calls), len(self.b.llm.calls), len(self.operations)), after)
        # A fresh signed status envelope may repeat the same already handled
        # human request; its stable request ID must not wake another dialogue.
        reply = await self.a.service._send(visit_id, {"op": "status"})
        self.a.service._synchronize(guest, reply["visit"], guest["host_id"])
        replayed = (len(self.a.llm.calls), len(self.b.llm.calls), len(self.operations))
        self.advance(60)
        await self.a.service.tick()
        await self.b.service.tick()
        self.assertEqual((len(self.a.llm.calls), len(self.b.llm.calls), len(self.operations)), replayed)

    async def test_both_same_second_human_suggestions_are_kept_until_the_independent_reply(self):
        visit_id = await self.start_trip(minutes=120, pause=120)
        host = self.b.service._state["visits"][visit_id]
        guest = self.a.service._state["visits"][visit_id]
        await self.a.service.invite(self.scope, self.peer_id, manual=True, reason="去看看花园")
        await self.b.service.invite(self.scope, host["remote"]["id"], manual=True, reason="也去逛逛集市")
        await self.a.service._advance(visit_id)
        self.assertEqual(set(host["outing_requests"]), {host["host_id"], host["guest_id"]})
        self.assertEqual(host["outing_requests"], guest["outing_requests"])
        self.b.llm.default_reply = json.dumps({"say": "先去花园，然后再去集市，好吗？",
            "continue_activity": True, "pause_minutes": 3}, ensure_ascii=False)
        self.a.llm.default_reply = json.dumps({"say": "嗯，就这样走吧。",
            "continue_activity": True, "pause_minutes": 3}, ensure_ascii=False)
        await self.b.service._advance(visit_id)
        self.assertEqual(set(host["outing_request_turn"]), {host["host_id"], host["guest_id"]})
        self.assertEqual(len(pending_outing_requests(host)), 2)
        self.advance(self.cfg.poll_seconds + 1)
        await self.a.service._advance(visit_id)
        self.assertFalse(pending_outing_requests(host))
        self.assertFalse(pending_outing_requests(guest))
        self.assertEqual(host["outing_requests_handled"], guest["outing_requests_handled"])

    async def test_human_suggestion_model_retry_waits_without_repeated_requests_or_inference(self):
        visit_id = await self.start_trip(minutes=120, pause=120)
        await self.a.service.invite(self.scope, self.peer_id, manual=True, reason="一起换个地方走走")
        self.b.llm.default_reply = "unfinished"
        await self.b.service._advance(visit_id)
        host = self.b.service._state["visits"][visit_id]
        self.assertTrue(host.get("model_retry"))
        self.assertEqual(host["status"], "active")
        before = (len(self.a.llm.calls), len(self.b.llm.calls), len(self.operations))
        self.advance(1)
        await self.b.service.tick()
        await self.a.service.tick()
        first = (len(self.a.llm.calls), len(self.b.llm.calls), len(self.operations))
        self.assertEqual(first[:2], before[:2])
        self.advance(1)
        await self.b.service.tick()
        await self.a.service.tick()
        self.assertEqual((len(self.a.llm.calls), len(self.b.llm.calls), len(self.operations)), first)
        self.b.llm.default_reply = self.host_reply(place="花园", pause=3)
        self.advance(host["model_retry"]["next_at"] - self.b.clock.now() + 1)
        await self.b.service.tick()
        self.advance(self.cfg.poll_seconds + 1)
        await self.a.service.tick()
        self.assertEqual(host["outing"]["place"], "花园")
        self.assertFalse(pending_outing_requests(host))

    async def test_request_arriving_after_host_inference_is_not_falsely_marked_discussed(self):
        visit_id = await self.start_trip(minutes=120, pause=3)
        host = self.b.service._state["visits"][visit_id]
        previous_log = copy.deepcopy(host["log"])
        self.advance(181)
        original = self.b.service._decide

        async def decide_then_new_request(identifier, phase):
            decision = await original(identifier, phase)
            await self.b.service.invite(self.scope, host["remote"]["id"], manual=True, reason="想去看看山顶")
            return decision

        self.b.service._decide = decide_then_new_request
        await self.b.service._advance(visit_id)
        self.b.service._decide = original
        self.assertEqual(host["log"], previous_log)
        self.assertFalse(host.get("outing_request_turn"))
        self.assertTrue(pending_outing_requests(host))
        self.b.llm.default_reply = self.host_reply(place="山顶", pause=3)
        await self.b.service._advance(visit_id)
        self.advance(self.cfg.poll_seconds + 1)
        await self.a.service._advance(visit_id)
        self.assertEqual(host["outing"]["place"], "山顶")
        self.assertFalse(pending_outing_requests(host))

    async def test_late_status_failure_does_not_delay_a_newer_request_already_delivered(self):
        visit_id = await self.start_trip(minutes=120, pause=120)
        delivered = asyncio.Event()
        release = asyncio.Event()
        original = self.a.service.transport.request
        hold_first = True

        async def delay_first_response(url, body):
            nonlocal hold_first
            response = await original(url, body)
            if hold_first:
                hold_first = False
                delivered.set()
                await release.wait()
            return response

        self.a.service.transport.request = delay_first_response
        first = asyncio.create_task(self.a.service.invite(self.scope, self.peer_id,
            manual=True, reason="先去花园看看"))
        try:
            await asyncio.wait_for(delivered.wait(), 5)
            second = await self.a.service.invite(self.scope, self.peer_id,
                manual=True, reason="还是先去山顶吧")
        finally:
            release.set()
        old_result = await asyncio.wait_for(first, 5)
        host = self.b.service._state["visits"][visit_id]
        guest = self.a.service._state["visits"][visit_id]
        self.assertEqual(old_result["id"], second["id"])
        self.assertEqual(host["outing_requests"], guest["outing_requests"])
        request = guest["outing_requests"][guest["guest_id"]]
        self.assertEqual(request["text"], "还是先去山顶吧")
        self.assertEqual(request["seq"], 2)
        self.assertFalse(guest.get("manual_continuation"))
        self.assertFalse(guest.get("continuation_error"))
        self.assertEqual(guest["next_at"], self.a.clock.now() + self.cfg.poll_seconds)

    async def test_peer_that_ignores_new_request_keeps_activity_and_retries_after_cooldown(self):
        visit_id = await self.start_trip(minutes=120, pause=120)
        host = self.b.service._state["visits"][visit_id]
        guest = self.a.service._state["visits"][visit_id]
        previous = copy.deepcopy(host["outing"])
        original = self.b.service._accept_outing_request
        # Simulate the previous receiver version, which supports manual visits
        # but ignores the newer human_request field in signed status messages.
        self.b.service._accept_outing_request = lambda visit, sender, payload: None
        before = (len(self.a.llm.calls), len(self.b.llm.calls))
        await self.a.service.invite(self.scope, self.peer_id, manual=True, reason="一起去花园看看")
        self.assertTrue(guest.get("manual_continuation"))
        self.assertIn("尚未确认收到", guest["continuation_error"])
        self.assertEqual(guest["next_at"], self.a.clock.now() + 120)
        self.assertEqual(host["outing"], previous)
        self.assertEqual(guest["outing"], previous)
        self.assertEqual((len(self.a.llm.calls), len(self.b.llm.calls)), before)
        network = len(self.operations)
        for _ in range(3):
            self.advance(30)
            await self.a.service.tick()
            await self.b.service.tick()
        self.assertEqual(len(self.operations), network)
        self.assertEqual((len(self.a.llm.calls), len(self.b.llm.calls)), before)
        self.b.service._accept_outing_request = original
        self.b.llm.default_reply = self.host_reply(place="花园", pause=3)
        self.a.llm.default_reply = self.guest_reply(pause=3)
        self.advance(31)
        await self.a.service.tick()
        await self.b.service.tick()
        self.advance(self.cfg.poll_seconds + 1)
        await self.a.service.tick()
        self.assertFalse(guest.get("manual_continuation"))
        self.assertFalse(guest.get("continuation_error"))
        self.assertEqual(host["outing"], guest["outing"])
        self.assertEqual(host["outing"]["place"], "花园")
        self.assertFalse(pending_outing_requests(host))


if __name__ == "__main__":
    unittest.main()
