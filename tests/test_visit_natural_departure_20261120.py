"""Natural endings preserve real clocks, both signed voices and durable receipts."""
from __future__ import annotations

import asyncio
import copy
import unittest

from tests import fake_astrbot
fake_astrbot.install()

from core.visits.decisions import VisitDecisionError, record_decision_candidate  # noqa: E402
from core.visits.departure import farewell_id  # noqa: E402
from core.visits.outings import begin_activity  # noqa: E402
from core.visits.protocol import envelope  # noqa: E402
from tests import test_visit_format_pause_20261115 as fixture  # noqa: E402


class NaturalDepartureTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.FormatPauseTests.asyncSetUp
    host_turn = fixture.FormatPauseTests.host_turn
    offered = fixture.FormatPauseTests.offered
    reload_service = fixture.FormatPauseTests.reload_service

    async def ended(self):
        visit_id, host = await self.host_turn()
        guest = self.a._state["visits"][visit_id]
        contexts = []
        for actor in (self.a_actor, self.b_actor):
            original = actor.decide

            async def decide(scope, phase, visit, *, actor=actor, original=original):
                if phase == "departure":
                    actor.calls.append((phase, visit["id"]))
                    contexts.append((actor.name, copy.deepcopy(visit.get("departure_context"))))
                    return {"say": actor.name + "：纸边还没看完，下次再接着看。"}
                return await original(scope, phase, visit)

            actor.decide = decide
        before = copy.deepcopy(host["log"])
        deadline = host["expires_at"]
        self.now = deadline + 7
        async with self.b._lock:
            await self.b._expire_visit(host, "本次停留到期，只保留已确认经过")
        return visit_id, host, guest, before, deadline, contexts

    async def test_both_real_models_say_their_own_goodbye_after_expiry_without_extending_life(self):
        visit_id, host, guest, before, deadline, contexts = await self.ended()
        self.assertIsNone(self.b.active(host["scope"]))
        self.assertEqual(host["ended_at"], deadline)
        await self.b._notify_end(visit_id)
        self.assertEqual(guest["status"], "completed")
        await self.b._observe_memory(visit_id)
        self.assertFalse(self.b_actor.memories)
        self.now += 3
        await self.a._notify_end(visit_id)
        self.assertEqual(host["log"], guest["log"])
        self.assertEqual(host["log"][:len(before)], before)
        self.assertEqual([row["actor_id"] for row in host["log"][-2:]], [host["host_id"], host["guest_id"]])
        self.assertEqual(len(host["departure_farewells"]), 2)
        self.assertEqual(host["calls"], guest["calls"])
        self.assertTrue(all(row["at"] > deadline for row in host["log"][-2:]))
        self.assertEqual(guest["ended_at"], deadline)
        self.assertIn("b：纸边还没看完", contexts[1][1]["friend_farewell"])
        self.assertEqual(sum(phase == "departure" for phase, _ in self.a_actor.calls), 1)
        self.assertEqual(sum(phase == "departure" for phase, _ in self.b_actor.calls), 1)
        await self.a._observe_memory(visit_id)
        await self.b._observe_memory(visit_id)
        await asyncio.sleep(.06)
        self.assertEqual(self.a_actor.memories[visit_id]["log"], host["log"])
        self.assertEqual(self.b_actor.memories[visit_id]["log"], host["log"])
        self.assertNotIn(visit_id, self.a._departure_tasks)
        self.assertNotIn(visit_id, self.b._departure_tasks)

    async def test_quote_delivery_retries_stop_at_the_original_closing_deadline(self):
        visit_id, host, _, _, _, _ = await self.ended()
        packets = []

        async def unavailable(url, body):
            packets.append(copy.deepcopy(body))
            raise ValueError("暂时不可达")

        self.b.transport.request = unavailable
        await self.b._notify_end(visit_id)
        self.assertTrue(host.get("outbox"))
        self.assertEqual(len(packets), 1)
        self.now = host["departure"]["deadline"]
        await self.b._notify_end(visit_id)
        self.now += 600
        await self.b._notify_end(visit_id)
        self.assertEqual(len(packets), 1)
        self.assertNotIn("end_pending", host)
        self.assertNotIn("outbox", host)
        self.assertEqual(host["departure"]["state"], "done")

    async def test_unissued_admission_write_failure_refunds_the_call_before_a_bounded_retry(self):
        visit_id, host, _, _, _, _ = await self.ended()
        original = self.b.db.call
        writes = []

        async def fail_first(operation, *args, **kwargs):
            if operation == "kv_set":
                writes.append(operation)
                if len(writes) == 1:
                    raise OSError("这次预留尚未保存")
            return await original(operation, *args, **kwargs)

        self.b.db.call = fail_first
        calls = host["calls"][host["host_id"]]
        with self.assertRaises(OSError):
            await self.b._advance_departure(visit_id)
        self.assertEqual(host["calls"][host["host_id"]], calls)
        self.assertFalse(any(phase == "departure" for phase, _ in self.b_actor.calls))
        self.assertNotIn("inflight", host["departure"])
        self.assertNotIn(visit_id, self.b._departure_tasks)
        self.b.db.call = original
        self.now = host["departure"]["next_at"]
        await self.b._notify_end(visit_id)
        self.assertEqual(host["calls"][host["host_id"]], calls + 1)
        self.assertEqual(sum(phase == "departure" for phase, _ in self.b_actor.calls), 1)

    async def test_natural_goodbye_above_one_hundred_characters_keeps_the_shared_say_limit(self):
        visit_id, host, _, _, _, _ = await self.ended()
        words = "下次来接着看看纸边，我会记住这个还没完的地方。" * 6

        async def longer(scope, phase, visit):
            self.b_actor.calls.append((phase, visit["id"]))
            return {"say": words}

        self.b_actor.decide = longer
        await self.b._notify_end(visit_id)
        self.assertGreater(len(words), 100)
        self.assertLess(len(words), 400)
        self.assertEqual(host["departure"]["quote"]["text"], words)
        self.assertEqual(host["departure"]["state"], "ready")
        self.assertEqual(sum(phase == "departure" for phase, _ in self.b_actor.calls), 1)

    async def test_saved_guest_goodbye_is_delivered_after_reload_without_another_model_call(self):
        visit_id, host, guest, _, _, _ = await self.ended()
        await self.b._notify_end(visit_id)
        await self.a._advance_departure(visit_id)
        quote = copy.deepcopy(guest["departure"]["quote"])
        calls = len(self.a_actor.calls)
        self.a = await self.reload_service(self.a)
        await self.a._notify_end(visit_id)
        restored = self.a._state["visits"][visit_id]
        self.assertEqual(len(self.a_actor.calls), calls)
        self.assertEqual(restored["departure"]["quote"], quote)
        self.assertEqual(restored["log"], host["log"])
        self.assertEqual(sum(row["text"] == quote["text"] for row in host["log"]), 1)

    async def test_lost_end_reply_reuses_exact_packet_and_never_duplicates_a_goodbye(self):
        visit_id, host, _, _, _, _ = await self.ended()
        original = self.b.transport.request
        packets = []

        async def lose_first(url, body):
            packets.append(copy.deepcopy(body))
            result = await original(url, body)
            if len(packets) == 1:
                raise ValueError("回执暂时丢失")
            return result

        self.b.transport.request = lose_first
        await self.b._notify_end(visit_id)
        self.now = host["end_pending"]["next_at"]
        await self.b._notify_end(visit_id)
        self.assertEqual(len(packets), 2)
        self.assertEqual(packets[0]["message_id"], packets[1]["message_id"])
        self.assertEqual(packets[0]["payload"], packets[1]["payload"])
        self.assertEqual(sum(phase == "departure" for phase, _ in self.b_actor.calls), 1)
        await self.a._notify_end(visit_id)
        self.assertEqual(len(host["departure_farewells"]), 2)

    async def test_repeated_model_failure_stops_after_three_real_calls_and_keeps_fact_log(self):
        visit_id, host, _, before, _, _ = await self.ended()
        attempts = []

        async def unavailable(scope, phase, visit):
            attempts.append(phase)
            raise VisitDecisionError("provider")

        self.b_actor.decide = unavailable
        calls = host["calls"][host["host_id"]]
        for delay in (30, 60, 0):
            await self.b._advance_departure(visit_id)
            self.now += delay
        self.assertEqual(attempts, ["departure"] * 3)
        self.assertEqual(host["calls"][host["host_id"]], calls + 3)
        self.assertEqual(host["departure"]["state"], "failed")
        self.assertEqual(host["log"], before)
        self.now += 600
        await self.b._advance_departure(visit_id)
        self.assertEqual(len(attempts), 3)
        self.assertIsNone(self.b.active(host["scope"]))

    async def test_identity_change_prevents_a_new_departure_call(self):
        visit_id, host, _, before, _, _ = await self.ended()
        self.b_actor.name = "a-different-person"
        await self.b._advance_departure(visit_id)
        self.assertFalse(any(phase == "departure" for phase, _ in self.b_actor.calls))
        self.assertEqual(host["departure"]["state"], "failed")
        self.assertEqual(host["log"], before)

    async def test_host_cannot_append_an_unsubmitted_guest_goodbye(self):
        visit_id, host, guest, _, _, _ = await self.ended()
        await self.b._notify_end(visit_id)
        forged = self.b._wire(host)
        for key in [key for key in forged if key.startswith("log_")]:
            forged.pop(key)
        quote = {"id": farewell_id(visit_id, guest["guest_id"], "她并没有说的告别", self.now),
                 "actor_id": guest["guest_id"], "text": "她并没有说的告别", "at": self.now}
        number = len(forged["log"]) + 1
        forged["log"].append({"seq": number, "actor_id": guest["guest_id"], "kind": "say",
                              "text": quote["text"], "at": quote["at"]})
        forged["departure_farewells"][guest["guest_id"]] = {**quote, "log_seq": number}
        before = copy.deepcopy(guest["log"])
        with self.assertRaisesRegex(ValueError, "不能补写"):
            self.a._synchronize(guest, forged, guest["host_id"], allow_terminal=True)
        self.assertEqual(guest["log"], before)

    async def test_previously_agreed_farewells_are_reused_without_models_or_duplicate_log_rows(self):
        visit_id, host = await self.host_turn()
        host["outing_host_farewell"] = "下次再一起看纸边。"
        begin_activity(host, {"place": "窗边", "activity": "看纸边", "minutes": 1, "return_after": True},
                       self.now, {"return_after": True, "farewell": "嗯，回家见。"})
        self.now = host["outing"]["until"]
        before_calls = len(self.b_actor.calls)
        self.assertTrue(await self.b._complete_outing_if_due(host))
        before = copy.deepcopy(host["log"])
        await self.b._advance_departure(visit_id)
        self.assertEqual(len(self.b_actor.calls), before_calls)
        self.assertEqual(host["log"], before)
        self.assertEqual(len(host["departure_farewells"]), 2)

    async def test_slow_model_does_not_delay_the_initial_end_notice_or_the_other_life(self):
        visit_id, host, guest, _, deadline, _ = await self.ended()
        release = asyncio.Event()
        packets = []
        request = self.b.transport.request

        async def observe(url, body):
            packets.append(copy.deepcopy(body))
            return await request(url, body)

        async def slow(scope, phase, visit):
            self.b_actor.calls.append((phase, visit["id"]))
            await release.wait()
            return {"say": "纸先留在这里，下次再来看看。"}

        self.b.transport.request = observe
        self.b_actor.decide = slow
        await self.b._notify_end(visit_id)
        self.assertEqual(guest["status"], "completed")
        self.assertEqual(guest["ended_at"], deadline)
        self.assertIsNone(self.a.active(guest["scope"]))
        self.assertNotIn("farewell", packets[0]["payload"])
        release.set()
        await self.b._departure_tasks[visit_id]
        await self.b._notify_end(visit_id)
        self.assertEqual(len(packets), 2)
        self.assertNotEqual(packets[0]["message_id"], packets[1]["message_id"])
        self.assertIn("farewell", packets[1]["payload"])
        await self.a._notify_end(visit_id)
        self.assertEqual(host["log"], guest["log"])
        self.assertEqual(len(host["departure_farewells"]), 2)

    async def test_model_finishing_after_the_closing_window_keeps_calls_but_does_not_append_its_draft(self):
        visit_id, host, _, before, deadline, _ = await self.ended()
        release = asyncio.Event()

        async def slow(scope, phase, visit):
            await release.wait()
            return {"say": "这个迟到草稿不能当成已经告别。"}

        self.b_actor.decide = slow
        calls = host["calls"][host["host_id"]]
        await self.b._advance_departure(visit_id)
        self.now = host["departure"]["deadline"] + 1
        release.set()
        await self.b._departure_tasks[visit_id]
        self.assertEqual(host["departure"]["state"], "failed")
        self.assertEqual(host["calls"][host["host_id"]], calls + 1)
        self.assertEqual(host["log"], before)
        self.assertEqual(host["ended_at"], deadline)
        self.assertNotIn("quote", host["departure"])

    async def test_known_candidate_survives_reload_verification_cancellation_without_double_charging(self):
        visit_id, host, guest, _, _, _ = await self.ended()
        await self.b._notify_end(visit_id)
        prepared = asyncio.Event()

        async def candidate_then_wait(scope, phase, visit):
            self.a_actor.calls.append((phase, visit["id"]))
            record_decision_candidate({"say": "未完的纸边下次一起看。"})
            prepared.set()
            await asyncio.Event().wait()

        self.a_actor.decide = candidate_then_wait
        await self.a._advance_departure(visit_id)
        await prepared.wait()
        charged = guest["calls"][guest["guest_id"]]
        self.a = await self.reload_service(self.a)
        restored = self.a._state["visits"][visit_id]
        self.assertIn("candidate", restored["departure"])
        await self.a._notify_end(visit_id)
        self.assertEqual(restored["calls"][restored["guest_id"]], charged)
        self.assertEqual(sum(phase == "departure" for phase, _ in self.a_actor.calls), 1)
        self.assertEqual(restored["log"], host["log"])
        self.assertEqual(sum(row["text"] == "未完的纸边下次一起看。" for row in host["log"]), 1)

    async def test_an_old_completed_record_cannot_start_a_new_five_minute_departure(self):
        _, host, _, before, _, _ = await self.ended()
        host.pop("departure")
        self.now += 86400
        self.b._prepare_departure(host)
        self.assertNotIn("departure", host)
        sender = host["guest_id"]
        quote = {"id": farewell_id(host["id"], sender, "事后补演的告别", self.now),
                 "actor_id": sender, "text": "事后补演的告别", "at": self.now}
        with self.assertRaisesRegex(ValueError, "原收尾时窗"):
            self.b._accept_departure_farewell(host, sender, {"departure_version": 1, "farewell": quote}, sent_at=self.now)
        self.assertNotIn("departure", host)
        self.assertEqual(host["log"], before)

    async def test_new_backdated_quote_is_rejected_without_changing_the_host_journal(self):
        _, host, guest, before, _, _ = await self.ended()
        sender = guest["guest_id"]
        at = host["arrived_at"] + 1
        quote = {"id": farewell_id(host["id"], sender, "这句不能回填到刚到访时", at),
                 "actor_id": sender, "text": "这句不能回填到刚到访时", "at": at}
        old_state = copy.deepcopy(host["departure"])
        with self.assertRaises(ValueError):
            await self.a._send(host["id"], {"op": "end", "visit": self.a._wire(guest),
                                          "departure_version": 1, "farewell": quote})
        self.assertEqual(host["log"], before)
        self.assertEqual(host["departure"], old_state)

    async def test_legacy_end_ack_stops_waiting_for_an_unavailable_bilateral_feature(self):
        visit_id, host, _, _, _, _ = await self.ended()
        original = self.b.transport.request
        key = self.b._find_peer(host["scope"], host["remote"]["id"])["secret"]

        async def legacy_ack(url, body):
            response = await original(url, body)
            payload = copy.deepcopy(response["payload"])
            payload.pop("departure_version", None)
            payload.pop("farewell_received", None)
            return envelope(key, response["sender"], response["target"], payload, self.now)

        self.b.transport.request = legacy_ack
        await self.b._notify_end(visit_id)
        self.assertFalse(host["departure"]["peer_supported"])
        self.assertEqual(host["departure"]["state"], "done")
        calls = len(self.b_actor.calls)
        self.now += 600
        await self.b._notify_end(visit_id)
        self.assertEqual(len(self.b_actor.calls), calls)
        self.assertEqual(len(host["departure_farewells"]), 1)


if __name__ == "__main__":
    unittest.main()
