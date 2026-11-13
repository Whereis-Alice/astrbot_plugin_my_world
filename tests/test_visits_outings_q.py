"""Shared-clock journeys through the real engine, SQLite and signed HTTP channel."""
from __future__ import annotations

import copy
from dataclasses import replace
import json
import unittest

from core.visits import VisitService
from core.visits.history_pages import LOG_PAGE_SIZE
from core.visits.protocol import clean_decision
from core.visits.activities import begin_shared_activity
from core.visits.snapshots import validate_snapshot
from tests import test_visits_manual_budget_q as manual_fixture


class OutingClockTests(unittest.IsolatedAsyncioTestCase):
    make_world = manual_fixture.ManualVisitBudgetTests.make_world
    exhaust = manual_fixture.ManualVisitBudgetTests.exhaust

    async def asyncSetUp(self):
        await manual_fixture.ManualVisitBudgetTests.asyncSetUp(self)
        for world in (self.a, self.b):
            world.service.config = replace(world.service.config, auto_enabled=False,
                reception_start=0, reception_end=0, daily_calls=100, daily_tokens=0)

    def advance(self, seconds):
        self.a.clock.advance(seconds)
        self.b.clock.advance(seconds)

    def guest_reply(self, *, accepted=True, return_after=False, farewell="", outcome="", pause=240):
        return json.dumps({"accept": True, "say": "好，一起走吧。", "outing_accept": accepted,
            "return_after": return_after, "farewell": farewell, "outcome": outcome, "pause_minutes": pause}, ensure_ascii=False)

    def host_reply(self, *, place="湖边", minutes=45, return_after=False, farewell="", outcome="", pause=240):
        return json.dumps({"accept": True, "say": "去那边看看吧。", "outcome": outcome,
            "farewell": farewell, "outing": {"place": place, "activity": "散步看风景", "minutes": minutes,
            "return_after": return_after, "pause_minutes": pause}}, ensure_ascii=False)

    async def start_trip(self, *, accepted=True, manual=True, minutes=45, return_after=False, pause=240):
        if not manual:
            for world in (self.a, self.b):
                world.service.config = replace(world.service.config, auto_enabled=True)
        self.a.llm.default_reply = self.guest_reply(accepted=accepted, return_after=return_after,
                                                  farewell="今天很开心，明天见。", pause=pause)
        self.b.llm.default_reply = self.host_reply(minutes=minutes, return_after=return_after,
                                                farewell="路上小心，回去后告诉我。", pause=pause)
        self.a.llm.replies = ['{"accept":true,"say":"能去你家玩一会儿吗？"}']
        result = await self.a.service.invite(self.scope, self.peer_id, manual=manual, automatic=not manual)
        visit_id = result["id"]
        await self.b.service._advance(visit_id)
        await self.a.service._advance(visit_id)
        self.advance(self.cfg.poll_seconds + 1)
        await self.a.service._advance(visit_id)
        for world in (self.a, self.b):
            world.service.config = replace(world.service.config, auto_enabled=False)
        return visit_id

    async def test_independent_acceptance_starts_shared_clock_and_rejection_does_not(self):
        visit_id = await self.start_trip(accepted=False)
        for world in (self.a, self.b):
            visit = world.service._state["visits"][visit_id]
            self.assertFalse(visit.get("outing"))
            self.assertFalse(any(row["kind"] == "activity" for row in visit["log"]))
        self.b.llm.default_reply = self.host_reply(place="花园")
        self.a.llm.default_reply = self.guest_reply()
        self.advance(301)
        await self.b.service._advance(visit_id)
        self.advance(self.cfg.poll_seconds + 1)
        await self.a.service._advance(visit_id)
        first = self.a.service._state["visits"][visit_id]["outing"]
        second = self.b.service._state["visits"][visit_id]["outing"]
        self.assertEqual(first, second)
        self.assertEqual(first["place"], "花园")
        self.assertEqual(first["until"] - first["started_at"], 2700)

    async def test_long_activity_ticks_use_no_network_or_model_and_survive_reload(self):
        visit_id = await self.start_trip(minutes=120)
        before = (len(self.a.llm.calls), len(self.b.llm.calls), len(self.operations))
        for _ in range(5):
            self.advance(600)
            await self.a.service.tick()
            await self.b.service.tick()
        self.assertEqual((len(self.a.llm.calls), len(self.b.llm.calls), len(self.operations)), before)
        await self.a.service._save()
        restored = VisitService(self.a.db, self.a.service.config, self.a.port, clock=self.a.clock.now)
        self.addAsyncCleanup(restored.close)
        await restored._load()
        self.assertEqual(restored._state["visits"][visit_id]["outing"],
                         self.a.service._state["visits"][visit_id]["outing"])

    async def test_next_place_renews_lease_beyond_original_three_hours_and_keeps_both_experiences(self):
        visit_id = await self.start_trip(minutes=120)
        original = self.a.clock.now() + 180 * 60 - self.cfg.poll_seconds - 1
        self.advance(7201)
        self.b.llm.default_reply = self.host_reply(place="山顶", minutes=120, outcome="看见湖面落日。")
        self.a.llm.default_reply = self.guest_reply(outcome="记住了湖边的风。")
        await self.b.service._advance(visit_id)
        self.advance(self.cfg.poll_seconds + 1)
        await self.a.service._advance(visit_id)
        host = self.b.service._state["visits"][visit_id]
        guest = self.a.service._state["visits"][visit_id]
        self.assertEqual(host["outing"]["place"], "山顶")
        self.assertGreater(host["outing"]["until"], original)
        self.assertEqual(host["expires_at"], guest["expires_at"])
        self.assertEqual(len(host["outing"]["route"]), 2)
        self.assertIn("看见湖面落日。", [row["text"] for row in host["log"] if row["kind"] == "experience"])
        self.assertIn("记住了湖边的风。", [row["text"] for row in host["log"] if row["kind"] == "experience"])
        self.advance(3601)
        await self.a.service.tick()
        await self.b.service.tick()
        self.assertEqual(host["status"], "active")
        self.assertEqual(guest["status"], "active")

    async def test_joint_return_uses_only_precommitted_original_farewells_without_new_inference(self):
        visit_id = await self.start_trip(minutes=10, return_after=True)
        before = (len(self.a.llm.calls), len(self.b.llm.calls))
        self.advance(601)
        await self.b.service.tick()
        await self.a.service.tick()
        host = self.b.service._state["visits"][visit_id]
        guest = self.a.service._state["visits"][visit_id]
        self.assertEqual(host["status"], "completed")
        self.assertEqual(host["log"], guest["log"])
        self.assertEqual([row["text"] for row in host["log"][-2:]],
                         ["路上小心，回去后告诉我。", "今天很开心，明天见。"])
        self.assertEqual(host["end_reason"], "")
        self.assertEqual((len(self.a.llm.calls), len(self.b.llm.calls)), before)

    async def test_budget_exhaustion_keeps_checkpoint_and_stops_repeated_network_until_reset(self):
        visit_id = await self.start_trip(manual=False, minutes=5)
        await self.exhaust(self.b)
        self.advance(301)
        before = len(self.b.llm.calls)
        await self.b.service._advance(visit_id)
        host = self.b.service._state["visits"][visit_id]
        guest = self.a.service._state["visits"][visit_id]
        self.assertEqual(host["status"], "active")
        self.assertEqual(host["outing"]["place"], "湖边")
        self.assertEqual(host["budget_holds"], guest["budget_holds"])
        self.assertGreater(host["next_at"], self.b.clock.now())
        network = len(self.operations)
        for _ in range(3):
            self.advance(60)
            await self.a.service.tick()
            await self.b.service.tick()
        self.assertEqual(len(self.b.llm.calls), before)
        self.assertEqual(len(self.operations), network)
        continued = await self.a.service.invite(self.scope, self.peer_id, manual=True, reason="我们继续去花园玩")
        self.assertEqual(continued["id"], visit_id)
        self.assertTrue(host["manual"] and guest["manual"])
        self.assertFalse(host.get("budget_holds"))
        self.assertFalse(guest.get("budget_holds"))

    async def test_wire_pages_keep_more_than_512_full_records_and_reject_prefix_rewrites(self):
        visit_id = await self.start_trip()
        host = self.b.service._state["visits"][visit_id]
        guest = self.a.service._state["visits"][visit_id]
        for world in (self.a, self.b):
            world.service.config = replace(world.service.config, outings_enabled=False)
        for batch in range(4):
            for index in range(150):
                self.b.service._append(host, host["host_id"], "experience", f"旅途见闻 {batch}:{index} " + "景" * 500)
            reply = await self.b.service._send(visit_id, {"op": "sync", "visit": self.b.service._wire(host)})
            self.assertEqual(reply["visit"]["log_count"], len(host["log"]))
            self.assertEqual(guest["log"], host["log"])
        wire = self.b.service._wire(host)
        self.assertGreater(wire["log_count"], 512)
        self.assertEqual(len(wire["log"]), LOG_PAGE_SIZE)
        self.assertEqual(wire["log"][0]["seq"], wire["log_offset"] + 1)
        before = copy.deepcopy(guest)
        altered = copy.deepcopy(wire)
        altered["log_prefix"] = "0" * 64
        with self.assertRaises(ValueError):
            validate_snapshot(guest, altered, now=self.a.clock.now())
        self.assertEqual(before, guest)
        await self.b.service._finish(host, "completed")
        await self.b.service._notify_end(visit_id)
        self.assertEqual(guest["log"], host["log"])
        await self.b.service._observe_memory(visit_id)
        await self.b.service._await_memory_tasks(tuple(self.b.service._memory_tasks.values()))
        self.assertTrue(host.get("remembered"))

    async def test_manual_new_plan_waits_for_independently_agreed_return_and_then_invites(self):
        visit_id = await self.start_trip(minutes=10, return_after=True)
        host = self.b.service._state["visits"][visit_id]
        guest = self.a.service._state["visits"][visit_id]
        prior = copy.deepcopy(host["outing"])
        queued = await self.a.service.invite(self.scope, self.peer_id, manual=True, reason="再去花园玩吧")
        self.assertTrue(queued["queued"])
        self.assertEqual(prior, host["outing"])
        self.assertEqual(prior, guest["outing"])
        self.assertEqual(len(self.a.service._state["visits"]), 1)
        self.advance(601)
        await self.b.service.tick()
        await self.a.service.tick()
        self.assertEqual(guest["status"], "completed")
        current = self.a.service.active(self.scope)
        self.assertIsNotNone(current)
        self.assertNotEqual(current["id"], visit_id)
        self.assertTrue(current["manual"])
        self.assertEqual(current["reason"], "再去花园玩吧")

    async def test_restored_long_route_pages_preserve_global_stage_and_allow_next_real_activity(self):
        visit_id = await self.start_trip(minutes=5)
        host = self.b.service._state["visits"][visit_id]
        guest = self.a.service._state["visits"][visit_id]
        route = copy.deepcopy(host["outing"]["route"])
        for index in range(1, 65):
            started = route[-1]["until"]
            route.append({"place": f"已确认地点 {index}", "activity": "一起走走",
                          "started_at": started, "until": started + 300})
        elapsed = route[-1]["started_at"] - self.a.clock.now()
        self.advance(elapsed)
        for visit in (host, guest):
            visit["outing"] = {"version": 1, "stage": 64, **route[-1], "route": copy.deepcopy(route),
                               "return_agreed": False, "farewells": {}}
            visit["expires_at"] = route[-1]["until"] + 3600
            visit["next_actor"], visit["next_at"] = visit["host_id"], route[-1]["until"]
            begin_shared_activity(visit)
        wire = self.b.service._wire(host)
        self.assertEqual(wire["outing"]["stage"], 64)
        self.assertEqual(wire["outing"]["route_offset"], 1)
        await self.b.service._send(visit_id, {"op": "sync", "visit": wire})
        self.assertEqual(guest["outing"]["route"], route)
        self.advance(301)
        self.b.llm.default_reply = self.host_reply(place="真正的新地点", minutes=10)
        await self.b.service._advance(visit_id)
        self.advance(self.cfg.poll_seconds + 1)
        await self.a.service._advance(visit_id)
        self.assertEqual(host["outing"]["stage"], 65)
        self.assertEqual(host["outing"]["place"], "真正的新地点")
        self.assertEqual(len(guest["outing"]["route"]), 66)
        self.assertEqual(host["outing"], guest["outing"])

    async def test_first_return_confirmation_cannot_replace_guests_own_words(self):
        visit_id = await self.start_trip()
        host = self.b.service._state["visits"][visit_id]
        guest = self.a.service._state["visits"][visit_id]
        self.advance(2701)
        self.b.llm.default_reply = self.host_reply(place="花园", return_after=True)
        await self.b.service._advance(visit_id)
        reply = await self.a.service._send(visit_id, {"op": "sync", "visit": self.a.service._wire(guest)})
        self.a.service._synchronize(guest, reply["visit"], guest["host_id"])
        choice = clean_decision({"outing_accept": True, "return_after": True, "farewell": "我的原话"})
        guest["decision"] = choice
        self.b.service._record_turn(host, host["guest_id"], choice)
        altered = self.b.service._wire(host)
        altered["outing"]["farewells"][guest["guest_id"]] = "别人替我写的"
        with self.assertRaisesRegex(ValueError, "本人原话"):
            validate_snapshot(guest, altered, now=self.a.clock.now())

    async def test_activity_can_chat_again_after_model_chosen_three_minute_quiet_without_new_route(self):
        visit_id = await self.start_trip(minutes=45, pause=3)
        host = self.b.service._state["visits"][visit_id]
        guest = self.a.service._state["visits"][visit_id]
        self.assertEqual(host["outing"]["quiet_until"], self.a.clock.now() + 180)
        original_until = host["outing"]["until"]
        before = (len(self.a.llm.calls), len(self.b.llm.calls), len(self.operations))
        self.advance(120)
        await self.a.service.tick()
        await self.b.service.tick()
        self.assertEqual((len(self.a.llm.calls), len(self.b.llm.calls), len(self.operations)), before)
        self.b.llm.default_reply = json.dumps({"accept": True, "say": "你看，那边有只鸟。",
            "action": "停下脚步抬头看鸟", "continue_activity": True, "pause_minutes": 3}, ensure_ascii=False)
        self.a.llm.default_reply = json.dumps({"accept": True, "say": "嗯，它停在树枝上。",
            "action": "一起抬头看", "continue_activity": True, "pause_minutes": 3}, ensure_ascii=False)
        self.advance(61)
        await self.b.service._advance(visit_id)
        self.advance(self.cfg.poll_seconds + 1)
        await self.a.service._advance(visit_id)
        self.assertEqual(host["outing"]["until"], original_until)
        self.assertEqual(len(host["outing"]["route"]), 1)
        self.assertEqual(host["outing"], guest["outing"])
        self.assertEqual(host["outing"]["quiet_until"], self.a.clock.now() + 180)
        self.assertEqual([row["text"] for row in host["log"] if row["kind"] == "say"][-2:],
                         ["你看，那边有只鸟。", "嗯，它停在树枝上。"])
        self.assertEqual((len(self.a.llm.calls), len(self.b.llm.calls)), (before[0] + 1, before[1] + 1))

    async def test_human_request_can_replace_uncontacted_automatic_proposal_without_refunding_real_cost(self):
        for world in (self.a, self.b):
            world.service.config = replace(world.service.config, auto_enabled=True)
        self.a.llm.default_reply = "unfinished"
        pending = await self.a.service.invite(self.scope, self.peer_id, automatic=True)
        visit = self.a.service._state["visits"][pending["id"]]
        self.assertFalse(visit.get("contacted"))
        self.assertFalse(self.b.service._state["visits"])
        self.assertEqual(len(self.a.llm.calls), 1)
        ledger, before = await self.exhaust(self.a)
        self.a.llm.default_reply = '{"accept":true,"say":"再约你一起玩，好吗？"}'
        result = await self.a.service.invite(self.scope, self.peer_id, manual=True, reason="真人想继续邀请")
        self.assertEqual(result["id"], pending["id"])
        self.assertTrue(visit["manual"])
        self.assertTrue(self.b.service._state["visits"][result["id"]]["manual"])
        self.assertEqual(len(self.a.llm.calls), 2)
        self.assertEqual(await self.a.db.call("background_status", ledger), before)


if __name__ == "__main__":
    unittest.main()
