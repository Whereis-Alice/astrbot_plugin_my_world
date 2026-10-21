"""Personal narration needs its own permission; living and replying still work."""
import json
import unittest
from unittest.mock import AsyncMock

from core.engine import TickOutcome
from core.json_actions import PlannedAction, parse_plan_payload
from tests import test_config_scope as scoped

A, B = scoped.A, scoped.B
P = "test:PrivateMessage:300"


class LifeShareTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = scoped.ConfigScopeTests.asyncSetUp
    override = scoped.ConfigScopeTests.override

    def configure(self, allowed=()):
        self.store.add_session(P, session_type="private")
        world = self.store.raw_world()
        world["proactive_session_whitelist"] = [A, B, P]
        world["life_share_session_whitelist"] = list(allowed)
        world["actions"] = [row for row in world["actions"] if row["id"] != "test_work"]
        world["actions"].append({"id":"test_work", "name":"收好手边的书", "scope":"global",
            "category":"continuous", "llm_level":"template", "duration":60, "duration_mode":"fixed",
            "visible":True, "template":"我去收一下书。", "on_complete":{
                "trigger":"llm_followup", "effects":{"energy":"+0.1"}}})
        self.store.save_world(world)
        self.engine.reload_config()

    async def finish(self, session=A, origin=None):
        outcome = TickOutcome(session)
        async with self.engine.session_state(session) as state:
            state.energy = .5
            action = {"type":"test_work", "duration_ticks":1,"elapsed_ticks":1,"session":session}
            if origin is not None:
                action["speech_kind"] = origin
            state.current_action = action
            await self.engine._finish_action(state, self.engine.node(state.node_id), outcome, action)
            self.assertIsNone(state.current_action)
            self.assertGreater(state.energy, .5)
        await self.engine._deliver(outcome)
        return outcome

    async def test_legacy_completion_without_origin_keeps_effects_and_memory_but_cannot_broadcast(self):
        self.configure()
        self.engine._llm_followup = AsyncMock(return_value=True)
        outcome = await self.finish()
        self.engine._llm_followup.assert_not_awaited()
        self.assertFalse(self.messenger.sent)
        memories = self.engine.memory.db.query_memories(session_id=A, limit=10)
        self.assertTrue(any("收好手边的书" in row["content"] for row in memories))
        self.assertTrue(any("生活分享未获全局授权" in note for note in outcome.notes))

    async def test_only_explicitly_allowed_group_or_private_can_share_completion(self):
        self.configure([A, P])
        async def speak(_state, _node, outcome, *_args, **_kwargs):
            outcome.add_speech("书已经收好了。")
            return True
        self.engine._llm_followup = AsyncMock(side_effect=speak)
        for session in (A, B, P):
            await self.finish(session, "life_share")
        self.assertEqual([session for session, _ in self.messenger.sent], [A, P])

    async def test_revoking_queued_life_speech_does_not_revoke_reply_or_contact(self):
        self.configure([A])
        pending = TickOutcome(A, speech_kind="life_share")
        pending.add_speech("我刚收完书。")
        self.configure()
        await self.engine._deliver(pending)
        for kind in ("reply", "participation", "interject", "outreach"):
            self.assertTrue(self.engine.speech_allowed(A, kind), kind)
        for kind in ("", "life_share", "schedule", "event"):
            self.assertFalse(self.engine.speech_allowed(A, kind), kind)
        self.engine._llm_followup = AsyncMock(return_value=True)
        await self.finish(A, "reply")
        self.engine._llm_followup.assert_awaited_once()
        self.assertFalse(self.messenger.sent)

    async def test_continuous_and_instant_physical_actions_still_happen_quietly(self):
        self.configure()
        async with self.engine.session_state(A) as state:
            state.current_action, state.current_plan = None, None
            outcome = TickOutcome(A)
            await self.engine._execute_actions(state, self.engine.node(state.node_id), outcome,
                [PlannedAction(type="test_work")], depth=0, autonomous=True)
            self.assertEqual(state.current_action["type"], "test_work")
            self.assertEqual(state.current_action["speech_kind"], "life_share")
            self.assertFalse(outcome.messages)
            definition = self.engine.world.action_map()["test_work"].model_copy(
                update={"id":"test_instant", "category":"instant", "llm_level":"single"})
            self.engine._instant_output = AsyncMock(return_value=["书收好了。"])
            state.energy = .5
            await self.engine._start_action(state, self.engine.node(state.node_id), outcome,
                definition, PlannedAction(type="test_instant"), depth=0, autonomous=True)
            self.assertGreater(state.energy, .5)
            self.engine._instant_output.assert_not_awaited()
            self.assertFalse(outcome.messages)

    async def test_shared_life_and_local_settings_do_not_expand_personal_destinations(self):
        self.configure([A])
        sessions = self.store.raw_sessions()
        sessions["groups"] = [{"id":"together","sessions":[A,B,P],"main_session":A}]
        self.store.save_sessions(sessions)
        self.override(B, life_share_session_whitelist=[B, P])
        outcome = TickOutcome(A, speech_kind="life_share", broadcast_targets=[B,P])
        outcome.add_speech("收完书了。")
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.sent, [(A,["收完书了。"])])

    async def test_model_cannot_label_its_life_plan_as_a_reply(self):
        self.configure()
        plan, _ = parse_plan_payload(json.dumps({"plan":[{
            "action":"say","messages":["我在收书"],"kind":"reply","interject":True}]}),
            available_actions={"say"})
        self.assertNotIn("kind", plan["steps"][0])
        self.assertNotIn("interject", plan["steps"][0])
        async with self.engine.session_state(A) as state:
            outcome = TickOutcome(A)
            await self.engine._apply_plan(state, self.engine.node(state.node_id), outcome, plan)
            self.assertEqual(state.current_plan["steps"][0]["kind"], "life_share")
            await self.engine._tick_plan(state, self.engine.node(state.node_id), outcome, depth=0)
        await self.engine._deliver(outcome)
        self.assertFalse(self.messenger.sent)
