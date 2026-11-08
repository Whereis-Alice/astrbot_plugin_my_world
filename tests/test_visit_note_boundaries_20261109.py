"""Transient chat material remains current across clock and character changes."""
from __future__ import annotations

import copy
import types
import unittest
from unittest.mock import AsyncMock

from core.state import WorldState
from tests import test_engine as fixture
from tests import test_visits_actor as actors


def enable_embedding(engine):
    engine.world.memory.semantic_enabled = True
    provider = types.SimpleNamespace(
        describe=lambda *_: {"ready": True, "fingerprint": "visit-note-boundaries"},
        embed=AsyncMock(side_effect=lambda texts, *_: [[1., 0.] for _ in texts]),
    )
    engine.memory.semantic.provider = provider
    return provider


class VisitRecallClockTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = actors.VisitActorTests.asyncSetUp

    async def prepare_visit(self):
        self.active = copy.deepcopy(self.visit)
        self.active.update(outing_version=1, host_id="actor-b", guest_id=self.local["actor_id"],
            outing={"version": 1, "stage": 0, "place": "窗边", "activity": "一起下棋",
                    "started_at": self.clock.now(), "until": self.clock.now() + 1800})
        self.active["remote"]["actor_id"] = "actor-b"
        self.provider = enable_embedding(self.engine)
        return await self.engine.prepare_visit_context(fixture.SESSION, "后来呢")

    async def test_crossing_a_minute_keeps_the_prepared_match_and_current_elapsed_time(self):
        prepared = await self.prepare_visit()
        self.clock.advance(61)

        async with self.engine.session_state(fixture.SESSION):
            notes = await self.engine.runtime_notes(fixture.SESSION, query="后来呢",
                                                   visit_semantic_match=prepared)

        self.assertIn("visit_context", notes)
        self.assertIn("棋盘已经摆好了", notes["visit_context"])
        self.assertIn("已经一起待了约1分钟", notes["visit_context"])
        self.assertEqual(1, self.provider.embed.await_count)

    async def test_unchanged_visit_reuses_embedding_across_elapsed_minutes(self):
        await self.prepare_visit()
        self.clock.advance(61)
        await self.engine.prepare_visit_context(fixture.SESSION, "后来呢")

        self.assertEqual(1, self.provider.embed.await_count)

    async def test_an_identical_looking_new_visit_cannot_reuse_the_previous_match(self):
        prepared = await self.prepare_visit()
        self.active["id"] = "another-visit-with-the-same-scene"

        notes = await self.engine.runtime_notes(fixture.SESSION, query="后来呢",
                                               visit_semantic_match=prepared)

        self.assertNotIn("visit_context", notes)
        self.assertEqual(1, self.provider.embed.await_count)

    async def test_confirmed_context_change_during_embedding_invalidates_the_old_match(self):
        await self.prepare_visit()
        self.engine.memory.semantic.relevance_times.clear()

        async def another_turn(texts, *_):
            self.active["log"].append({"seq": 2, "actor_id": "actor-b", "kind": "say",
                                       "text": "先把棋子收好吧"})
            return [[1., 0.] for _ in texts]

        self.provider.embed.side_effect = another_turn
        prepared = await self.engine.prepare_visit_context(fixture.SESSION, "她后来怎么样")
        notes = await self.engine.runtime_notes(fixture.SESSION, query="她后来怎么样",
                                               visit_semantic_match=prepared)

        self.assertNotIn("visit_context", notes)
        self.assertEqual(2, self.provider.embed.await_count)


class ChatNotePersonaTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    async def test_character_switch_hides_note_before_embedding_and_prompt_selection(self):
        provider = enable_embedding(self.engine)
        original_persona = self.persona.text
        note = "刚才被叫作爱丽丝让我有点害羞"
        async with self.engine.session_state(fixture.SESSION) as state:
            self.engine.note_chat_note(state, note, fixture.SESSION)
        saved = copy.deepcopy(state.chat_notes)
        self.persona.text = "你是新搬来的莉耶芙，与此前角色独立。"
        state = await self.engine.load_state(fixture.SESSION, cold_start=False)
        request = self.engine.context_request(state, fixture.SESSION, text="你在吗", rows=[])

        await self.engine.prepare_context_memory(request, state=state)
        _, runtime = self.engine.prompts.build_dialogue_prompt(state=state,
            node=self.engine.node(state.node_id), persona_text=self.persona.text,
            context_request=request)
        embedded = "\n".join(text for call in provider.embed.await_args_list for text in call.args[0])

        self.assertNotIn(note, embedded)
        self.assertNotIn(note, runtime)
        legacy = fixture.EngineTestCase.prompts_prompt(self, state)
        self.assertNotIn(note, legacy)
        self.assertEqual(state.chat_notes, saved)
        self.persona.text = original_persona
        request = self.engine.context_request(state, fixture.SESSION, text="你在吗", rows=[])
        await self.engine.prepare_context_memory(request, state=state)
        _, runtime = self.engine.prompts.build_dialogue_prompt(state=state,
            node=self.engine.node(state.node_id), persona_text=self.persona.text,
            context_request=request)
        self.assertIn(note, runtime)

    async def test_live_snapshot_does_not_present_another_characters_note_as_current(self):
        async with self.engine.session_state(fixture.SESSION) as state:
            self.engine.note_chat_note(state, "爱丽丝刚才聊的风车", fixture.SESSION)
        self.persona.text = "你是另一位独立的角色。"

        snapshot = await self.engine.snapshot(fixture.SESSION)

        self.assertEqual("", snapshot["chat_note"])

    def test_foreign_note_never_resurrects_legacy_fallback(self):
        state = WorldState(fixture.SESSION, chat_note="旧版的聊天备忘",
            chat_notes={fixture.SESSION: {"text": "角色甲的备忘", "persona_key": "actor-a"}})

        self.assertEqual("", state.chat_note_text(fixture.SESSION, persona_key="actor-b"))
        self.assertEqual("角色甲的备忘", state.chat_note_text(fixture.SESSION, persona_key="actor-a"))

    def test_untagged_legacy_note_keeps_its_original_session_and_expiry(self):
        state = WorldState(fixture.SESSION, chat_notes={fixture.SESSION: {"text": "旧版的聊天备忘", "at": 1000}})

        self.assertEqual("旧版的聊天备忘", state.chat_note_text(fixture.SESSION,
            persona_key="current", max_minutes=30, now=1100))
        self.assertEqual("", state.chat_note_text(fixture.OTHER_SESSION, persona_key="current"))
        self.assertEqual("", state.chat_note_text(fixture.SESSION,
            persona_key="current", max_minutes=30, now=3000))


class PlanningInnerVoiceTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    async def test_chat_draft_does_not_become_shared_autonomous_intent(self):
        provider = enable_embedding(self.engine)
        async with self.engine.session_state(fixture.SESSION) as state:
            owner = self.engine._matter_persona_key(state)
            state.note_reasoning({"inner": "只在这次交谈里惦记那封来信", "intent": "回应来信的人"},
                                 source="reflect", session_id=fixture.SESSION, persona_key=owner)
            state.add_thought("对那次私下谈话的感受", dialogue_session=fixture.SESSION, persona_key=owner)
            state.thoughts[-1]["at"] = self.clock.now()
        state = await self.engine.load_state(fixture.SESSION, cold_start=False)
        request = self.engine.context_request(state, fixture.SESSION, task="plan",
                                              text="接下来做什么", rows=[])

        await self.engine.prepare_context_memory(request, state=state)
        prompt = self.engine.prompts.build_autonomous_system_prompt(state=state,
            node=self.engine.node(state.node_id), persona_text=self.persona.text,
            available_tools={}, context_request=request, mode="plan")
        embedded = "\n".join(text for call in provider.embed.await_args_list for text in call.args[0])

        for material in (embedded, prompt):
            self.assertNotIn("只在这次交谈里惦记那封来信", material)
            self.assertNotIn("对那次私下谈话的感受", material)
        reply_request = self.engine.context_request(state, fixture.SESSION, text="后来呢", rows=[])
        await self.engine.prepare_context_memory(reply_request, state=state)
        _, runtime = self.engine.prompts.build_dialogue_prompt(state=state,
            node=self.engine.node(state.node_id), persona_text=self.persona.text,
            context_request=reply_request)
        self.assertIn("只在这次交谈里惦记那封来信", runtime)

    async def test_shared_life_thought_still_supplies_autonomous_continuity(self):
        for source in ("plan", "reflect"):
            with self.subTest(source=source):
                async with self.engine.session_state(fixture.SESSION) as state:
                    owner = self.engine._matter_persona_key(state)
                    state.note_reasoning({"inner": "有点想继续修昨天的风车模型" if source == "plan"
                                          else "只属于原交谈的想法"}, source=source,
                                         session_id=fixture.SESSION, persona_key=owner)
                    state.add_thought("有点想继续修昨天的风车模型", persona_key=owner)
                request = self.engine.context_request(state, fixture.SESSION, task="plan",
                                                      text="风车模型", rows=[])
                prompt = self.engine.prompts.build_autonomous_system_prompt(state=state,
                    node=self.engine.node(state.node_id), persona_text=self.persona.text,
                    available_tools={}, context_request=request, mode="plan")

                self.assertIn("有点想继续修昨天的风车模型", prompt)
                self.assertNotIn("只属于原交谈的想法", prompt)
