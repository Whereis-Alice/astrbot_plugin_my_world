"""Conversation opportunities, transport receipts and cross-module contact boundaries."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from core.config_store import ConfigStore
from core.db import AsyncDatabase
from core.engine import MyWorldEngine, MessageContext, TickOutcome
from core.json_actions import PlannedAction
from core.ports import LLMReply
from core.state import STATE_IDLE, STATE_SLEEPING
from tests.stub_ports import StubClock, StubLLM, StubMessenger, StubPersona
from core.participation import parse_participation
from core.speech_policy import reply_target
from core.engine.participation import ParticipationMixin

GROUP = "aiocqhttp:GroupMessage:1001"
OTHER = "aiocqhttp:GroupMessage:2002"
PRIVATE = "aiocqhttp:PrivateMessage:alice"
SAY = '{"inner":"山顶的风车让我有点期待。","actions":[{"type":"say","messages":["那个我也想试试。"]}]}'


class ParticipationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        store = ConfigStore(Path(folder.name))
        store.ensure_files()
        for sid, kind in ((GROUP, "group"), (OTHER, "group"), (PRIVATE, "private")):
            store.add_session(sid, session_type=kind, platform="aiocqhttp", cold_start_node="lobby")
        raw = store.raw_world()
        raw["proactive_session_whitelist"] = [GROUP, OTHER, PRIVATE]
        raw["life_share_session_whitelist"] = [GROUP, OTHER, PRIVATE]
        raw["sleep"]["mode"] = "fixed"
        raw["calendar"]["auto_generate"] = False
        raw["diary"]["automatic"] = False
        store.save_world(raw)
        self.db = AsyncDatabase(store.db_path)
        self.addAsyncCleanup(self.db.close)
        self.clock = StubClock()
        self.llm = StubLLM([SAY])
        self.messenger = StubMessenger()
        self.engine = MyWorldEngine(store=store, db=self.db, llm=self.llm,
            persona=StubPersona("你喜欢风车和旅行，说话简短。"),
            messenger=self.messenger, clock=self.clock)
        self.addAsyncCleanup(self.engine.close_background)
        self.engine.participation_sample = lambda *args: True
        async with self.engine.session_state(GROUP) as state:
            state.state = STATE_IDLE
            state.energy = .9
            state.current_action = None
            state.current_plan = None

    async def chat(self, text="想去看看风车", *, uid="alice", sid=GROUP, mine=False, age=0, **extra):
        async with self.engine.session_state(sid) as state:
            row = state.note_chat(user_id="__self__" if mine else uid, name=uid, text=text,
                now=self.clock.now() - age, origin=sid, is_self=mine, **extra)
            return dict(row)

    async def burst(self):
        await self.chat("周末想去山上看看风车", uid="alice")
        await self.chat("我也想去，山顶应该风很大", uid="bob")

    async def test_silence_trace_matches_each_opportunity_without_changing_config(self):
        await self.burst()
        state = await self.engine.load_state(GROUP, cold_start=False)
        for configured in (False, True):
            self.engine.world.decider.natural_silence = configured
            for source in ("message", "browse", "completion"):
                with self.subTest(configured=configured, source=source):
                    request = self.engine.context_request(state, GROUP, task="participation")
                    system, _, _ = self.engine.participation_prompt(
                        state, GROUP, "喜欢风车", source=source, request=request)
                    effective = configured or source in {"browse", "completion"}
                    self.assertIn("自然沉默：" + ("开启" if effective else "关闭"), system)
                    self.assertEqual(request.trace["natural_silence"], effective)
                    self.assertEqual(request.trace["silence_policy"], {
                        "source": source, "configured": configured, "effective": effective,
                    })
                    self.assertEqual(self.engine.world.decider.natural_silence, configured)

    async def test_recent_context_is_not_live_conversation(self):
        await self.chat("两个小时前的话题", age=7200)
        await self.chat("旧回复", uid="bob", age=7199)
        state = await self.engine.load_state(GROUP, cold_start=False)
        self.assertTrue(self.engine.chat_context(state))
        self.assertFalse(self.engine.group_is_chatting(state))
        self.assertFalse(self.engine.group_is_alive(state))
        self.assertIsNone(await self.engine.maybe_participate(GROUP))
        self.assertEqual(self.llm.calls, [])

    async def test_join_is_one_short_line_and_commits_exact_observed_boundary(self):
        await self.burst()
        outcome = await self.engine.maybe_participate(GROUP)
        self.assertIsNotNone(outcome)
        self.assertEqual(self.messenger.sent, [(GROUP, ["那个我也想试试。"])])
        state = await self.engine.load_state(GROUP, cold_start=False)
        self.assertEqual(state.last_interject_at, 0)
        self.assertFalse(state.awaiting_reply)
        self.assertEqual(self.engine.contact_status(GROUP)["left"], 6)
        self.assertEqual(state.participation[GROUP]["decision"], "joined")
        self.assertEqual(self.engine.live_chat(state), [])
        self.assertIn("请生成本轮接话", self.llm.calls[0]["prompt"])
        self.assertIn("自然沉默：关闭", self.llm.calls[0]["system_prompt"])
        self.assertIn("你喜欢风车", self.llm.calls[0]["system_prompt"])

    async def test_empty_actions_listens_without_retrying_the_same_burst(self):
        self.llm.replies = ['{"inner":"先听听他们聊到哪里。","actions":[]}']
        await self.burst()
        await self.engine.maybe_participate(GROUP)
        self.clock.advance(120)
        await self.engine.maybe_participate(GROUP)
        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual(self.messenger.sent, [])
        state = await self.engine.load_state(GROUP, cold_start=False)
        self.assertEqual(state.last_interject_at, 0)
        self.assertFalse(state.awaiting_reply)
        self.assertFalse(self.engine.group_is_chatting(state))

    async def test_natural_silence_can_be_enabled_without_changing_scene_boundaries(self):
        self.engine.world.decider.natural_silence = True
        self.llm.replies = ['{"inner":"这件事让我有点在意。","say":"","reply_to":""}']
        await self.burst()
        await self.engine.maybe_participate(GROUP)
        call = self.llm.calls[0]
        self.assertIn("自然沉默：开启", call["system_prompt"])
        self.assertIn("没话想说时可以继续听", call["system_prompt"])
        self.assertIn("没有人要求你必须回复", call["prompt"])
        self.assertIn("不主动介绍自己的日程", call["system_prompt"])
        self.assertEqual(self.messenger.sent, [])

    async def test_silence_policy_is_identical_in_evaluation_and_live_prompt(self):
        await self.burst()
        state = await self.engine.load_state(GROUP)
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                self.engine.world.decider.natural_silence = enabled
                request = self.engine.context_request(state, GROUP, task="participation", rows=state.recent_chat)
                system, prompt, _ = self.engine.participation_prompt(state, GROUP, "风车少女", request=request)
                self.assertIs(request.trace["natural_silence"], enabled)
                self.assertEqual("没有人要求你必须回复" in prompt, enabled)
                self.assertEqual("没话想说时可以继续听" in system, enabled)
                self.llm.replies = ['{"inner":"这件事让我有点在意。","say":"风吹起来一定很好看。","reply_to":""}']
                await self.engine.run_eval(GROUP, [{"user_text": "周末去看风车吧"}], variant="participation")
                self.assertIn("自然沉默：" + ("开启" if enabled else "关闭"), self.llm.calls[-1]["system_prompt"])
        self.assertEqual(self.messenger.sent, [])

    async def test_silence_markers_and_fenced_json_never_leak(self):
        for body in ("<NO_RESPONSE>", "<no_reply />", '```json\n{"actions":[]}\n```',
                     '{"actions":[{"type":"say","messages":["<NO_RESPONSE>"]}]}'):
            with self.subTest(body=body):
                self.llm.replies = [body]
                async with self.engine.session_state(GROUP) as state:
                    messages = await self.engine._generate_text_actions(state,
                        self.engine.node(state.node_id), "旁听", session_id=GROUP)
                outcome = TickOutcome(GROUP, messages=messages)
                await self.engine._deliver(outcome)
                self.assertEqual(self.messenger.sent, [])

    async def test_paused_resting_busy_and_pending_reply_make_no_model_call(self):
        await self.burst()
        for field, value in (("state", STATE_SLEEPING), ("energy", .1),
                             ("current_action", {"type": "read_book"})):
            with self.subTest(field=field):
                async with self.engine.session_state(GROUP) as state:
                    old = getattr(state, field)
                    setattr(state, field, value)
                self.assertIsNone(await self.engine.maybe_participate(GROUP))
                async with self.engine.session_state(GROUP) as state:
                    setattr(state, field, old)
        self.engine.register_incoming(GROUP, "@她 有个问题")
        self.assertIsNone(await self.engine.maybe_participate(GROUP))
        self.assertEqual(self.llm.calls, [])

    async def test_private_chat_does_not_start_an_ambient_worker(self):
        self.engine.queue_participation(PRIVATE)
        self.assertFalse(getattr(self.engine, "_background_jobs", {}))
        self.assertIsNone(await self.engine.maybe_participate(PRIVATE))

    async def test_turning_off_message_participation_stops_already_queued_work(self):
        self.engine.world.decider.settle_seconds = 0
        await self.burst()
        self.engine.queue_participation(GROUP)
        self.engine.world.decider.message_driven = False
        await self.engine._participation_settlers[self.engine.state_key(GROUP)]
        self.assertIsNone(await self.engine.maybe_participate(GROUP))
        self.assertEqual(self.llm.calls, [])
        self.assertEqual(self.engine.interject_gate(await self.engine.load_state(GROUP)), "message_driven_disabled")

    async def test_slow_generation_does_not_reply_to_an_expired_topic(self):
        await self.burst()
        original = self.llm.generate
        async def slow(**kwargs):
            self.clock.advance(181)
            return await original(**kwargs)
        self.llm.generate = slow
        await self.engine.maybe_participate(GROUP)
        self.assertEqual(self.messenger.sent, [])
        self.assertEqual(self.engine.contact_status(GROUP)["left"], 6)

    async def test_expired_contact_token_starts_a_new_counted_contact(self):
        first = TickOutcome(GROUP, speech_kind="outreach", messages=["我去找找那张照片"], contact_id="one-plan")
        await self.engine._deliver(first)
        self.clock.advance(121)
        second = TickOutcome(GROUP, speech_kind="outreach", messages=["找到了一张新照片"], contact_id="one-plan")
        await self.engine._deliver(second)
        self.assertEqual(len(self.messenger.sent), 2)
        self.assertEqual(self.engine.contact_status(GROUP)["left"], 4)

    async def test_old_plan_cannot_bypass_the_daily_limit(self):
        self.engine.world.profile.proactive_daily_max = 1
        first = TickOutcome(GROUP, speech_kind="outreach", messages=["先说一句"], contact_id="one-plan")
        await self.engine._deliver(first)
        self.clock.advance(121)
        first.add_speech("晚些又来一句")
        await self.engine._deliver(first)
        self.assertEqual(len(self.messenger.sent), 1)

    async def test_partial_native_style_flush_leaves_space_before_final_delivery(self):
        self.engine.say_sink = AsyncMock(return_value=True)
        outcome = TickOutcome(GROUP, messages=["我帮你看看"], speech_kind="reply")
        await self.engine._flush_say(outcome)
        self.assertTrue(self.engine.outreach_gate(GROUP))
        self.assertEqual(self.engine.contact_status(GROUP)["left"], 6)

    async def test_evaluation_reports_provider_failure_instead_of_silent_success(self):
        self.llm.generate = AsyncMock(return_value=LLMReply(ok=False,
            error="provider unavailable at https://model.invalid/chat key=abk_fake_test_credential"))
        before = (await self.engine.load_state(GROUP)).to_payload()
        result = await self.engine.run_eval(GROUP, [{"user_text": "甲：周末去哪玩？"}], variant="participation")
        self.assertTrue(result["results"][0]["error"])
        self.assertNotIn("abk_fake_test_credential", result["results"][0]["error"])
        self.assertNotIn("https://model.invalid", result["results"][0]["error"])
        self.assertEqual(result["results"][0]["reply"], [])
        self.assertEqual((await self.engine.load_state(GROUP)).to_payload(), before)

    async def test_message_burst_uses_one_settler_without_occupying_model_workers(self):
        self.engine.queue_participation(GROUP)
        self.engine.queue_participation(GROUP)
        self.assertEqual(len(self.engine._participation_settlers), 1)
        await asyncio.sleep(0)
        self.assertEqual(getattr(self.engine, "_background_running_jobs", 0), 0)
        await self.engine.close_background()
        self.assertEqual(self.engine._participation_settlers, {})

    async def test_evaluation_sanitizes_a_raised_provider_exception(self):
        self.llm.generate = AsyncMock(side_effect=RuntimeError(
            "request https://model.invalid/chat failed Authorization: Bearer fake_test_credential"))
        result = await self.engine.run_eval(GROUP, [{"user_text": "你好"}], variant="participation")
        error = result["results"][0]["error"]
        self.assertIn("RuntimeError", error)
        self.assertNotIn("https://model.invalid", error)
        self.assertNotIn("fake_test_credential", error)

    async def test_quiet_interruptible_activity_continues_while_she_briefly_joins(self):
        await self.burst()
        async with self.engine.session_state(GROUP) as state:
            state.current_action = {"type": "life_simulation", "duration_ticks": 30,
                                    "elapsed_ticks": 2, "intent": "慢慢看风车的图册"}
        await self.engine.maybe_participate(GROUP)
        state = await self.engine.load_state(GROUP, cold_start=False)
        self.assertEqual(state.current_action["type"], "life_simulation")
        self.assertEqual(state.current_action["elapsed_ticks"], 2)
        self.assertTrue(self.messenger.sent)

    async def test_failed_send_does_not_consume_quota_or_interjection_cooldown(self):
        self.messenger.send_text = AsyncMock(return_value=False)
        await self.burst()
        await self.engine.maybe_participate(GROUP)
        state = await self.engine.load_state(GROUP, cold_start=False)
        self.assertEqual(state.last_interject_at, 0)
        self.assertFalse(state.awaiting_reply)
        self.assertEqual(self.engine.contact_status(GROUP)["left"], 6)
        self.assertEqual(self.engine._watermark(state, GROUP), (0, 0))

    async def test_new_message_during_inference_discards_old_speech(self):
        await self.burst()
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.llm.generate
        async def delayed(**kwargs):
            entered.set()
            await release.wait()
            return await original(**kwargs)
        self.llm.generate = delayed
        job = asyncio.create_task(self.engine.maybe_participate(GROUP))
        await asyncio.wait_for(entered.wait(), 5)
        await self.chat("改主意了，周末下雨不去了")
        release.set()
        self.assertIsNone(await asyncio.wait_for(job, 5))
        self.assertEqual(self.messenger.sent, [])

    async def test_last_burst_during_inference_is_reconsidered_without_another_message(self):
        # Skip wall-clock delays; production bounds are validated by config.
        self.engine.world.decider.settle_seconds = 0
        self.engine.world.decider.evaluation_cooldown_seconds = 0
        self.llm.replies = [SAY, SAY]
        await self.burst()
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.llm.generate
        calls = 0
        async def delayed(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await release.wait()
            return await original(**kwargs)
        self.llm.generate = delayed
        self.engine.queue_participation(GROUP)
        await asyncio.wait_for(entered.wait(), 5)
        await self.chat("改主意了，下雨就去室内看模型", uid="bob")
        self.engine.queue_participation(GROUP)
        release.set()
        await asyncio.wait_for(self.engine._participation_settlers[self.engine.state_key(GROUP)], 5)
        self.assertEqual(calls, 2)
        self.assertEqual(len(self.messenger.sent), 1)
        self.assertIn("室内看模型", self.llm.calls[-1]["prompt"])

    async def test_one_message_can_join_even_when_all_old_contact_limits_are_exhausted(self):
        self.engine.world.profile.proactive_daily_max = 0
        self.engine.world.limits.max_autonomous_per_hour = 0
        self.engine.world.limits.max_llm_text_per_hour = 0
        async with self.engine.session_state(GROUP) as state:
            state.last_interject_at = self.clock.now()
            state.last_reply_at = self.clock.now()
            state.cooldown_until = self.clock.now() + 7200
        self.engine.note_visible_speech(GROUP)
        await self.chat("今天终于把风车修好了")
        await self.engine.maybe_participate(GROUP)
        self.assertEqual(len(self.messenger.sent), 1)

    async def test_new_topic_after_two_minutes_can_continue_without_twenty_minute_wait(self):
        self.llm.replies = [SAY, SAY]
        await self.burst()
        await self.engine.maybe_participate(GROUP)
        self.clock.advance(120)
        await self.chat("接下来打算在那边野餐")
        await self.engine.maybe_participate(GROUP)
        self.assertEqual(len(self.messenger.sent), 2)
        self.assertEqual(self.engine.contact_status(GROUP)["left"], 6)

    async def test_selected_earlier_message_is_quoted_without_leaking_other_session(self):
        await self.chat("群里的图纸", message_id="group-old")
        await self.chat("私密图纸", sid=PRIVATE, message_id="private-id")
        await self.chat("今天的天气", uid="bob", message_id="group-new")
        self.llm.replies = ['{"inner":"这件事让我有点在意。","say":"第一张图纸的左翼很有意思。","reply_to":"m1"}']
        captured = []
        async def send(target, lines):
            captured.append(reply_target(target))
            return True
        self.messenger.send_text = send
        outcome = await self.engine.maybe_participate(GROUP)
        self.assertEqual(captured, ["group-old"])
        self.assertEqual(outcome.reply_targets, {GROUP: "group-old"})
        self.assertNotIn("私密图纸", str(self.llm.calls))
        self.assertEqual(reply_target(GROUP), "")

    async def test_sampling_a_quiet_burst_does_not_retry_it(self):
        self.engine.participation_sample = lambda *args: False
        await self.burst()
        await self.engine.maybe_participate(GROUP, sample=True)
        await self.engine.maybe_participate(GROUP, sample=True)
        self.assertEqual(self.llm.calls, [])
        state = await self.engine.load_state(GROUP, cold_start=False)
        self.assertGreater(state.participation[GROUP]["checked_seq"], 0)

    async def test_disabling_live_participation_also_blocks_prepared_delivery(self):
        row = await self.chat()
        outcome = TickOutcome(GROUP, messages=["接一句"], speech_kind="participation")
        self.engine.claim_chat_scene(outcome, GROUP, [row])
        self.engine.world.decider.message_driven = False
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.sent, [])

    async def test_global_background_budget_still_bounds_lightweight_participation(self):
        self.engine.world.limits.background_daily_calls = 1
        self.llm.replies = [SAY, SAY]
        await self.chat()
        await self.engine.maybe_participate(GROUP)
        self.clock.advance(120)
        await self.chat("新话题")
        await self.engine.maybe_participate(GROUP)
        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual(len(self.messenger.sent), 1)

    async def test_evaluation_uses_the_lightweight_protocol_without_building_a_life_plan(self):
        self.llm.replies = ['{"inner":"这件事让我有点在意。","say":"那个风车我也想看看。","reply_to":""}']
        with patch.object(self.engine.prompts, "build_autonomous_system_prompt",
                          side_effect=AssertionError("旁听不需要完整规划提示词")):
            result = await self.engine.run_eval(GROUP,
                [{"user_text": "大家周末要去看风车吗？"}], variant="participation")
        row = result["results"][0]
        self.assertFalse(row["error"])
        self.assertEqual(row["reply"], ["那个风车我也想看看。"])
        self.assertIn("你喜欢风车和旅行", self.llm.calls[0]["system_prompt"])

    async def test_observation_opportunities_follow_degree_bursts_and_recent_own_speech(self):
        state = await self.engine.load_state(GROUP, cold_start=False)
        sample = ParticipationMixin.participation_sample
        with patch("core.engine.participation.random.random", return_value=.2):
            self.assertFalse(sample(self.engine, state, GROUP, [{}]))
            self.assertTrue(sample(self.engine, state, GROUP, [{}, {}]))
            self.engine.world.decider.participation_level = "quiet"
            self.assertFalse(sample(self.engine, state, GROUP, [{}, {}]))
            self.engine.world.decider.participation_level = "lively"
            self.assertTrue(sample(self.engine, state, GROUP, [{}]))
            self.engine.world.decider.participation_level = "natural"
            state.recent_chat = [{"origin": GROUP, "is_self": True, "at": self.clock.now()}] * 2
            self.assertFalse(sample(self.engine, state, GROUP, [{}, {}]))
            self.assertTrue(sample(self.engine, state, GROUP, [{"addressing": "me"}]))

    async def test_debug_echo_does_not_steal_selected_message_quote(self):
        row = await self.chat(message_id="older")
        outcome = TickOutcome(GROUP, messages=["接一句"], speech_kind="participation",
                              debug_messages=["调试说明"], debug_positions=[0])
        self.engine.claim_chat_scene(outcome, GROUP, [row])
        outcome.reply_targets[GROUP] = "older"
        quotes = []
        async def send(target, lines):
            quotes.extend(reply_target(target, i) for i in range(len(lines)))
            return True
        self.messenger.send_text = send
        await self.engine._deliver(outcome)
        self.assertEqual(quotes, ["", "older"])

    def test_compact_parser_never_speaks_empty_or_malformed_structured_decisions(self):
        for raw in ('{}', '{"say":null}', '{"say":[]}', '{"actions":[]}',
                    '{"say":"<NO_RESPONSE>"}', '{"actions":true}', '{"actions":5}',
                    '说明\n{"say":"没闭合', '```json\n{"say":', '<think>secret</think>{"say":""}'):
            with self.subTest(raw=raw):
                self.assertEqual(parse_participation(raw, {"m1": "real"}), ("", ""))
        self.assertEqual(parse_participation('{"say":"好呀","reply_to":"private-id"}',
            {"m1": "real"}), ("好呀", ""))

    async def test_native_agent_turn_blocks_all_autonomous_contact_until_sent(self):
        turn = {"session_id": GROUP, "started_at": self.clock.now()}
        self.engine.begin_native_turn(turn)
        self.assertTrue(self.engine.contact_status(GROUP)["reason"])
        self.engine.end_native_turn(turn)
        self.assertFalse(self.engine.contact_status(GROUP)["reason"])
        self.engine.begin_native_turn(turn)
        self.clock.advance(301)
        self.assertFalse(self.engine.contact_status(GROUP)["reason"])

    async def test_new_message_during_send_is_not_marked_answered(self):
        await self.burst()
        async def sending(target, messages):
            await self.chat("我还想带上相机", uid="bob")
            return True
        self.messenger.send_text = AsyncMock(side_effect=sending)
        await self.engine.maybe_participate(GROUP)
        state = await self.engine.load_state(GROUP, cold_start=False)
        self.assertEqual([r["text"] for r in self.engine.live_chat(state)], ["我还想带上相机"])

    async def test_event_scene_exposes_only_its_target_conversation(self):
        await self.chat("群里正在聊风车")
        await self.chat("私聊里的一件私事", sid=PRIVATE)
        state = await self.engine.load_state(GROUP, cold_start=False)
        hint = self.engine.conversation_scene_hint(state, GROUP)
        self.assertIn("群里正在聊风车", hint)
        self.assertNotIn("私聊里的一件私事", hint)

    async def test_schedule_event_and_native_reply_all_create_space_before_contact(self):
        for kind in ("schedule", "event", "reply"):
            with self.subTest(kind=kind):
                await self.engine._deliver(TickOutcome(GROUP, messages=["我回来啦"], speech_kind=kind))
                blocked = TickOutcome(GROUP, messages=["现在有人吗？"])
                await self.engine._deliver(blocked)
                self.assertTrue(self.engine.outreach_gate(GROUP))
                self.assertFalse(blocked.messages)
                self.clock.advance(91)
        await self.engine.record_self_reply(GROUP, "知道啦")
        self.assertTrue(self.engine.outreach_gate(GROUP))

    async def test_same_contact_can_flush_split_lines_but_new_module_cannot(self):
        outcome = TickOutcome(GROUP, speech_kind="outreach", messages=["一会儿见"])
        await self.engine._deliver(outcome)
        receipt, reason = self.engine._reserve_contact(outcome, GROUP, ["outreach"])
        self.assertFalse(reason)
        self.engine._finish_contact(receipt, False)
        other = TickOutcome(GROUP, speech_kind="outreach")
        receipt, reason = self.engine._reserve_contact(other, GROUP, ["outreach"])
        self.assertIsNone(receipt)
        self.assertTrue(reason)

    async def test_pending_send_blocks_another_contact_without_spending_quota(self):
        one, two = TickOutcome(GROUP, speech_kind="outreach"), TickOutcome(GROUP, speech_kind="outreach")
        receipt, reason = self.engine._reserve_contact(one, GROUP, ["outreach"])
        self.assertFalse(reason)
        blocked, reason = self.engine._reserve_contact(two, GROUP, ["outreach"])
        self.assertIsNone(blocked)
        self.assertTrue(reason)
        self.engine._finish_contact(receipt, False)
        self.assertEqual(self.engine.contact_status(GROUP)["left"], 6)

    async def test_unanswered_person_is_shared_between_group_and_private(self):
        sessions = self.engine.store.raw_sessions()
        sessions["groups"] = [{"id": "friends", "name": "朋友们", "sessions": [GROUP, PRIVATE],
                               "main_session": GROUP}]
        self.engine.store.save_sessions(sessions)
        self.engine.reload_config()
        one = TickOutcome(GROUP, speech_kind="outreach", messages=["Alice，风车的照片找到了"], contact_people={"alice"})
        await self.engine._deliver(one)
        self.clock.advance(100)
        self.assertTrue(self.engine.contact_status(PRIVATE, "alice")["reason"])
        self.assertFalse(self.engine.contact_status(PRIVATE, "bob")["reason"])
        self.clock.advance(6 * 3600)
        self.assertFalse(self.engine.contact_status(PRIVATE, "alice")["reason"])

    async def test_person_backoff_survives_reload_and_a_reply_elsewhere_clears_it(self):
        sessions = self.engine.store.raw_sessions()
        sessions["groups"] = [{"id": "friends", "name": "朋友们", "sessions": [GROUP, PRIVATE],
                               "main_session": GROUP}]
        self.engine.store.save_sessions(sessions)
        self.engine.reload_config()
        self.engine.note_visible_speech(GROUP, token="outgoing", people=["alice"])
        self.clock.advance(100)
        self.engine.reload_config()
        self.assertTrue(self.engine.contact_status(PRIVATE, "alice")["reason"])
        await self.engine.note_presence(MessageContext(PRIVATE, user_id="alice", text="照片看到了", is_private=True))
        self.assertFalse(self.engine.contact_status(PRIVATE, "alice")["reason"])

    async def test_only_direct_response_clears_waiting_person(self):
        self.engine.note_visible_speech(GROUP, token="outgoing", people=["alice"])
        self.clock.advance(100)
        await self.engine.note_presence(MessageContext(GROUP, user_id="alice", text="Bob你看这个", addressing="others"))
        self.assertTrue(self.engine.contact_status(GROUP, "alice")["reason"])
        await self.engine.note_presence(MessageContext(GROUP, user_id="alice", text="好的呀", is_mentioned=True))
        self.assertFalse(self.engine.contact_status(GROUP, "alice")["reason"])

    async def test_reply_to_another_person_does_not_become_direct_by_adjacency(self):
        await self.chat("风车呢", uid="alice")
        await self.chat("在山顶", mine=True)
        state = await self.engine.load_state(GROUP, cold_start=False)
        self.assertEqual(self.engine.reply_addressing(state,
            MessageContext(GROUP, user_id="bob", text="你今天去哪玩")), "interject")
        self.assertEqual(self.engine.reply_addressing(state,
            MessageContext(GROUP, user_id="alice", text="你也去吗", addressing="others")), "interject")

    async def test_old_own_message_or_another_session_cannot_claim_response(self):
        await self.chat("前天的话", mine=True, age=7200)
        state = await self.engine.load_state(GROUP, cold_start=False)
        self.assertEqual(self.engine.reply_addressing(state,
            MessageContext(GROUP, user_id="alice", text="你看这个")), "interject")
        self.assertEqual(self.engine.reply_addressing(state,
            MessageContext(OTHER, user_id="alice", text="你看这个")), "interject")

    async def test_schedule_silence_marker_and_new_chinese_commands_are_not_chat(self):
        await self.engine._deliver(TickOutcome(GROUP, messages=["<NO_RESPONSE>"], speech_kind="schedule"))
        self.assertEqual(self.messenger.sent, [])
        for command in ("/小世界 状态", "小世界 event 一件事", "/小世界"):
            self.assertTrue(self.engine.is_plugin_command(command))

    async def test_plan_creation_alone_does_not_mark_interjection_sent(self):
        async with self.engine.session_state(GROUP) as state:
            await self.engine._apply_plan(state, self.engine.node(state.node_id), TickOutcome(GROUP),
                {"steps": [{"action": "say", "interject": True}], "reason": "想接一句"})
            self.assertEqual(state.last_interject_at, 0)

    async def test_queued_interjection_does_not_reply_to_expired_topic(self):
        await self.chat("很早的话", age=7200)
        async with self.engine.session_state(GROUP) as state:
            outcome = TickOutcome(GROUP)
            await self.engine._start_action(state, self.engine.node(state.node_id), outcome,
                self.engine.world.action_map()["say"], PlannedAction(type="say", interject=True),
                autonomous=True, depth=0)
            self.assertEqual(outcome.messages, [])
        self.assertEqual(self.llm.calls, [])

    async def test_same_user_id_on_another_platform_does_not_clear_or_inherit_wait(self):
        telegram = "telegram:PrivateMessage:alice"
        self.engine.store.add_session(telegram, session_type="private", platform="telegram", cold_start_node="lobby")
        sessions = self.engine.store.raw_sessions()
        sessions["groups"] = [{"id": "friends", "name": "朋友们", "sessions": [GROUP, PRIVATE, telegram],
                               "main_session": GROUP}]
        self.engine.store.save_sessions(sessions)
        self.engine.reload_config()
        self.engine.note_visible_speech(GROUP, token="outgoing", people=["alice"])
        self.clock.advance(100)
        self.assertFalse(self.engine.contact_status(telegram, "alice")["reason"])
        await self.engine.note_presence(MessageContext(telegram, user_id="alice", text="你好", is_private=True))
        self.assertTrue(self.engine.contact_status(PRIVATE, "alice")["reason"])
        await self.engine.note_presence(MessageContext(PRIVATE, user_id="alice", text="照片看到了", is_private=True))
        self.assertFalse(self.engine.contact_status(PRIVATE, "alice")["reason"])

    async def test_legacy_person_wait_is_migrated_using_its_origin(self):
        self.db.raw.kv_set("outreach:" + self.engine.state_key(GROUP),
            {"people": {"alice": {"at": self.clock.now(), "origin": "telegram:GroupMessage:1001", "token": "old"}}})
        self.assertFalse(self.engine.contact_status(GROUP, "alice")["reason"])
        self.engine.note_visible_speech(GROUP, people=["bob"])
        ledger = self.db.raw.kv_get("outreach:" + self.engine.state_key(GROUP))
        self.assertIn("telegram:alice", ledger["people"])
        self.assertIn("aiocqhttp:bob", ledger["people"])

    async def test_rule_interjection_never_reroutes_a_group_topic_into_private(self):
        await self.burst()
        async with self.engine.session_state(GROUP) as state:
            self.engine.resolve_send_to = lambda *args, **kwargs: PRIVATE
            outcome = TickOutcome(GROUP)
            await self.engine._start_action(state, self.engine.node(state.node_id), outcome,
                self.engine.world.action_map()["say"], PlannedAction(type="say", interject=True, send_to=PRIVATE),
                autonomous=True, depth=0)
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.sent, [])
        self.assertEqual(len(self.llm.calls), 0)
        # Intentional sharing still supports another allowed conversation.
        async with self.engine.session_state(GROUP) as state:
            outcome = TickOutcome(GROUP)
            await self.engine._start_action(state, self.engine.node(state.node_id), outcome,
                self.engine.world.action_map()["share"], PlannedAction(type="share", messages=["找到风车的照片了"], send_to=PRIVATE),
                autonomous=True, depth=0)
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.sent, [(PRIVATE, ["找到风车的照片了"])])

    async def test_prepared_interjection_reconsiders_the_current_scene(self):
        raw = self.engine.store.raw_world()
        raw["life_share_session_whitelist"] = []
        self.engine.store.save_world(raw)
        self.engine.reload_config()
        await self.chat("下雨，周末不去山上了")
        self.llm.replies = ['{"inner":"先听听他们聊到哪里。","actions":[]}']
        async with self.engine.session_state(GROUP) as state:
            outcome = TickOutcome(GROUP)
            await self.engine._start_action(state, self.engine.node(state.node_id), outcome,
                self.engine.world.action_map()["say"],
                PlannedAction(type="say", interject=True, messages=["周末一起去山上吧"]),
                autonomous=True, depth=0)
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.sent, [])
        self.assertEqual(len(self.llm.calls), 1)
        self.assertIn("周末不去山上了", self.llm.calls[0]["prompt"])

    async def test_interjection_expiring_during_split_flush_drops_remaining_line(self):
        row = await self.chat()
        outcome = TickOutcome(GROUP, messages=["第一句", "第二句"], speech_kind="interject")
        self.engine.claim_chat_scene(outcome, GROUP, [row])
        async def slow_send(target, text):
            self.clock.advance(181)
            return True
        self.engine.say_sink = AsyncMock(side_effect=slow_send)
        await self.engine._flush_say(outcome)
        self.assertEqual(self.engine.say_sink.await_count, 1)
        self.assertEqual(outcome.live_messages, ["第一句"])
        self.assertEqual(self.engine.contact_status(GROUP)["left"], 6)

    async def test_final_delivery_drops_expired_scene_without_spending_quota(self):
        row = await self.chat()
        outcome = TickOutcome(GROUP, messages=["接一句"], speech_kind="interject")
        self.engine.claim_chat_scene(outcome, GROUP, [row])
        self.clock.advance(181)
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.sent, [])
        self.assertEqual(self.engine.contact_status(GROUP)["left"], 6)

    async def test_failed_reply_flush_does_not_start_visible_speech_cadence(self):
        self.engine.say_sink = AsyncMock(return_value=False)
        outcome = TickOutcome(GROUP, messages=["我帮你看看"], speech_kind="reply")
        await self.engine._flush_say(outcome)
        self.assertFalse(self.engine.outreach_gate(GROUP))
        self.assertEqual(outcome.messages, ["我帮你看看"])

    async def test_slow_rule_interjection_expires_before_delivery(self):
        await self.burst()
        original = self.llm.generate
        async def slow(**kwargs):
            self.clock.advance(181)
            return await original(**kwargs)
        self.llm.generate = slow
        async with self.engine.session_state(GROUP) as state:
            outcome = TickOutcome(GROUP)
            await self.engine._start_action(state, self.engine.node(state.node_id), outcome,
                self.engine.world.action_map()["share"], PlannedAction(type="share", interject=True),
                autonomous=True, depth=0)
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.sent, [])
        self.assertEqual(self.engine.contact_status(GROUP)["left"], 6)

    async def test_legacy_contact_continuation_is_counted_once_then_expires(self):
        outcome = TickOutcome(GROUP, speech_kind="outreach", contact_id="legacy-plan")
        token = "legacy-plan:" + GROUP
        self.db.raw.kv_set("proactive_contacts:" + self.engine.state_key(GROUP),
            {"day": self.engine._today_key(self.clock.now()), "contacts": {token: []}})
        self.engine.note_visible_speech(GROUP, token=token)
        self.clock.advance(91)
        receipt, reason = self.engine._reserve_contact(outcome, GROUP, ["outreach"])
        self.assertFalse(reason)
        self.engine._finish_contact(receipt, True)
        self.assertEqual(self.engine.contact_status(GROUP)["left"], 5)
        self.clock.advance(121)
        receipt, reason = self.engine._reserve_contact(outcome, GROUP, ["outreach"])
        self.assertFalse(reason)
        self.engine._finish_contact(receipt, True)
        self.assertEqual(self.engine.contact_status(GROUP)["left"], 4)

    async def test_new_person_in_split_contact_cannot_bypass_their_existing_wait(self):
        self.engine.note_visible_speech(GROUP, token="old-contact", people=["alice"])
        self.clock.advance(100)
        outcome = TickOutcome(GROUP, speech_kind="outreach", messages=["顺便分享给大家"])
        await self.engine._deliver(outcome)
        token = self.engine._contact_token(outcome, GROUP)
        self.assertFalse(self.engine.contact_status(GROUP, "bob", token=token)["reason"])
        self.assertTrue(self.engine.contact_status(GROUP, "alice", token=token)["reason"])
