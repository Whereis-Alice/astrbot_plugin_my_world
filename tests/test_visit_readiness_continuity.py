"""A received call survives transient work and retains its real continuation evidence."""
from __future__ import annotations

import copy
from dataclasses import replace
import json
import unittest

from tests import fake_astrbot
fake_astrbot.install()

from tests import test_engine as engine_fixture  # noqa: E402
from tests import test_visits_readiness_review_p as fixture  # noqa: E402
from core.visits.decisions import VisitDecisionError  # noqa: E402
from core.planner import create_plan  # noqa: E402
from core.engine import TickOutcome  # noqa: E402


class ReadinessContinuityTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.VisitReadinessReviewTests.asyncSetUp
    connect = fixture.VisitReadinessReviewTests.connect
    start = fixture.VisitReadinessReviewTests.start
    advance_home = fixture.VisitReadinessReviewTests.advance_home

    async def test_incoming_automatic_call_is_kept_when_the_world_lock_is_busy(self):
        await self.connect()
        async with self.engine.lock(engine_fixture.SESSION):
            card = await self.port.describe(engine_fixture.SESSION)
            self.assertFalse(card["ready"])
            self.assertTrue(card["readiness_wait"])
            payload = {"op": "offer", "id": "a" * 32, "role": "host", "manual": False,
                       "duration": 1800, "max_calls": 12, "turn_limit": 3, "calls": 1,
                       "outing_version": 0}
            response = await self.a._inbox(self.peer, card, payload)
            self.assertEqual(response["visit"]["status"], "offered")
            self.assertNotIn("declined", response)
            with self.assertRaises(VisitDecisionError) as caught:
                await self.port.decide(engine_fixture.SESSION, "receive", self.a._state["visits"]["a" * 32])
            self.assertFalse(caught.exception.request_started)
        self.assertTrue((await self.port.describe(engine_fixture.SESSION))["ready"])
        self.assertFalse(self.llm.calls)

    async def test_awake_receiver_can_accept_outside_the_automatic_calling_window(self):
        await self.connect()
        self.engine.visits.config = replace(self.a.config,
            reception_start=(self.engine.local_now().hour + 1) % 24,
            reception_end=(self.engine.local_now().hour + 2) % 24)
        card = await self.port.describe(engine_fixture.SESSION)
        self.assertFalse(card["reception_open"])
        result = await self.port.decide(engine_fixture.SESSION, "receive", self.visit)
        self.assertTrue(result["accept"])
        self.assertEqual(len(self.llm.calls), 1)

    async def test_original_plan_and_action_pause_on_arrival_then_resume_at_home(self):
        await self.connect()
        await self.start("read")
        async with self.engine.session_state(engine_fixture.SESSION) as state:
            plan = create_plan(steps=[{"action": "read", "kind": "quiet", "duration": 1800}],
                world_time=state.world_time, now=self.clock.now(), source="manual",
                valid_for=7200, persona_key=self.local["persona_key"])
            await self.engine._apply_plan(state, self.engine.node(state.node_id),
                TickOutcome(session_id=state.session_id), plan)
        original = await self.advance_home()
        self.assertTrue((await self.port.describe(engine_fixture.SESSION))["ready"])
        await self.a.tick()
        original = await self.advance_home()
        await self.b.tick()
        await self.a.tick()
        visit = next(iter(self.a._state["visits"].values()))
        self.assertTrue(visit.get("arrived_at"), {key: visit.get(key) for key in
            ("status", "end_reason", "model_retry", "log", "proposal_stage")})
        away = await self.advance_home()
        self.assertEqual(away.current_action, original.current_action)
        self.assertEqual(away.current_plan, original.current_plan)
        await self.a.return_home(engine_fixture.SESSION)
        returned = await self.advance_home()
        self.assertEqual(returned.current_plan["id"], original.current_plan["id"])
        self.assertEqual(returned.current_action["elapsed_ticks"], original.current_action["elapsed_ticks"] + 1)

    async def test_progress_contains_wait_evidence_without_the_packet_or_private_choice(self):
        await self.connect()
        visit = copy.deepcopy(self.visit)
        visit.update(status="active", next_at=self.clock.now() + 60,
                     outbox={"secret": "private", "payload": {"say": "not yet sent"}},
                     private={"thought": "private thought"}, network_failures=2)
        progress = self.a._visible_progress(visit, {})
        self.assertEqual(progress["kind"], "delivery_wait")
        self.assertEqual(progress["attempts"], 2)
        self.assertNotIn("private", json.dumps(progress))
        self.assertNotIn("not yet sent", json.dumps(progress))
        visit["status"] = "interrupted"
        self.assertEqual(self.a._visible_progress(visit, {}), {})

    async def test_failed_calls_do_not_evict_the_retained_encounter_journal(self):
        await self.connect()
        self.a.config = replace(self.a.config, history_keep=2)
        self.a._state["visits"] = {}
        for index in range(5):
            visit = self.a._new_visit(engine_fixture.SESSION, self.peer, self.local,
                f"{index:032x}", "guest", "", False)
            visit.update(status="completed" if index == 0 else "declined", remembered=True,
                         created_at=self.clock.now() + index, ended_at=self.clock.now() + index)
            if index == 0:
                visit["arrived_at"] = self.clock.now()
                visit["log"] = [{"seq": 1, "kind": "say", "text": "saved encounter", "actor_id": "friend"}]
            self.a._state["visits"][visit["id"]] = visit
        await self.a._save()
        self.assertEqual(set(self.a._state["visits"]), {f"{index:032x}" for index in (0, 3, 4)})
        self.assertEqual(self.a._state["visits"]["0" * 32]["log"][0]["text"], "saved encounter")


class DecisionContractTests(unittest.TestCase):
    def test_non_text_fields_do_not_turn_into_character_dialogue(self):
        port = fixture.actor_fixture.VisitActorPort
        for malformed in ({"accept": True, "say": {"internal": "value"}},
                          {"say": "hi", "leave": "false"},
                          {"say": "hi", "scene": {"place": ["somewhere"]}}):
            with self.subTest(malformed=malformed), self.assertRaises(VisitDecisionError):
                port._parse_decision("receive" if "accept" in malformed else "turn", json.dumps(malformed))

    def test_phase_output_contract_is_small_and_keeps_real_acceptance(self):
        port = fixture.actor_fixture.VisitActorPort
        invitation = port._decision_output_rules("receive", {})
        ending = port._decision_output_rules("finish", {})
        self.assertIn("accept", invitation)
        self.assertNotIn("outcome", invitation)
        self.assertIn("reflection", ending)
        self.assertNotIn("say", ending)
