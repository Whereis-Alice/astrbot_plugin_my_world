"""Generic action evidence reaches both public runtime prompt paths."""
import unittest

from core.engine import TickOutcome
from core.state import WorldState
from tests import test_engine as fixture


class PublicExpressionGuardrailTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    async def test_both_runtime_paths_have_the_same_execution_order_evidence(self):
        state = WorldState(fixture.SESSION, node_id="study", current_action={"type": "read"},
            recent_events=[{"kind": "action_done", "detail": {"type": "read"}, "at": self.clock.now()-600}],
            current_plan={"current_step": 0, "steps": [{"action": "read"}, {"action": "walk_to", "target_node": "window"}]})
        original = state.to_payload()
        request = self.engine.context_request(state, fixture.SESSION, task="expression", text="整理读书结果")
        for selected in (None, request):
            with self.subTest(scoped_context=selected is not None):
                text = self.engine.prompts.runtime_layer(state, node=self.engine.node("study"), context_request=selected)
                self.assertIn("自己行动的执行事实", text)
                self.assertIn("约 10 分钟前完成", text)
                self.assertIn("正在进行，尚未结束", text)
                self.assertIn("尚未开始的安排", text)
        self.assertEqual(state.to_payload(), original)

    async def test_visiting_does_not_claim_home_execution_in_either_path(self):
        state = WorldState(fixture.SESSION, node_id="study", current_action={"type": "read"},
            recent_events=[{"kind": "action_done", "detail": {"type": "read"}, "at": self.clock.now()}])
        request = self.engine.context_request(state, fixture.SESSION, task="expression", text="正在串门")
        for selected in (None, request):
            text = self.engine.prompts.runtime_layer(state, node=self.engine.node("study"),
                context_request=selected, world_visit="友邻的花园", visit_activity="和朋友一起散步")
            self.assertIn("友邻的花园", text)
            self.assertNotIn("自己行动的执行事实", text)

    async def test_legacy_bad_execution_records_do_not_interrupt_either_runtime_path(self):
        state = WorldState(fixture.SESSION, node_id="study")
        state.current_action = ["broken"]
        state.current_plan = {"current_step": -1, "steps": [{"action": "read"}]}
        state.recent_events = [None, {"kind": "action_done", "detail": {"type": "read"}, "at": "nan"}]
        original = state.to_payload()
        request = self.engine.context_request(state, fixture.SESSION, task="reply", text="你在做什么")
        for selected in (None, request):
            with self.subTest(scoped_context=selected is not None):
                text = self.engine.prompts.runtime_layer(state, node=self.engine.node("study"), context_request=selected)
                self.assertIn("你的状态", text)
                self.assertNotIn("尚未开始的安排", text)
                self.assertNotIn("约 0 分钟前完成", text)
        self.assertEqual(state.to_payload(), original)

    async def test_followup_uses_generic_evidence_without_rewriting_action_configuration(self):
        state = await self.engine.load_state(fixture.SESSION, cold_start=False)
        definition = self.engine.world.action_map()["read"]
        definition.on_complete.prompt_hint = "说说读到的细节。"
        original = definition.model_dump()
        outcome = TickOutcome(fixture.SESSION, speech_kind="life_share")
        self.llm.replies = ['{"reasoning":{"inner":"把这点记在心里就好。"},"intent":"listen","actions":[]}']
        await self.engine._llm_followup(state, self.engine.node(state.node_id), outcome,
            definition.on_complete.prompt_hint, "你刚刚读完了这本书。", definition=definition)
        prompt = self.llm.calls[-1]["prompt"]
        self.assertIn("动作完成不要求向外通知", prompt)
        self.assertIn("不自行补出附带的设备、环境或身体状态变化", prompt)
        self.assertIn("没有新事实、新问题或新的情绪变化就保持安静", prompt)
        self.assertEqual(definition.model_dump(), original)
        self.assertFalse(outcome.messages or self.messenger.sent)


if __name__ == "__main__":
    unittest.main()
