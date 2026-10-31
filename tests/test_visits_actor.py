"""Real life engine, SQLite receipts and providers at the visit adapter seam."""
from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
import sys
import types
import unittest

from core.model_calls import model_context
from core.ports import LLMReply
from core.visit_presence import active_visit
from tests import test_engine as fixture


# Import this adapter without importing the AstrBot entrypoint. Share the actual
# core modules so task-local budget attribution is the production attribution.
package = types.ModuleType("visit_adapter_fixture")
package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules[package.__name__] = package
sys.modules[package.__name__ + ".core"] = importlib.import_module("core")
for module_name in ("json_actions", "memory", "model_calls", "state", "visit_presence"):
    sys.modules[package.__name__ + ".core." + module_name] = importlib.import_module("core." + module_name)
VisitActorPort = importlib.import_module(package.__name__ + ".adapters.visits").VisitActorPort


class VisitActorTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"

    async def asyncSetUp(self):
        await fixture.EngineTestCase.asyncSetUp(self)
        self.plugin = types.SimpleNamespace(engine=self.engine, enabled=True, retired=False)
        self.active = None
        self.engine.visits = types.SimpleNamespace(config=types.SimpleNamespace(max_output_tokens=240),
                                                 active=lambda scope: self.active)
        self.port = VisitActorPort(self.plugin)
        async with self.engine.session_state(fixture.SESSION) as state:
            state.state = "idle"
            state.energy = .8
            state.current_action = None
            state.current_plan = None
            state.recent_chat = []
            state.life_updated_at = self.clock.now()
        self.llm.replies = []
        self.llm.default_reply = '{"accept":true,"say":"带点心去看看她。","thought":"有些期待"}'
        self.local = await self.port.describe(fixture.SESSION)
        self.visit = {"id": "visit-one", "scope": fixture.SESSION, "role": "guest", "status": "active",
                      "local": self.local, "remote": {"id": "instance-b:actor-b", "name": "另一个她", "world": "港湾"},
                      "scene": {"place": "窗边", "activity": "一起下棋", "event": ""},
                      "reason": "想一起解昨天的谜题", "arrived_at": self.clock.now(),
                      "log": [{"seq": 1, "actor_id": "actor-b", "kind": "say", "text": "棋盘已经摆好了"}]}

    async def test_public_card_has_stable_identity_without_private_materials(self):
        first = await self.port.describe(fixture.SESSION)
        second = await VisitActorPort(self.plugin).describe(fixture.SESSION)
        self.assertEqual(first["actor_id"], second["actor_id"])
        self.assertNotIn(self.persona.text, json.dumps(first, ensure_ascii=False))
        self.persona.text = "换了另一个人格"
        third = await self.port.describe(fixture.SESSION)
        self.assertEqual(first["actor_id"], third["actor_id"])
        self.assertNotEqual(first["persona_key"], third["persona_key"])

    async def test_manual_proposal_can_follow_its_own_human_invitation(self):
        turn = {"session_id": fixture.SESSION, "started_at": self.clock.now()}
        self.engine.begin_native_turn(turn)
        self.addCleanup(self.engine.end_native_turn, turn)
        async with self.engine.session_state(fixture.SESSION) as state:
            state.recent_chat = [{"text": "去找她玩一会儿", "at": self.clock.now(), "is_self": False, "addressing": "me"}]
        public = await self.port.describe(fixture.SESSION)
        self.assertTrue(public["ready"])
        self.assertTrue(public["chat_busy"])
        value = await self.port.decide(fixture.SESSION, "propose", self.visit)
        self.assertTrue(value["accept"])
        self.assertEqual(len(self.llm.calls), 1)

    async def test_receiver_busy_with_a_person_postpones_without_model(self):
        async with self.engine.session_state(fixture.SESSION) as state:
            state.recent_chat = [{"text": "有人正在找她", "at": self.clock.now(), "is_self": False, "addressing": "me"}]
        value = await self.port.decide(fixture.SESSION, "receive", self.visit)
        self.assertFalse(value["accept"])
        self.assertIn("稍后", value["say"])
        self.assertFalse(self.llm.calls)

    async def test_nested_critical_event_prevents_travel(self):
        async with self.engine.session_state(fixture.SESSION) as state:
            state.event_threads = [{"status": "open", "root": {"critical": True}}]
        public = await self.port.describe(fixture.SESSION)
        self.assertFalse(public["ready"])
        value = await self.port.decide(fixture.SESSION, "receive", self.visit)
        self.assertFalse(value["accept"])
        self.assertFalse(self.llm.calls)

    async def test_other_peoples_group_chat_does_not_block_receiving_a_friend(self):
        async with self.engine.session_state(fixture.SESSION) as state:
            state.recent_chat = [{"text": "群友互相聊天", "at": self.clock.now(), "is_self": False, "addressing": "others"}]
        self.assertFalse((await self.port.describe(fixture.SESSION))["chat_busy"])
        self.assertTrue((await self.port.decide(fixture.SESSION, "receive", self.visit))["accept"])

    async def test_visit_uses_main_provider_and_background_budget_inside_a_human_tool(self):
        self.engine.world.limits.background_daily_calls = 1
        with model_context("reply", "用户邀请", fixture.SESSION):
            value = await self.port.decide(fixture.SESSION, "propose", self.visit)
            self.assertTrue(value["accept"])
            with self.assertRaises(ValueError):
                await self.port.decide(fixture.SESSION, "turn", self.visit)
        self.assertEqual(len(self.llm.calls), 1)
        status = await self.db.call("background_status", self.engine._today_key(self.clock.now()))
        self.assertEqual(status["calls"], 1)
        self.assertEqual(self.llm.calls[0]["session_id"], fixture.SESSION)

    async def test_boolean_strings_do_not_accept_or_leave(self):
        self.llm.default_reply = '{"accept":"false","leave":"false","say":"等等"}'
        value = await self.port.decide(fixture.SESSION, "propose", self.visit)
        self.assertIs(value["accept"], False)
        self.assertIs(value["leave"], False)

    async def test_prompt_excludes_private_chat_and_unrelated_memories(self):
        async with self.engine.session_state(fixture.SESSION) as state:
            state.recent_chat = [{"text": "用户的秘密暗号", "is_self": False, "at": self.clock.now() - 300}]
        self.engine.memory.remember(session_id=fixture.SESSION, persona_id=self.local["persona_key"],
            node_id="study", content="私人事项的未公开密码", source="runtime")
        await self.port.decide(fixture.SESSION, "turn", self.visit)
        prompt = self.llm.calls[0]["prompt"]
        self.assertIn("棋盘已经摆好了", prompt)
        self.assertNotIn("秘密暗号", prompt)
        self.assertNotIn("未公开密码", prompt)
        self.assertFalse(self.tools.calls)

    async def test_late_result_after_persona_change_is_discarded(self):
        async def change(**kwargs):
            self.persona.text = "现在换了角色"
            return LLMReply(text='{"accept":true}', ok=True)
        self.llm.generate = change
        with self.assertRaisesRegex(ValueError, "作废"):
            await self.port.decide(fixture.SESSION, "propose", self.visit)

    async def test_accepted_trip_keeps_needs_moving_and_freezes_home_action(self):
        self.engine.world.actions.append(fixture.ActionDef(id="visit_home_work", name="家里的阅读",
                                                        enabled=True, category="continuous", idle=False))
        self.active = self.visit
        async with self.engine.session_state(fixture.SESSION) as state:
            state.current_action = {"type": "visit_home_work", "duration_ticks": 20, "elapsed_ticks": 2}
            state.current_plan = {"id": "home-plan", "source": "test", "steps": [], "expires_at": self.clock.now() + 3000}
            before = state.world_time, state.energy, state.node_id
        self.clock.advance(60)
        outcome = await self.engine._tick_session(fixture.SESSION)
        after = await self.engine.load_state(fixture.SESSION)
        self.assertTrue(outcome.summary["visiting"])
        self.assertGreater(after.world_time, before[0])
        self.assertLess(after.energy, before[1])
        self.assertEqual(after.node_id, before[2])
        self.assertEqual(after.current_action["elapsed_ticks"], 2)
        self.assertEqual(after.current_plan["id"], "home-plan")
        self.assertFalse(self.llm.calls)
        self.assertIsNone(await self.engine.maybe_decide(fixture.SESSION, force=True))

    async def test_invitation_does_not_replace_home_presence(self):
        self.active = {**self.visit, "status": "offered"}
        self.assertIsNone(active_visit(self.engine, fixture.SESSION))
        notes = await self.engine.runtime_notes(fixture.SESSION)
        self.assertNotIn("world_visit", notes)
        self.active = self.visit
        notes = await self.engine.runtime_notes(fixture.SESSION)
        self.assertIn("港湾", notes["world_visit"])

    async def test_late_home_inference_cannot_commit_a_plan_after_arrival(self):
        from core.engine.life_inference import life_work
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed(**kwargs):
            entered.set()
            await release.wait()
            return LLMReply(text='{"accept":true}', ok=True)
        self.llm.generate = delayed

        @life_work()
        async def home_decision(engine):
            async with engine.session_state(fixture.SESSION) as state:
                await engine._budgeted_generate(self.llm, session_id=fixture.SESSION,
                                               system_prompt="home", prompt="plan")
                state.current_plan = {"id": "must-not-commit"}

        task = asyncio.create_task(home_decision(self.engine))
        await asyncio.wait_for(entered.wait(), 2)
        self.active = self.visit
        release.set()
        await asyncio.wait_for(task, 2)
        after = await self.engine.load_state(fixture.SESSION)
        self.assertIsNone(after.current_plan)

    async def test_memories_are_idempotent_namespaced_and_persona_filtered(self):
        completed = {**self.visit, "status": "completed", "private": {"reflection": "这次有些开心"}}
        await self.port.remember(fixture.SESSION, completed)
        await self.port.remember(fixture.SESSION, completed)
        rows = await self.db.call("query_memories", session_id=fixture.SESSION,
                                  life_persona_key=self.local["persona_key"])
        visits = [row for row in rows if row["source"] == "life_event"]
        self.assertEqual(len(visits), 1)
        self.assertEqual(visits[0]["type"], "event")
        self.assertEqual(visits[0]["related_users"], [])
        self.assertEqual(visits[0]["participants"], ["world_actor:instance-b:actor-b"])
        self.assertIn("主观", visits[0]["content"])
        other = await self.db.call("query_memories", session_id=fixture.SESSION, life_persona_key="other-persona")
        self.assertFalse([row for row in other if row["source"] == "life_event"])

    async def test_failed_invitation_is_not_a_visit_memory(self):
        for status in ("declined", "expired", "interrupted"):
            visit = {**self.visit, "status": status, "arrived_at": 0, "reason": "只是计划"}
            await self.port.remember(fixture.SESSION, visit)
        rows = await self.db.call("query_memories", session_id=fixture.SESSION)
        self.assertFalse([row for row in rows if row["source"] == "life_event"])

    async def test_same_experience_on_two_visits_does_not_revise_the_first_trip(self):
        first = {**self.visit, "status": "completed"}
        second = {**first, "id": "visit-two"}
        await self.port.remember(fixture.SESSION, first)
        await self.port.remember(fixture.SESSION, second)
        rows = await self.db.call("query_memories", session_id=fixture.SESSION)
        visits = [row for row in rows if row["source"] == "life_event"]
        self.assertEqual(len(visits), 2)
        self.assertEqual({json.loads(row["context"])["visit_id"] for row in visits}, {"visit-one", "visit-two"})

    async def test_old_friend_memory_is_not_hidden_by_new_unrelated_life_events(self):
        await self.port.remember(fixture.SESSION, {**self.visit, "status": "completed"})
        for index in range(90):
            self.db.raw.add_memory(session_id=fixture.SESSION, persona_id=self.local["persona_key"],
                scope="group_persona", node_id="study", content=f"后来的一件无关事件{index}",
                memory_type="event", source="life_event", context=json.dumps({"persona_key": self.local["persona_key"]}))
        await self.port.decide(fixture.SESSION, "turn", self.visit)
        materials = json.loads(self.llm.calls[-1]["prompt"])
        self.assertTrue(materials["previous_visits"])
        self.assertIn("棋盘已经摆好了", materials["previous_visits"][0])

    async def test_event_resolution_uses_both_confirmed_action_plans(self):
        visit = {**self.visit, "role": "host", "scene_stage": "event_resolve",
                 "action_plans": [{"actor_id": "actor-b", "action": "找一把小勺"},
                                  {"actor_id": self.local["actor_id"], "action": "把蛋糕扶稳"}]}
        await self.port.decide(fixture.SESSION, "scene", visit)
        materials = json.loads(self.llm.calls[-1]["prompt"])
        self.assertEqual(materials["scene_stage"], "event_resolve")
        self.assertEqual(materials["action_plans"], visit["action_plans"])
        self.assertIn("不能替任何一方作新选择", self.llm.calls[-1]["system_prompt"])
