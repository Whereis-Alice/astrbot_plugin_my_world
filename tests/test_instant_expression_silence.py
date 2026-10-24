"""A real instant action must retain an empty or explicitly quiet model result."""
import json
import unittest

from core.engine import TickOutcome
from core.json_actions import PlannedAction
from core.models import ActionDef
from tests import test_engine as fixture


class InstantExpressionSilenceTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    async def execute(self, response, *, blocked=False):
        definition = ActionDef(id="sunset_adjust_bench_posture", name="倚坐长椅微调坐姿",
            category="instant", llm_level="single", visible=True,
            on_complete={"effects": {"energy": "+0.04", "mood": "mood:放松"}})
        self.engine.world.actions.append(definition)
        self.llm.replies = [response]
        if blocked:
            self.engine.world.limits.max_llm_text_per_hour = 0
        outcome = TickOutcome(fixture.SESSION, speech_kind="outreach")
        async with self.engine.session_state(fixture.SESSION) as state:
            state.energy = .5
            await self.engine._execute_actions(state, self.engine.node(state.node_id),
                outcome=outcome, actions=[PlannedAction(type=definition.id)], autonomous=True, depth=0)
            self.assertAlmostEqual(.54, state.energy)
            self.assertEqual("放松", state.mood)
        await self.engine._deliver(outcome)
        return outcome

    async def test_explicit_listening_keeps_thought_and_activity_without_narration(self):
        outcome = await self.execute(json.dumps({
            "reasoning": {"inner": "这角度靠着确实舒服多了。", "intent": "保持安静继续休息"},
            "intent": "listen", "actions": [],
        }, ensure_ascii=False))
        self.assertEqual([], self.messenger.flat_messages)
        self.assertEqual([], outcome.messages)
        self.assertEqual("listening", outcome.speech_decisions[0]["status"])
        thoughts = (await self.engine.thoughts_payload(fixture.SESSION))["thoughts"]
        self.assertTrue(any(row["content"] == "这角度靠着确实舒服多了。" for row in thoughts))
        rows = await self.db.call("query_events", session_id=fixture.SESSION, limit=10)
        actions = [row for row in rows if row["event_type"] == "action"]
        self.assertTrue(any(row["detail"]["type"] == "sunset_adjust_bench_posture" for row in actions))

    async def test_empty_model_result_does_not_fabricate_a_stage_direction(self):
        await self.execute('{"actions":[]}')
        self.assertEqual([], self.messenger.flat_messages)

    async def test_exhausted_generation_budget_does_not_bypass_quiet_result(self):
        await self.execute('{"actions":[]}', blocked=True)
        self.assertEqual([], self.messenger.flat_messages)
        self.assertEqual([], self.llm.calls)

    async def test_successful_expression_retains_exact_message_and_receipt(self):
        outcome = await self.execute(json.dumps({
            "reasoning": {"inner": "靠着椅背舒服多了。", "intent": "随口分享"},
            "intent": "outreach", "actions": [{"type": "say", "messages": ["这边坐着挺舒服。"]}],
        }, ensure_ascii=False))
        self.assertEqual(["这边坐着挺舒服。"], self.messenger.flat_messages)
        self.assertEqual("sent", outcome.speech_decisions[0]["status"])
