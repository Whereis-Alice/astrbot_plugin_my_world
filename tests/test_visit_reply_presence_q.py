"""A real incoming QQ reply receives the current confirmed shared scene."""
from __future__ import annotations

import unittest

from tests import fake_astrbot

fake_astrbot.install()

from core.models import ActionDef  # noqa: E402 - install the SDK fixture before importing the engine
from tests import test_engine as fixture  # noqa: E402
from tests import test_visits_actor as actor_fixture  # noqa: E402


class VisitReplyPresenceTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = actor_fixture.VisitActorTests.asyncSetUp
    ctx = fixture.EngineTestCase.ctx

    async def test_real_injected_reply_knows_current_visit_without_a_plugin_model_call(self):
        self.active = self.visit
        ctx = self.ctx(text="你现在在干嘛呀？", is_mentioned=True)
        await self.engine.handle_incoming(ctx)
        plan = await self.engine.handle_reply(ctx, inject_only=True)
        self.assertTrue(plan.ok, plan.reason)
        self.assertIn("港湾", plan.runtime_part)
        self.assertIn("一起下棋", plan.runtime_part)
        self.assertFalse(self.llm.calls)

    async def test_invitation_does_not_present_a_planned_visit_as_current_place(self):
        self.active = {**self.visit, "status": "offered", "arrived_at": 0}
        ctx = self.ctx(text="你现在在哪儿？", is_mentioned=True)
        await self.engine.handle_incoming(ctx)
        plan = await self.engine.handle_reply(ctx, inject_only=True)
        self.assertTrue(plan.ok, plan.reason)
        self.assertNotIn("正在港湾拜访", plan.runtime_part)
        self.assertFalse(self.llm.calls)

    async def test_legacy_prompt_api_keeps_home_sleep_paused_and_uses_the_visit_scene(self):
        state = await self.engine.load_state(fixture.SESSION)
        state.state = "sleeping"
        state.current_action = {"desc": "家中睡觉", "persona_key": self.local["persona_key"]}
        node = self.engine.world.node_map()[self.node_id]
        system, runtime = self.engine.prompts.build_dialogue_prompt(state=state, persona_text=self.persona.text,
            node=node, world_visit="正在港湾拜访另一个她；在河边小公园，沿河散步。",
            visit_activity="与另一个她在河边小公园，沿河散步")
        self.assertIn("实际地点与活动见运行时状态", system)
        self.assertIn("当前动作：与另一个她在河边小公园，沿河散步", runtime)
        self.assertNotIn("当前动作：正在睡觉", runtime)
        self.assertEqual(state.state, "sleeping")
        self.assertEqual(state.current_action["desc"], "家中睡觉")
        self.assertFalse(self.llm.calls)

    async def test_reply_sees_confirmed_activity_and_not_an_unaccepted_new_place(self):
        self.active = {**self.visit, "outing": {"version": 1, "stage": 0,
            "place": "河边小公园", "activity": "沿河散步", "started_at": self.clock.now(),
            "until": self.clock.now() + 1800, "route": [{"place": "河边小公园",
                "activity": "沿河散步", "started_at": self.clock.now(), "until": self.clock.now() + 1800}]},
            "outing_offer": {"place": "远方海边", "activity": "捡贝壳", "minutes": 120}}
        async with self.engine.session_state(fixture.SESSION) as state:
            state.node_since = self.clock.now() - 7200
        ctx = self.ctx(text="你们在哪里玩？", is_mentioned=True)
        await self.engine.handle_incoming(ctx)
        self.clock.advance(600)
        plan = await self.engine.handle_reply(ctx, inject_only=True)
        self.assertTrue(plan.ok)
        self.assertIn("河边小公园", plan.runtime_part)
        self.assertIn("沿河散步", plan.runtime_part)
        self.assertIn("10分钟", plan.runtime_part)
        self.assertNotIn("你在这个地方已经待了", plan.runtime_part)
        self.assertNotIn("远方海边", plan.runtime_part)
        self.assertFalse(self.llm.calls)

    async def test_arrival_before_joint_activity_confirmation_does_not_claim_the_hosts_plan(self):
        self.active = {**self.visit, "outing_version": 1,
            "scene": {"place": "远方海边", "activity": "捡贝壳"},
            "outing_offer": {"place": "远方海边", "activity": "捡贝壳", "minutes": 120}}
        ctx = self.ctx(text="你们已经开始玩了吗？", is_mentioned=True)
        await self.engine.handle_incoming(ctx)
        plan = await self.engine.handle_reply(ctx, inject_only=True)
        self.assertTrue(plan.ok)
        self.assertIn("已经来到港湾", plan.runtime_part)
        self.assertIn("尚未共同确认", plan.runtime_part)
        self.assertNotIn("远方海边", plan.runtime_part)
        self.assertNotIn("捡贝壳", plan.runtime_part)
        self.assertFalse(self.llm.calls)

    async def test_elapsed_stage_does_not_invent_a_new_place_or_result(self):
        self.active = {**self.visit, "outing": {"version": 1, "stage": 0,
            "place": "河边小公园", "activity": "沿河散步", "started_at": self.clock.now() - 1900,
            "until": self.clock.now() - 100, "route": [{"place": "河边小公园",
                "activity": "沿河散步", "started_at": self.clock.now() - 1900, "until": self.clock.now() - 100}]}}
        ctx = self.ctx(text="现在在做什么？", is_mentioned=True)
        await self.engine.handle_incoming(ctx)
        plan = await self.engine.handle_reply(ctx, inject_only=True)
        self.assertTrue(plan.ok)
        self.assertIn("下一步还在商量", plan.runtime_part)
        self.assertIn("双方下一步尚未确认", plan.runtime_part)
        self.assertFalse(self.llm.calls)

    async def test_paused_home_nap_does_not_silence_her_during_the_confirmed_visit(self):
        self.active = self.visit
        self.engine.world.sleep.block_plugins = True
        self.engine.world.sleep.block_scope = "all"
        self.engine.world.sleep.reply_mode = "silent"
        self.engine.world.actions.append(ActionDef(id="reply-home-nap", name="家中的小睡",
            enabled=True, category="continuous", recovery_kind="nap", during={"state": "napping"}))
        async with self.engine.session_state(fixture.SESSION) as state:
            state.state = "napping"
            state.current_action = {"type": "reply-home-nap", "duration_ticks": 20,
                "elapsed_ticks": 2, "persona_key": self.local["persona_key"]}
        ctx = self.ctx(text="在朋友那里玩什么呢？", is_mentioned=True)
        self.assertFalse(await self.engine.should_block_sleep(ctx))
        self.assertIsNone(await self.engine.sleep_gate(ctx))
        state = await self.engine.load_state(fixture.SESSION)
        self.assertEqual(state.state, "napping")
        self.assertEqual(state.current_action["elapsed_ticks"], 2)
        self.assertFalse(self.llm.calls)
