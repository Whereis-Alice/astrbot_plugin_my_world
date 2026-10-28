"""Reading age informs the decision; original evidence and fresh drafts constrain delivery."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock

from tests import test_chat_browse as fixture

S = fixture.S


class BrowseFreshnessTests(unittest.IsolatedAsyncioTestCase):
    node_id = fixture.ChatBrowseTests.node_id
    asyncSetUp = fixture.ChatBrowseTests.asyncSetUp
    configure = fixture.ChatBrowseTests.configure
    note = fixture.ChatBrowseTests.note
    prepare = fixture.ChatBrowseTests.prepare
    complete = fixture.ChatBrowseTests.complete

    async def test_message_older_than_fifteen_minutes_can_be_replied_to_after_reading(self):
        action = await self.prepare(reply_enabled=True, message_id="read-original")
        self.clock.advance(20 * 60)
        state = await self.complete(action, decision='{"intent":"continue_chat","say":"刚看到你的花，开得真好。","reply_to":"m1"}')
        self.assertEqual(self.messenger.sent, [(S, ["刚看到你的花，开得真好。"])])
        self.assertTrue(state.thoughts[-1]["browse_replied"])
        self.assertEqual(state.thoughts[-1]["detail"]["speech"]["reply_to"], "read-original")
        scene = json.loads(self.llm.calls[-1]["prompt"].split("\n")[1])
        self.assertGreaterEqual(scene[0]["age_seconds"], 20 * 60)
        self.assertTrue(scene[0]["can_reply"])

    async def test_two_hour_old_message_is_available_when_it_was_just_read(self):
        self.configure(reply_enabled=True)
        async with self.engine.session_state(S) as state:
            state.state, state.current_action, state.current_plan, state.energy = "idle", None, None, .8
            row = self.note(state, "早些时候窗边的花开了", message_id="two-hours-old")
            row["at"] -= 2 * 3600
            self.assertTrue(self.engine.start_chat_browse(state, "browse_phone"))
            action = dict(state.current_action)
        await self.complete(action, decision='{"intent":"continue_chat","say":"刚翻到这张花的消息，很喜欢。","reply_to":"m1"}')
        self.assertEqual(self.messenger.sent, [(S, ["刚翻到这张花的消息，很喜欢。"])])

    async def test_later_unread_messages_are_context_and_cannot_take_over_reading_reply(self):
        action = await self.prepare(reply_enabled=True, message_id="read-original")
        async with self.engine.session_state(S) as state:
            self.note(state, "后来他们换到另一个话题", message_id="not-read")
        self.llm.replies = [json.dumps({"inner": "窗边的花让人心情平静。", "source_seqs": [action["browse_rows"][0]["seq"]]}),
            '{"intent":"join_chat","say":"这句没读过","reply_to":"m2"}',
            '{"intent":"join_chat","say":"仍然引用没读过的消息","reply_to":"m2"}']
        await self.engine._complete_chat_browse(S, action["id"])
        self.assertFalse(self.messenger.sent)
        scene = json.loads(self.llm.calls[1]["prompt"].split("\n")[1])
        self.assertTrue(scene[0]["can_reply"])
        self.assertNotIn("can_reply", scene[1])
        self.assertIn("后来他们换到另一个话题", self.llm.calls[1]["prompt"])

    async def test_crossing_message_age_boundary_during_short_generation_does_not_cancel_draft(self):
        action = await self.prepare(reply_enabled=True, message_id="read-original")
        self.clock.advance(880)
        self.engine._ask_judge = AsyncMock(return_value=json.dumps({
            "inner": "刚看到花开的消息，想接一句。", "source_seqs": [action["browse_rows"][0]["seq"]]}))
        self.llm.replies = ['{"intent":"continue_chat","say":"刚看到，花开得很好。","reply_to":"m1"}']
        generate = self.llm.generate

        async def short_generation(**options):
            self.clock.advance(30)
            return await generate(**options)

        self.llm.generate = short_generation
        await self.engine._complete_chat_browse(S, action["id"])
        self.assertEqual(self.messenger.sent, [(S, ["刚看到，花开得很好。"])])

    async def test_cited_old_reading_survives_many_later_context_rows(self):
        action = await self.prepare(reply_enabled=True, message_id="read-original")
        self.clock.advance(20 * 60)
        async with self.engine.session_state(S) as state:
            for index in range(45):
                self.note(state, f"后来大家谈的另一件事{index}", message_id=f"later-{index}")
        await self.complete(action, decision='{"intent":"continue_chat","say":"刚翻到那盆花，很喜欢。","reply_to":"m1"}')
        self.assertEqual(self.messenger.sent, [(S, ["刚翻到那盆花，很喜欢。"])])
        scene = json.loads(self.llm.calls[-1]["prompt"].split("\n")[1])
        self.assertTrue(scene[0]["can_reply"])
        self.assertIn("窗边的花", scene[0]["text"])
        self.assertLessEqual(len(scene), 40)
        self.assertTrue(any("另一件事" in row["text"] for row in scene))
        self.assertTrue(all("can_reply" not in row for row in scene[1:]))

    async def test_old_reading_can_wait_briefly_for_native_reply_without_age_expiry(self):
        action = await self.prepare(reply_enabled=True, message_id="read-original")
        self.clock.advance(20 * 60)
        release = asyncio.Event()
        owner = asyncio.create_task(release.wait())
        self.engine.register_incoming(S, "正在处理的正常回复", expected_owner=owner)
        try:
            await self.complete(action, decision='{"intent":"continue_chat","say":"刚看到花的消息，真好。","reply_to":"m1"}')
            self.assertEqual(len(self.llm.calls), 1)
            release.set()
            await owner
            tasks = list(getattr(self.engine, "_participation_settlers", {}).values())
            await asyncio.wait_for(asyncio.gather(*tasks), 7)
            self.assertEqual(self.messenger.sent, [(S, ["刚看到花的消息，真好。"])])
            self.assertEqual(len(self.llm.calls), 2)
        finally:
            release.set()
            owner.cancel()
            await asyncio.gather(owner, return_exceptions=True)

    async def test_model_can_leave_an_old_resolved_topic_alone(self):
        action = await self.prepare(reply_enabled=True, message_id="read-original")
        self.clock.advance(20 * 60)
        async with self.engine.session_state(S) as state:
            self.note(state, "已经处理好了，不用再讨论啦", message_id="resolved-later")
        state = await self.complete(action, decision='{"intent":"listen","motivation":"他们已经处理好了，留着自己的感想。"}')
        self.assertFalse(self.messenger.sent)
        self.assertEqual(len(self.llm.calls), 2)
        self.assertEqual(state.thoughts[-1]["detail"]["speech"]["intent"], "listen")
        self.assertIn("已经处理好了", self.llm.calls[-1]["prompt"])
