"""Action speech must leave its own thoughts without fabricating sent replies."""

import json
import unittest

from core.engine import TickOutcome
from core.json_actions import PlannedAction
from tests import test_config_scope as scoped

A, B = scoped.A, scoped.B


class ExpressionThoughtTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = scoped.ConfigScopeTests.asyncSetUp

    def configure(self):
        world = self.store.raw_world()
        world["reasoning_enabled"] = True
        world["actions"].extend([
            {"id": "test_bookmarks", "name": "整理书签", "scope": "global",
             "category": "continuous", "llm_level": "template", "duration": 600,
             "on_complete": {"trigger": "llm_followup", "prompt_hint": "说说书签"}},
            {"id": "test_glasses", "name": "擦眼镜", "scope": "global",
             "category": "instant", "llm_level": "single", "visible": True},
        ])
        self.store.save_world(world)
        sessions = self.store.raw_sessions()
        sessions["groups"] = [{"id": "shared", "sessions": [A, B], "main_session": A}]
        self.store.save_sessions(sessions)
        self.engine.reload_config()

    async def test_completed_action_and_nested_speech_keep_their_own_scoped_thoughts(self):
        self.configure()
        self.engine.llm.replies = [
            json.dumps({"reasoning": {"inner": "刚才的误会已经解释清楚了。",
                "who": "朋友", "intent": "顺着刚才的话接一句"}, "actions": [
                {"type": "say", "messages": ["这下说清楚了。"]},
                {"type": "test_glasses"}]}),
            json.dumps({"reasoning": {"inner": "擦完眼镜，心里也轻松了。",
                "who": "朋友", "intent": "轻松地回应"}, "actions": [
                {"type": "say", "messages": ["现在清楚多了。"]}]}),
        ]
        outcome = TickOutcome(B, speech_kind="life_share")
        async with self.engine.session_state(B) as state:
            self.assertEqual(state.session_id, A)
            action = {"type": "test_bookmarks", "duration_ticks": 10,
                "elapsed_ticks": 10, "session": B, "speech_kind": "life_share"}
            state.current_action = action
            await self.engine._finish_action(state, self.engine.node(state.node_id), outcome, action)
            # The older queued thought must not substitute for either new one.
            await self.engine._execute_actions(state, self.engine.node(state.node_id), outcome,
                [PlannedAction(type="think", content="书签已经干透了。")], depth=0, autonomous=True)

        # Reload the persisted state through the same payload used by the WebUI.
        rows = (await self.engine.thoughts_payload(B))["thoughts"]
        sources = {row["source"]: row for row in rows}
        self.assertEqual(sources["动作结束后"]["content"], "刚才的误会已经解释清楚了。")
        self.assertEqual(sources["准备说话"]["content"], "擦完眼镜，心里也轻松了。")
        self.assertEqual(sources["她想事情"]["content"], "书签已经干透了。")
        self.assertEqual(sources["动作结束后"]["detail"]["intent"], "顺着刚才的话接一句")
        self.assertEqual(sources["动作结束后"]["target"], "朋友")
        for source in ("动作结束后", "准备说话"):
            self.assertFalse(sources[source]["reply"])
            self.assertFalse(sources[source]["browse_session"])
        other_rows = (await self.engine.thoughts_payload(A))["thoughts"]
        self.assertFalse(any(row["source"] in {"动作结束后", "准备说话"} for row in other_rows))
        self.assertEqual(len(self.engine.llm.calls), 2)
        self.assertFalse(self.messenger.sent)
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.sent, [(B, ["这下说清楚了。", "现在清楚多了。"])])

    async def test_silent_followup_keeps_the_model_thought_without_sending(self):
        self.configure()
        self.engine.llm.replies = [json.dumps({"reasoning": {
            "inner": "先安静地把书收好。", "intent": "不打断别人"}, "actions": []})]
        outcome = TickOutcome(B, speech_kind="life_share")
        async with self.engine.session_state(B) as state:
            spoke = await self.engine._llm_followup(state, self.engine.node(state.node_id),
                outcome, "说说书签", "整理完了书签。")
        self.assertFalse(spoke)
        rows = (await self.engine.thoughts_payload(B))["thoughts"]
        self.assertEqual([row["content"] for row in rows], ["先安静地把书收好。"])
        await self.engine._deliver(outcome)
        self.assertFalse(self.messenger.sent)

    async def test_text_generation_keeps_silent_inner_and_does_not_invent_missing_inner(self):
        self.configure()
        self.engine.llm.replies = [
            '{"reasoning":{"inner":"这句话还是先留在心里。"},"actions":[]}',
            '{"actions":[{"type":"say","messages":["书收好了。"]}]}',
            '眼镜也擦好了。',
        ]
        async with self.engine.session_state(B) as state:
            results = [await self.engine._generate_text_actions(state, self.engine.node(state.node_id),
                "说说刚做的事", session_id=B) for _ in range(3)]
        self.assertEqual(results, [[], ["书收好了。"], ["眼镜也擦好了。"]])
        rows = (await self.engine.thoughts_payload(B))["thoughts"]
        self.assertEqual([row["content"] for row in rows], ["这句话还是先留在心里。"])
        self.assertFalse(rows[0]["reply"])
        self.assertFalse(self.messenger.sent)

    async def test_reasoning_switch_controls_the_generation_output_contract(self):
        self.configure()
        for enabled in (True, False):
            self.engine.world.reasoning_enabled = enabled
            self.engine.llm.replies = ['{"actions":[]}']
            async with self.engine.session_state(B) as state:
                await self.engine._generate_text_actions(state, self.engine.node(state.node_id),
                    "说说刚做的事", session_id=B)
            output_contract = self.engine.llm.calls[-1]["prompt"].split("只输出 JSON：")[-1]
            self.assertEqual("reasoning" in output_contract, enabled)
