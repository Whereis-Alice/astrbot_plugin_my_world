"""A reading is a new opportunity; an earlier observation is not a reply."""
import asyncio
import copy
import unittest

from core.ports import LLMReply
from tests import test_chat_browse as fixture

S = fixture.S


class BrowseObservationBoundaryTests(unittest.IsolatedAsyncioTestCase):
    node_id = fixture.ChatBrowseTests.node_id
    asyncSetUp = fixture.ChatBrowseTests.asyncSetUp
    configure = fixture.ChatBrowseTests.configure
    note = fixture.ChatBrowseTests.note
    prepare = fixture.ChatBrowseTests.prepare
    complete = fixture.ChatBrowseTests.complete

    async def observed_reading(self):
        self.configure(reply_enabled=True)
        async with self.engine.session_state(S) as state:
            state.state, state.current_action, state.current_plan, state.energy = "idle", None, None, .8
            self.note(state, "窗边的花开了，今天终于赶上花期", message_id="read-flower")
        self.llm.replies = ['{"inner":"先看看他们怎么说。","intent":"listen"}']
        await self.engine.maybe_participate(S)
        async with self.engine.session_state(S) as state:
            self.assertEqual(state.participation[S]["decision"], "listening")
            self.assertEqual(state.chat_replied_seq, 0)
            self.assertTrue(self.engine.start_chat_browse(state, "browse_phone"))
            return copy.deepcopy(state.current_action)

    async def test_reading_after_real_listening_can_choose_to_reply(self):
        action = await self.observed_reading()
        state = await self.complete(action, decision=(
            '{"intent":"continue_chat","say":"刚翻到这盆花，花期赶得真好。","reply_to":"m1"}'))
        self.assertEqual(self.messenger.sent, [(S, ["刚翻到这盆花，花期赶得真好。"])])
        thought = next(row for row in state.thoughts if row.get("browse_id") == action["id"])
        self.assertTrue(thought["browse_replied"])
        self.assertEqual(len(self.llm.calls), 3)  # Observe, read, independently decide after reading.

    async def test_reading_keeps_message_observation_watermark_and_cadence(self):
        action = await self.prepare(reply_enabled=True, message_id="read-flower")
        async with self.engine.session_state(S) as state:
            state.participation[S] = {"checked_seq": 0}
        state = await self.complete(action, decision='{"intent":"listen","motivation":"留着这个想法。"}')
        self.assertEqual(state.participation[S]["checked_seq"], 0)
        self.assertEqual(state.participation[S]["evaluated_at"], self.clock.now())
        self.assertFalse(self.messenger.sent)

    async def test_waiting_reading_survives_an_independent_observation_watermark(self):
        action = await self.prepare(reply_enabled=True, message_id="read-flower")
        release = asyncio.Event()
        owner = asyncio.create_task(release.wait())
        self.engine.register_incoming(S, "另一条直接回复", expected_owner=owner)
        try:
            await self.complete(action, decision=(
                '{"intent":"continue_chat","say":"刚看到花开了，真好。","reply_to":"m1"}'))
            async with self.engine.session_state(S) as state:
                state.participation.setdefault(S, {})["checked_seq"] = state.chat_seq
            release.set()
            await owner
            tasks = list(getattr(self.engine, "_participation_settlers", {}).values())
            await asyncio.wait_for(asyncio.gather(*tasks), 7)
            self.assertEqual(self.messenger.sent, [(S, ["刚看到花开了，真好。"])])
            self.assertEqual(len(self.llm.calls), 2)
        finally:
            release.set()
            owner.cancel()
            await asyncio.gather(owner, return_exceptions=True)

    async def test_same_reading_listening_is_not_evaluated_twice(self):
        action = await self.prepare(reply_enabled=True, message_id="read-flower")
        await self.complete(action, decision='{"intent":"listen","motivation":"只想留着这点感受。"}')
        await self.engine.maybe_participate(S, source="browse", browse_id=action["id"])
        self.assertEqual(len(self.llm.calls), 2)
        self.assertFalse(self.messenger.sent)

    async def test_same_failed_reading_decision_is_not_replayed(self):
        action = await self.prepare(reply_enabled=True, message_id="read-flower")
        generate = self.llm.generate

        async def fail_speech(**options):
            if self.llm.calls:
                self.llm.calls.append(options)
                return LLMReply(ok=False, error="temporary speech failure")
            return await generate(**options)

        self.llm.generate = fail_speech
        state = await self.complete(action)
        failure = copy.deepcopy(state.thoughts[-1]["detail"]["browse_reply"])
        await self.engine.maybe_participate(S, source="browse", browse_id=action["id"])
        self.assertEqual(len(self.llm.calls), 2)
        self.assertFalse(self.messenger.sent)
        state = await self.engine.load_state(S)
        self.assertEqual(state.thoughts[-1]["detail"]["browse_reply"], failure)

    async def test_duplicate_while_inference_runs_does_not_cancel_original(self):
        action = await self.prepare(reply_enabled=False, message_id="read-flower")
        await self.complete(action)
        self.engine.world.chat_browse.reply_enabled = True
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_speech(**options):
            self.llm.calls.append(options)
            entered.set()
            await release.wait()
            return LLMReply(ok=True, text='{"intent":"continue_chat","say":"刚看到这盆花，真好。","reply_to":"m1"}')

        self.llm.generate = slow_speech
        first = asyncio.create_task(self.engine.maybe_participate(S, source="browse", browse_id=action["id"]))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            self.assertIsNone(await self.engine.maybe_participate(S, source="browse", browse_id=action["id"]))
            self.assertEqual(len(self.llm.calls), 2)
            release.set()
            await first
            self.assertEqual(self.messenger.sent, [(S, ["刚看到这盆花，真好。"])])
        finally:
            release.set()
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)

    async def test_cancelled_inference_does_not_replay_same_reading(self):
        action = await self.prepare(reply_enabled=False, message_id="read-flower")
        await self.complete(action)
        self.engine.world.chat_browse.reply_enabled = True
        entered = asyncio.Event()

        async def wait_for_cancellation(**options):
            self.llm.calls.append(options)
            entered.set()
            await asyncio.Event().wait()

        self.llm.generate = wait_for_cancellation
        task = asyncio.create_task(self.engine.maybe_participate(S, source="browse", browse_id=action["id"]))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertIsNone(await self.engine.maybe_participate(S, source="browse", browse_id=action["id"]))
        self.assertEqual(len(self.llm.calls), 2)
        self.assertFalse(self.messenger.sent)

    async def test_real_reply_still_consumes_the_reading_scene(self):
        action = await self.observed_reading()
        async with self.engine.session_state(S) as state:
            self.engine.mark_chat_replied(state, S)
        state = await self.complete(action)
        thought = next(row for row in state.thoughts if row.get("browse_id") == action["id"])
        self.assertEqual(thought["detail"]["browse_reply"]["status"], "no_scene")
        self.assertEqual(len(self.llm.calls), 2)
        self.assertFalse(self.messenger.sent)
