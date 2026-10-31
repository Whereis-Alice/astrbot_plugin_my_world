"""Two independent servers exchange actual signed messages and keep separate memories."""
from __future__ import annotations

from dataclasses import replace
import copy
import tempfile
from pathlib import Path
import unittest

from tests import fake_astrbot
fake_astrbot.install()

from core.db import AsyncDatabase  # noqa: E402 - install the SDK fixture before core imports
from core.visits import VisitConfig, VisitService  # noqa: E402
from core.visits.config import normalize_url  # noqa: E402
from core.visits.protocol import envelope, verify  # noqa: E402


class Actor:
    def __init__(self, name):
        self.name, self.calls, self.memories = name, [], {}
        self.ready, self.accept = True, True

    async def describe(self, scope):
        return {"actor_id": self.name, "persona_key": "persona-" + self.name,
                "name": self.name, "world": self.name + "的世界", "ready": self.ready,
                "ready_reason": "正在休息" if not self.ready else "", "home_node": "home",
                "chat_busy": False, "local_date": "2026-11-01"}

    async def decide(self, scope, phase, visit):
        self.calls.append((phase, visit["id"]))
        event = "茶叶不够了" if visit.get("scene_stage") == "event_offer" else ""
        return {"accept": self.accept and self.ready, "say": self.name + "：" + phase,
                "action": "各自提出自己的办法", "thought": "我的私有内心", "reflection": self.name + "自己的感受",
                "scene": {"place": "客厅", "activity": "一起喝茶", "event": event},
                "outcome": "根据双方的办法泡好了茶"}

    async def remember(self, scope, visit):
        if visit.get("arrived_at") and visit["status"] != "declined":
            self.memories[visit["id"]] = copy.deepcopy(visit)


class VisitProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 1793505600.0
        self.a_actor, self.b_actor = Actor("a"), Actor("b")
        config = VisitConfig(enabled=True, listen_host="127.0.0.1", listen_port=0, auto_enabled=False)
        self.a_db = AsyncDatabase(Path(self.directory.name) / "a.db")
        self.b_db = AsyncDatabase(Path(self.directory.name) / "b.db")
        self.addAsyncCleanup(self.a_db.close)
        self.addAsyncCleanup(self.b_db.close)
        self.a = VisitService(self.a_db, config, self.a_actor, clock=lambda: self.now, rng=lambda: .9)
        self.b = VisitService(self.b_db, config, self.b_actor, clock=lambda: self.now, rng=lambda: .9)
        await self.a.start()
        await self.b.start()
        self.a.config = replace(config, public_url=f"http://127.0.0.1:{self.a.transport.bound_port}")
        self.b.config = replace(config, public_url=f"http://127.0.0.1:{self.b.transport.bound_port}")
        self.addAsyncCleanup(self.a.close)
        self.addAsyncCleanup(self.b.close)
        code = await self.b.create_pair_code("life-b")
        peer = await self.a.pair("life-a", self.b.config.public_url, code["code"], "朋友b")
        self.peer = peer["id"]

    async def advance(self, count=24):
        for _ in range(count):
            self.now += 65
            await self.a.tick()
            await self.b.tick()

    async def test_invitation_precedes_arrival_and_each_model_keeps_own_memory(self):
        pending = await self.a.invite("life-a", self.peer, reason="一起喝茶")
        self.assertEqual(pending["status"], "inviting")
        self.assertNotIn("arrived_at", pending)
        self.assertEqual(len(self.a_actor.calls), 1)
        self.assertEqual(self.b_actor.calls, [])
        await self.advance()
        a = (await self.a.overview("life-a"))["visits"][0]
        b = (await self.b.overview("life-b"))["visits"][0]
        self.assertEqual(a["status"], "completed", a)
        self.assertEqual(a["log"], b["log"])
        self.assertIsNone(self.a.active("life-a"))
        self.assertEqual(len(self.a_actor.memories), 1)
        self.assertEqual(len(self.b_actor.memories), 1)
        self.assertLessEqual(len(self.a_actor.calls) + len(self.b_actor.calls), 12)
        self.assertNotIn("我的私有内心", str(a))
        self.assertNotIn("secret", str(await self.a.overview("life-a")))
        self.assertIn("a自己的感受", str(next(iter(self.a_actor.memories.values()))["private"]))

    async def test_host_can_invite_the_other_world_to_come_over(self):
        await self.a.invite("life-a", self.peer, direction="incoming")
        await self.advance()
        a = (await self.a.overview("life-a"))["visits"][0]
        self.assertEqual(a["role"], "host")
        self.assertEqual(a["status"], "completed", a)
        self.assertTrue(any(row["kind"] == "say" and row["actor_id"].endswith(":b") for row in a["log"]))

    async def test_rejection_never_records_a_visit(self):
        self.b_actor.accept = False
        await self.a.invite("life-a", self.peer)
        await self.advance(8)
        self.assertEqual((await self.a.overview("life-a"))["visits"][0]["status"], "declined")
        self.assertEqual(self.a_actor.memories, {})
        self.assertEqual(self.b_actor.memories, {})

    async def test_pair_code_is_one_use_and_messages_are_authenticated(self):
        code = await self.b.create_pair_code("life-b")
        await self.a.pair("life-a", self.b.config.public_url, code["code"])
        with self.assertRaises(ValueError):
            await self.a.pair("life-a", self.b.config.public_url, code["code"])
        peer = self.a._state["peers"][self.peer]
        body = envelope("wrong-key", "untrusted", self.peer, {"op": "status"}, self.now)
        with self.assertRaises(ValueError):
            await self.b.receive(body)
        good = envelope(peer["secret"], public_id(self.a, "a"), self.peer, {"op": "end", "id": "absent"}, self.now)
        good["payload"]["op"] = "offer"
        with self.assertRaises(ValueError):
            verify(good, peer["secret"], self.now)

    async def test_replayed_offer_returns_same_receipt_without_model_or_duplicate_trip(self):
        original = self.a.transport.request
        captured = []
        async def capture(url, body):
            captured.append(copy.deepcopy(body))
            return await original(url, body)
        self.a.transport.request = capture
        await self.a.invite("life-a", self.peer)
        request = captured[-1]
        first = await self.b.receive(request)
        second = await self.b.receive(request)
        self.assertEqual(first, second)
        self.assertEqual(len(self.b._state["visits"]), 1)
        self.assertEqual(self.b_actor.calls, [])

    async def test_budget_is_negotiated_and_failure_does_not_get_free_retries(self):
        self.b.config = replace(self.b.config, max_calls=4)
        await self.a.invite("life-a", self.peer)
        await self.advance()
        self.assertLessEqual(len(self.a_actor.calls) + len(self.b_actor.calls), 4)
        self.assertIsNone(self.a.active("life-a"))

    async def test_disconnect_returns_home_without_inventing_finished_activity(self):
        await self.a.invite("life-a", self.peer)
        await self.advance(4)
        await self.b.close()
        await self.advance(24)
        a = (await self.a.overview("life-a"))["visits"][0]
        self.assertIn(a["status"], {"interrupted", "expired"})
        self.assertFalse(any(row["kind"] == "result" for row in a["log"]))
        self.assertIsNone(self.a.active("life-a"))

    async def test_restart_preserves_identity_and_drops_incomplete_inference(self):
        await self.a.invite("life-a", self.peer)
        visit = next(iter(self.a._state["visits"].values()))
        visit["model_pending"] = "lost-inference"
        await self.a._save()
        instance = self.a._state["instance_id"]
        await self.a.close()
        resumed = VisitService(self.a_db, replace(self.a.config, enabled=False), self.a_actor, clock=lambda: self.now)
        self.addAsyncCleanup(resumed.close)
        await resumed.start()
        self.assertEqual(resumed._state["instance_id"], instance)
        self.assertEqual(resumed._state["visits"][visit["id"]]["status"], "interrupted")
        self.assertEqual(len(self.a_actor.calls), 1)

    async def test_automatic_draw_is_local_once_daily_and_remains_used_after_restart(self):
        self.a.config = replace(self.a.config, auto_enabled=True, auto_probability=1)
        self.a._random = lambda: 0
        await self.a.tick()
        initial = len(self.a_actor.calls)
        for _ in range(4):
            await self.a.tick()
        self.assertEqual(len(self.a_actor.calls), initial)
        self.assertEqual(self.a._state["draws"]["life-a"]["count"], 1)
        await self.a.return_home("life-a")
        await self.a.tick()
        self.assertEqual(len(self.a_actor.calls), initial)

    async def test_optional_feature_does_not_bind_a_port(self):
        off = VisitService(self.a_db, VisitConfig(), self.a_actor)
        await off.start()
        self.assertIsNone(off.transport.runner)
        await off.close()

    async def test_lost_offer_reply_still_negotiates_the_smaller_budget(self):
        self.b.config = replace(self.b.config, max_calls=4)
        original = self.a.transport.request
        lost = False
        async def losing(url, body):
            nonlocal lost
            result = await original(url, body)
            if body["payload"]["op"] == "offer" and not lost:
                lost = True
                raise ValueError("lost reply")
            return result
        self.a.transport.request = losing
        await self.a.invite("life-a", self.peer)
        await self.advance()
        self.assertLessEqual(len(self.a_actor.calls) + len(self.b_actor.calls), 4)

    async def test_lost_step_reply_reuses_delivery_but_next_turn_is_new(self):
        original = self.a.transport.request
        lost = False
        async def losing(url, body):
            nonlocal lost
            result = await original(url, body)
            if body["payload"]["op"] == "step" and not lost:
                lost = True
                raise ValueError("lost reply")
            return result
        self.a.transport.request = losing
        await self.a.invite("life-a", self.peer)
        await self.advance()
        self.assertEqual(sum(phase == "turn" for phase, _ in self.a_actor.calls), 3)
        a, b = (await self.a.overview("life-a"))["visits"][0], (await self.b.overview("life-b"))["visits"][0]
        self.assertEqual(a["log"], b["log"])

    async def test_common_event_waits_for_both_choices_and_is_settled_once(self):
        self.b._random = lambda: .1
        await self.a.invite("life-a", self.peer)
        await self.advance()
        a = (await self.a.overview("life-a"))["visits"][0]
        self.assertEqual(a["status"], "completed")
        self.assertEqual(sum(row["kind"] == "event" for row in a["log"]), 1)
        self.assertEqual(sum(row["kind"] == "result" for row in a["log"]), 1)
        self.assertLessEqual(len(self.a_actor.calls) + len(self.b_actor.calls), 12)

    async def test_expired_model_result_cannot_arrive_after_the_deadline(self):
        decide = self.a_actor.decide
        async def slow(scope, phase, visit):
            result = await decide(scope, phase, visit)
            self.now += 1900
            return result
        self.a_actor.decide = slow
        result = await self.a.invite("life-a", self.peer)
        self.assertEqual(result["status"], "expired")
        self.assertNotIn("arrived_at", result)
        self.assertEqual(self.b_actor.calls, [])


def public_id(service, actor):
    return service._state["instance_id"] + ":" + actor


class AddressTests(unittest.TestCase):
    def test_domain_direct_port_and_ipv6(self):
        self.assertEqual(normalize_url("friend.example"), "https://friend.example")
        self.assertEqual(normalize_url("127.0.0.1"), "http://127.0.0.1:38471")
        self.assertEqual(normalize_url("127.0.0.1:12345"), "http://127.0.0.1:12345")
        self.assertEqual(normalize_url("https://friend.example/visits/"), "https://friend.example/visits")
        self.assertEqual(normalize_url("[::1]:38471"), "http://[::1]:38471")
        for invalid in ("https://user:password@friend.example", "ftp://friend.example", "http://x/a/../b", "https://x?key=secret"):
            with self.assertRaises(ValueError):
                normalize_url(invalid)
