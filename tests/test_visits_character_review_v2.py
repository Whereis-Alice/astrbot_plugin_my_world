"""Visiting must describe the shared scene and preserve paused home work."""
from __future__ import annotations

import copy
import json
import unittest

from core.context_selection import ContextRequest
from core.context_sources import runtime_context
from core.visit_presence import presence_text
from tests import test_engine as engine_fixture
from tests import test_visits_actor as actor_fixture


class VisitCharacterReviewTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = actor_fixture.VisitActorTests.asyncSetUp

    async def test_guest_decision_uses_confirmed_scene_and_exact_speakers(self):
        own_id = "instance-a:" + self.local["actor_id"]
        remote_id = self.visit["remote"]["id"]
        visit = {**self.visit, "guest_id": own_id, "host_id": remote_id,
                 "log": [{"seq": 1, "actor_id": remote_id, "kind": "say", "text": "轮到你了"},
                         {"seq": 2, "actor_id": own_id, "kind": "say", "text": "我想一想"}]}
        self.active = visit
        await self.port.decide(engine_fixture.SESSION, "turn", visit)
        materials = json.loads(self.llm.calls[-1]["prompt"])
        self.assertEqual(materials["current_place"], visit["scene"]["place"])
        self.assertEqual(materials["home_place"], self.local["place"])
        self.assertEqual(materials["self"]["id"], own_id)
        self.assertEqual([row["speaker"] for row in materials["confirmed_log"]], ["friend", "self"])

    async def test_pending_invitation_still_describes_own_place(self):
        visit = {**self.visit, "status": "inviting", "arrived_at": 0}
        await self.port.decide(engine_fixture.SESSION, "propose", visit)
        materials = json.loads(self.llm.calls[-1]["prompt"])
        self.assertEqual(materials["current_place"], self.local["place"])

    async def test_long_visit_gap_preserves_home_action_and_plan(self):
        self.engine.world.actions.append(engine_fixture.ActionDef(
            id="visit_home_work", name="家中的阅读", enabled=True, category="continuous", idle=False))
        self.active = self.visit
        async with self.engine.session_state(engine_fixture.SESSION) as state:
            state.current_action = {"type": "visit_home_work", "duration_ticks": 100,
                                    "elapsed_ticks": 2, "persona_key": self.local["persona_key"]}
            state.current_plan = {"id": "saved-home-plan", "source": "test", "steps": [],
                                  "expires_at": self.clock.now() + 1800,
                                  "persona_key": self.local["persona_key"]}
            before_tick, before_energy = state.world_time, state.energy
        self.clock.advance(600)
        outcome = await self.engine._tick_session(engine_fixture.SESSION)
        after = await self.engine.load_state(engine_fixture.SESSION)
        self.assertTrue(outcome.summary["visiting"])
        self.assertIsNotNone(after.current_action)
        self.assertEqual(after.current_action["elapsed_ticks"], 2)
        self.assertEqual(after.current_plan["id"], "saved-home-plan")
        self.assertGreater(after.world_time, before_tick)
        self.assertLess(after.energy, before_energy)
        self.assertEqual(after.life_updated_at, self.clock.now())
        self.assertFalse(self.llm.calls)

    async def test_paused_home_nap_does_not_recover_energy_during_visit(self):
        self.engine.world.actions.append(engine_fixture.ActionDef(
            id="visit_home_nap", name="家中的小睡", enabled=True, category="continuous", idle=False,
            recovery_kind="nap"))
        self.active = self.visit
        async with self.engine.session_state(engine_fixture.SESSION) as state:
            state.state = "napping"
            state.energy = .5
            state.current_action = {"type": "visit_home_nap", "duration_ticks": 100,
                                    "elapsed_ticks": 2, "persona_key": self.local["persona_key"]}
        self.clock.advance(600)
        await self.engine._tick_session(engine_fixture.SESSION)
        after = await self.engine.load_state(engine_fixture.SESSION)
        self.assertLess(after.energy, .5)
        self.assertEqual(after.current_action["elapsed_ticks"], 2)
        self.assertEqual(after.state, "napping")

    async def test_reply_context_reports_visiting_and_keeps_home_work_as_paused(self):
        self.active = self.visit
        state = await self.engine.load_state(engine_fixture.SESSION)
        state.current_action = {"type": "read", "desc": "家中的阅读", "duration_ticks": 20,
                                "elapsed_ticks": 2, "persona_key": self.local["persona_key"]}
        state.current_plan = {"id": "home-plan", "steps": [{"action": "read", "status": "pending"}],
                              "persona_key": self.local["persona_key"]}
        before = copy.deepcopy(state.to_payload())
        request = ContextRequest("reply", engine_fixture.SESSION, self.clock.now(),
                                 life_persona=self.local["persona_key"])
        text = runtime_context(self.engine.prompts, state, self.engine.node(state.node_id), request,
                               {"world_visit": presence_text(self.visit)})
        self.assertIn("# 当前位置：正在港湾拜访", text)
        self.assertIn("家中保留的安排", text)
        self.assertNotIn("# 当前动作：家中的阅读", text)
        self.assertNotIn("# 你手头的事（接着做，不用重新打算）", text)
        self.assertEqual(state.to_payload(), before)

    async def test_nonvisit_long_gap_keeps_existing_offline_work_policy(self):
        self.engine.world.actions.append(engine_fixture.ActionDef(
            id="visit_home_work", name="家中的阅读", enabled=True, category="continuous", idle=False))
        state = await self.engine.load_state(engine_fixture.SESSION)
        state.current_action = {"type": "visit_home_work", "duration_ticks": 100, "elapsed_ticks": 2}
        state.current_plan = {"id": "home-plan", "expires_at": self.clock.now() + 1800}
        self.clock.advance(600)
        self.engine.reconcile_life_time(state)
        self.assertIsNone(state.current_action)
        self.assertIsNone(state.current_plan)
