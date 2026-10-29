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

    async def test_default_private_queue_keeps_its_source_during_flush(self):
        private = "aiocqhttp:PrivateMessage:2002"
        self.store.add_session(private, session_type="private")
        self.add_group(sessions=[SESSION, private])
        self.engine.say_sink = AsyncMock(return_value=True)
        outcome = TickOutcome(SESSION, speech_home=private, speech_kind="reply")
        outcome.add_speech("私聊默认排队内容")
        await self.engine._flush_say(outcome)
        outcome.speech_home = SESSION
        await self.engine._deliver(outcome)
        self.engine.say_sink.assert_awaited_once_with(private, "私聊默认排队内容")
        self.assertEqual(outcome.live_origins, [private])

    async def test_explicit_group_queue_does_not_follow_a_later_private_step(self):
        private = "aiocqhttp:PrivateMessage:2002"
        self.store.add_session(private, session_type="private")
        self.add_group(sessions=[SESSION, private])
        self.engine.say_sink = AsyncMock(return_value=True)
        outcome = TickOutcome(SESSION, speech_kind="reply")
        outcome.add_speech("先排在原群的话", SESSION)
        outcome.speech_home = private
        await self.engine._flush_say(outcome)
        self.engine.say_sink.assert_awaited_once_with(SESSION, "先排在原群的话")
        self.assertEqual(outcome.home(), private)

    async def test_private_failed_flush_keeps_its_source_for_final_delivery(self):
        private = "aiocqhttp:PrivateMessage:2002"
        self.store.add_session(private, session_type="private")
        self.add_group(sessions=[SESSION, private])
        self.engine.say_sink = AsyncMock(return_value=False)
        outcome = TickOutcome(SESSION, speech_home=private, speech_kind="reply")
        outcome.add_speech("私聊延后送达内容")
        await self.engine._flush_say(outcome)
        outcome.speech_home = SESSION
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.sent, [(private, ["私聊延后送达内容"])])

    async def test_legacy_private_failed_flush_keeps_its_source_for_final_delivery(self):
        private = "aiocqhttp:PrivateMessage:2002"
        self.store.add_session(private, session_type="private")
        self.add_group(sessions=[SESSION, private])
        self.engine.say_sink = AsyncMock(return_value=False)
        outcome = TickOutcome(SESSION, speech_home=private, speech_kind="reply", messages=["旧缓冲的私聊内容"])
        await self.engine._flush_say(outcome)
        outcome.speech_home = SESSION
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.sent, [(private, ["旧缓冲的私聊内容"])])

    async def test_private_event_reminder_synchronizes_its_default_speech_home(self):
        private = "aiocqhttp:PrivateMessage:2002"
        self.store.add_session(private, session_type="private")
        self.add_group(sessions=[SESSION, private])
        raw = self.store.raw_world()
        for key in ("proactive_session_whitelist", "life_share_session_whitelist"):
            if private not in raw[key]:
                raw[key].append(private)
        self.store.save_world(raw)
        self.engine.reload_config()
        self.engine.resolve_send_to = lambda state, raw, **kwargs: private if raw == private else ""
        self.engine._compose_remind_line = AsyncMock(return_value="那我先自己找找。")
        outcome = TickOutcome(SESSION, speech_home=SESSION)
        async with self.engine.session_state(SESSION) as state:
            now = self.clock.now()
            state.event_threads.append({"id": "private-reminder", "session": private, "status": "open",
                "decision": {"send_to": private}, "root": {"title": "那本书"}})
            state.pending_help = {"thread_id": "private-reminder", "state": "active",
                                  "asked_at": now - 60, "active_until": now - 1, "title": "那本书"}
            await self.engine._advance_pending_help(state, self.engine.node(state.node_id), outcome, now)
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.sent, [(private, ["那我先自己找找。"])])

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
