"""A saved decision is consumed once across reloads, within its original phase."""
from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
import unittest
from unittest.mock import patch

from core.visits import VisitService
from tests import test_visits_protocol as fixture


class VisitDecisionResumeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.VisitProtocolTests.asyncSetUp

    async def reload_service(self, service):
        await service.close()
        resumed = VisitService(service.db, service.config, service.actor_port,
                               clock=lambda: self.now, rng=lambda: .9)
        self.addAsyncCleanup(resumed.close)
        await resumed._load()
        return resumed

    async def offered(self):
        pending = await self.a.invite("life-a", self.peer)
        return pending["id"], self.b._state["visits"][pending["id"]]

    async def host_turn(self):
        visit_id, visit = await self.offered()
        await self.b._advance(visit_id)
        self.b._append(visit, visit["guest_id"], "say", "访客已经交付的回合")
        visit["turns"][visit["guest_id"]] = 1
        visit["next_actor"] = visit["host_id"]
        await self.b._save()
        return visit_id, visit

    async def test_saved_receive_is_applied_after_real_database_reload_without_regeneration(self):
        visit_id, visit = await self.offered()
        await self.b._decide(visit_id, "receive")
        before = len(self.b_actor.calls)
        resumed = await self.reload_service(self.b)
        await resumed._advance(visit_id)
        self.assertEqual(len(self.b_actor.calls), before)
        self.assertEqual(resumed._state["visits"][visit_id]["status"], "active")
        self.assertTrue(any(row["text"] == "b：receive"
                            for row in resumed._state["visits"][visit_id]["log"]))

    async def test_saved_host_turn_is_applied_after_reload_without_regeneration(self):
        visit_id, visit = await self.host_turn()
        await self.b._decide(visit_id, "turn")
        before = len(self.b_actor.calls)
        resumed = await self.reload_service(self.b)
        await resumed._advance(visit_id)
        current = resumed._state["visits"][visit_id]
        self.assertEqual(len(self.b_actor.calls), before)
        self.assertEqual(current["turns"][current["host_id"]], 1)
        self.assertEqual(sum(row["text"] == "b：turn" for row in current["log"]), 1)

    async def assert_receive_gate_after_reload(self, changed, reason, status="declined"):
        visit_id, visit = await self.offered()
        await self.b._decide(visit_id, "receive")
        resumed = await self.reload_service(self.b)
        original = self.b_actor.describe

        async def describe(scope):
            result = await original(scope)
            result.update(changed)
            return result

        self.b_actor.describe = describe
        await resumed._advance(visit_id)
        current = resumed._state["visits"][visit_id]
        self.assertEqual(len(self.b_actor.calls), 1)
        self.assertEqual(current["status"], status)
        self.assertEqual(current["end_reason"], reason)
        self.assertNotIn("arrived_at", current)
        self.assertNotIn("我的私有内心", str(resumed._visible(current)))
        if status == "declined":
            self.assertEqual(current["log"][-1]["kind"], "contact_status")
            self.assertEqual(current["log"][-1]["text"], reason)

    async def test_cached_receive_rechecks_closed_reception_without_a_model(self):
        await self.assert_receive_gate_after_reload({"reception_open": False}, "现在不在接待时间，稍后再约")

    async def test_cached_receive_rechecks_busy_life_without_a_model(self):
        await self.assert_receive_gate_after_reload({"ready": False, "ready_reason": "正在休息"}, "正在休息")

    async def test_cached_receive_rechecks_human_chat_without_a_model(self):
        await self.assert_receive_gate_after_reload({"chat_busy": True}, "正在接眼前的交谈，稍后再约")

    async def test_cached_receive_rechecks_identity_without_a_model(self):
        await self.assert_receive_gate_after_reload({"persona_key": "another-persona"},
                                                  "生活身份变化，本次联系结束", "interrupted")

    async def test_receive_cache_query_cannot_reactivate_after_close(self):
        visit_id, visit = await self.offered()
        await self.b._decide(visit_id, "receive")
        resumed = await self.reload_service(self.b)
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.b_actor.describe

        async def slow(scope):
            result = await original(scope)
            entered.set()
            await release.wait()
            return result

        self.b_actor.describe = slow
        task = asyncio.create_task(resumed._advance(visit_id))
        await entered.wait()
        await resumed.close()
        release.set()
        await task
        current = resumed._state["visits"][visit_id]
        self.assertEqual(current["status"], "offered")
        self.assertNotIn("arrived_at", current)
        self.assertEqual(len(self.b_actor.calls), 1)

    async def test_receive_cache_query_cannot_overwrite_a_concurrent_activation(self):
        visit_id, visit = await self.offered()
        await self.b._decide(visit_id, "receive")
        resumed = await self.reload_service(self.b)
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.b_actor.describe

        async def slow(scope):
            result = await original(scope)
            entered.set()
            await release.wait()
            return result

        self.b_actor.describe = slow
        task = asyncio.create_task(resumed._advance(visit_id))
        await entered.wait()
        current = resumed._state["visits"][visit_id]
        async with resumed._lock:
            resumed._activate(current)
            await resumed._save()
        confirmed = copy.deepcopy(current)
        release.set()
        await task
        self.assertEqual(current, confirmed)
        self.assertEqual(len(self.b_actor.calls), 1)

    async def test_saved_final_budget_receive_is_reused_before_exhaustion_check(self):
        self.a.config = replace(self.a.config, max_calls=2)
        self.b.config = replace(self.b.config, max_calls=2)
        visit_id, visit = await self.offered()
        await self.b._decide(visit_id, "receive")
        self.assertEqual(visit["calls"][visit["host_id"]], 1)
        resumed = await self.reload_service(self.b)
        await resumed._advance(visit_id)
        self.assertEqual(len(self.b_actor.calls), 1)
        self.assertEqual(resumed._state["visits"][visit_id]["status"], "active")

    async def test_expired_saved_receive_does_not_arrive_or_regenerate(self):
        visit_id, visit = await self.offered()
        await self.b._decide(visit_id, "receive")
        self.now = visit["expires_at"] + 1
        resumed = await self.reload_service(self.b)
        await resumed._advance(visit_id)
        current = resumed._state["visits"][visit_id]
        self.assertEqual(len(self.b_actor.calls), 1)
        self.assertEqual(current["status"], "expired")
        self.assertNotIn("arrived_at", current)

    async def test_advancing_to_another_turn_requires_a_new_decision(self):
        visit_id, visit = await self.host_turn()
        await self.b._decide(visit_id, "turn")
        resumed = await self.reload_service(self.b)
        await resumed._advance(visit_id)
        current = resumed._state["visits"][visit_id]
        resumed._append(current, current["guest_id"], "say", "访客的下一回合")
        current["turns"][current["guest_id"]] += 1
        current["next_actor"] = current["host_id"]
        await resumed._save()
        before = len(self.b_actor.calls)
        await resumed._decide(visit_id, "turn")
        self.assertEqual(len(self.b_actor.calls), before + 1)

    async def test_saved_scene_is_applied_after_reload_without_regeneration(self):
        visit_id, visit = await self.host_turn()
        visit["scene_stage"] = "event_offer"
        await self.b._decide(visit_id, "scene")
        before = len(self.b_actor.calls)
        resumed = await self.reload_service(self.b)
        await resumed._scene(visit_id, "event_offer")
        current = resumed._state["visits"][visit_id]
        self.assertEqual(len(self.b_actor.calls), before)
        self.assertEqual(current["event_stage"], "offered")
        self.assertEqual(sum(row["text"] == "茶叶不够了" for row in current["log"]), 1)

    async def test_legacy_decision_without_cursor_is_not_guessed(self):
        visit_id, visit = await self.offered()
        visit["decision"] = {"accept": True, "say": "无法确认阶段的旧结果"}
        await self.b._save()
        resumed = await self.reload_service(self.b)
        await resumed._advance(visit_id)
        self.assertEqual(len(self.b_actor.calls), 1)
        self.assertNotIn("无法确认阶段的旧结果", str(resumed._state["visits"][visit_id]["log"]))

    async def test_each_cursor_component_invalidates_the_saved_output(self):
        visit_id, visit = await self.host_turn()
        await self.b._decide(visit_id, "turn")
        baseline = copy.deepcopy(visit)
        changes = {
            "phase": lambda item: None,
            "status": lambda item: item.update(status="offered"),
            "log_seq": lambda item: item["log"].append({"seq": len(item["log"]) + 1,
                "actor_id": item["guest_id"], "kind": "say", "text": "新的确认", "at": self.now}),
            "turns": lambda item: item["turns"].update({item["guest_id"]: 2}),
            "event_stage": lambda item: item.update(event_stage="offered"),
            "scene_stage": lambda item: item.update(scene_stage="event_resolve"),
            "next_actor": lambda item: item.update(next_actor=item["guest_id"]),
        }
        for name, mutate in changes.items():
            with self.subTest(component=name):
                self.b._state["visits"][visit_id] = copy.deepcopy(baseline)
                mutate(self.b._state["visits"][visit_id])
                before = len(self.b_actor.calls)
                await self.b._decide(visit_id, "scene" if name == "phase" else "turn")
                self.assertEqual(len(self.b_actor.calls), before + 1)

    async def test_zero_probability_direct_auto_invite_does_not_prepare_a_peer(self):
        self.a.config = replace(self.a.config, auto_probability=0)
        with patch.object(self.a, "_prepare_peer", wraps=self.a._prepare_peer) as prepare:
            with self.assertRaisesRegex(ValueError, "自主邀请概率"):
                await self.a.invite("life-a", self.peer, automatic=True)
            prepare.assert_not_awaited()
            self.assertEqual(self.a_actor.calls, [])
            await self.a.invite("life-a", self.peer, automatic=False)
            prepare.assert_awaited_once()
        self.assertEqual(len(self.a_actor.calls), 1)
