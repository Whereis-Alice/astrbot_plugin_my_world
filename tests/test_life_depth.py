"""Reproduce missing inner thoughts and preserve concrete, scoped stage continuity."""
import copy
import json
import unittest
from unittest.mock import Mock

from core.engine import TickOutcome
from core import life_matters as matters
from core.models import ActionDef, MemoryConfig
from tests import test_engine as fixture

S = fixture.SESSION


class LifeDepthTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp
    add_group = fixture.EngineTestCase.add_group

    async def test_model_inner_survives_plan_conversion_and_is_displayed(self):
        self.llm.replies = [json.dumps({"plan": [{"action": "read", "duration": 600}],
            "inner": "想接着昨天读到的地方往下看。", "reason": "昨天的阅读还留了一个问题"})]
        async with self.engine.session_state(S) as state:
            outcome = TickOutcome(S)
            plan = await self.engine._ask_llm_for_plan(state, self.engine.node("study"), outcome)
            self.assertIsNotNone(plan)
            await self.engine._apply_plan(state, self.engine.node("study"), outcome, plan)
        payload = await self.engine.thoughts_payload(S)
        self.assertEqual(payload["thoughts"][0]["content"], "想接着昨天读到的地方往下看。")
        self.assertEqual(payload["thoughts"][0]["detail"]["intent"], "昨天的阅读还留了一个问题")
        self.assertEqual(self.messenger.sent, [])

    async def test_quiet_planning_uses_existing_budget_and_never_speaks_or_calls_tools(self):
        self.engine.decider.should_ask_llm = Mock(return_value=True)
        self.llm.replies = [json.dumps({"plan": [{"action": "say", "messages": ["不应发送"]},
            {"action": "read", "duration": 600}], "inner": "刚读过一点，现在慢慢继续。", "reason": "安静读书"})]
        async with self.engine.session_state(S) as state:
            state.energy = .9
            await self.engine._quiet_rule_plan(state, self.engine.node("study"), TickOutcome(S))
            self.assertEqual(len(self.llm.calls), 1)
            state.current_action = state.current_plan = None
            await self.engine._quiet_rule_plan(state, self.engine.node("study"), TickOutcome(S))
            self.assertEqual(len(self.llm.calls), 1, "quiet thoughts cannot bypass their hourly interval")
        self.assertEqual(self.messenger.sent, [])
        self.assertEqual(self.tools.calls, [])
        self.assertIn("不发消息", self.llm.calls[0]["prompt"])

    async def test_zero_planning_allowance_is_respected_in_a_new_hour(self):
        state = await self.engine.load_state(S, cold_start=False)
        self.engine.world.limits.max_llm_plan_per_hour = 0
        state.llm_plan_hour_marker = -1
        self.assertFalse(self.engine._llm_plan_allowed(state))

    async def test_thought_health_distinguishes_stale_clock_without_inventing_thoughts(self):
        async with self.engine.session_state(S) as state:
            state.thoughts = []
            state.life_updated_at = self.clock.now() - 33 * 3600
        payload = await self.engine.thoughts_payload(S)
        self.assertFalse(payload["health"]["clock_recent"])
        self.assertEqual(payload["thoughts"], [])
        async with self.engine.session_state(S) as state:
            state.life_updated_at = self.clock.now()
        self.assertTrue((await self.engine.thoughts_payload(S))["health"]["clock_recent"])

    async def test_rule_motive_uses_actual_activity_and_does_not_repeat_the_first_action(self):
        state = await self.engine.load_state(S, cold_start=False)
        state.node_id, state.energy, state.boredom = "study", .9, .7
        state.life_interests = {"read": {"label": "阅读", "minutes": 45, "last_at": self.clock.now()}}
        self.engine.world.actions.append(ActionDef(id="draw", name="画一会儿", life_role="interest", category="continuous", duration=600))
        plan = self.engine.decider._custom_life_plan(state, grounded_only=True)
        self.assertNotEqual(plan["steps"][-1]["action"], "read")
        self.assertIn("阅读", plan["reason"])
        self.assertNotIn("喜欢", plan["reason"])

    async def test_stage_notes_feed_the_next_plan_with_revision_and_source_protection(self):
        self.engine.schedules.schedules = []
        async with self.engine.session_state(S) as state:
            state.state, state.energy = "idle", .9
            state.current_action = state.current_plan = None
            item = self.engine.add_life_matter(state, "写一篇短篇故事")
            item.update(needs_review=True, status="active", steps=[{"id": "draft", "text": "写开头", "status": "done",
                "result": {"ok": True, "kind": "life_simulation", "minutes": 20, "result": "世界内写了开头"}}])
            snapshot = copy.deepcopy(item)
        self.llm.replies = [json.dumps({"outcome": "continue", "reason": "还需要发展冲突", "stage": {
            "summary": "写下风车停转的开头", "content": "风车停转的早晨，她在塔底发现一封没有署名的信。", "next_focus": "接着信件的线索写人物分歧", "basis_step_ids": ["draft"]}})]
        await self.engine._review_matter_stage(S, snapshot, world=self.engine.world, state_key=self.engine.state_key(S), persona=await self.engine._persona_text(S))
        state = await self.engine.load_state(S, cold_start=False)
        updated = matters.find(state, item["id"])
        self.assertEqual(updated["stage_notes"][0]["basis_step_ids"], ["draft"])
        self.assertIn("信件", updated["next_step"])
        block = matters.context_block(state, session_id=S, now=self.clock.now())
        self.assertIn("没有署名的信", block)
        self.assertIn("模拟内容", block)
        self.assertIsNone(matters.record_stage(updated, updated["stage_notes"][0], now=self.clock.now()), "a completed step cannot be reviewed twice")
        self.llm.replies = [json.dumps({"steps": [{"text": "延续信件的线索", "action": "life_simulation", "node": "study", "minutes": 5, "day_offset": 0}]})]
        await self.engine.advance_life_matters(S)
        self.assertIn("没有署名的信", self.llm.calls[-1]["prompt"])
        self.assertIn("next_focus", self.llm.calls[-1]["prompt"])
        updated["who"] = "42"
        self.assertNotIn("没有署名的信", matters.activity_basis(state, now=self.clock.now()))
        self.assertNotIn("没有署名的信", matters.context_block(state, session_id=fixture.OTHER_SESSION, now=self.clock.now()))

    async def test_changing_goal_keeps_old_notes_as_history_without_feeding_the_new_goal(self):
        async with self.engine.session_state(S) as state:
            item = self.engine.add_life_matter(state, "写短篇")
            item["stage_notes"] = [{"summary": "旧目标笔记", "content": "旧草稿", "at": self.clock.now()}]
            item["steps"] = [{"id": "old", "status": "done", "result": {"ok": True, "kind": "life_simulation", "minutes": 5}}]
            self.engine.manage_life_matter(state, item["id"], "edit", text="学习一道新菜")
            self.assertEqual(item["stage_notes"][0]["goal"], "写短篇")
            self.assertEqual(matters.current_stage_notes(item), [])
            self.assertNotIn("旧草稿", matters.context_block(state, session_id=S, now=self.clock.now()))
            self.assertIsNone(matters.record_stage(item, {"summary": "新菜学会了", "content": "假结论", "basis_step_ids": ["old"]}, now=self.clock.now()))

    async def test_stage_notes_do_not_displace_other_matters_from_context(self):
        async with self.engine.session_state(S) as state:
            state.life_matters = []
            for index in range(6):
                item = self.engine.add_life_matter(state, f"生活事项{index}")
                item["stage_notes"] = [{"summary": f"阶段{index}", "content": f"具体内容{index}", "at": self.clock.now()}]
            block = matters.context_block(state, session_id=S, now=self.clock.now())
            for index in range(6):
                self.assertIn(f"生活事项{index}", block)
                self.assertIn(f"具体内容{index}", block)

    def test_stage_rejects_unexecuted_and_forged_sources_and_bounds_content(self):
        item = {"steps": [{"id": "future", "status": "pending"}]}
        stage = {"summary": "结论", "content": "内容", "basis_step_ids": ["future"]}
        self.assertIsNone(matters.record_stage(item, stage, now=1))
        item["steps"][0].update(status="done", result={"ok": True, "kind": "life_simulation", "minutes": 5})
        self.assertIsNone(matters.record_stage(item, {**stage, "basis_step_ids": ["made-up"]}, now=1))
        note = matters.record_stage(item, {**stage, "content": "细节" * 5000}, now=1)
        self.assertEqual(len(note["content"]), 1200)

    def test_direct_embedding_settings_keep_their_source(self):
        old = MemoryConfig(semantic_endpoint="https://example.invalid/v1", semantic_model="bge")
        self.assertEqual(old.semantic_source, "custom")
        self.assertEqual(MemoryConfig().semantic_source, "provider")
