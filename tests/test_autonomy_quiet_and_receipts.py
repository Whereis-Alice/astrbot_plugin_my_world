"""Silence is a real life choice, and only accepted chat affects reply progress."""

import json
import unittest

from core.engine import TickOutcome
from core.planner import create_plan
from tests import test_participation as fixture


class AutonomyQuietAndReceiptTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.ParticipationTests.asyncSetUp
    chat = fixture.ParticipationTests.chat

    def rule_interjection(self, state, **_):
        return create_plan(steps=[{"action": "say", "interject": True}], world_time=state.world_time)

    async def test_empty_listen_plan_keeps_the_thought_and_ends_the_decision(self):
        self.engine.decider.rule_plan = self.rule_interjection
        self.engine.decider.should_ask_llm = lambda state: True
        self.engine.maybe_start_chat_browse = lambda state: False
        await self.chat("这个风车晚上还开吗？", message_id="a")
        self.llm.replies = [
            json.dumps({"plan": [], "intent": "listen", "inner": "我暂时还想坐一会儿。"}, ensure_ascii=False),
            fixture.SAY,
        ]
        outcome = await self.engine.maybe_decide(fixture.GROUP, force=True)
        state = await self.engine.load_state(fixture.GROUP, cold_start=False)
        self.assertIsNotNone(outcome)
        self.assertTrue(any(thought.get("content") == "我暂时还想坐一会儿。" for thought in state.thoughts))
        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual(self.messenger.sent, [])

    async def test_arrival_stays_quiet_when_the_only_draft_is_rejected(self):
        self.engine.decider.rule_plan = self.rule_interjection
        await self.chat("这个风车晚上还开吗？", message_id="a")
        self.llm.replies = [
            '{"plan":[{"action":"say","messages":["开到很晚"]}],'
            '"intent":"continue_chat","reply_to":"stale-ref","inner":"附近安静多了。"}',
            fixture.SAY,
        ]
        outcome = TickOutcome(fixture.GROUP)
        async with self.engine.session_state(fixture.GROUP) as state:
            await self.engine.decide_after_arrival(state, self.engine.node(state.node_id), outcome)
        await self.engine._deliver(outcome)
        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual(self.messenger.sent, [])

    async def test_bad_model_output_still_uses_rule_fallback(self):
        self.engine.decider.rule_plan = self.rule_interjection
        self.engine.decider.should_ask_llm = lambda state: True
        await self.chat("这台风车我以前见过", message_id="a")
        self.llm.replies = ["invalid plan", fixture.SAY]
        outcome = TickOutcome(fixture.GROUP)
        async with self.engine.session_state(fixture.GROUP) as state:
            await self.engine._decide_normally(state, self.engine.node(state.node_id), outcome, fixture.GROUP)
        await self.engine._deliver(outcome)
        self.assertEqual(len(self.llm.calls), 2)
        self.assertTrue(self.messenger.sent)

    async def pending_life_share(self):
        row = await self.chat("你们见过那种旧风车吗？", message_id="a")
        async with self.engine.session_state(fixture.GROUP) as state:
            state.current_plan = create_plan(steps=[{"action": "say", "kind": "life_share",
                "messages": ["书签压好啦。"]}], world_time=state.world_time)
        outcome = await self.engine._tick_session(fixture.GROUP)
        self.assertEqual(outcome.messages, ["书签压好啦。"])
        return row, outcome

    async def test_tick_life_share_never_marks_unrelated_messages_as_replied(self):
        row, outcome = await self.pending_life_share()
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.sent, [(fixture.GROUP, ["书签压好啦。"])])
        state = await self.engine.load_state(fixture.GROUP, cold_start=False)
        self.assertLess(self.engine._watermark(state, fixture.GROUP)[1], row["seq"])

    async def test_tick_continuation_only_advances_the_receipted_scene(self):
        row = await self.chat("再下去就是晚上了吧", message_id="a")
        async with self.engine.session_state(fixture.GROUP) as state:
            state.current_plan = self.rule_interjection(state)
        outcome = await self.engine._tick_session(fixture.GROUP)
        self.assertTrue(outcome.messages)
        state = await self.engine.load_state(fixture.GROUP, cold_start=False)
        self.assertLess(self.engine._watermark(state, fixture.GROUP)[1], row["seq"])
        latest = await self.chat("另外一边的车轮卡住了", message_id="b")
        await self.engine._deliver(outcome)
        state = await self.engine.load_state(fixture.GROUP, cold_start=False)
        self.assertEqual(self.engine._watermark(state, fixture.GROUP)[1], row["seq"])
        self.assertGreater(latest["seq"], row["seq"])

    async def test_mixed_receipt_only_for_life_share_does_not_consume_chat_claim(self):
        row = await self.chat("你们见过那种旧风车吗？", message_id="a")
        outcome = TickOutcome(fixture.GROUP)
        async with self.engine.session_state(fixture.GROUP) as state:
            self.engine.claim_chat_scene(outcome, fixture.GROUP, [row], state=state)
            outcome.add_speech("桥旁见过一座。", kind="continuation")
            outcome.add_speech("书签压好啦。", kind="life_share")

        async def send(_target, _lines):
            return False

        self.messenger.send_text = send
        self.messenger.take_sent_texts = lambda target: ["书签压好啦。"]
        await self.engine._deliver(outcome)
        self.assertEqual(outcome.delivered_messages[fixture.GROUP], ["书签压好啦。"])
        state = await self.engine.load_state(fixture.GROUP, cold_start=False)
        self.assertLess(self.engine._watermark(state, fixture.GROUP)[1], row["seq"])
