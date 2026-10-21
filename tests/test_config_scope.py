"""Configuration isolation and speech controls using the real engine."""

import asyncio
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from core.config_store import ConfigStore
from core.db import AsyncDatabase
from core.engine import MyWorldEngine, TickOutcome
from core.json_actions import PlannedAction
from core.models import ChainStep
from core.planner import create_plan
from tests.stub_ports import StubClock, StubLLM, StubMessenger, StubPersona

A = "test:GroupMessage:100"
B = "test:GroupMessage:200"


class ConfigScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = ConfigStore(Path(directory.name))
        self.store.ensure_files()
        self.store.add_session(A)
        self.store.add_session(B)
        world = self.store.raw_world()
        world["proactive_session_whitelist"] = [A, B]
        world["life_share_session_whitelist"] = [A, B]
        self.store.save_world(world)
        self.db = AsyncDatabase(self.store.db_path)
        self.addAsyncCleanup(self.db.close)
        self.messenger = StubMessenger()
        self.clock = StubClock()
        self.engine = MyWorldEngine(
            store=self.store, db=self.db, messenger=self.messenger, clock=self.clock,
            llm=StubLLM(), persona=StubPersona("test persona"), tick_seconds=60,
        )

    def override(self, session=A, blocks=None, **fields):
        world = self.engine._base_runtime["world"].model_dump(mode="json", by_alias=True)
        world.update(fields)
        self.store.save_scope(session, world, {"schedules": []}, blocks or ["settings"])
        self.engine.reload_config()

    def pause(self):
        world = self.store.raw_world()
        world["proactive_chat_paused"] = True
        self.store.save_world(world)
        self.engine.reload_config()

    async def test_inherited_blocks_follow_global_updates_and_can_be_restored(self):
        self.override(name="local")
        global_world = self.store.raw_world()
        global_world["name"] = "new global"
        global_world["nodes"][0]["name"] = "new room"
        self.store.save_world(global_world)
        self.engine.reload_config()
        with self.engine.config_scope(A):
            self.assertEqual(self.engine.world.name, "local")
            self.assertEqual(self.engine.world.nodes[0].name, "new room")
            self.assertIs(self.engine.prompts.world, self.engine.world)
        with self.engine.config_scope(B):
            self.assertEqual(self.engine.world.name, "new global")
        self.store.save_scope(A, global_world, {"schedules": []}, [])
        self.engine.reload_config()
        with self.engine.config_scope(A):
            self.assertEqual(self.engine.world.name, "new global")

    async def test_two_inflight_sessions_and_exception_do_not_leak_configuration(self):
        self.override(A, name="A")
        self.override(B, name="B")
        ready = asyncio.Event()

        async def read(session, name):
            with self.engine.config_scope(session):
                await ready.wait()
                await asyncio.sleep(0)
                self.assertEqual(self.engine.world.name, name)
                async with self.engine.session_state(session):
                    self.assertEqual(self.engine.world.name, name)
                raise ValueError(name)

        tasks = [asyncio.create_task(read(A, "A")), asyncio.create_task(read(B, "B"))]
        ready.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        self.assertTrue(all(isinstance(result, ValueError) for result in results))
        self.assertEqual(self.engine.config_scope_id, "")
        self.assertNotIn(self.engine.world.name, {"A", "B"})

    async def test_shared_life_uses_stable_main_configuration(self):
        self.override(A, name="main")
        self.override(B, name="member")
        sessions = self.store.raw_sessions()
        sessions["groups"] = [{"id": "together", "sessions": [A, B], "main_session": A}]
        self.store.save_sessions(sessions)
        self.engine.reload_config()
        with self.engine.config_scope(B):
            self.assertEqual(self.engine.world.name, "main")
        self.store.set_session_enabled(A, False)
        self.engine.reload_config()
        with self.engine.config_scope(B):
            self.assertEqual(self.engine.world.name, "main")

    async def test_repair_uses_each_characters_own_map_and_actions(self):
        world = self.store.raw_world()
        room = copy.deepcopy(world["nodes"][0])
        room.update(id="private_room", name="Private room")
        action = {"id": "private_read", "name": "Read", "category": "continuous", "llm_level": "template"}
        self.override(A, blocks=["map", "actions"], nodes=[room], edges=[], actions=[action])
        for session, node, kind in ((A, "private_room", "private_read"), (B, "study", "read")):
            async with self.engine.session_state(session) as state:
                state.node_id = node
                state.current_action = {"type": kind, "elapsed_ticks": 1, "duration_ticks": 10}
        # A repair can be triggered from either the default editor or a scoped editor.
        for caller in ("", A):
            with self.subTest(caller=caller), self.engine.config_scope(caller):
                await self.engine.repair_states_after_config_change()
                for session, node, kind in ((A, "private_room", "private_read"), (B, "study", "read")):
                    saved = await self.engine.load_state(session, cold_start=False)
                    self.assertEqual(saved.node_id, node)
                    self.assertEqual(saved.current_action["type"], kind)

    async def test_sleep_mode_change_applies_only_to_the_effective_scope(self):
        for session in (A, B):
            async with self.engine.session_state(session) as state:
                state.state = "idle"
                state.current_plan = create_plan(steps=[{"action":"sleep"}],
                    world_time=state.world_time, source="sleep_rhythm")
                state.sleep_rhythm = {"mode":"autonomous", "decision":"sleep", "duration_seconds":25200}
        sleep = self.store.raw_world()["sleep"]
        self.override(A, sleep={**sleep, "mode":"fixed"})
        async with self.engine.session_state(A) as state:
            self.assertIsNone(state.current_plan)
            self.assertEqual(state.sleep_rhythm["decision"], "continue")
        async with self.engine.session_state(B) as state:
            self.assertEqual(state.current_plan["source"], "sleep_rhythm")
            self.assertEqual(state.sleep_rhythm["decision"], "sleep")

    async def test_invalid_override_is_not_saved_and_global_pause_cannot_be_overridden(self):
        before = self.store.raw_sessions()
        for blocks in ([{}], ["unknown"]):
            with self.assertRaises(ValueError):
                self.store.save_scope(A, {}, {}, blocks)
        self.assertEqual(self.store.raw_sessions(), before)
        self.override(proactive_chat_paused=False, proactive_chat_enabled=True, admin_ids=["rogue"])
        saved = self.store.raw_sessions()["sessions"][0]["config_overrides"]["settings"]
        self.assertNotIn("proactive_chat_paused", saved)
        self.assertNotIn("admin_ids", saved)
        self.pause()
        self.assertFalse(self.engine.proactive_chat_allowed(A))
        self.assertFalse(self.engine.proactive_chat_allowed(B))

    async def test_local_schedule_add_and_remove_leave_global_unchanged(self):
        self.override(blocks=["schedules"])
        before = copy.deepcopy(self.store.raw_schedules())
        ok, note = await self.engine.schedule_add(
            {"id": "local", "time": "12:00", "action_chain": [{"type": "say", "messages": ["local"]}]},
            session_id=A,
        )
        self.assertTrue(ok, note)
        with self.engine.config_scope(A):
            self.assertEqual([row.id for row in self.engine.schedules.schedules], ["local"])
            ok, note = await self.engine.schedule_remove({"id": "local"})
            self.assertTrue(ok, note)
            self.assertEqual(self.engine.schedules.schedules, [])
        self.assertEqual(self.store.raw_schedules(), before)

    async def test_due_schedules_use_each_sessions_library(self):
        self.store.save_schedules({"schedules": []})
        self.engine.reload_config()
        now = self.engine.local_now()
        self.store.save_scope(A, self.store.raw_world(), {"schedules": [{
            "id": "local", "time": now.strftime("%H:%M"),
            "action_chain": [{"type": "say", "messages": ["scheduled"]}],
        }]}, ["schedules"])
        self.engine.reload_config()
        outcomes = await self.engine.run_schedules()
        self.assertEqual([outcome.session_id for outcome in outcomes], [A])
        for outcome in outcomes:
            await self.engine._deliver(outcome)
        self.assertIn("scheduled", self.messenger.flat_messages)

    async def test_global_once_schedule_reaches_all_inheritors_before_removal(self):
        self.pause()
        now = self.engine.local_now()
        self.store.save_schedules({"schedules": [{
            "id": "shared_once", "time": now.strftime("%H:%M"), "once": True,
            "action_chain": [{"type": "say", "messages": ["once"]}],
        }]})
        self.engine.reload_config()
        outcomes = await self.engine.run_schedules()
        self.assertEqual({outcome.session_id for outcome in outcomes}, {A, B})
        self.assertEqual(self.store.raw_schedules()["schedules"], [])

    async def test_pausing_preserves_instant_virtual_actions_and_their_effects(self):
        self.pause()
        world = self.store.raw_world()
        world["actions"].append({
            "id": "tea_test", "name": "Tea", "category": "instant", "llm_level": "template",
            "template": "drinking tea", "on_complete": {"effects": {"energy": "+0.1"}},
        })
        self.store.save_world(world)
        self.engine.reload_config()
        async with self.engine.session_state(A) as state:
            state.energy = 0.2
            outcome = TickOutcome(A)
            await self.engine._execute_actions(state, self.engine.node(state.node_id), outcome,
                                               [PlannedAction(type="tea_test")], depth=0, autonomous=True)
            self.assertGreater(state.energy, 0.2)
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.flat_messages, [])

    async def test_pause_drops_all_autonomous_origins_but_keeps_replies(self):
        self.pause()
        for kind in ("", "reply", "event", "schedule"):
            outcome = TickOutcome(A, speech_kind=kind)
            outcome.add_speech(kind or "autonomous")
            outcome.add_speech(f"routed-{kind}", B)
            outcome.add_images([f"image-{kind}"])
            await self.engine._deliver(outcome)
        self.assertNotIn("autonomous", self.messenger.flat_messages)
        self.assertNotIn("routed-", self.messenger.flat_messages)
        self.assertNotIn("image-", self.messenger.flat_images)
        for kind in ("event", "schedule"):
            self.assertNotIn(kind, self.messenger.flat_messages)
        for kind in ("reply",):
            self.assertIn(kind, self.messenger.flat_messages)
            self.assertIn(f"routed-{kind}", self.messenger.flat_messages)
        state = await self.engine.load_state(A, cold_start=False)
        self.assertFalse(any(row.get("text") == "autonomous" for row in state.recent_chat))

    async def test_mixed_tick_and_immediate_speech_keep_origin(self):
        self.pause()
        outcome = TickOutcome(A)
        outcome.add_speech("reply", kind="reply")
        outcome.add_speech("autonomous")
        self.engine.say_sink = AsyncMock(return_value=True)
        await self.engine._flush_say(outcome)
        self.engine.say_sink.assert_awaited_once_with(A, "reply")
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.flat_messages, [])
        self.assertEqual(outcome.live_messages, ["reply"])

    async def test_paused_action_still_completes_but_scheduled_continuation_is_silent(self):
        self.pause()
        world = self.store.raw_world()
        world["actions"].append({
            "id": "rest_test", "name": "Rest", "category": "continuous", "duration": 60,
            "llm_level": "template", "on_complete": {"effects": {"energy": "+0.1"}},
        })
        self.store.save_world(world)
        self.engine.reload_config()
        async with self.engine.session_state(A) as state:
            state.energy = 0.2
            outcome = TickOutcome(A)
            await self.engine._run_chain(state, [
                ChainStep(type="rest_test"), ChainStep(type="say", messages=["done"]),
            ], outcome, depth=0)
            self.assertEqual(state.current_action["speech_kind"], "schedule")
            await self.engine._tick_continuous(state, self.engine.node(state.node_id), outcome)
            self.assertGreater(state.energy, 0.2)
            await self.engine._tick_plan(state, self.engine.node(state.node_id), outcome, depth=0)
        await self.engine._deliver(outcome)
        self.assertNotIn("done", self.messenger.flat_messages)

    async def test_session_pause_also_blocks_routed_autonomous_speech(self):
        self.override(B, proactive_chat_enabled=False)
        outcome = TickOutcome(A)
        outcome.add_speech("allowed")
        outcome.add_speech("blocked", B)
        outcome.broadcast_targets = [B]
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.sent, [(A, ["allowed"])])

    async def test_broadcast_mixed_origins_respect_each_destinations_pause(self):
        self.override(B, proactive_chat_enabled=False)
        outcome = TickOutcome(A)
        outcome.add_speech("autonomous")
        outcome.add_speech("event", kind="event")
        outcome.broadcast_targets = [B]
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.sent, [(A, ["autonomous", "event"]), (B, ["event"])])

    async def test_generated_schedule_followup_plan_keeps_its_origin(self):
        async with self.engine.session_state(A) as state:
            plan = create_plan(steps=[{"action": "say", "messages": ["scheduled greeting"]}],
                               world_time=state.world_time, valid_for=1800, reason="schedule followup", source="llm")
            outcome = TickOutcome(A, speech_kind="schedule")
            await self.engine._apply_plan(state, self.engine.node(state.node_id), outcome, plan)
            self.assertEqual(state.current_plan["steps"][0]["kind"], "schedule")
            later = TickOutcome(A)
            await self.engine._tick_plan(state, self.engine.node(state.node_id), later, depth=0)
        await self.engine._deliver(later)
        self.assertIn("scheduled greeting", self.messenger.flat_messages)
