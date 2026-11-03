"""A plan's next personal activity must not append an unchosen second commentary."""
import json
import unittest

from core.engine import TickOutcome
from core.json_actions import PlannedAction
from core.models import ActionDef
from tests import test_engine as fixture


class PersonalExpressionTurnTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    async def activity(self, outcome):
        definition = ActionDef(id="turn_catalogue_page", name="翻一页图鉴",
            llm_level="single", visible=True,
            on_complete={"effects": {"energy": "+0.04", "mood": "mood:平静"}})
        self.engine.world.actions.append(definition)
        self.llm.replies = [json.dumps({"intent": "life_share",
            "reasoning": {"inner": "这页的标签有点意思。"},
            "actions": [{"type": "say", "messages": ["这页还有个待核的标签。"]}]}, ensure_ascii=False)]
        async with self.engine.session_state(fixture.SESSION) as state:
            state.energy = .5
            await self.engine._execute_actions(state, self.engine.node(state.node_id), outcome,
                [PlannedAction(type=definition.id)], autonomous=outcome.speech_kind != "reply", depth=0)
            self.assertAlmostEqual(state.energy, .54)
            self.assertEqual(state.mood, "平静")
        return outcome

    async def test_outreach_then_personal_step_does_not_generate_another_comment(self):
        outcome = TickOutcome(fixture.SESSION, speech_kind="outreach")
        outcome.add_speech("嗯，我这边已经关屏了。")
        await self.activity(outcome)
        self.assertFalse(self.llm.calls)
        self.assertEqual(outcome.messages, ["嗯，我这边已经关屏了。"])
        memories = self.engine.memory.db.query_memories(session_id=fixture.SESSION, limit=10)
        self.assertTrue(any("翻一页图鉴" in row["content"] for row in memories))

    async def test_accepted_earlier_segment_also_keeps_the_activity_quiet(self):
        outcome = TickOutcome(fixture.SESSION, speech_kind="outreach")
        outcome.accepted_speech_at[fixture.SESSION] = self.clock.now()
        await self.activity(outcome)
        self.assertFalse(self.llm.calls)
        self.assertFalse(outcome.messages)

    async def test_another_room_does_not_consume_this_rooms_expression(self):
        outcome = TickOutcome(fixture.SESSION, speech_kind="life_share")
        outcome.add_speech("书收好了。", fixture.OTHER_SESSION)
        await self.activity(outcome)
        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual(outcome.messages, ["这页还有个待核的标签。"])

    async def test_user_requested_action_keeps_its_following_expression(self):
        outcome = TickOutcome(fixture.SESSION, speech_kind="reply")
        outcome.add_speech("我翻一下。")
        await self.activity(outcome)
        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual(len(outcome.messages), 2)

    async def test_next_turns_new_life_event_can_still_be_shared(self):
        await self.activity(TickOutcome(fixture.SESSION, speech_kind="life_share"))
        self.assertEqual(len(self.llm.calls), 1)


if __name__ == "__main__":
    unittest.main()
