"""The visitor waits for the host's result before starting a new scene turn."""
from __future__ import annotations

import asyncio
import copy
import unittest

from tests import fake_astrbot
fake_astrbot.install()

from tests import test_visits_model_retry as fixture  # noqa: E402
from core.visits.protocol import envelope  # noqa: E402


class VisitSceneTurnHandoffTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.VisitModelRetryTests.asyncSetUp
    offered = fixture.VisitModelRetryTests.offered
    host_turn = fixture.VisitModelRetryTests.host_turn

    async def scene_ready(self):
        visit_id, host = await self.host_turn()
        choice = await self.b_actor.decide("life-b", "turn", host)
        host["event_stage"] = "offered"
        host["action_plans"] = {host["guest_id"]: "访客先看叶子"}
        self.b._record_turn(host, host["host_id"], choice)
        await self.b._save()
        self.assertEqual(len(host["action_plans"]), 2)
        self.assertEqual(host["next_actor"], host["guest_id"])
        self.now = host["next_at"]
        return visit_id, host

    async def test_guest_waits_for_both_choices_to_be_settled_before_new_turn(self):
        visit_id, host = await self.scene_ready()
        before_calls = len(self.a_actor.calls)
        before_turns = copy.deepcopy(host["turns"])
        await self.a._advance(visit_id)
        guest = self.a._state["visits"][visit_id]
        self.assertEqual(len(self.a_actor.calls), before_calls)
        self.assertEqual(guest["turns"], before_turns)
        self.assertEqual(guest["event_stage"], "offered")
        self.assertNotIn("unsent_turn", guest)
        self.assertNotIn("outbox", guest)
        await self.b._scene(visit_id, "event_resolve")
        self.assertEqual(host["event_stage"], "resolved")
        self.now = host["next_at"]
        await self.a._advance(visit_id)
        self.assertEqual(len(self.a_actor.calls), before_calls + 1)
        self.assertEqual(host["turns"][host["guest_id"]], before_turns[host["guest_id"]] + 1)
        self.assertEqual(guest["log"], host["log"])
        results = [index for index, row in enumerate(guest["log"]) if row["kind"] == "result"]
        self.assertEqual(len(results), 1)
        self.assertGreater(len(guest["log"]), results[0] + 1)
        self.assertEqual(guest["status"], "active")

    async def test_inflight_old_scene_step_gets_signed_non_application_then_continues(self):
        visit_id, host = await self.scene_ready()
        await self.a._advance(visit_id)  # Read both action choices, without guessing the result.
        guest = self.a._state["visits"][visit_id]
        old_choice = await self.a._decide(visit_id, "turn")
        guest["unsent_turn"] = True
        before_log = copy.deepcopy(guest["log"])
        before_turns = copy.deepcopy(guest["turns"])
        paid_calls = copy.deepcopy(guest["calls"])
        step = {"op": "step", "seq": len(guest["log"]), "decision":
                {key: old_choice[key] for key in ("say", "action", "leave")}}
        original = self.a.transport.request
        entered, release = asyncio.Event(), asyncio.Event()
        outgoing = []

        async def delayed(url, body):
            if body["payload"]["op"] == "step" and not outgoing:
                outgoing.append(copy.deepcopy(body))
                entered.set()
                await release.wait()
            return await original(url, body)

        self.a.transport.request = delayed
        sending = asyncio.create_task(self.a._send(visit_id, step))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            await self.b._scene(visit_id, "event_resolve")
            self.assertEqual(host["event_stage"], "resolved")
            release.set()
            reply = await asyncio.wait_for(sending, 3)
            self.assertIs(reply["step_superseded"], True)
            self.assertEqual(reply["step_seq"], len(before_log))
            self.assertEqual(host["turns"], before_turns)
            self.assertEqual(host["log"][:len(before_log)], before_log)
            self.assertEqual(host["log"][-1]["kind"], "result")
            async with self.a._lock:
                self.a._synchronize_guest_reply(guest, reply)
                await self.a._save()
            self.assertEqual(guest["log"], host["log"])
            self.assertEqual(guest["calls"][guest["guest_id"]], paid_calls[guest["guest_id"]])
            self.assertNotIn("decision", guest)
            self.assertNotIn("unsent_turn", guest)
            self.assertNotIn("outbox", guest)
            self.assertFalse(self.a_actor.memories)
            before_replay = copy.deepcopy(host["log"])
            await original(self.b.config.public_url, outgoing[0])
            self.assertEqual(host["log"], before_replay)
            self.assertEqual(host["turns"], before_turns)
            self.now = host["next_at"]
            await self.a._advance(visit_id)
            self.assertEqual(host["turns"][host["guest_id"]], before_turns[host["guest_id"]] + 1)
            self.assertEqual(guest["log"], host["log"])
            self.assertEqual(guest["status"], "active")
        finally:
            release.set()
            if not sending.done():
                sending.cancel()
            await asyncio.gather(sending, return_exceptions=True)

    async def test_an_old_step_cannot_ignore_other_confirmed_turns(self):
        _, host = await self.scene_ready()
        seq = len(host["log"])
        self.b._append(host, host["guest_id"], "say", "这是已确认的新回合")
        host["event_stage"] = "resolved"
        self.assertFalse(self.b._scene_superseded_step(host, host["guest_id"], {"seq": seq}))
        with self.assertRaises(ValueError):
            self.b._receive_step(host, host["guest_id"], {"seq": seq, "decision": {"say": "旧稿"}})

    async def test_non_application_reply_requires_same_confirmed_turn_counts(self):
        visit_id, host = await self.scene_ready()
        await self.a._advance(visit_id)
        guest = self.a._state["visits"][visit_id]
        await self.a._decide(visit_id, "turn")
        guest["unsent_turn"] = True
        seq = len(guest["log"])
        await self.b._scene(visit_id, "event_resolve")
        remote = self.b._wire(host)
        remote["turns"][guest["guest_id"]] += 1
        before = copy.deepcopy(guest)
        with self.assertRaisesRegex(ValueError, "未被采用"):
            self.a._synchronize_guest_reply(guest, {"visit": remote, "step_superseded": True,
                "step_seq": seq, "delivered_op": "step"})
        self.assertEqual(guest, before)

    async def test_invalid_signed_non_application_keeps_original_outbox_and_choice(self):
        visit_id, host = await self.scene_ready()
        await self.a._advance(visit_id)
        guest = self.a._state["visits"][visit_id]
        choice = await self.a._decide(visit_id, "turn")
        guest["unsent_turn"] = True
        seq = len(guest["log"])
        step = {"op": "step", "seq": seq, "decision":
                {key: choice[key] for key in ("say", "action", "leave")}}
        await self.b._scene(visit_id, "event_resolve")
        original = self.a.transport.request
        secret = self.a._state["peers"][self.peer]["secret"]
        before_log = copy.deepcopy(guest["log"])
        paid_calls = copy.deepcopy(guest["calls"])
        captured = []

        for mutation in ("seq", "seq_type", "marker_type", "turns", "tail", "orphan"):
            async def invalid(url, body):
                captured.append(copy.deepcopy(body))
                response = await original(url, body)
                changed = copy.deepcopy(response["payload"])
                if mutation == "seq":
                    changed["step_seq"] += 1
                elif mutation == "seq_type":
                    changed["step_seq"] = True
                elif mutation == "marker_type":
                    changed["step_superseded"] = "true"
                elif mutation == "turns":
                    changed["visit"]["turns"][guest["guest_id"]] += 1
                elif mutation == "tail":
                    changed["visit"]["log"][-1]["kind"] = "action_plan"
                else:
                    changed.pop("step_superseded")
                return envelope(secret, response["sender"], response["target"], changed,
                                self.now, message_id=response["message_id"])

            self.a.transport.request = invalid
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                await self.a._send(visit_id, step)
            self.assertEqual(guest["outbox"], captured[0])
            self.assertEqual(guest["log"], before_log)
            self.assertEqual(guest["decision"], choice)
            self.assertEqual(guest["calls"], paid_calls)
            self.assertTrue(guest["unsent_turn"])
            self.assertEqual(guest["status"], "active")
        self.a.transport.request = original
        reply = await self.a._send(visit_id, step)
        self.a._synchronize_guest_reply(guest, reply)
        self.assertEqual(guest["log"], host["log"])
        self.assertNotIn("outbox", guest)
        self.assertNotIn("decision", guest)
