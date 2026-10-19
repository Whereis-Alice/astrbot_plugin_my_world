"""Quiet reading: bounded evidence, isolated feelings, no sends/tools or replay."""
import asyncio
import copy
import json
import unittest
from unittest.mock import AsyncMock, Mock

from core.chat_browse import feeling_context, reading_view
from core.json_actions import PlannedAction
from core.models import ActionDef, ChatBrowseConfig
from core.ports import LLMReply
from core.state import WorldState
from tests import test_engine as fixture

S, G, P = fixture.SESSION, fixture.OTHER_SESSION, fixture.PRIVATE_SESSION


class ChatBrowseTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp
    add_group = fixture.EngineTestCase.add_group

    def configure(self, **values):
        raw = self.store.raw_world()
        raw["chat_browse"] = {"enabled": True, "phone": True, **values}
        self.store.save_world(raw)
        self.engine.reload_config()

    def note(self, state, text="大家聊起了窗边的花", origin=S, **kwargs):
        return state.note_chat(user_id="42", name="小明", text=text,
            now=self.clock.now(), origin=origin, keep=100, **kwargs)

    async def prepare(self, source=S):
        self.configure()
        async with self.engine.session_state(S) as state:
            state.state, state.current_action, state.current_plan = "idle", None, None
            state.energy = .8
            self.note(state, origin=source)
            self.assertTrue(self.engine.start_chat_browse(state, "browse_phone"))
            return copy.deepcopy(state.current_action)

    async def complete(self, action, **payload):
        self.llm.replies = [json.dumps({"inner": "也想在自己的窗边留一小盆花。", "source_seqs": [action["browse_rows"][0]["seq"]],
            "meaningful": False, "valence_delta": .01, **payload}, ensure_ascii=False)]
        await self.engine._complete_chat_browse(S, action["id"])
        return await self.engine.load_state(S)

    async def test_default_off_missing_device_and_empty_window_cost_nothing(self):
        async with self.engine.session_state(S) as state:
            self.note(state)
            self.assertFalse(self.engine.start_chat_browse(state, "browse_phone"))
        self.configure(phone=False)
        async with self.engine.session_state(S) as state:
            self.assertFalse(self.engine.start_chat_browse(state, "browse_phone"))
            self.assertFalse(self.engine.start_chat_browse(state, "browse_computer"))
        self.configure()
        async with self.engine.session_state(S) as state:
            state.recent_chat = []
            self.assertFalse(self.engine.start_chat_browse(state, "browse_phone"))
            self.assertEqual(state.chat_browsing, {})
        self.assertEqual(self.llm.calls, [])

    async def test_reading_view_is_bounded_and_does_not_follow_reply_cursor(self):
        state = WorldState(S)
        for i in range(30):
            self.note(state, f"{i}:" + "花" * 400)
        state.chat_replied_seq = 999
        rows = reading_view(state, S, ChatBrowseConfig(), self.clock.now())
        self.assertLessEqual(len(rows), 20)
        self.assertLessEqual(sum(len(row["text"]) for row in rows), 3000)
        self.assertEqual(rows[-1]["seq"], state.chat_seq)
        self.assertEqual(set(rows[0]), {"seq", "at", "name", "self", "text"})
        self.note(state, "内部记录", internal=True)
        self.note(state, "别的会话", origin=G)
        self.note(state, "只有她的自言自语", is_self=True)
        state.recent_chat[0]["at"] = self.clock.now() + 1
        state.recent_chat[1]["at"] = self.clock.now() - 86401
        view = reading_view(state, S, ChatBrowseConfig(), self.clock.now())
        self.assertFalse(any(row["text"] in {"内部记录", "别的会话"} for row in view))
        state.recent_chat = [state.recent_chat[-1]]
        self.assertEqual(reading_view(state, S, ChatBrowseConfig(), self.clock.now()), [])

    async def test_external_memory_and_media_refs_are_not_read_as_new_messages(self):
        state = WorldState(S)
        self.note(state, "<Anamnesis-Memory>旧聊天秘密</Anamnesis-Memory>现在窗边有花", images=["file:///private/media.png"])
        self.note(state, "<RAG-Faiss-Memory>只有旧回忆</RAG-Faiss-Memory>")
        rows = reading_view(state, S, ChatBrowseConfig(), self.clock.now())
        self.assertEqual([row["text"] for row in rows], ["现在窗边有花"])
        self.assertNotIn("private", json.dumps(rows))

    async def test_shared_group_source_whitelist_and_computer_location(self):
        self.add_group(sessions=[S, G, P])
        self.configure(phone=False, computer_nodes=["study"], sessions=[P])
        async with self.engine.session_state(G) as state:
            self.note(state, origin=S)
            self.note(state, origin=P)
            self.assertFalse(self.engine.start_chat_browse(state, "browse_phone"))
            self.assertTrue(self.engine.start_chat_browse(state, "browse_computer"))
            self.assertEqual(state.current_action["browse_session"], P)
            self.engine._stop_chat_browse(state, state.current_action)
            state.chat_browsing = {}
            state.node_id = "outside"
            self.assertFalse(self.engine.start_chat_browse(state, "browse_computer"))
        self.store.add_session("aiocqhttp:GroupMessage:9999", session_type="group", platform="aiocqhttp")
        self.engine.reload_config()
        other = await self.engine.load_state("aiocqhttp:GroupMessage:9999")
        self.note(other, origin=P)
        self.assertFalse(self.engine.start_chat_browse(other, "browse_computer"))

    async def test_success_ignores_instructions_and_preserves_reply_affinity_and_profiles(self):
        action = await self.prepare()
        before = await self.engine.load_state(S)
        watermarks = (before.chat_replied_until, before.chat_replied_seq, copy.deepcopy(before.chat_watermarks))
        people = self.engine.profiles.list_people(S)
        state = await self.complete(action, valence_delta=100, actions=[{"type": "say", "messages": ["发出去"]}],
            tools=[{"name": "shell", "params": {"command": "bad"}}], facts=[{"text": "伪造事实"}])
        self.assertIsNone(state.current_action)
        self.assertEqual(state.chat_browsing["sessions"][S]["seq"], action["browse_rows"][-1]["seq"])
        self.assertEqual((state.chat_replied_until, state.chat_replied_seq, state.chat_watermarks), watermarks)
        self.assertEqual(self.engine.profiles.list_people(S), people)
        self.assertLessEqual(abs(state.valence_offset - before.valence_offset), .02001)
        self.assertEqual(self.messenger.sent, [])
        self.assertEqual(self.tools.calls, [])
        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual(self.engine.profiles.list_people(S), [])
        self.assertIn("主观", feeling_context(state, S, self.clock.now()))

    async def test_empty_feeling_consumes_cursor_without_memory_and_no_repeat(self):
        action = await self.prepare()
        state = await self.complete(action, inner="", source_seqs=[])
        self.assertEqual(state.thoughts, [])
        self.clock.advance(7201)
        self.assertFalse(self.engine.start_chat_browse(state, "browse_phone"))
        self.assertFalse(any(row["source"] == "chat_browse" for row in self.engine.memory.db.query_memories(session_id=S)))

    async def test_invalid_citation_does_not_commit_progress(self):
        action = await self.prepare()
        state = await self.complete(action, source_seqs=[99999])
        self.assertNotIn("sessions", state.chat_browsing)
        self.assertEqual(state.thoughts, [])

    async def test_cooldown_daily_cap_and_next_day_are_shared(self):
        self.add_group(sessions=[S, G])
        action = await self.prepare()
        await self.complete(action)
        async with self.engine.session_state(G) as state:
            self.note(state, origin=G)
            self.assertFalse(self.engine.start_chat_browse(state, "browse_phone"))
            state.chat_browsing.update(next_at=0, count=3)
            self.assertFalse(self.engine.start_chat_browse(state, "browse_phone"))
        self.clock.advance(86400)
        async with self.engine.session_state(G) as state:
            self.note(state, "新的一天的消息", origin=G)
            self.assertTrue(self.engine.start_chat_browse(state, "browse_phone"))
            self.assertEqual(state.chat_browsing["count"], 1)

    async def test_sleep_low_energy_schedule_and_busy_win(self):
        self.configure()
        async with self.engine.session_state(S) as state:
            self.note(state)
            for change in [{"state": "sleeping"}, {"state": "drowsy"}, {"energy": .3}, {"current_action": {"type": "read"}}]:
                saved = {key: getattr(state, key) for key in change}
                for key, value in change.items():
                    setattr(state, key, value)
                self.assertFalse(self.engine.start_chat_browse(state, "browse_phone"), change)
                for key, value in saved.items():
                    setattr(state, key, value)
            with unittest.mock.patch.object(self.engine, "life_choice", return_value={"kind": "schedule"}):
                self.assertFalse(self.engine.start_chat_browse(state, "browse_phone"))

    async def test_private_feelings_do_not_leak_to_group_or_autonomous_prompt(self):
        self.add_group(sessions=[S, P])
        action = await self.prepare(source=P)
        await self.complete(action, inner="私聊后的独有感受")
        private = await self.engine.thoughts_payload(P)
        group = await self.engine.thoughts_payload(S)
        self.assertEqual(len(private["thoughts"]), 1)
        self.assertEqual(group["thoughts"], [])
        self.assertEqual(private["thoughts"][0]["browse_session"], P)
        self.assertNotIn("私聊后的独有感受", await self.engine.preview_autonomous_prompt(S))
        self.assertNotIn("私聊后的独有感受", await self.engine.preview_autonomous_prompt(P))
        state = await self.engine.load_state(S)
        for sid, visible in [(P, True), (S, False)]:
            _, prompt = self.engine.prompts.build_dialogue_prompt(persona_text="人设", state=state, node=None, current_session=sid)
            self.assertEqual("私聊后的独有感受" in prompt, visible)
        self.clock.advance(86401)
        self.assertEqual((await self.engine.thoughts_payload(P))["thoughts"], [])
        self.assertEqual(feeling_context(state, P, self.clock.now()), "")

    async def test_meaningful_memory_retains_source_but_never_enters_review_or_generic_runtime(self):
        self.add_group(sessions=[S, P])
        action = await self.prepare(source=P)
        state = await self.complete(action, meaningful=True, reason="想在自己的窗边慢慢养花")
        rows = self.engine.memory.db.query_memories(session_id=P)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["source"], rows[0]["type"]), ("chat_browse", "inner"))
        self.assertNotIn("大家聊起", rows[0]["context"])
        memories = self.engine.memory.recall(session_id=P, persona_id="", mode="global", now=self.clock.now(), include_reading=True)
        self.assertEqual(len([row for row in memories if row.source == "chat_browse"]), 1)
        self.assertFalse(any(row.source == "chat_browse" for row in self.engine.memory.recall(session_id=S, persona_id="", mode="global", include_reading=True)))
        self.assertFalse(any(row.source == "chat_browse" for row in self.engine.memory.recall(session_id=P, persona_id="", mode="global")))
        _, passive = self.engine.prompts.build_dialogue_prompt(persona_text="人设", state=state, node=None, current_session=P, memories=memories)
        self.assertEqual(passive.count("也想在自己的窗边留一小盆花。"), 1)
        self.clock.advance(86401)
        _, passive = self.engine.prompts.build_dialogue_prompt(persona_text="人设", state=state, node=None, current_session=P, memories=memories)
        self.assertIn("阅读本会话曾留下的感想", passive)
        self.assertNotIn("也想在", self.engine.prompts.runtime_layer(state, node=None, memories=memories, current_session=P))
        self.assertNotIn("也想在", "\n".join(self.engine.consolidator.memory_lines(S)))
        self.engine.memory.schedule_review(rows[0]["id"], next_at=self.clock.now() - 10)
        self.assertEqual(self.engine.memory.due_reviews(session_ids=[S, P], now=self.clock.now()), [])

    async def test_slow_inference_leaves_lock_available_and_accepts_new_messages(self):
        action = await self.prepare()
        started, release = asyncio.Event(), asyncio.Event()
        async def slow(**kwargs):
            started.set()
            await release.wait()
            return LLMReply(ok=True, text=json.dumps({"inner": "想看看花", "source_seqs": [action["browse_rows"][0]["seq"]]}))
        self.llm.generate = slow
        task = asyncio.create_task(self.engine._complete_chat_browse(S, action["id"]))
        try:
            await asyncio.wait_for(started.wait(), 2)
            async with asyncio.timeout(1):
                async with self.engine.session_state(S) as state:
                    self.note(state, "新来的另一条消息")
            release.set()
            await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        state = await self.engine.load_state(S)
        self.assertEqual(state.thoughts[-1]["content"], "想看看花")
        self.assertLess(state.chat_browsing["sessions"][S]["seq"], state.chat_seq)

    async def test_changed_activity_is_never_cleared_by_stale_job(self):
        action = await self.prepare()
        async with self.engine.session_state(S) as state:
            state.current_action = {"type": "read", "id": "new-reading"}
            state.state = "reading"
        await self.engine._complete_chat_browse(S, action["id"])
        state = await self.engine.load_state(S)
        self.assertEqual(state.current_action["id"], "new-reading")
        self.assertEqual(self.llm.calls, [])
        self.engine._stop_chat_browse(state, {})
        self.assertEqual(state.state, "reading")

    async def test_reading_completion_preserves_a_newly_queued_normal_plan(self):
        action = await self.prepare()
        async with self.engine.session_state(S) as state:
            state.current_plan = {"steps": [{"action": "read"}], "source": "reply", "expires_at": self.clock.now() + 1800}
        state = await self.complete(action)
        self.assertIsNone(state.current_action)
        self.assertEqual(state.current_plan["source"], "reply")

    async def test_stale_result_is_discarded_after_interrupt_edit_persona_or_clear(self):
        for change in ["interrupt", "persona", "clear", "disabled", "day", "location", "evidence"]:
            with self.subTest(change=change):
                action = await self.prepare()
                self.engine._ask_judge = AsyncMock()
                async def mutate(*args, **kwargs):
                    if change == "persona":
                        self.persona.text = "另一份人设"
                    elif change == "disabled":
                        self.configure(enabled=False)
                    elif change == "day":
                        self.clock.advance(86400)
                    async with self.engine.session_state(S) as state:
                        if change == "interrupt":
                            state.current_action = {"type": "read", "id": "new-reading"}
                        elif change == "clear":
                            state.recent_chat = []
                        elif change == "location":
                            state.node_id = "bedroom"
                        elif change == "evidence":
                            state.recent_chat[-1]["text"] = "被编辑的原文"
                    return json.dumps({"inner": "过期感想", "source_seqs": [action["browse_rows"][0]["seq"]]})
                self.engine._ask_judge.side_effect = mutate
                await self.engine._complete_chat_browse(S, action["id"])
                state = await self.engine.load_state(S)
                self.assertFalse(any(row["content"] == "过期感想" for row in state.thoughts))
                if change == "interrupt":
                    self.assertEqual(state.current_action["id"], "new-reading")
                async with self.engine.session_state(S) as state:
                    state.chat_browsing, state.recent_chat, state.thoughts = {}, [], []
                    state.node_id = "study"
                self.persona.text = "你是一个温柔黏人的少女。"

    async def test_failure_restart_and_duplicate_finish_do_not_replay(self):
        action = await self.prepare()
        self.engine._ask_judge = AsyncMock(side_effect=RuntimeError("provider down"))
        await self.engine._complete_chat_browse(S, action["id"])
        state = await self.engine.load_state(S)
        self.assertIsNone(state.current_action)
        self.assertEqual(state.chat_browsing["count"], 1)
        state.current_action = {**action, "inference_started": True}
        restored = WorldState.from_payload(state.to_payload(), S)
        self.assertEqual(restored.chat_browsing, state.chat_browsing)
        self.engine.finish_chat_browse(restored, restored.current_action)
        self.assertIsNone(restored.current_action)
        self.engine._complete_chat_browse = AsyncMock()
        state.current_action = action
        for _ in range(3):
            self.engine.finish_chat_browse(state, action)
        tasks = list(self.engine._background_jobs.values())
        await asyncio.gather(*tasks)
        self.engine._complete_chat_browse.assert_awaited_once()

    async def test_output_cap_call_attribution_and_budget_denial(self):
        from core.model_calls import call_context
        action = await self.prepare()
        observed = []
        async def generate(**kwargs):
            observed.append((kwargs["max_tokens"], call_context()))
            return LLMReply(ok=True, text='{"inner":"", "source_seqs":[]}')
        self.llm.generate = generate
        await self.engine._complete_chat_browse(S, action["id"])
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0][0], 600)
        self.assertEqual(observed[0][1]["purpose"], "chat_browse")
        async with self.engine.session_state(S) as state:
            state.chat_browsing = {}
        self.configure()
        raw = self.store.raw_world()
        raw.setdefault("limits", {})["background_daily_calls"] = 1
        self.store.save_world(raw)
        self.engine.reload_config()
        async with self.engine.session_state(S) as state:
            self.note(state, "另一条新消息")
            self.assertTrue(self.engine.start_chat_browse(state, "browse_phone"))
            action = copy.deepcopy(state.current_action)
        await self.engine._complete_chat_browse(S, action["id"])
        self.assertEqual(len(observed), 1)  # Previous request already used the day's budget.
        state = await self.engine.load_state(S)
        self.assertIsNone(state.current_action)
        self.assertEqual(state.thoughts, [])

    async def test_long_persona_keeps_important_traits_at_the_end(self):
        self.persona.text = "角色原有的世界背景。" * 1000 + "末尾的重要设定：不喜欢花，不能因阅读改变这个喜好。"
        action = await self.prepare()
        await self.complete(action, inner="", source_seqs=[])
        material = json.loads(self.llm.calls[-1]["prompt"])
        self.assertEqual(material["persona"], self.persona.text)
        self.assertIn("不能因阅读改变这个喜好", material["persona"])

    async def test_cancelled_inference_stops_on_next_finish_without_retry(self):
        action = await self.prepare()
        started = asyncio.Event()
        async def generate(**kwargs):
            started.set()
            await asyncio.Event().wait()
        self.llm.generate = generate
        task = asyncio.create_task(self.engine._complete_chat_browse(S, action["id"]))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        async with self.engine.session_state(S) as state:
            self.assertTrue(state.current_action["inference_started"])
            self.engine.finish_chat_browse(state, state.current_action)
            self.assertIsNone(state.current_action)
            self.assertNotIn("sessions", state.chat_browsing)

    async def test_mixed_action_batch_and_extension_cannot_publish(self):
        self.configure()
        self.engine.extensions.action_note = Mock(return_value="不能发出去")
        async with self.engine.session_state(S) as state:
            self.note(state)
            outcome = fixture.TickOutcome(session_id=S)
            actions = [PlannedAction(type="say", messages=["先发"]), PlannedAction(type="browse_phone"),
                       PlannedAction(type="say", messages=["后发"])]
            self.assertTrue(await self.engine._execute_actions(state, None, outcome, actions, depth=0, autonomous=True))
            self.assertEqual(outcome.messages, [])
            self.assertFalse(self.engine._action_speaks(ActionDef(id="browse_phone", visible=True)))
            self.engine.extensions.action_note.assert_not_called()
            self.engine.suspend_life(state, reason="该睡了", source="rest")
            self.assertEqual(state.life_resume, {})

    async def test_decider_can_read_with_proactive_speech_off_and_tick_finishes_silently(self):
        self.configure()
        async with self.engine.session_state(S) as state:
            self.note(state)
        self.engine.advance_life_matters = AsyncMock()
        self.engine.rng = Mock(random=Mock(return_value=0), choice=Mock(return_value="browse_phone"), randint=Mock(return_value=3))
        self.engine.proactive_chat_allowed = Mock(return_value=False)
        await self.engine.maybe_decide(S, force=True)
        state = await self.engine.load_state(S)
        self.assertEqual(state.current_action["type"], "browse_phone")
        self.engine._complete_chat_browse = AsyncMock()
        outcome = fixture.TickOutcome(session_id=S)
        await self.engine._finish_action(state, None, outcome, state.current_action)
        await asyncio.gather(*self.engine._background_jobs.values())
        self.assertEqual(outcome.messages, [])
        self.assertEqual(self.messenger.sent, [])
        self.engine._complete_chat_browse.assert_awaited_once()
