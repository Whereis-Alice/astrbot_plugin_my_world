"""The character remembers accepted sends and live speech, never rejected plans."""

import unittest
from unittest.mock import AsyncMock

from core.engine import TickOutcome
from tests import test_engine as fixture

SESSION = fixture.SESSION


class DeliveryIntegrityTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp
    add_group = fixture.EngineTestCase.add_group

    async def test_failed_send_does_not_enter_chat_or_apply_speech_cooldown(self):
        before = await self.engine.load_state(SESSION, cold_start=False)
        self.messenger.send_text = AsyncMock(return_value=False)
        outcome = TickOutcome(session_id=SESSION, messages=["没有送达的这句话"])
        await self.engine._deliver(outcome)
        state = await self.engine.load_state(SESSION, cold_start=False)
        self.assertFalse(any(item.get("text") == outcome.messages[0] for item in state.recent_chat))
        self.assertEqual(state.last_engagement_time, before.last_engagement_time)
        self.assertEqual(state.awaiting_reply, before.awaiting_reply)
        self.assertEqual(state.unanswered_count, before.unanswered_count)
        events = await self.db.call("query_events", session_id=SESSION, event_type="bot_message")
        self.assertEqual(events, [])

    async def test_live_speech_survives_a_later_failed_send(self):
        self.messenger.send_text = AsyncMock(return_value=False)
        outcome = TickOutcome(session_id=SESSION, messages=["后半句没有送达"], live_messages=["先说出口了"])
        await self.engine._deliver(outcome)
        state = await self.engine.load_state(SESSION, cold_start=False)
        texts = [item["text"] for item in state.recent_chat if item.get("is_self")]
        self.assertIn("先说出口了", texts)
        self.assertNotIn("后半句没有送达", texts)

    async def test_routed_failure_does_not_create_a_reply_in_the_wrong_conversation(self):
        private = "aiocqhttp:PrivateMessage:2002"
        self.store.add_session(private, session_type="private")
        self.add_group(sessions=[SESSION, private])
        self.messenger.send_text = AsyncMock(side_effect=lambda target, texts: target == SESSION)
        outcome = TickOutcome(session_id=SESSION, messages=["群聊送达"], routed={private: ["私聊未送达"]}, speech_kind="reply")
        await self.engine._deliver(outcome)
        state = await self.engine.load_state(SESSION, cold_start=False)
        self.assertTrue(any(item.get("text") == "群聊送达" for item in state.recent_chat))
        self.assertFalse(any(item.get("text") == "私聊未送达" for item in state.recent_chat))

    async def test_partial_segment_receipt_only_records_the_accepted_segment(self):
        self.messenger.send_text = AsyncMock(return_value=True)
        self.messenger.take_sent_texts = lambda _target: ["第一句已经送达"]
        outcome = TickOutcome(session_id=SESSION, messages=["第一句已经送达", "第二句没有送达"])
        await self.engine._deliver(outcome)
        state = await self.engine.load_state(SESSION, cold_start=False)
        texts = [item["text"] for item in state.recent_chat if item.get("is_self")]
        self.assertIn("第一句已经送达", texts)
        self.assertNotIn("第二句没有送达", texts)

    async def test_live_speech_keeps_the_origin_before_a_plan_restores_its_home(self):
        private = "aiocqhttp:PrivateMessage:2002"
        self.store.add_session(private, session_type="private")
        self.add_group(sessions=[SESSION, private])
        self.engine.say_sink = AsyncMock(return_value=True)
        outcome = TickOutcome(session_id=SESSION, speech_home=private, messages=["私下先说一句"], speech_kind="reply")
        await self.engine._flush_say(outcome)
        outcome.speech_home = SESSION
        await self.engine._deliver(outcome)
        state = await self.engine.load_state(SESSION, cold_start=False)
        said = [item for item in state.recent_chat if item.get("text") == "私下先说一句"]
        self.assertEqual(len(said), 1)
        self.assertEqual(said[0]["origin"], private)

    async def test_partial_reply_success_does_not_count_an_unsent_autonomous_segment(self):
        self.messenger.send_text = AsyncMock(return_value=True)
        self.messenger.take_sent_texts = lambda _target: ["已回复对方"]
        outcome = TickOutcome(session_id=SESSION, messages=["已回复对方", "主动继续搭话"],
                              speech_kinds={"": ["reply", "proactive"]})
        before = await self.engine.load_state(SESSION, cold_start=False)
        await self.engine._deliver(outcome)
        state = await self.engine.load_state(SESSION, cold_start=False)
        self.assertEqual(state.awaiting_reply, before.awaiting_reply)


if __name__ == "__main__":
    unittest.main()
