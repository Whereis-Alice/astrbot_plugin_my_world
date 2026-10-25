"""Real dispatch ownership and context-aware autonomous opportunities."""
import asyncio
import inspect
import unittest
from types import SimpleNamespace

from tests import test_participation as participation
from tests.test_main_import import load_plugin_module

GROUP = participation.GROUP


class DispatchEvent:
    unified_msg_origin = GROUP
    is_at_or_wake_command = False
    call_llm = False
    stopped = False

    def get_message_str(self):
        return "周末去看风车"

    def get_extra(self, key=None, default=None):
        return default

    def is_wake_up(self):
        return True  # ALL/GROUP handlers match ordinary messages in AstrBot.

    def is_private_chat(self):
        return False

    def is_stopped(self):
        return self.stopped

    def get_self_id(self):
        return "bot"

    def get_sender_id(self):
        return "alice"


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = participation.ParticipationTests.asyncSetUp
    chat = participation.ParticipationTests.chat

    async def test_listener_wake_does_not_own_a_reply(self):
        module = load_plugin_module()
        plugin = SimpleNamespace(enabled=True, retired=False, engine=self.engine,
                                 in_self_initiated_call=lambda: False)
        plugin._should_expect_reply = module.ParticipationHooksMixin._should_expect_reply.__get__(plugin)
        event = DispatchEvent()
        self.assertFalse(plugin._should_expect_reply(event))
        for attr in ("is_at_or_wake_command", "call_llm"):
            setattr(event, attr, True)
            self.assertTrue(plugin._should_expect_reply(event))
            setattr(event, attr, False)
        event.is_private_chat = lambda: True
        self.assertTrue(plugin._should_expect_reply(event))

    async def test_helper_explicit_block_cannot_start_an_ambient_worker(self):
        module = load_plugin_module()
        plugin = SimpleNamespace(enabled=True, retired=False, engine=self.engine,
                                 in_self_initiated_call=lambda: False)
        plugin._should_expect_reply = module.ParticipationHooksMixin._should_expect_reply.__get__(plugin)
        event = DispatchEvent()
        event.get_extra = lambda key=None, default=None: True if key == "helper_tools_wake_block_llm_request" else default
        self.assertFalse(plugin._should_expect_reply(event))
        module.ParticipationHooksMixin._queue_ambient_participation(plugin, event)
        self.assertFalse(getattr(self.engine, "_participation_settlers", {}))

    async def test_finished_dispatch_releases_a_live_event_registration(self):
        event, release = DispatchEvent(), asyncio.Event()
        entered = asyncio.Event()

        async def dispatch():
            kwargs = {"expected_owner": asyncio.current_task()} if "expected_owner" in inspect.signature(self.engine.register_incoming).parameters else {}
            self.engine.register_incoming(GROUP, "真实唤醒词请求", expected_event=event,
                                          **kwargs)
            entered.set()
            await release.wait()

        task = asyncio.create_task(dispatch())
        self.addAsyncCleanup(self.engine.close_background)
        try:
            await asyncio.wait_for(entered.wait(), 2)
            self.assertTrue(self.engine.pending_reply_busy(GROUP))
            release.set()
            await task
            await asyncio.sleep(0)
            self.assertFalse(self.engine.pending_reply_busy(GROUP))
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_completion_chat_kind_has_a_valid_continuation_contract(self):
        await self.chat("那张图纸还能看看吗", message_id="question")
        outcome = participation.TickOutcome(GROUP, speech_kind="browse_reply")
        async with self.engine.session_state(GROUP) as state:
            allowed, _, _ = self.engine.expression_options(state, GROUP, "browse_reply")
            self.assertIn("continue_chat", allowed)
            kind = await self.engine.note_expression(state, outcome,
                '{"inner":"他还想看那张图纸。","intent":"join_chat","reply_to":"c1"}',
                ["好，我翻给你。"], target=GROUP, trigger="completion")
            self.assertEqual(kind, "continuation")
            self.assertEqual(outcome.reply_targets[GROUP], "question")

    async def test_owned_dispatch_does_not_expire_while_it_is_still_running(self):
        event, release = DispatchEvent(), asyncio.Event()
        entered = asyncio.Event()

        async def dispatch():
            record = self.engine.register_incoming(GROUP, "慢回复", expected_event=event,
                expected_owner=asyncio.current_task())
            record["at"] -= 3600
            entered.set()
            await release.wait()

        task = asyncio.create_task(dispatch())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            self.assertTrue(self.engine.pending_reply_busy(GROUP))
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.assertFalse(self.engine.pending_reply_busy(GROUP))
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_busy_scene_is_revisited_without_another_message(self):
        self.engine.world.decider.settle_seconds = 0
        await self.chat("周末的风车照片", message_id="fresh")
        release = asyncio.Event()
        owner = asyncio.create_task(release.wait())
        event = DispatchEvent()
        self.engine.register_incoming(GROUP, "直接回复", expected_event=event, expected_owner=owner)
        self.engine.queue_participation(GROUP)
        await asyncio.sleep(.1)
        self.assertEqual(self.llm.calls, [])
        release.set()
        await owner
        self.engine._pending_replies.clear()
        await asyncio.wait_for(self.engine._participation_settlers[self.engine.state_key(GROUP)], 7)
        self.assertEqual(len(self.llm.calls), 1)

    async def test_two_sessions_in_one_life_keep_both_bursts(self):
        sessions = self.engine.store.raw_sessions()
        sessions["groups"] = [{"id": "shared", "name": "同一个生活", "sessions": [GROUP, participation.OTHER], "main_session": GROUP}]
        self.engine.store.save_sessions(sessions)
        self.engine.reload_config()
        self.engine.world.decider.settle_seconds = 0
        self.llm.replies = [participation.SAY, participation.SAY]
        await self.chat("群一的风车", sid=GROUP)
        await self.chat("群二的纸飞机", sid=participation.OTHER)
        self.engine.queue_participation(GROUP)
        self.engine.queue_participation(participation.OTHER)
        await asyncio.wait_for(self.engine._participation_settlers[self.engine.state_key(GROUP)], 5)
        self.assertEqual({target for target, _ in self.messenger.sent}, {GROUP, participation.OTHER})

    async def test_relationship_attention_stays_in_the_actual_session(self):
        from core.engine.participation import ParticipationMixin
        await self.chat("风车已经修好了", message_id="attention")
        self.engine.profiles.adjust_affinity(participation.OTHER, "alice", 100, now=self.clock.now())
        async with self.engine.session_state(GROUP) as state:
            new = self.engine.live_chat(state, GROUP)
            state.participation[GROUP] = {"evaluated_at": self.clock.now()}
            ParticipationMixin.participation_sample(self.engine, state, GROUP, new, "你喜欢风车")
            self.assertEqual(state.participation[GROUP]["opportunity"]["relationship"], 0)
            self.engine.profiles.adjust_affinity(GROUP, "alice", 100, now=self.clock.now())
            ParticipationMixin.participation_sample(self.engine, state, GROUP, new, "你喜欢风车")
            self.assertGreater(state.participation[GROUP]["opportunity"]["relationship"], 0)

    async def test_real_opportunity_entry_prioritizes_affinity_but_can_still_listen(self):
        del self.engine.participation_sample
        self.llm.replies = ['{"inner":"先看看他们想去哪里。","intent":"listen"}'] * 2
        await self.chat("周末去看风车", message_id="first")
        await self.engine.maybe_participate(GROUP, sample=True)
        self.clock.advance(135)
        await self.chat("这张风车图纸也不错", message_id="second")
        await self.engine.maybe_participate(GROUP, sample=True)
        self.assertEqual(len(self.llm.calls), 1)
        state = await self.engine.load_state(GROUP, cold_start=False)
        self.assertEqual(state.participation[GROUP]["decision"], "deferred")
        ordinary_due = state.participation[GROUP]["opportunity"]["due_at"]
        self.engine.profiles.adjust_affinity(GROUP, "alice", 100, now=self.clock.now())
        await self.engine.maybe_participate(GROUP, sample=True)
        state = await self.engine.load_state(GROUP, cold_start=False)
        close_due = state.participation[GROUP]["opportunity"]["due_at"]
        self.assertLess(close_due, ordinary_due)
        self.clock.advance((close_due + ordinary_due) / 2 - self.clock.now())
        await self.engine.maybe_participate(GROUP, sample=True)
        self.assertEqual(len(self.llm.calls), 2)
        self.assertEqual(self.messenger.sent, [])
        state = await self.engine.load_state(GROUP, cold_start=False)
        self.assertEqual(state.participation[GROUP]["decision"], "listening")
        self.assertEqual(state.thoughts[-1]["detail"]["speech"]["intent"], "listen")

    async def test_busy_expired_scene_is_not_sent_after_release(self):
        self.engine.world.decider.settle_seconds = 0
        await self.chat("旧现场", message_id="expiring")
        event, release = DispatchEvent(), asyncio.Event()
        owner = asyncio.create_task(release.wait())
        self.engine.register_incoming(GROUP, "直接回复", expected_event=event, expected_owner=owner)
        self.engine.queue_participation(GROUP)
        settler = self.engine._participation_settlers[self.engine.state_key(GROUP)]
        await asyncio.sleep(.1)
        self.clock.advance(181)
        release.set()
        await owner
        await asyncio.wait_for(settler, 7)
        self.assertEqual(self.llm.calls, [])
        state = await self.engine.load_state(GROUP, cold_start=False)
        self.assertEqual(state.participation[GROUP]["decision"], "idle")

    async def test_reply_that_starts_while_waiting_for_a_worker_keeps_the_scene(self):
        await self.queued_reply_scene(expires=False)

    async def test_scene_that_expires_after_worker_admission_is_not_retried(self):
        await self.queued_reply_scene(expires=True)

    async def queued_reply_scene(self, *, expires):
        self.engine.world.decider.settle_seconds = 0
        await self.chat("周末一起去看风车", message_id="worker-wait")
        blocked, release_worker, release_reply = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def occupy_worker():
            blocked.set()
            await release_worker.wait()

        blocker = self.engine.queue_background("test_busy_worker", occupy_worker)
        owner = asyncio.create_task(release_reply.wait())
        try:
            await asyncio.wait_for(blocked.wait(), 2)
            self.engine.queue_participation(GROUP)
            key = self.engine.state_key(GROUP)
            settler = self.engine._participation_settlers[key]
            async with asyncio.timeout(3):
                while "participation:" + key not in self.engine._background_jobs:
                    await asyncio.sleep(.01)
            worker = self.engine._background_jobs["participation:" + key]
            event = DispatchEvent()
            self.engine.register_incoming(GROUP, "直接回复", expected_event=event, expected_owner=owner)
            release_worker.set()
            await asyncio.wait_for(blocker, 3)
            await asyncio.wait_for(asyncio.shield(worker), 3)
            await asyncio.sleep(.1)  # allow the collector to handle the unadmitted scene
            self.assertEqual(self.llm.calls, [])
            state = await self.engine.load_state(GROUP, cold_start=False)
            self.assertEqual(state.participation[GROUP]["decision"], "waiting")
            self.assertFalse(state.participation[GROUP].get("checked_seq"))
            self.assertFalse(state.participation[GROUP].get("attempts"))
            if expires:
                self.clock.advance(181)
            release_reply.set()
            await owner
            await asyncio.wait_for(asyncio.shield(settler), 7)
            self.assertEqual(len(self.llm.calls), 0 if expires else 1)
            if expires:
                state = await self.engine.load_state(GROUP, cold_start=False)
                self.assertEqual(state.participation[GROUP]["decision"], "idle")
        finally:
            release_worker.set()
            release_reply.set()
            owner.cancel()
            await asyncio.gather(owner, return_exceptions=True)

    async def test_short_statement_reaches_real_judgement_and_can_still_listen(self):
        del self.engine.participation_sample
        self.llm.replies = ['{"inner":"他说累了，先听听有没有后话。","intent":"listen"}']
        await self.chat("累", message_id="short-statement")
        await self.engine.maybe_participate(GROUP, sample=True)
        self.assertEqual(len(self.llm.calls), 1)
        self.assertRegex(self.llm.calls[0]["prompt"], r'"text"\s*:\s*"累"')
        state = await self.engine.load_state(GROUP, cold_start=False)
        self.assertEqual(state.participation[GROUP]["decision"], "listening")
        self.assertEqual(state.thoughts[-1]["content"], "他说累了，先听听有没有后话。")
        self.assertEqual(self.messenger.sent, [])


