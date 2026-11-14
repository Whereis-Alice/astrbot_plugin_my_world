"""A life/contact plan cannot borrow another speech source's chat attention."""
import copy
import json
import unittest
from unittest.mock import AsyncMock

from tests import fake_astrbot
fake_astrbot.install()

from core.background_budget import record_background_admission  # noqa: E402
from core.engine.participation import ParticipationMixin  # noqa: E402
from core.engine import TickOutcome  # noqa: E402
from tests import test_participation as fixture  # noqa: E402
from tests import test_autonomous_plan_intent as autonomy_fixture  # noqa: E402
from tests import test_expression_attention as expression_fixture  # noqa: E402

GROUP, OTHER = fixture.GROUP, fixture.OTHER


class PlanChatAttentionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.ParticipationTests.asyncSetUp
    chat = fixture.ParticipationTests.chat
    prepare = autonomy_fixture.AutonomousPlanIntentTests.prepare
    run_plan = autonomy_fixture.AutonomousPlanIntentTests.run_plan

    def cadence(self):
        self.engine.participation_sample = ParticipationMixin.participation_sample.__get__(self.engine)
        self.engine.world.decider.participation_level = "quiet"
        self.engine.world.decider.participation_hourly_max = 6

    async def full_budget(self):
        async with self.engine.session_state(GROUP) as state:
            state.participation[OTHER] = {"attempts": [self.clock.now() - 10] * 6}

    async def test_full_shared_budget_blocks_plan_chat_but_keeps_inner_and_quiet_activity(self):
        self.cadence()
        await self.chat(message_id="fresh")
        await self.full_budget()
        plan, _ = await self.prepare(intent="continue_chat", reply="c1")
        self.assertEqual([(step["action"], step["kind"]) for step in plan["steps"]], [("quiet_sit", "quiet")])
        self.assertEqual(plan["speech_choice"]["intent"], "continue_chat")
        self.assertIn("预算", plan["speech_choice"]["blocked"])
        await self.run_plan()
        state = await self.engine.load_state(GROUP, cold_start=False)
        self.assertEqual(state.current_action["type"], "quiet_sit")
        self.assertTrue(any(thought["content"] == "先把今晚的事情放下。" for thought in state.thoughts))
        thought = next(thought for thought in state.thoughts if thought["content"] == "先把今晚的事情放下。")
        self.assertEqual(sum(thought["content"] == "先把今晚的事情放下。" for thought in state.thoughts), 1)
        self.assertEqual(thought["detail"]["plan_speech"][0]["intent"], "continue_chat")
        self.assertEqual(thought["detail"]["plan_speech"][0]["status"], "not_sent")
        self.assertEqual(len(self.llm.calls), 1)
        self.assertFalse(self.messenger.sent)
        self.assertFalse(state.participation[GROUP].get("attempts"))

    async def test_recent_actual_reply_leaves_space_before_plan_continuation(self):
        self.cadence()
        await self.chat("她刚说过一句", mine=True, age=1, message_id="own")
        await self.chat("另一个话题", message_id="fresh")
        plan, _ = await self.prepare(intent="continue_chat", reply="c1")
        self.assertIn("blocked", plan["speech_choice"])
        await self.run_plan()
        self.assertFalse(self.messenger.sent)
        self.assertEqual(len(self.llm.calls), 1)

    async def test_fresh_plan_chat_charges_once_through_immediate_saved_step(self):
        self.cadence()
        await self.chat(message_id="fresh")
        plan, _ = await self.prepare(intent="continue_chat", reply="c1")
        self.assertNotIn("blocked", plan["speech_choice"])
        await self.run_plan()
        state = await self.engine.load_state(GROUP, cold_start=False)
        self.assertEqual(len(state.participation[GROUP]["attempts"]), 1)
        self.assertEqual(len(self.messenger.sent), 1)
        self.assertEqual(len(self.llm.calls), 1)

    async def test_new_native_reply_invalidates_saved_plan_admission(self):
        self.cadence()
        await self.chat(message_id="fresh")
        await self.prepare(intent="continue_chat", reply="c1")
        self.clock.advance(1)
        await self.chat("刚回了另一个人", mine=True, message_id="native")
        await self.run_plan()
        state = await self.engine.load_state(GROUP, cold_start=False)
        self.assertFalse(self.messenger.sent)
        self.assertEqual(state.current_action["type"], "quiet_sit")
        self.assertEqual(len(state.participation[GROUP]["attempts"]), 1)

    async def test_legacy_saved_plan_without_proof_cannot_ignore_full_budget(self):
        self.cadence()
        await self.chat(message_id="fresh")
        await self.prepare(intent="continue_chat", reply="c1")
        async with self.engine.session_state(GROUP) as state:
            state.participation = {}
            state.current_plan["speech_scene"].pop("attention", None)
            for step in state.current_plan["steps"]:
                (step.get("speech_scene") or {}).pop("attention", None)
        await self.full_budget()
        await self.run_plan()
        self.assertFalse(self.messenger.sent)
        state = await self.engine.load_state(GROUP, cold_start=False)
        self.assertEqual(state.current_action["type"], "quiet_sit")
        self.assertEqual(len(self.llm.calls), 1)

    async def test_later_plan_step_is_another_local_opportunity_after_actual_speech(self):
        self.cadence()
        first_source = await self.chat(message_id="fresh")
        second_source = await self.chat("还有另一个具体细节", message_id="later-source")
        await self.prepare(intent="continue_chat", reply="c1")
        async with self.engine.session_state(GROUP) as state:
            later = copy.deepcopy(state.current_plan["steps"][0])
            later["messages"] = ["过一会儿又想接一句"]
            state.current_plan["steps"][0]["speech_scene"]["rows"] = [first_source]
            later["speech_scene"]["rows"] = [second_source]
            later["speech_scene"]["reply_to"] = "later-source"
            state.current_plan["steps"].append(later)
        await self.run_plan()
        self.clock.advance(1)
        async with self.engine.session_state(GROUP) as state:
            state.current_action = None
            state.current_plan["current_step"] = 2
        await self.run_plan()
        state = await self.engine.load_state(GROUP, cold_start=False)
        self.assertEqual(len(self.messenger.sent), 1)
        self.assertEqual(len(state.participation[GROUP]["attempts"]), 1)
        self.assertEqual(len(self.llm.calls), 1)
        thought = next(thought for thought in state.thoughts if thought["content"] == "先把今晚的事情放下。")
        self.assertTrue(any(row["status"] == "not_sent" and row["drafts"] == ["过一会儿又想接一句"]
                            for row in thought["detail"]["plan_speech"]))

    async def test_true_life_share_and_outreach_do_not_spend_chat_attention(self):
        self.cadence()
        await self.full_budget()
        plan, _ = await self.prepare(intent="life_share", text="书签刚夹好")
        self.assertNotIn("blocked", plan["speech_choice"])
        await self.run_plan()
        self.assertTrue(self.messenger.sent)
        state = await self.engine.load_state(GROUP, cold_start=False)
        self.assertFalse((state.participation.get(GROUP) or {}).get("attempts"))

    async def test_slow_already_charged_plan_request_is_not_charged_again_at_return(self):
        self.cadence()
        self.engine.world.life_share_session_whitelist = []
        await self.chat("还有一个细节", addressing="me", message_id="fresh")
        original = self.engine._ask_speech_media

        async def slow(*args, **kwargs):
            reply = await original(*args, **kwargs)
            self.clock.advance(130)
            return reply

        self.engine._ask_speech_media = slow
        await self.prepare(intent="continue_chat", reply="c1")
        await self.run_plan()
        state = await self.engine.load_state(GROUP, cold_start=False)
        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual(len(state.participation[GROUP]["attempts"]), 1)
        self.assertEqual(len(self.messenger.sent), 1)


class CompletionChatAttentionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.ParticipationTests.asyncSetUp
    chat = fixture.ParticipationTests.chat
    cadence = PlanChatAttentionTests.cadence
    completion = expression_fixture.ExpressionAttentionTests.completion

    async def test_authorized_life_source_cannot_convert_to_chat_after_budget_is_full(self):
        self.cadence()
        await self.chat(message_id="fresh")
        async with self.engine.session_state(GROUP) as state:
            state.participation[OTHER] = {"attempts": [self.clock.now() - 10] * 6}

        async def generate(*args, **kwargs):
            record_background_admission(started=True)
            return json.dumps({"inner": "真的想接这句。", "intent": "continue_chat", "reply_to": "c1",
                "actions": [{"type": "say", "messages": ["我也想去看看。"]}]}, ensure_ascii=False)

        self.engine._ask_llm = AsyncMock(side_effect=generate)
        outcome = await self.completion("life_share")
        self.assertFalse(outcome.messages)
        self.assertEqual(outcome.speech_decisions[-1]["intent"], "continue_chat")
        self.assertEqual(outcome.speech_decisions[-1]["status"], "not_sent")
        state = await self.engine.load_state(GROUP, cold_start=False)
        self.assertTrue(any(thought["content"] == "真的想接这句。" for thought in state.thoughts))
        self.engine._ask_llm.assert_awaited_once()

    async def test_slow_charged_completion_return_keeps_one_observation(self):
        self.cadence()
        self.engine.world.life_share_session_whitelist = []
        await self.chat("还有一个细节", addressing="me", message_id="fresh")

        async def generate(*args, **kwargs):
            record_background_admission(started=True)
            self.clock.advance(130)
            return json.dumps({"inner": "现在想接一句。", "intent": "continue_chat", "reply_to": "c1",
                "actions": [{"type": "say", "messages": ["我也想去看看。"]}]}, ensure_ascii=False)

        self.engine._ask_llm = AsyncMock(side_effect=generate)
        outcome = await self.completion("continuation")
        state = await self.engine.load_state(GROUP, cold_start=False)
        self.assertEqual(len(state.participation[GROUP]["attempts"]), 1)
        self.assertTrue(outcome.messages)
        self.engine._ask_llm.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
