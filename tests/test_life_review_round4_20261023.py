"""Event continuity through real submission, character switches and shared chats."""
import copy
import json
import time
import unittest
from hashlib import sha256
from unittest.mock import AsyncMock

from core.engine import MessageContext, TickOutcome
from core.consolidate import ConsolidateResult
from core.events import new_thread, event_from_payload
from tests import test_events as events
from tests import test_consolidate as consolidate

S = events.SESSION
OTHER = "aiocqhttp:PrivateMessage:7"


class EventContinuityTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = events.EventEngineTest.asyncSetUp
    use_pool_only = events.EventEngineTest.use_pool_only
    start_event = events.EventEngineTest.start_event
    group_says = events.EventEngineTest.group_says

    async def ongoing(self, *, help_needed=False):
        await self.group_says()
        package = {**events.TEST_POOL_EVENT, "title": "旧角色的盒子",
                   "critical": True, "kind": "need_intervene" if help_needed else "solo"}
        self.use_pool_only(package)
        self.main.default_reply = json.dumps({"pick": 1, "reason": "先找工具",
                                             "ask_help": help_needed,
                                             "say": {"plan": ["能帮我看看这个盒子吗？"]}})
        self.helper.default_reply = '{"outcome":"尚未打开","followup":"再找一把工具"}'
        await self.start_event()
        return copy.deepcopy((await self.engine.load_state(S)).event_threads[-1])

    async def test_same_second_submissions_do_not_select_the_closed_predecessor(self):
        self.main.default_reply = '{"pick":1,"reason":"试一下"}'
        self.use_pool_only({**events.TEST_POOL_EVENT, "title": "第一件"})
        self.helper.default_reply = '{"outcome":"第一件结束"}'
        self.assertEqual(await self.engine.submit_event(S, "第一件事"), "")
        self.use_pool_only({**events.TEST_POOL_EVENT, "title": "第二件"})
        self.helper.default_reply = '{"outcome":"第二件未完","followup":"继续找工具"}'
        self.assertEqual(await self.engine.submit_event(S, "第二件事"), "")
        state = await self.engine.load_state(S)
        first, second = state.event_threads
        self.assertNotEqual(first["id"], second["id"])
        self.helper.default_reply = '{"outcome":"第二件结束"}'
        note = await self.engine.advance_thread(S, second["id"])
        self.assertIn("推进了一幕", note)
        state = await self.engine.load_state(S)
        self.assertEqual(len(state.event_threads[0]["steps"]), 1)
        self.assertEqual(len(state.event_threads[1]["steps"]), 2)

    async def test_new_events_record_character_and_actual_source_session(self):
        item = await self.ongoing()
        self.assertEqual(item.get("persona_key"), sha256(self.persona.text.encode()).hexdigest()[:24])
        self.assertEqual(item.get("session"), S)

    async def test_other_character_event_does_not_block_its_free_life_or_enter_prompt(self):
        await self.ongoing()
        self.persona.text = "另一位完全不同的园艺少女。"
        async with self.engine.session_state(S) as state:
            self.assertIsNone(self.engine._active_thread(state))
            self.assertNotIn("旧角色的盒子", self.engine.event_journal(state))
            self.assertEqual(self.engine.pending_event_line(state), "")
            self.assertNotEqual(self.engine.life_choice(state)["kind"], "event")

    async def test_runtime_background_reload_also_respects_current_character(self):
        await self.ongoing(help_needed=True)
        self.persona.text = "另一位完全不同的园艺少女。"
        notes = await self.engine.runtime_notes(S)
        self.assertNotIn("旧角色的盒子", notes["event_journal"])
        self.assertEqual(notes["pending_event"], "")

    async def test_other_character_event_is_not_advanced_or_expired(self):
        original = await self.ongoing()
        self.persona.text = "另一位完全不同的园艺少女。"
        self.clock.advance(2 * 86400)
        async with self.engine.session_state(S) as state:
            outcome = TickOutcome(session_id=S)
            self.assertFalse(await self.engine._continue_thread(state, self.engine.node("study"), outcome,
                                                               self.clock.now()))
            self.assertEqual(state.event_threads[0], original)

    async def test_explicit_manual_advance_cannot_reassign_another_characters_event(self):
        original = await self.ongoing()
        self.persona.text = "另一位完全不同的园艺少女。"
        self.assertNotIn("推进了一幕", await self.engine.advance_thread(S, original["id"]))
        self.assertEqual((await self.engine.load_state(S)).event_threads[0], original)

    async def test_old_character_help_neither_reminds_nor_consumes_current_reply(self):
        original = await self.ongoing(help_needed=True)
        before = copy.deepcopy((await self.engine.load_state(S)).pending_help)
        self.persona.text = "另一位完全不同的园艺少女。"
        self.clock.advance(200)
        self.engine._filter_suggestions = AsyncMock()
        async with self.engine.session_state(S) as state:
            outcome = TickOutcome(session_id=S)
            await self.engine._advance_pending_help(state, self.engine.node("study"), outcome, self.clock.now())
            self.assertEqual(outcome.messages, [])
            self.assertEqual(state.pending_help, before)
            self.assertEqual(self.engine.pending_event_line(state), "")
        ctx = MessageContext(session_id=S, user_id="7", user_name="友人", text="今天吃什么？")
        self.assertIsNone(await self.engine._consume_help_reply(ctx))
        self.engine._filter_suggestions.assert_not_awaited()
        self.assertEqual((await self.engine.load_state(S)).event_threads[0], original)

    async def test_observation_and_intervention_use_current_character(self):
        await self.ongoing(help_needed=True)
        self.persona.text = "另一位完全不同的园艺少女。"
        overview = await self.engine.event_overview(S)
        self.assertEqual(overview["open_count"], 0)
        self.assertEqual(overview["active_id"], "")
        self.assertEqual(overview["pending_help"]["state"], "")
        self.assertEqual(await self.engine.pending_intervention(S), 0)

    async def test_event_memory_is_recalled_only_by_the_character_who_experienced_it(self):
        # Memory storage uses wall time; keep this probe in that same time domain.
        self.clock.advance(time.time() - self.clock.now())
        await self.ongoing()
        async with self.engine.session_state(S) as state:
            request = self.engine.context_request(state, S, task="reply", text="尚未打开", rows=[])
            await self.engine.prepare_context_memory(request)
            self.assertTrue(self.engine.context_memories(state, request))
        self.persona.text = "另一位完全不同的园艺少女。"
        async with self.engine.session_state(S) as state:
            request = self.engine.context_request(state, S, task="reply", text="尚未打开", rows=[])
            await self.engine.prepare_context_memory(request)
            self.assertFalse(self.engine.context_memories(state, request))

    async def test_folded_event_keeps_original_content_and_hides_internal_ownership_fields(self):
        self.clock.advance(time.time() - self.clock.now())
        await self.ongoing()
        async with self.engine.session_state(S) as state:
            request = self.engine.context_request(state, S, task="reply", text="尚未打开", rows=[])
            await self.engine.prepare_context_memory(request)
            original = self.engine.context_memories(state, request)[0]
            self.engine.memory.fold(original.id, text="盒子还没开")
            rendered = self.engine.context_memories(state, request)[0].render()
            self.assertIn("尚未打开", rendered)
            self.assertNotIn("persona_key", rendered)
            self.assertNotIn("event_id", rendered)

    async def test_old_event_digest_is_not_shown_as_current_characters_day(self):
        await self.ongoing()
        self.persona.text = "另一位完全不同的园艺少女。"
        snapshot = await self.engine.snapshot(S)
        self.assertEqual(snapshot["event_digest"], [])
        self.assertEqual(snapshot["event_threads"], [])
        self.assertEqual(snapshot["pending_help"]["state"], "")

    async def test_returning_character_and_legacy_event_can_continue(self):
        text = self.persona.text
        original = await self.ongoing()
        self.persona.text = "另一位角色。"
        self.persona.text = text
        self.helper.default_reply = '{"outcome":"找着工具了"}'
        self.assertIn("推进了一幕", await self.engine.advance_thread(S, original["id"]))
        package = event_from_payload(events.TEST_POOL_EVENT)
        legacy = new_thread(package, now=self.clock.now(), thread_id="th_old")
        legacy.update(pending_followup="找工具", next_step_at=self.clock.now())
        async with self.engine.session_state(S) as state:
            state.event_threads = [legacy]
        self.assertIn("推进了一幕", await self.engine.advance_thread(S, "th_old"))

    def add_shared_chat(self):
        self.store.add_session(OTHER, session_type="private", platform="aiocqhttp", cold_start_node="study")
        raw = self.store.raw_sessions()
        raw["groups"] = [{"id": "same_girl", "name": "同一个她", "sessions": [S, OTHER], "main_session": S}]
        self.store.save_sessions(raw)
        self.engine.reload_config()

    async def test_shared_chat_reply_does_not_implicitly_answer_help_asked_elsewhere(self):
        self.add_shared_chat()
        await self.ongoing(help_needed=True)
        self.engine._filter_suggestions = AsyncMock(return_value=None)
        ctx = MessageContext(session_id=OTHER, user_id="7", user_name="友人", text="今天吃什么？")
        self.assertIsNone(await self.engine._consume_help_reply(ctx))
        self.engine._filter_suggestions.assert_not_awaited()
        self.assertEqual(await self.engine.pending_intervention(OTHER), 0)

    async def test_help_is_preserved_when_two_characters_ask_before_returning(self):
        first_persona = self.persona.text
        first = await self.ongoing(help_needed=True)
        self.persona.text = "另一位完全不同的园艺少女。"
        second = await self.ongoing(help_needed=True)
        async with self.engine.session_state(S) as state:
            second_id = self.engine._active_thread(state)["id"]
        self.persona.text = first_persona
        async with self.engine.session_state(S) as state:
            self.assertEqual(self.engine._active_thread(state)["id"], first["id"])
            self.assertEqual(self.engine._pending_event_help(state)["thread_id"], first["id"])
        await self.engine.close_thread(S, first["id"])
        self.persona.text = "另一位完全不同的园艺少女。"
        async with self.engine.session_state(S) as state:
            self.assertEqual(self.engine._pending_event_help(state)["thread_id"], second_id)
        self.assertNotEqual(first["id"], second_id)
        self.assertTrue(second)

    async def test_consuming_current_digest_preserves_other_characters_pending_experience(self):
        first_persona = self.persona.text
        await self.ongoing()
        self.persona.text = "另一位完全不同的园艺少女。"
        async with self.engine.session_state(S) as state:
            self.engine._append_event_digest(state, ["新角色读完园艺笔记"])
            self.assertEqual(self.engine._event_digest_for(state), ["新角色读完园艺笔记"])
            self.engine._consume_event_digest(state)
        self.persona.text = first_persona
        async with self.engine.session_state(S) as state:
            self.assertTrue(any("旧角色的盒子" in text for text in self.engine._event_digest_for(state)))

    async def test_continuation_keeps_the_original_destination_when_no_new_target_is_chosen(self):
        self.add_shared_chat()
        self.engine.world.life_share_session_whitelist = [S, OTHER]
        self.use_pool_only(events.TEST_POOL_EVENT)
        self.main.default_reply = json.dumps({"pick": 1, "reason": "问他看看", "send_to": OTHER})
        self.helper.default_reply = '{"outcome":"尚未打开","followup":"再找工具"}'
        await self.start_event()
        self.clock.advance(61)
        self.main.default_reply = '{"pick":1,"reason":"接着处理"}'
        self.helper.default_reply = '{"outcome":"找着工具了"}'
        async with self.engine.session_state(S) as state:
            outcome = TickOutcome(session_id=S)
            self.assertTrue(await self.engine._continue_thread(state, self.engine.node("study"), outcome, self.clock.now()))
            self.assertEqual(outcome.home(), OTHER)

    async def test_explicit_life_lookup_excludes_other_characters_memories(self):
        self.engine.memory.remember(session_id=S, persona_id="", node_id="study", content="旧角色的天文研究进展",
            source="life_matter", context=json.dumps({"persona_key": "old_character", "matter_id": "old_project"}))
        items, _ = await self.engine.lookup_life_memories(S, "", "天文研究")
        self.assertEqual(items, [])

    async def test_foreign_due_review_does_not_consume_current_recall_or_change_its_time(self):
        old = self.engine.memory.remember(session_id=S, persona_id="", node_id="study", content="旧角色的天文研究进展",
            source="life_matter", context=json.dumps({"persona_key": "old_character", "matter_id": "old_project"}),
            next_review_at=self.clock.now()-1)
        async with self.engine.session_state(S) as state:
            self.assertEqual(self.engine.take_due_reviews(state), [])
            self.assertEqual(state.review_visits, [])
        row = await self.db.call("memory_by_id", old)
        self.assertEqual(row["next_review_at"], self.clock.now()-1)

    async def test_own_due_review_is_found_behind_many_other_characters_records(self):
        key = sha256(self.persona.text.encode()).hexdigest()[:24]
        for index in range(12):
            self.engine.memory.remember(session_id=S, persona_id="", node_id="study", content=f"其他角色的记录{index}",
                source="life_matter", context=json.dumps({"persona_key": "old_character"}), next_review_at=self.clock.now()-10)
        current = self.engine.memory.remember(session_id=S, persona_id="", node_id="study", content="当前角色的记录",
            source="life_matter", context=json.dumps({"persona_key": key}), next_review_at=self.clock.now()-1)
        async with self.engine.session_state(S) as state:
            self.assertEqual([row["id"] for row in self.engine.take_due_reviews(state)], [current])


class LifeMemoryOwnershipTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = consolidate.ConsolidateTestCase.asyncSetUp

    def remember(self, text, key):
        return self.memory.remember(session_id=consolidate.SESSION, persona_id="", node_id="study",
            content=text, source="life_matter", context=json.dumps({"persona_key": key, "matter_id": "project"}))

    async def test_sleep_review_does_not_give_old_character_progress_to_current_persona(self):
        persona = "新角色喜欢园艺。"
        self.remember("旧角色的天文研究进展", "old_character")
        self.remember("新角色的园艺实践", sha256(persona.encode()).hexdigest()[:24])
        result = await self.consolidator.run(session_id=consolidate.SESSION, llm=AsyncMock(return_value="{}"),
            mode="full", persona_text=persona, dry_run=True)
        self.assertIn("新角色的园艺实践", str(result.preview))
        self.assertNotIn("旧角色的天文研究进展", str(result.preview))

    def test_explicit_merge_does_not_mix_different_character_ownership(self):
        old = self.remember("旧角色的进展", "old_character")
        current = self.remember("当前角色的进展", "current_character")
        result = ConsolidateResult(mode="full")
        self.consolidator._apply_full(consolidate.SESSION,
            {"merge": [{"keep_id": current, "drop_ids": [old], "text": "两个角色混在一起的进展"}],
             "_review_ids": {old, current}}, result, now=time.time())
        self.assertEqual(result.merged, 0)
        self.assertEqual(self.db.memory_by_id(current)["content"], "当前角色的进展")

    def test_identical_experiences_do_not_overwrite_another_characters_record(self):
        old = self.remember("今天读完了这本书", "old_character")
        current = self.remember("今天读完了这本书", "current_character")
        self.assertNotEqual(old, current)
        self.assertEqual(json.loads(self.db.memory_by_id(old)["context"])["persona_key"], "old_character")

