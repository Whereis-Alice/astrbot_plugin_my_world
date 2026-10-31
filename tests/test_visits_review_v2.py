"""Concurrent invitations and in-flight protocol work keep one confirmed visit."""
from __future__ import annotations

import asyncio
import copy
import unittest
from dataclasses import replace

from tests import test_visits_protocol as protocol_tests
from core.visits.protocol import envelope, new_id
from core.visits import VisitService


class VisitReviewTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = protocol_tests.VisitProtocolTests.asyncSetUp
    advance = protocol_tests.VisitProtocolTests.advance

    def offer(self):
        peer = self.a._state["peers"][self.peer]
        return envelope(peer["secret"], protocol_tests.public_id(self.a, "a"), self.peer,
                        {"op": "offer", "id": new_id(), "role": "host", "max_calls": 12,
                         "turn_limit": 3, "duration": 1800, "invitation": None}, self.now)

    async def test_simultaneous_invitations_become_one_shared_visit(self):
        both_sending = asyncio.Event()
        sends = 0

        def rendezvous(original):
            async def request(url, body):
                nonlocal sends
                if body["payload"]["op"] == "offer":
                    sends += 1
                    if sends == 2:
                        both_sending.set()
                    await both_sending.wait()
                return await original(url, body)
            return request

        self.a.transport.request = rendezvous(self.a.transport.request)
        self.b.transport.request = rendezvous(self.b.transport.request)
        await asyncio.gather(
            self.a.invite("life-a", self.peer),
            self.b.invite("life-b", protocol_tests.public_id(self.a, "a")),
        )
        await self.advance()
        a = [v for v in self.a._state["visits"].values() if v.get("arrived_at")]
        b = [v for v in self.b._state["visits"].values() if v.get("arrived_at")]
        self.assertEqual(len(a), 1)
        self.assertEqual(len(b), 1)
        self.assertEqual(a[0]["id"], b[0]["id"])
        self.assertEqual(a[0]["log"], b[0]["log"])
        self.assertEqual(a[0]["status"], "completed")
        self.assertEqual(b[0]["status"], "completed")
        self.assertEqual(len(self.a_actor.memories), 1)
        self.assertEqual(len(self.b_actor.memories), 1)

    async def test_concurrent_same_id_changed_payload_is_rejected(self):
        await self.a.invite("life-a", self.peer)
        visit_id = next(iter(self.b._state["visits"]))
        original = self.b_actor.describe
        entered = [asyncio.Event(), asyncio.Event()]
        release = [asyncio.Event(), asyncio.Event()]
        describes = 0

        async def delayed(scope):
            nonlocal describes
            index = describes
            describes += 1
            entered[index].set()
            await release[index].wait()
            return await original(scope)

        self.b_actor.describe = delayed
        peer = self.a._state["peers"][self.peer]
        message_id = new_id()
        first = envelope(peer["secret"], protocol_tests.public_id(self.a, "a"), self.peer,
                         {"op": "status", "id": visit_id}, self.now, message_id=message_id)
        changed = envelope(peer["secret"], first["sender"], first["target"],
                           {"op": "end", "id": visit_id}, self.now, message_id=message_id)
        task_first = asyncio.create_task(self.b.receive(first))
        await entered[0].wait()
        task_changed = asyncio.create_task(self.b.receive(changed))
        await entered[1].wait()
        release[0].set()
        await task_first
        release[1].set()
        with self.assertRaisesRegex(ValueError, "消息内容发生变化"):
            await task_changed
        self.assertEqual(self.b._state["visits"][visit_id]["status"], "offered")

    async def test_removed_peer_cannot_deliver_an_inflight_offer(self):
        request = self.offer()
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.b_actor.describe

        async def delayed(scope):
            entered.set()
            await release.wait()
            return await original(scope)

        self.b_actor.describe = delayed
        task = asyncio.create_task(self.b.receive(request))
        await entered.wait()
        await self.b.remove_peer("life-b", request["sender"])
        release.set()
        with self.assertRaises(ValueError):
            await task
        self.assertIsNone(self.b.active("life-b"))
        self.assertEqual(self.b._state["visits"], {})

    async def test_crossing_offer_cannot_interrupt_an_arrived_visit(self):
        await self.a.invite("life-a", self.peer)
        await self.advance(3)
        current = self.b.active("life-b")
        self.assertIsNotNone(current)
        self.assertTrue(current.get("arrived_at"))
        with self.assertRaisesRegex(ValueError, "已有邀请或串门"):
            await self.b.receive(self.offer())
        self.assertEqual(self.b.active("life-b")["id"], current["id"])
        self.assertEqual(self.b.active("life-b")["log"], current["log"])

    async def test_offer_from_another_friend_cannot_interrupt_pending_invitation(self):
        local = await self.b_actor.describe("life-b")
        remote = {"id": "c" * 32 + ":c", "instance_id": "c" * 32,
                  "actor_id": "c", "name": "c", "world": "c的世界"}
        peer = self.b._add_peer("life-b", local, remote, "http://friend-c.example", "fixture-key")
        visit = self.b._new_visit("life-b", peer, local, new_id(), "guest", "一起读书", False)
        self.b._state["visits"][visit["id"]] = visit
        await self.b._save()
        with self.assertRaisesRegex(ValueError, "已有邀请或串门"):
            await self.b.receive(self.offer())
        self.assertEqual(self.b.active("life-b")["id"], visit["id"])
        self.assertEqual(visit["status"], "inviting")

    async def test_invalid_crossing_offer_cannot_cancel_our_valid_invitation(self):
        await self.b.invite("life-b", protocol_tests.public_id(self.a, "a"))
        current = self.b.active("life-b")
        request = self.offer()
        payload = {**request["payload"], "id": "0" * 32, "duration": -1}
        key = self.a._state["peers"][self.peer]["secret"]
        request = envelope(key, request["sender"], request["target"], payload, self.now)
        with self.assertRaisesRegex(ValueError, "无效停留期限"):
            await self.b.receive(request)
        self.assertEqual(self.b.active("life-b")["id"], current["id"])
        self.assertEqual(self.b.active("life-b")["status"], "inviting")

    async def test_guest_budget_end_releases_host_without_waiting_for_expiry(self):
        self.b.config = replace(self.b.config, max_calls=4)
        await self.a.invite("life-a", self.peer)
        await self.advance(16)
        self.assertIsNone(self.a.active("life-a"))
        self.assertIsNone(self.b.active("life-b"))
        a = copy.deepcopy(next(iter(self.a._state["visits"].values())))
        b = copy.deepcopy(next(iter(self.b._state["visits"].values())))
        self.assertEqual(a["log"], b["log"])
        self.assertEqual(a["status"], "completed")
        self.assertEqual(b["status"], "completed")
        self.assertLess(a["ended_at"], a["expires_at"])
        self.assertLess(b["ended_at"], b["expires_at"])

    async def test_host_final_end_updates_already_finished_guest_memory(self):
        await self.a.invite("life-a", self.peer)
        await self.advance(4)
        a = self.a._state["visits"][self.a.active("life-a")["id"]]
        b = self.b._state["visits"][a["id"]]
        await self.a._finish(a, "completed", "预算结束", notify=False)
        await self.a._remember(a["id"])
        self.assertTrue(a["remembered"])
        self.b._append(b, b["host_id"], "say", "最后记得把茶带回去。")
        await self.b._finish(b, "completed", "告别归家")
        await self.b._notify_end(b["id"])
        self.assertEqual(a["log"], b["log"])
        self.assertFalse(a.get("remembered", False))
        self.assertFalse(a.get("end_pending"))
        await self.a._remember(a["id"])
        self.assertIn("最后记得把茶带回去。", str(self.a_actor.memories[a["id"]]))

    async def test_final_snapshot_cannot_rewrite_an_ended_guests_confirmed_history(self):
        await self.a.invite("life-a", self.peer)
        await self.advance(4)
        a = self.a._state["visits"][self.a.active("life-a")["id"]]
        b = self.b._state["visits"][a["id"]]
        await self.a._finish(a, "completed", "预算结束", notify=False)
        before = copy.deepcopy(a["log"])
        snapshot = self.b._wire(b)
        snapshot.update(status="completed", ended_at=self.now)
        snapshot["log"][0]["text"] = "这句其实没有发生过。"
        peer = self.b._state["peers"][protocol_tests.public_id(self.a, "a")]
        request = envelope(peer["secret"], self.peer, protocol_tests.public_id(self.a, "a"),
                           {"op": "end", "id": a["id"], "visit": snapshot}, self.now)
        with self.assertRaisesRegex(ValueError, "已经|已确认"):
            await self.a.receive(request)
        self.assertEqual(a["log"], before)
        self.assertEqual(a["status"], "completed")

    async def _restart_during_arrived_inference(self, service, actor, db, scope):
        await self.a.invite("life-a", self.peer)
        await self.advance(4)
        visit = service._state["visits"][service.active(scope)["id"]]
        self.assertTrue(visit.get("arrived_at"))
        visit["model_pending"] = "inference-lost-on-reload"
        await service._save()
        instance, peers = service._state["instance_id"], copy.deepcopy(service._state["peers"])
        calls_before = len(self.a_actor.calls) + len(self.b_actor.calls)
        await service.close()
        resumed = VisitService(db, service.config, actor, clock=lambda: self.now)
        self.addAsyncCleanup(resumed.close)
        await resumed.start()
        restored = resumed._state["visits"][visit["id"]]
        self.assertEqual(restored["status"], "interrupted")
        self.assertNotIn("model_pending", restored)
        self.assertTrue(restored.get("end_pending"))
        self.assertEqual(resumed._state["instance_id"], instance)
        self.assertEqual(resumed._state["peers"], peers)
        await resumed.tick()
        self.assertNotIn("end_pending", restored)
        self.assertIsNone(resumed.active(scope))
        other = self.b if service is self.a else self.a
        other_scope = "life-b" if service is self.a else "life-a"
        self.assertIsNone(other.active(other_scope))
        remote = other._state["visits"][visit["id"]]
        self.assertEqual(restored["log"], remote["log"])
        await other.tick()
        self.assertEqual(len(self.a_actor.calls) + len(self.b_actor.calls), calls_before)
        self.assertEqual(self.a_actor.memories[visit["id"]]["log"],
                         self.b_actor.memories[visit["id"]]["log"])

    async def test_reloaded_guest_notifies_host_and_remembers_only_confirmed_visit(self):
        await self._restart_during_arrived_inference(self.a, self.a_actor, self.a_db, "life-a")

    async def test_reloaded_host_notifies_guest_and_remembers_only_confirmed_visit(self):
        await self._restart_during_arrived_inference(self.b, self.b_actor, self.b_db, "life-b")

    async def test_invalid_host_calls_cannot_partly_commit_a_snapshot(self):
        await self.a.invite("life-a", self.peer)
        await self.advance(4)
        visit = self.a._state["visits"][self.a.active("life-a")["id"]]
        host = self.b._state["visits"][visit["id"]]
        before = copy.deepcopy(visit)
        self.b._append(host, host["host_id"], "say", "校验前不能提交这句话。")
        valid = self.b._wire(host)
        for bad_calls in (None, [], "not-calls", {host["host_id"]: host["max_calls"] + 1}):
            with self.subTest(calls=bad_calls):
                snapshot = copy.deepcopy(valid)
                snapshot["calls"] = bad_calls
                with self.assertRaisesRegex(ValueError, "调用额度无效"):
                    self.a._synchronize(visit, snapshot, host["host_id"])
                self.assertEqual(visit, before)

    async def test_remove_peer_finishes_visit_while_old_history_is_pruned(self):
        await self.a.invite("life-a", self.peer)
        active = next(iter(self.a._state["visits"].values()))
        oldest = copy.deepcopy(active)
        oldest.update(id=new_id(), status="completed", ended_at=self.now - 1)
        self.a._state["visits"][oldest["id"]] = oldest
        self.a.config = replace(self.a.config, history_keep=1)
        await self.a.remove_peer("life-a", self.peer)
        self.assertNotIn(self.peer, self.a._state["peers"])
        self.assertIsNone(self.a.active("life-a"))
        self.assertEqual(active["status"], "interrupted")
        self.assertNotIn(oldest["id"], self.a._state["visits"])

    async def test_invalid_goodbye_confirmation_is_bounded_and_keeps_confirmed_history(self):
        await self.a.invite("life-a", self.peer)
        await self.advance(4)
        visit = self.a._state["visits"][self.a.active("life-a")["id"]]
        host = self.b._state["visits"][visit["id"]]
        await self.a._finish(visit, "interrupted", "真人要求归家")
        confirmed = copy.deepcopy(visit["log"])
        remote = self.b._wire(host)
        remote.update(status="completed", ended_at=self.now)
        remote["log"][0]["text"] = "这句不应改写已确认的邀请。"
        attempts = 0

        async def invalid_confirmation(*args):
            nonlocal attempts
            attempts += 1
            return {"visit": copy.deepcopy(remote)}

        self.a._send = invalid_confirmation
        for _ in range(4):
            await self.a._notify_end(visit["id"])
            self.now += 601
        self.assertEqual(attempts, 3)
        self.assertNotIn("end_pending", visit)
        self.assertEqual(visit["log"], confirmed)
        self.assertEqual(visit["status"], "interrupted")

    async def test_host_goodbye_refreshes_same_log_with_final_status(self):
        await self.a.invite("life-a", self.peer)
        await self.advance(4)
        guest = self.a._state["visits"][self.a.active("life-a")["id"]]
        host = self.b._state["visits"][guest["id"]]
        self.a._synchronize(guest, self.b._wire(host), host["host_id"])
        self.assertEqual(guest["log"], host["log"])
        await self.a._finish(guest, "interrupted", "先归家", notify=False)
        await self.a._remember(guest["id"])
        self.assertTrue(guest["remembered"])
        await self.b._finish(host, "completed", "双方确认相见结束")
        await self.b._notify_end(host["id"])
        self.assertEqual(guest["status"], "completed")
        self.assertFalse(guest.get("remembered", False))
        await self.a._remember(guest["id"])
        self.assertEqual(self.a_actor.memories[guest["id"]]["status"], "completed")

    async def test_goodbye_receipt_refreshes_same_log_with_final_status(self):
        await self.a.invite("life-a", self.peer)
        await self.advance(4)
        guest = self.a._state["visits"][self.a.active("life-a")["id"]]
        host = self.b._state["visits"][guest["id"]]
        self.a._synchronize(guest, self.b._wire(host), host["host_id"])
        self.assertEqual(guest["log"], host["log"])
        await self.a._finish(guest, "interrupted", "先归家")
        await self.a._remember(guest["id"])
        await self.b._finish(host, "completed", "双方确认相见结束", notify=False)
        await self.a._notify_end(guest["id"])
        self.assertEqual(guest["status"], "completed")
        self.assertFalse(guest.get("remembered", False))
        await self.a._remember(guest["id"])
        self.assertEqual(self.a_actor.memories[guest["id"]]["status"], "completed")

    async def test_recovered_third_goodbye_attempt_never_sends_a_fourth_request(self):
        await self.a.invite("life-a", self.peer)
        visit = self.a._state["visits"][self.a.active("life-a")["id"]]
        await self.a._finish(visit, "interrupted", "联系结束")
        visit["end_pending"] = {"attempts": 3, "next_at": self.now}

        async def unexpected_request(*args):
            self.fail("The third attempt was already admitted before restart")

        self.a._send = unexpected_request
        await self.a._notify_end(visit["id"])
        self.assertNotIn("end_pending", visit)
        self.assertNotIn("outbox", visit)

    async def test_old_memory_write_cannot_hide_same_log_final_confirmation(self):
        await self.a.invite("life-a", self.peer)
        await self.advance(4)
        guest = self.a._state["visits"][self.a.active("life-a")["id"]]
        host = self.b._state["visits"][guest["id"]]
        self.a._synchronize(guest, self.b._wire(host), host["host_id"])
        await self.a._finish(guest, "interrupted", "先归家", notify=False)
        entered, release = asyncio.Event(), asyncio.Event()
        remember = self.a_actor.remember

        async def delayed(scope, snapshot):
            entered.set()
            await release.wait()
            await remember(scope, snapshot)

        self.a_actor.remember = delayed
        pending = asyncio.create_task(self.a._remember(guest["id"]))
        await asyncio.wait_for(entered.wait(), 2)
        await self.b._finish(host, "completed", "双方确认相见结束")
        await self.b._notify_end(host["id"])
        release.set()
        await asyncio.wait_for(pending, 2)
        self.assertEqual(guest["status"], "completed")
        self.assertFalse(guest.get("remembered", False))
        self.a_actor.remember = remember
        await self.a._remember(guest["id"])
        self.assertEqual(self.a_actor.memories[guest["id"]]["status"], "completed")
