"""Confirmed visits can continue while their original home work stays paused."""
from __future__ import annotations

import asyncio
import copy
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock

from tests import test_visits_actor as actors
from tests import test_engine as fixture
from tests.test_visits_protocol import Actor
from tests.test_visits_lifecycle_races import MemoryDb
from core.visits import VisitConfig, VisitService
from core.visits.protocol import public_actor
from core.models import ActionDef
from core.json_actions import PlannedAction
from core.engine.models import TickOutcome
from core.state import STATE_DROWSY, STATE_NAPPING, STATE_SLEEPING


class VisitPresenceReviewTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = actors.VisitActorTests.asyncSetUp

    async def start_home_work(self, *, queued=True, sleep_kind=""):
        action_id = "visit_home_" + (sleep_kind or "work")
        extra = ({"recovery_kind": sleep_kind,
                  "during": {"state": STATE_NAPPING if sleep_kind == "nap" else STATE_SLEEPING}}
                 if sleep_kind else {})
        self.engine.world.actions.append(ActionDef(id=action_id, name="家中的活动",
            enabled=True, category="continuous", idle=False, duration=1200, **extra))
        async with self.engine.session_state(fixture.SESSION) as state:
            actions = [PlannedAction(type=action_id) for _ in range(2 if queued else 1)]
            outcome = TickOutcome(session_id=fixture.SESSION)
            self.assertTrue(await self.engine._execute_actions(state, self.engine.node(state.node_id),
                outcome, actions, depth=0, autonomous=True), outcome.notes)
            self.assertIsNotNone(state.current_action)
            if queued:
                self.assertIsNotNone(state.current_plan)
            return copy.deepcopy(state.current_action), copy.deepcopy(state.current_plan)

    async def exercise_pending_invitation(self, direction, *, sleep_kind=""):
        clock = self.clock.now
        config = VisitConfig(enabled=True, listen_host="127.0.0.1", listen_port=0,
                             auto_enabled=False, reception_start=0, reception_end=0)
        local = VisitService(self.db, config, self.port, clock=clock, rng=lambda: .9)
        other_actor = Actor("friend")
        remote = VisitService(MemoryDb(), config, other_actor, clock=clock, rng=lambda: .9)
        self.engine.visits = local
        await local.start()
        await remote.start()
        self.addAsyncCleanup(local.close)
        self.addAsyncCleanup(remote.close)
        local.config = replace(config, public_url=f"http://127.0.0.1:{local.transport.bound_port}")
        remote.config = replace(config, public_url=f"http://127.0.0.1:{remote.transport.bound_port}")
        own = await self.port.describe(fixture.SESSION)
        other = await other_actor.describe("remote-life")
        friend = local._add_peer(fixture.SESSION, own, public_actor(other, remote._state["instance_id"]),
                                remote.config.public_url, "isolated-test-key")
        remote._add_peer("remote-life", other, public_actor(own, local._state["instance_id"]),
                         local.config.public_url, "isolated-test-key")
        # The lifecycle starts after binding; all visit messages use real HTTP.
        local._prepare_peer = AsyncMock(return_value=friend["id"])
        pending = await local.invite(fixture.SESSION, friend["id"], direction=direction)
        self.assertEqual(pending["status"], "inviting")
        action, plan = await self.start_home_work(sleep_kind=sleep_kind)
        self.assertFalse((await self.port.describe(fixture.SESSION))["ready"])
        await remote.tick()
        self.clock.advance(61)
        await local.tick()
        current = local._state["visits"][pending["id"]]
        self.assertEqual(current["status"], "active")
        self.assertTrue(current.get("arrived_at"))
        outcome = await self.engine._tick_session(fixture.SESSION)
        self.assertTrue(outcome.summary["visiting"])
        after = await self.engine.load_state(fixture.SESSION, cold_start=False)
        self.assertEqual(after.current_action, action)
        self.assertEqual(after.current_plan, plan)
        if sleep_kind:
            self.assertEqual(after.state, STATE_NAPPING if sleep_kind == "nap" else STATE_SLEEPING)
            self.assertLess(after.energy, .8)
            self.assertGreater(after.energy, .25)
        self.assertTrue((await self.port.describe(fixture.SESSION))["ready"])
        self.llm.default_reply = '{"say":"我想先听你说说。","action":"坐在窗边"}'
        own_id, before_calls = local._local_id(current), len(self.llm.calls)
        for _ in range(4):
            self.clock.advance(61)
            await remote.tick()
            await local.tick()
            if current["turns"][own_id]:
                break
        self.assertEqual(current["status"], "active")
        self.assertEqual(current["turns"][own_id], 1)
        self.assertEqual(len(self.llm.calls) - before_calls, 1)
        self.assertNotIn("model_retry", current)
        after = await self.engine.load_state(fixture.SESSION, cold_start=False)
        self.assertEqual(after.current_action["elapsed_ticks"], action["elapsed_ticks"])
        self.assertEqual(after.current_plan["id"], plan["id"])
        self.assertTrue(any(row["kind"] == "say" and row["actor_id"] == own_id
                            and row["text"] == "我想先听你说说。" for row in current["log"]))

    async def test_guest_can_continue_after_home_work_starts_while_waiting(self):
        await self.exercise_pending_invitation("outgoing")

    async def test_host_can_continue_after_home_work_starts_while_waiting(self):
        await self.exercise_pending_invitation("incoming")

    async def test_guest_can_continue_with_paused_home_nap_after_waiting(self):
        await self.exercise_pending_invitation("outgoing", sleep_kind="nap")

    async def test_host_can_continue_with_paused_home_nap_after_waiting(self):
        await self.exercise_pending_invitation("incoming", sleep_kind="nap")

    async def test_unconfirmed_or_wrong_life_or_role_cannot_bypass_home_work(self):
        await self.start_home_work()
        base = copy.deepcopy(self.visit)
        variants = [None, {**base, "status": "offered"}, {**base, "arrived_at": 0},
                    {**base, "scope": fixture.OTHER_SESSION}, {**base, "status": "completed"},
                    {**base, "local": {**base["local"], "actor_id": "other-role"}},
                    {**base, "local": {**base["local"], "persona_key": "other-persona"}}]
        for visit in variants:
            with self.subTest(visit=visit and visit.get("status"), local=visit and visit.get("local")):
                self.active = visit
                card = await self.port.describe(fixture.SESSION)
                self.assertFalse(card["ready"])
                self.assertIn("手头的活动", card["ready_reason"])
        self.assertEqual(self.llm.calls, [])

    async def test_pending_plan_still_blocks_before_arrival_but_not_during_confirmed_visit(self):
        self.active = None
        async with self.engine.session_state(fixture.SESSION) as state:
            state.current_plan = {"id": "home-plan", "source": "test", "steps": [],
                                  "expires_at": self.clock.now() + 3000}
        card = await self.port.describe(fixture.SESSION)
        self.assertFalse(card["ready"])
        self.assertIn("原先的安排", card["ready_reason"])
        self.active = self.visit
        self.assertTrue((await self.port.describe(fixture.SESSION))["ready"])

    async def test_confirmed_visit_keeps_sleep_low_energy_and_emergency_protection(self):
        await self.start_home_work()
        self.active = self.visit
        for state_name, energy, threads in (
            (STATE_SLEEPING, .8, []), (STATE_DROWSY, .8, []), ("idle", .2, []),
            ("idle", .8, [{"status": "open", "root": {"critical": True}}]),
        ):
            with self.subTest(state=state_name, energy=energy, critical=bool(threads)):
                async with self.engine.session_state(fixture.SESSION) as state:
                    state.state, state.energy, state.event_threads = state_name, energy, threads
                card = await self.port.describe(fixture.SESSION)
                self.assertFalse(card["ready"])
        self.assertEqual(self.llm.calls, [])

    async def test_confirmed_visit_does_not_bypass_a_current_human_or_life_lock(self):
        await self.start_home_work()
        self.active = self.visit
        lock = self.engine.lock(fixture.SESSION)
        await lock.acquire()
        try:
            async with asyncio.timeout(2):
                card = await self.port.describe(fixture.SESSION)
            self.assertFalse(card["ready"])
            self.assertIn("正在回应", card["ready_reason"])
        finally:
            lock.release()
        self.assertEqual(self.llm.calls, [])

    async def test_only_corresponding_confirmed_sleep_action_can_bypass_saved_sleep_state(self):
        for kind, correct, wrong in (("nap", STATE_NAPPING, STATE_SLEEPING),
                                     ("sleep", STATE_SLEEPING, STATE_NAPPING)):
            with self.subTest(kind=kind):
                self.active = None
                action_id = "saved-home-" + kind
                self.engine.world.actions.append(ActionDef(id=action_id, name="原先家中的休息",
                    enabled=True, category="continuous", duration=1200, recovery_kind=kind,
                    during={"state": correct}))
                async with self.engine.session_state(fixture.SESSION) as state:
                    state.current_action = {"type": action_id, "duration_ticks": 20, "elapsed_ticks": 2,
                                            "persona_key": self.local["persona_key"]}
                    state.current_plan = None
                    state.state, state.energy = correct, .8
                self.assertFalse((await self.port.describe(fixture.SESSION))["ready"])
                self.active = self.visit
                self.assertTrue((await self.port.describe(fixture.SESSION))["ready"])
                saved = await self.engine.load_state(fixture.SESSION, cold_start=False)
                definition = self.engine.world.action_map()[action_id]
                definition.enabled = False
                try:
                    ready, _ = self.port._ready(fixture.SESSION, saved, actor_id=self.local["actor_id"],
                                                persona_key=self.local["persona_key"])
                    self.assertFalse(ready)
                finally:
                    definition.enabled = True
                async with self.engine.session_state(fixture.SESSION) as state:
                    self.assertEqual(state.state, correct)
                    state.state = wrong
                self.assertFalse((await self.port.describe(fixture.SESSION))["ready"])
        self.assertEqual(self.llm.calls, [])

    async def test_unconfirmed_nap_still_blocks_and_low_energy_or_emergency_still_wins(self):
        await self.start_home_work(sleep_kind="nap")
        for visit in (None, {**self.visit, "status": "inviting"}, {**self.visit, "arrived_at": 0},
                      {**self.visit, "scope": fixture.OTHER_SESSION},
                      {**self.visit, "local": {**self.local, "actor_id": "other-role"}}):
            with self.subTest(visit=visit and visit.get("status")):
                self.active = visit
                self.assertFalse((await self.port.describe(fixture.SESSION))["ready"])
        self.active = self.visit
        for state_name, energy, threads in (
            (STATE_NAPPING, .2, []), (STATE_DROWSY, .8, []),
            (STATE_NAPPING, .8, [{"status": "open", "root": {"critical": True}}]),
        ):
            with self.subTest(state=state_name, energy=energy, critical=bool(threads)):
                async with self.engine.session_state(fixture.SESSION) as state:
                    state.state, state.energy, state.event_threads = state_name, energy, threads
                self.assertFalse((await self.port.describe(fixture.SESSION))["ready"])
        async with self.engine.session_state(fixture.SESSION) as state:
            state.state, state.energy, state.event_threads = STATE_NAPPING, .8, []
        lock = self.engine.lock(fixture.SESSION)
        await lock.acquire()
        try:
            async with asyncio.timeout(2):
                card = await self.port.describe(fixture.SESSION)
            self.assertFalse(card["ready"])
            self.assertIn("正在回应", card["ready_reason"])
        finally:
            lock.release()
        self.assertEqual(self.llm.calls, [])

    async def test_saved_sleep_without_a_corresponding_action_still_blocks_during_visit(self):
        self.active = self.visit
        for action in (None, {"type": "unknown-sleep-action"}, {"type": "read"}):
            for state_name in (STATE_NAPPING, STATE_SLEEPING):
                with self.subTest(action=action, state=state_name):
                    async with self.engine.session_state(fixture.SESSION) as state:
                        state.current_action = copy.deepcopy(action)
                        state.state, state.energy = state_name, .8
                    card = await self.port.describe(fixture.SESSION)
                    self.assertFalse(card["ready"])
                    self.assertIn("需要休息", card["ready_reason"])
        self.assertEqual(self.llm.calls, [])
