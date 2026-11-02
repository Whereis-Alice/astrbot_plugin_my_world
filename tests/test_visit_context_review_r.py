"""Confirmed travel facts at the real actor and incoming reply seams."""
from __future__ import annotations

import copy
import json
import unittest

from tests import fake_astrbot

fake_astrbot.install()

from tests import test_engine as fixture  # noqa: E402 - install the SDK fixture first
from tests import test_visits_actor as actor_fixture  # noqa: E402
from tests.test_visits_exchange_review_r import _paused_exchange  # noqa: E402
from core.visits.outings import pause_activity  # noqa: E402
from core.visits.snapshots import validate_snapshot  # noqa: E402


def test_submitted_farewell_cannot_be_used_as_a_new_shared_pause():
    service, host, guest, now = _paused_exchange()
    guest["next_actor"] = guest["guest_id"]
    guest["unsent_turn"] = True
    guest["decision"] = {"leave": True, "pause_minutes": 3}
    guest["decision_cursor"] = service._decision_cursor(guest, "turn")
    host["turns"][guest["guest_id"]] += 1
    pause_activity(host, 120, 3, now + 30)
    try:
        validate_snapshot(guest, service._wire(host), now=now + 30)
    except ValueError as error:
        assert "独立回应" in str(error)
    else:
        raise AssertionError("An explicit farewell cannot authorize a new shared pause")


class VisitContextReviewTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = actor_fixture.VisitActorTests.asyncSetUp
    ctx = fixture.EngineTestCase.ctx

    async def test_guest_decision_before_agreement_does_not_claim_the_planned_place(self):
        visit = {**self.visit, "outing_version": 1,
            "scene": {"place": "远方海边", "activity": "捡贝壳"},
            "outing_offer": {"place": "远方海边", "activity": "捡贝壳", "minutes": 120}}
        await self.port.decide(fixture.SESSION, "turn", visit)
        materials = json.loads(self.llm.calls[-1]["prompt"])
        self.assertNotEqual(materials["current_place"], "远方海边")
        self.assertNotEqual(materials["current_place"], self.local["place"])
        self.assertEqual(materials["current_world"], "港湾")
        self.assertEqual(materials["scene"], {})
        self.assertEqual(materials["outing_offer"]["place"], "远方海边")

    async def test_host_decision_before_agreement_keeps_its_actual_home_place(self):
        visit = {**self.visit, "role": "host", "outing_version": 1,
            "scene": {"place": "远方海边", "activity": "捡贝壳"}}
        await self.port.decide(fixture.SESSION, "turn", visit)
        materials = json.loads(self.llm.calls[-1]["prompt"])
        self.assertEqual(materials["current_place"], self.local["place"])
        self.assertEqual(materials["scene"], {})

    async def test_actor_current_place_follows_the_confirmed_outing(self):
        visit = {**self.visit, "outing_version": 1,
            "scene": {"place": "远方海边", "activity": "捡贝壳"},
            "outing": {"version": 1, "stage": 0, "place": "河边小公园", "activity": "沿河散步",
                "started_at": self.clock.now(), "until": self.clock.now() + 1800}}
        await self.port.decide(fixture.SESSION, "turn", visit)
        materials = json.loads(self.llm.calls[-1]["prompt"])
        self.assertEqual(materials["current_place"], "河边小公园")
        self.assertEqual(materials["scene"]["activity"], "沿河散步")

    async def test_current_public_event_is_kept_with_its_confirmed_activity(self):
        visit = {**self.visit, "outing_version": 1,
            "scene": {"place": "河边小公园", "activity": "沿河散步", "event": "湖边有只小猫走近"},
            "outing": {"version": 1, "stage": 0, "place": "河边小公园", "activity": "沿河散步",
                "started_at": self.clock.now(), "until": self.clock.now() + 1800}}
        await self.port.decide(fixture.SESSION, "turn", visit)
        self.assertEqual(json.loads(self.llm.calls[-1]["prompt"])["scene"]["event"], "湖边有只小猫走近")

    async def test_changed_persona_cannot_receive_the_previous_roles_travel_overlay(self):
        self.active = copy.deepcopy(self.visit)
        retained = copy.deepcopy(self.active)
        self.persona.text = "现在我是刚搬来这里的新角色。"
        ctx = self.ctx(text="你现在在做什么？", is_mentioned=True)
        await self.engine.handle_incoming(ctx)
        plan = await self.engine.handle_reply(ctx, inject_only=True)
        self.assertTrue(plan.ok, plan.reason)
        self.assertNotIn("正在港湾拜访", plan.runtime_part)
        self.assertNotIn("一起下棋", plan.runtime_part)
        self.assertEqual(self.active, retained)
        self.assertFalse(self.llm.calls)

    async def test_only_pending_human_requests_are_shown_with_the_actual_actor(self):
        own_id, friend_id = self.local["actor_id"], "actor-b"
        visit = {**self.visit, "guest_id": own_id, "host_id": friend_id,
            "outing_request": {"text": "旧版保存的重复请求"},
            "outing_requests": {own_id: {"id": "new", "text": "还想一起去小公园"},
                friend_id: {"id": "old", "text": "已经商量过的旧请求"}},
            "outing_requests_handled": {friend_id: "old"}}
        await self.port.decide(fixture.SESSION, "turn", visit)
        materials = json.loads(self.llm.calls[-1]["prompt"])
        self.assertEqual(len(materials["outing_requests"]), 1)
        self.assertEqual(materials["outing_request"], {})
        request = materials["outing_requests"][0]
        self.assertEqual(request["actor_id"], own_id)
        self.assertEqual(request["text"], "还想一起去小公园")
        self.assertNotEqual(request["speaker"], "unknown")
