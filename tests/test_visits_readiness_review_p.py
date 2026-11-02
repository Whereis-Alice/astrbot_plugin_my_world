"""Real home activities keep time while a character chooses whether to call."""
from __future__ import annotations

import copy
from dataclasses import replace
import json
import unittest

from core.json_actions import PlannedAction
from core.engine import TickOutcome
from core.planner import create_plan
from core.visits import VisitConfig, VisitService
from core.visits.protocol import public_actor
from tests import test_engine as engine_fixture
from tests import test_visits_actor as actor_fixture
from tests import test_visits_lifecycle_races as lifecycle_fixture
from tests import test_visits_protocol as protocol_fixture


class VisitReadinessReviewTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = actor_fixture.VisitActorTests.asyncSetUp

    async def connect(self):
        cfg = VisitConfig(enabled=True, auto_enabled=True, auto_probability=1,
                          auto_interval_minutes=0, reception_start=0, reception_end=0)
        self.a = VisitService(self.db, cfg, self.port, clock=self.clock.now, rng=lambda: 0)
        self.b_actor = protocol_fixture.Actor("friend")
        self.b = VisitService(lifecycle_fixture.MemoryDb(), replace(cfg, auto_enabled=False),
                              self.b_actor, clock=self.clock.now)
        self.addAsyncCleanup(self.a.close)
        self.addAsyncCleanup(self.b.close)
        self.engine.visits = self.a
        await self.a._load()
        await self.b._load()
        self.local = await self.port.describe(engine_fixture.SESSION)
        self.remote = await self.b_actor.describe("friend-life")
        self.peer = self.a._add_peer(engine_fixture.SESSION, self.local,
            public_actor(self.remote, self.b._state["instance_id"]), "http://not-contacted.example", "isolated-key")
        self.b._add_peer("friend-life", self.remote,
            public_actor(self.local, self.a._state["instance_id"]), "http://not-contacted.example", "isolated-key")
        self.requests = []

        async def exchange(url, outgoing):
            self.requests.append(copy.deepcopy(outgoing["payload"]))
            return await self.b.receive(outgoing)

        self.a.transport.request = exchange
        self.llm.default_reply = '{"accept":true,"say":"读到有趣的地方，想打电话问问她要不要一起下棋。"}'

    async def start(self, action_id="read", *, speech_kind="life_share", from_plan=False):
        async with self.engine.session_state(engine_fixture.SESSION) as state:
            definition = self.engine.world.action_map()[action_id]
            if not definition.available_in(state.node_id):
                state.node_id = definition.allowed_nodes[0]
            state.current_action, state.current_plan = None, None
            state.state, state.energy = "idle", .8
            state.recent_chat, state.event_threads = [], []
            state.life_updated_at = self.clock.now()
            outcome = TickOutcome(session_id=state.session_id, speech_kind=speech_kind)
            await self.engine._start_action(state, self.engine.node(state.node_id), outcome, definition,
                PlannedAction(type=action_id, duration=1800), 0, autonomous=True, from_plan=from_plan,
                skip_drowsy=True)
            self.assertEqual(state.current_action["type"], action_id)
            self.assertEqual(state.current_action["elapsed_ticks"], 0)
        return await self.engine.load_state(engine_fixture.SESSION)

    async def advance_home(self):
        self.clock.advance(60)
        await self.engine._tick_session(engine_fixture.SESSION)
        return await self.engine.load_state(engine_fixture.SESSION)

    async def test_standalone_reading_can_make_one_automatic_invitation_after_real_home_ticks(self):
        await self.connect()
        await self.start("read")
        for _ in range(5):
            state = await self.advance_home()
        self.assertEqual(state.current_action["elapsed_ticks"], 5)
        self.assertIsNone(state.current_plan)
        self.assertTrue((await self.port.describe(engine_fixture.SESSION))["ready"])
        await self.a.tick()
        visits = list(self.a._state["visits"].values())
        self.assertEqual(len(visits), 1)
        self.assertEqual(sum(row["op"] == "offer" for row in self.requests), 1)
        self.assertEqual(len(self.llm.calls), 1)
        after = await self.engine.load_state(engine_fixture.SESSION)
        self.assertEqual(after.current_action, state.current_action)
        materials = json.loads(self.llm.calls[0]["prompt"])
        self.assertIn("看书", materials["current_personal_activity"]["label"])
        self.assertFalse(visits[0].get("arrived_at"))

    async def test_standalone_staring_can_answer_a_manual_invitation_without_erasing_progress(self):
        await self.connect()
        await self.start("stare")
        state = await self.advance_home()
        turn = {"session_id": engine_fixture.SESSION, "started_at": self.clock.now()}
        self.engine.begin_native_turn(turn)
        self.addCleanup(self.engine.end_native_turn, turn)
        result = await self.a.invite(engine_fixture.SESSION, self.peer["id"], "去找她一起下棋")
        self.assertEqual(self.a._state["visits"][result["id"]]["proposal_stage"], "sent")
        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual(sum(row["op"] == "offer" for row in self.requests), 1)
        self.assertEqual((await self.engine.load_state(engine_fixture.SESSION)).current_action, state.current_action)

    async def test_reading_pauses_only_after_confirmed_arrival_and_resumes_when_home(self):
        await self.connect()
        await self.start("read")
        before = await self.advance_home()
        await self.a.tick()
        visit = next(iter(self.a._state["visits"].values()))
        self.assertFalse(visit.get("arrived_at"))
        while_inviting = await self.advance_home()
        self.assertEqual(while_inviting.current_action["elapsed_ticks"], before.current_action["elapsed_ticks"] + 1)
        await self.b.tick()
        await self.a.tick()
        self.assertTrue(visit.get("arrived_at"))
        away = await self.advance_home()
        self.assertEqual(away.current_action, while_inviting.current_action)
        self.assertEqual(away.node_id, before.node_id)
        await self.a.return_home(engine_fixture.SESSION)
        self.assertFalse(self.a.active(engine_fixture.SESSION))
        returned = await self.advance_home()
        self.assertEqual(returned.current_action["type"], "read")
        self.assertEqual(returned.current_action["elapsed_ticks"], away.current_action["elapsed_ticks"] + 1)
        self.assertEqual(len(self.llm.calls), 1)

    async def test_character_can_decline_call_and_continue_the_real_reading_activity(self):
        await self.connect()
        await self.start("read")
        before = await self.advance_home()
        self.llm.default_reply = '{"accept":false,"say":"这一章还没读完，明天再一起下棋，好吗？"}'
        await self.a.tick()
        visit = next(iter(self.a._state["visits"].values()))
        self.assertEqual(visit["status"], "declined")
        self.assertIn("这一章还没读完", visit["end_reason"])
        self.assertFalse(any(row["op"] == "offer" for row in self.requests))
        self.assertEqual((await self.engine.load_state(engine_fixture.SESSION)).current_action, before.current_action)
        later = await self.advance_home()
        self.assertEqual(later.current_action["elapsed_ticks"], before.current_action["elapsed_ticks"] + 1)
        self.assertEqual(len(self.llm.calls), 1)

    async def assert_automatic_blocked(self, reason):
        local = await self.port.describe(engine_fixture.SESSION)
        self.assertFalse(local["ready"])
        self.assertIn(reason, local["ready_reason"])
        await self.a.tick()
        self.assertFalse(self.a._state["visits"])
        self.assertFalse(self.requests)
        self.assertFalse(self.llm.calls)

    async def test_scheduled_reading_keeps_its_appointment_gate(self):
        await self.connect()
        await self.start("read", speech_kind="schedule")
        state = await self.advance_home()
        self.assertIsNone(state.current_plan)
        self.assertEqual(state.current_action["speech_kind"], "schedule")
        await self.assert_automatic_blocked("手头的活动")
    async def test_real_planned_next_step_still_blocks_a_call_during_interruptible_reading(self):
        await self.connect()
        await self.start("read")
        async with self.engine.session_state(engine_fixture.SESSION) as state:
            plan = create_plan(steps=[{"action": "read", "duration": 600}], world_time=state.world_time,
                now=self.clock.now(), tick_seconds=60, valid_for=3600, reason="把这几章依次读完",
                source="manual", persona_key=self.local["persona_key"])
            await self.engine._apply_plan(state, self.engine.node(state.node_id),
                                         TickOutcome(session_id=state.session_id), plan)
        state = await self.advance_home()
        self.assertTrue(state.current_plan)
        self.assertEqual(state.current_action["elapsed_ticks"], 1)
        await self.assert_automatic_blocked("手头的活动")

    async def test_uninterruptible_reading_still_blocks_a_call(self):
        await self.connect()
        raw = self.store.raw_world()
        next(row for row in raw["actions"] if row["id"] == "read")["interruptible"] = False
        self.store.save_world(raw)
        self.engine.reload_config()
        await self.start("read")
        state = await self.advance_home()
        self.assertFalse(state.current_action["interruptible"])
        await self.assert_automatic_blocked("手头的活动")


    async def test_current_definition_can_revoke_permission_to_pause_an_old_action(self):
        await self.connect()
        await self.start("read")
        raw = self.store.raw_world()
        next(row for row in raw["actions"] if row["id"] == "read")["interruptible"] = False
        self.store.save_world(raw)
        self.engine.reload_config()
        state = await self.engine.load_state(engine_fixture.SESSION)
        self.assertTrue(state.current_action["interruptible"])
        await self.assert_automatic_blocked("手头的活动")

    async def test_sleeping_after_a_real_sleep_action_still_blocks_visits(self):
        await self.connect()
        await self.start("sleep")
        state = await self.advance_home()
        self.assertTrue(state.is_sleeping)
        self.llm.calls.clear()
        await self.assert_automatic_blocked("需要休息")

    async def test_drowsiness_low_energy_and_critical_events_keep_their_gates(self):
        await self.connect()
        await self.start("read")
        await self.advance_home()
        for condition, reason in (({"state": "drowsy"}, "需要休息"),
                                  ({"energy": .2}, "需要休息"),
                                  ({"event_threads": [{"status": "open", "root": {"critical": True}}]}, "紧急")):
            with self.subTest(condition=condition):
                async with self.engine.session_state(engine_fixture.SESSION) as state:
                    state.state, state.energy, state.event_threads = "reading", .8, []
                    for key, value in condition.items():
                        setattr(state, key, value)
                await self.assert_automatic_blocked(reason)

    async def test_native_human_turn_still_blocks_only_the_automatic_invitation(self):
        await self.connect()
        await self.start("read")
        await self.advance_home()
        turn = {"session_id": engine_fixture.SESSION, "started_at": self.clock.now()}
        self.engine.begin_native_turn(turn)
        self.addCleanup(self.engine.end_native_turn, turn)
        self.assertTrue((await self.port.describe(engine_fixture.SESSION))["chat_busy"])
        await self.a.tick()
        self.assertFalse(self.a._state["visits"])
        self.assertFalse(self.llm.calls)
        self.assertFalse(self.requests)

    async def test_actual_idle_activity_keeps_its_existing_opportunity_to_call(self):
        await self.connect()
        state = await self.advance_home()
        self.assertFalse(state.current_action)
        self.assertTrue(state.idle_activity)
        self.assertTrue((await self.port.describe(engine_fixture.SESSION))["ready"])
        await self.a.tick()
        self.assertEqual(sum(row["op"] == "offer" for row in self.requests), 1)
        self.assertEqual(len(self.llm.calls), 1)

    async def test_old_persona_reading_is_not_made_pausable_for_a_new_character(self):
        await self.connect()
        await self.start("read")
        before = await self.advance_home()
        self.persona.text = "这里已经换成另一位角色。"
        self.assertNotEqual((await self.port.describe(engine_fixture.SESSION))["persona_key"],
                            before.current_action["persona_key"])
        await self.assert_automatic_blocked("手头的活动")
