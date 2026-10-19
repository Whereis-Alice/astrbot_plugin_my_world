"""Cross-module regressions for character continuity, calendar and memory boundaries."""
import asyncio
import copy
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

from core.engine import TickOutcome
from core.models import ChainStep, ScheduleDef
from core.state import WorldState
from tests import test_engine as fixture
from tests import test_life_matters as matters_fixture

S, P = fixture.SESSION, fixture.PRIVATE_SESSION


class LifeAuditTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = matters_fixture.MattersAcceptanceTests.asyncSetUp
    add_group = fixture.EngineTestCase.add_group

    async def idle(self):
        async with self.engine.session_state(S) as state:
            state.state, state.energy, state.world_time = "idle", .9, 600
            state.current_action, state.current_plan = None, None

    def production_clock(self, stamp):
        # No synthetic wall-clock override: exercise the actual configured timezone.
        self.engine.clock = None
        self.engine._now = Mock(return_value=stamp)
        self.engine.world.timezone = "Asia/Shanghai"

    async def test_one_calendar_drives_clock_day_budgets_and_practice(self):
        stamp = datetime(2026, 10, 19, 16, 5, tzinfo=timezone.utc).timestamp()
        self.production_clock(stamp)
        self.assertEqual(self.engine.local_now().isoformat(), "2026-10-20T00:05:00+08:00")
        self.assertEqual(self.engine._today_key(stamp), "2026-10-20")
        state = WorldState(S, node_id="study")
        state.current_action = {"type": "read", "duration_ticks": 20, "elapsed_ticks": 10}
        self.engine.settle_life_practice(state, state.current_action)
        episodes = state.life_interests["read"]["episodes"]
        self.assertEqual([(row["day"], row["period"], row["minutes"]) for row in episodes],
                         [("2026-10-19", "夜间", 5), ("2026-10-20", "夜间", 5)])
        self.engine._miss_push_left(state)
        self.engine._desire_push_left(state)
        self.assertEqual((state.miss_push_day, state.desire_push_day), ("2026-10-20", "2026-10-20"))
        self.engine.profiles.touch(S, "42", now=stamp)
        self.assertEqual(self.engine.profiles.profile(S, "42")["payload"]["days"], ["2026-10-20"])
        self.engine.dynamics._spend_chat_valence(state, .01, now=stamp)
        self.assertEqual(state.chat_day, "2026-10-20")
        self.assertIn("今天 00:05", self.engine._journal_time_text(stamp, stamp))

    async def test_configured_timezone_can_be_changed_without_restarting(self):
        stamp = datetime(2026, 10, 19, 16, 5, tzinfo=timezone.utc).timestamp()
        self.production_clock(stamp)
        self.assertEqual(self.engine._today_key(stamp), "2026-10-20")
        self.engine.world.timezone = "UTC"
        self.assertEqual(self.engine._today_key(stamp), "2026-10-19")

    async def test_long_persona_is_kept_for_life_planning_and_reflection(self):
        self.persona.text = "她的设定。" * 1800 + "核心边界：不能饮茶，也不能随意承诺。"
        await self.idle()
        judge = self.engine._ask_judge = AsyncMock(return_value='{"text":"", "actions":[]}')
        await self.engine.seed_life_matter(S)
        self.assertIn(self.persona.text, judge.call_args.args[2])
        async with self.engine.session_state(S) as state:
            item = self.engine.add_life_matter(state, "慢慢读完一本书")
        await self.engine.advance_life_matters(S)
        self.assertIn(self.persona.text, judge.call_args.args[2])
        await self.engine.reflect_turn(S, user_text="晚上好", reply_text="嗯")
        self.assertIn(self.persona.text, judge.call_args.args[2])
        async with self.engine.session_state(S) as state:
            current = next(row for row in state.life_matters if row["id"] == item["id"])
            current.update(needs_review=True, resume_at=0)
            snapshot = copy.deepcopy(current)
        await self.engine._review_matter_stage(S, snapshot, world=self.engine.world,
            state_key=S, persona=self.persona.text)
        self.assertIn(self.persona.text, judge.call_args.args[2])

    async def test_persona_summary_and_voice_generation_see_the_whole_character(self):
        persona = "原始设定。" * 2000 + "末尾禁忌：不能饮茶。"
        for build in (self.engine.prompts.build_persona_brief_prompt,
                      self.engine.prompts.build_persona_review_prompt,
                      self.engine.prompts.build_voice_sample_prompt,
                      self.engine.prompts.build_eval_script_prompt):
            with self.subTest(build=build.__name__):
                _, prompt = build(persona_text=persona)
                self.assertIn(persona, prompt)

    async def test_sleep_and_social_learning_keep_persona_boundaries_at_the_end(self):
        self.persona.text = "原有的性格与习惯。" * 1200 + "不可改变的末尾边界。"
        await self.idle()
        self.engine.profiles.touch(S, "42", "小明")
        for i in range(5):
            sources = self.engine.interaction_sources(S, "42", "谢谢", [
                {"user_id": "42", "text": "谢谢", "message_id": f"thanks_{i}"}])
            self.engine.record_turn_interaction(S, "42", sources, .1, "愿意倾听")
        judge = self.engine._ask_judge = AsyncMock(return_value='{"habits":[]}')
        await self.engine.maintain_social_learning(S)
        self.assertIn(self.persona.text, judge.call_args.args[2])
        self.engine.world.sleep.mode = "autonomous"
        self.clock.set_struct(datetime(2026, 10, 19, 21, 30))
        self.engine._ask_llm = AsyncMock(return_value='{"decision":"continue"}')
        async with self.engine.session_state(S) as state:
            state.energy, state.sleep_pressure = .3, .7
            state.sleep_rhythm = {"mode": "autonomous", "awake_since": self.clock.now() - 14 * 3600}
            await self.engine._tick_sleep_rhythm(state, TickOutcome(S))
        self.assertIn(self.persona.text, self.engine._ask_llm.call_args.args[2])

    async def test_yesterdays_self_start_cannot_create_todays_goal(self):
        self.clock._now = datetime(2026, 10, 19, 23, 59).timestamp()
        await self.idle()
        entered, release = asyncio.Event(), asyncio.Event()

        async def judge(*args, **kwargs):
            entered.set()
            await release.wait()
            return '{"text":"昨天临时想到的项目"}'

        self.engine._ask_judge = AsyncMock(side_effect=judge)
        task = asyncio.create_task(self.engine.seed_life_matter(S))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            self.clock.advance(120)
        finally:
            release.set()
            await asyncio.wait_for(task, 2)
        self.assertEqual((await self.engine.load_state(S)).life_matters, [])

    async def test_matter_plans_and_stage_reviews_cannot_commit_across_midnight(self):
        for review in (False, True):
            with self.subTest(review=review):
                self.clock._now = datetime(2026, 10, 19, 23, 59).timestamp()
                await self.idle()
                async with self.engine.session_state(S) as state:
                    state.life_matters = []
                    item = self.engine.add_life_matter(state, "继续原来的读书项目")
                    item.update(needs_review=review)
                entered, release = asyncio.Event(), asyncio.Event()

                async def judge(*args, **kwargs):
                    entered.set()
                    await release.wait()
                    return json.dumps({"outcome": "completed", "reason": "旧阶段", "steps": [
                        {"text": "旧安排", "action": "read", "node": "study", "minutes": 5, "day_offset": 0}]})

                self.engine._ask_judge = AsyncMock(side_effect=judge)
                task = asyncio.create_task(self.engine.advance_life_matters(S))
                try:
                    await asyncio.wait_for(entered.wait(), 2)
                    self.clock.advance(120)
                finally:
                    release.set()
                    await asyncio.wait_for(task, 2)
                saved = next(row for row in (await self.engine.load_state(S)).life_matters if row["id"] == item["id"])
                self.assertNotEqual(saved["status"], "completed")
                self.assertEqual(saved["steps"], [])

    async def test_arrival_updates_the_place_for_needs_events_and_nickname(self):
        await self.idle()
        async with self.engine.session_state(S) as state:
            state.current_action = {"type": "walk_to", "target_node": "bedroom", "duration_ticks": 1,
                                    "elapsed_ticks": 0, "arrival_decide": 0}
        events = self.engine._maybe_run_event = AsyncMock()
        nickname = self.engine._sync_nickname = AsyncMock()
        dynamics = self.engine.dynamics.tick = Mock(wraps=self.engine.dynamics.tick)
        await self.engine._tick_session(S)
        self.assertEqual(events.call_args.args[1].id, "bedroom")
        self.assertEqual(nickname.call_args.args[1].id, "bedroom")
        self.assertEqual(dynamics.call_args.kwargs["node"].id, "bedroom")

    async def test_autonomous_sleep_schedule_is_not_presented_as_next_executable_plan(self):
        self.engine.world.sleep.mode = "autonomous"
        self.clock.set_struct(datetime(2026, 10, 19, 21, 30))
        self.engine.schedules.schedules = [
            ScheduleDef(id="fixed_sleep", time="21:35", action_chain=[ChainStep(type="sleep")]),
            ScheduleDef(id="reading", time="22:00", action_chain=[ChainStep(type="read")]),
        ]
        self.assertEqual(self.engine._next_schedule(S)["id"], "reading")
        state = WorldState(S, node_id="study", energy=.9)
        self.assertEqual(self.engine.life_choice(state)["kind"], "idle")

    async def test_private_promise_memory_cannot_cross_its_source_or_enter_shared_context(self):
        self.add_group(sessions=[S, P])
        await self.idle()
        self.engine.world.action_map()["read"].on_complete.trigger = "none"
        async with self.engine.session_state(S) as state:
            item = self.engine.add_life_matter(state, "悄悄为42准备一封私人信", session=P, who="42")
            item["steps"] = [{"id": "private_step", "text": "读完来信", "action": "read", "node": "study",
                              "minutes": 5, "status": "pending"}]
            await self.engine._start_matter_step(state, item)
            state.current_action["elapsed_ticks"] = 5
            await self.engine._finish_action(state, self.engine.node("study"), TickOutcome(S), state.current_action)
        memories = [row for row in self.db.raw.query_memories(session_id=P) if row["source"] == "life_matter"]
        self.assertEqual(len(memories), 1)
        for mode in ("group", "group_persona", "persona", "global"):
            with self.subTest(mode=mode):
                result = self.engine.memory.recall(session_id=S, persona_id="", mode=mode,
                    include_reading=True, include_private_matters=True)
                self.assertFalse(any("私人信" in row.content for row in result))
        candidates = self.engine.memory._query(S, "", "global", session_ids=[S, P])
        self.assertFalse(any("私人信" in row["content"] for row in candidates), "embedding uses the same prefiltered candidates")
        # A shared dream/automatic check-in cannot turn a private promise into public life.
        self.assertNotIn("私人信", "\n".join(self.engine.consolidator.memory_lines(S)))
        self.engine.memory.schedule_review(memories[0]["id"], next_at=self.clock.now() - 1)
        self.assertEqual(self.engine.memory.due_reviews(session_ids=[S, P], now=self.clock.now()), [])
        result, _ = await self.engine.lookup_life_memories(P, "", "私人信")
        self.assertTrue(any("私人信" in row.content for row in result))
        private_prompt = self.engine.prompts.build_dialogue_prompt(persona_text="角色", state=await self.engine.load_state(S),
            node=None, current_session=P, memories=result)[1]
        self.assertIn("私人信", private_prompt)
        generic_prompt = self.engine.prompts.runtime_layer(await self.engine.load_state(S), node=None, memories=result, current_session=P)
        self.assertNotIn("私人信", generic_prompt)
        passive = self.engine.memory.recall_participants(records=[], session_id=P, persona_id="")
        self.assertTrue(any("私人信" in row.content for row in passive))
        group_prompt = self.engine.prompts.build_dialogue_prompt(persona_text="角色", state=await self.engine.load_state(S),
            node=None, current_session=S, memories=result)[1]
        self.assertNotIn("私人信", group_prompt)

    async def test_shared_life_memory_is_still_available_in_other_member_sessions(self):
        self.add_group(sessions=[S, P])
        mid = self.engine.memory.remember(session_id=P, persona_id="", node_id="study",
            content="今天读完了小说的一章", source="life_matter")
        rows = self.engine.memory.recall(session_id=S, persona_id="", mode="group")
        self.assertIn(mid, [row.id for row in rows])

    async def test_conversation_inner_voice_stays_in_its_actual_session(self):
        self.add_group(sessions=[S, P])
        secret = "私人来信让我想起海边的约定"
        self.engine._ask_judge = AsyncMock(return_value=json.dumps({
            "reasoning": {"inner": secret, "intent": "回应私下来信"}}, ensure_ascii=False))
        await self.engine.reflect_turn(P, user_id="42", user_text="请只在这里谈", reply_text="嗯")
        state = await self.engine.load_state(S, cold_start=False)
        common = self.engine.prompts.runtime_layer(state, node=None, current_session=P)
        self.assertNotIn(secret, common)
        for sid, visible in ((S, False), (P, True)):
            with self.subTest(session=sid):
                prompt = self.engine.prompts.build_dialogue_prompt(persona_text="角色", state=state,
                    node=None, current_session=sid)[1]
                self.assertEqual(secret in prompt, visible)
                thoughts = (await self.engine.thoughts_payload(sid))["thoughts"]
                self.assertEqual(any(secret == row["content"] for row in thoughts), visible)
                snapshot = await self.engine.snapshot(sid)
                self.assertEqual(secret in str(snapshot["last_reasoning"]), visible)
        # Repeating a feeling in another chat must not be discarded by cross-chat deduplication.
        await self.engine.reflect_turn(S, user_id="42", user_text="也想到那时", reply_text="嗯")
        self.assertTrue(any(secret == row["content"] for row in
            (await self.engine.thoughts_payload(S))["thoughts"]))

    async def test_private_reading_records_do_not_starve_due_public_memories(self):
        for i in range(12):
            self.db.raw.add_memory(session_id=S, persona_id="", scope="group", node_id="study",
                content=f"翻阅消息的想法{i}", memory_type="inner", source="chat_browse", next_review_at=1)
        expected = self.db.raw.add_memory(session_id=S, persona_id="", scope="group", node_id="study",
            content="花盆需要浇水", next_review_at=2)
        self.assertEqual([row["id"] for row in self.engine.memory.due_reviews(session_ids=[S], now=self.clock.now(), limit=1)], [expected])
        self.assertEqual(self.engine.memory.due_reviews(session_ids=[], now=self.clock.now()), [])
        self.assertEqual(self.engine.memory.due_reviews(session_ids=[""], now=self.clock.now()), [])
        self.assertEqual(self.engine.memory.due_reviews(session_ids=[S], now=self.clock.now(), limit=0), [])

    async def test_clearing_context_or_thoughts_preserves_other_origins(self):
        self.add_group(sessions=[S, P])
        for operation in (self.engine.clear_chat_context, self.engine.clear_thoughts):
            with self.subTest(operation=operation.__name__):
                async with self.engine.session_state(S) as state:
                    state.thoughts = []
                    state.add_thought("另一边的想法", dialogue_session=P)
                    state.add_thought("本会话想法", dialogue_session=S)
                    state.note_reasoning({"inner": "本会话推理"}, source="reflect", session_id=S)
                await operation(S)
                state = await self.engine.load_state(S, cold_start=False)
                self.assertEqual([row["content"] for row in state.thoughts], ["另一边的想法"])
                if operation == self.engine.clear_chat_context:
                    self.assertEqual(state.last_reasoning, {})

    async def test_legacy_dialogue_drafts_are_not_reused_as_autonomous_intent(self):
        state = WorldState(S, last_reasoning={"_source": "reply", "intent": "回答私下的问题"})
        state.add_thought("私下的旧想法", source="聊完一轮", quote="旧消息")
        self.assertEqual(state.reasoning_for(), {})
        self.assertEqual(state.reasoning_for(P), {})
        self.assertEqual(state.reasoning_for(S), {})
        self.assertNotIn("私下的旧想法", self.engine.prompts._inner_voice(state))
        self.assertEqual(self.engine.prompts._inner_voice(state, S, dialogue_only=True), "")
        state.note_reasoning({"intent": "继续读世界中的小说"}, source="plan")
        self.assertIn("小说", state.reasoning_for()["intent"])

    async def test_automatic_capacity_cleanup_preserves_pinned_memories(self):
        pinned = self.db.raw.add_memory(session_id=S, persona_id="", scope="group", node_id="study",
            content="重要的第一天", weight=0, pinned=True)
        for i in range(6):
            self.db.raw.add_memory(session_id=S, persona_id="", scope="group", node_id="study", content=f"平常经历{i}", weight=.5)
        self.engine.memory._enforce_limit(session_id=S, persona_id="", max_per_scope=3)
        self.assertIsNotNone(self.db.raw.memory_by_id(pinned))
        self.assertEqual(len(self.db.raw.query_memories(session_id=S)), 3)

    async def test_configurable_relationship_rules_have_no_hardcoded_group_ceiling(self):
        text = self.engine.prompts.world_layer()
        self.assertNotIn("群友怎么聊都到不了抱抱亲亲", text)
        self.assertIn("本轮", text)