class OpportunityTests(unittest.TestCase):
    def choice(self, **overrides):
        from core.participation_opportunity import assess_opportunity
        kwargs = dict(now=1000., rows=[], new=[{"text": "周末去看风车", "user_id": "alice"}],
                      observation={"evaluated_at": 900.}, attempts=[], persona="你喜欢风车和旅行",
                      affinities={})
        kwargs.update(overrides)
        return assess_opportunity(**kwargs)

    def test_close_relevant_author_gets_earlier_attention_without_forcing_speech(self):
        ordinary = self.choice()
        close = self.choice(affinities={"alice": 1.})
        self.assertLess(close.due_at, ordinary.due_at)
        self.assertFalse(close.ready)
        self.assertEqual(close.due_at, self.choice(affinities={"alice": 1.}, relation_weight=1).due_at)
        self.assertEqual(ordinary.due_at, self.choice(affinities={"alice": 1.}, relation_weight=0).due_at)
        self.assertEqual(ordinary.due_at, self.choice(affinities={"alice": -1.}).due_at)

    def test_affinity_alone_cannot_prioritize_an_unrelated_topic(self):
        new = [{"text": "打印机买来了", "user_id": "alice"}]
        self.assertEqual(self.choice(new=new).due_at, self.choice(new=new, affinities={"alice": 1}).due_at)

    def test_split_receipts_count_as_one_turn_even_with_interleaved_chat(self):
        from core.participation_opportunity import visible_turns
        rows = [{"is_self": True, "at": 995, "text": "第一句", "speech_turn": "turn-a"},
                {"is_self": False, "at": 996, "text": "哦"},
                {"is_self": True, "at": 997, "text": "第二句", "speech_turn": "turn-a"},
                {"is_self": True, "at": 998, "text": "下一轮", "speech_turn": "turn-b"}]
        self.assertEqual(len(visible_turns(rows, 1000)), 2)
        one = self.choice(rows=rows[:1], new=[{"text": "打印机买来了"}])
        split = self.choice(rows=rows[:3], new=[{"text": "打印机买来了"}])
        self.assertEqual(one.due_at, split.due_at)

    def test_continuation_has_priority_but_keeps_hourly_budget(self):
        new = [{"text": "还有个细节", "addressing": "me"}]
        self.assertTrue(self.choice(new=new).ready)
        full = self.choice(new=new, attempts=[950.] * 12)
        self.assertFalse(full.ready)
        self.assertEqual(full.due_at, 4550.)

    def test_acknowledgement_does_not_turn_into_a_delayed_reply_obligation(self):
        self.assertFalse(self.choice(new=[{"text": "嗯嗯"}], observation={}).ready)
        self.assertTrue(self.choice(new=[{"text": "", "has_images": True}], observation={}).ready)

    def test_short_statements_and_emoji_are_not_filtered_by_search_tokenization(self):
        for text in ("累", "饿了", "怕", "🤕", "？"):
            with self.subTest(text=text):
                self.assertTrue(self.choice(new=[{"text": text}], observation={}).ready)
        for text in ("", "   ", "嗯嗯", "好的", "哈哈", "ok"):
            with self.subTest(text=text):
                self.assertFalse(self.choice(new=[{"text": text}], observation={}).ready)


if __name__ == "__main__":
    unittest.main()
