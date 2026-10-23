"""Ordinary activities and dreams retain their actual character, end to end."""
import json
import time
import unittest
from hashlib import sha256
from unittest.mock import AsyncMock

from core.context_sources import life_memory_metadata
from core.engine import TickOutcome
from core.json_actions import PlannedAction
from core.state import WorldState
from tests import test_consolidate as consolidate
from tests import test_life_audit as audit

S = audit.S


class ActivityMemoryContinuityTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = audit.LifeAuditTests.asyncSetUp
    idle = audit.LifeAuditTests.idle

    async def think(self, text):
        await self.idle()
        async with self.engine.session_state(S) as state:
            outcome = TickOutcome(session_id=S)
            self.assertTrue(await self.engine._execute_actions(state, self.engine.node("study"), outcome,
                [PlannedAction(type="think", content=text)], depth=0, autonomous=True))
            self.assertEqual(outcome.messages, [])

    async def test_actual_think_action_is_not_recalled_as_another_characters_thought(self):
        await self.think("旧角色想把风车涂成蓝色")
        self.persona.text = "另一位完全不同的园艺少女。"
        items, _ = await self.engine.lookup_life_memories(S, "", "风车")
        self.assertEqual(items, [])

    async def test_same_thought_by_two_characters_keeps_two_independent_records(self):
        old_key = sha256(self.persona.text.encode()).hexdigest()[:24]
        await self.think("想慢慢读完这本书")
        self.persona.text = "另一位完全不同的园艺少女。"
        await self.think("想慢慢读完这本书")
        rows = self.engine.memory.db.query_memories(session_id=S, memory_type="inner")
        self.assertEqual(len(rows), 2)
        self.assertEqual({life_memory_metadata(row["source"], row["context"]).get("persona_key") for row in rows},
            {old_key, sha256(self.persona.text.encode()).hexdigest()[:24]})

    async def test_repeated_thought_by_same_character_still_uses_existing_record(self):
        await self.think("想慢慢读完这本书")
        await self.think("想慢慢读完这本书")
        self.assertEqual(len(self.engine.memory.db.query_memories(session_id=S, memory_type="inner")), 1)

    async def test_continuous_activity_remembers_the_character_that_started_it(self):
        await self.idle()
        original = self.persona.text
        async with self.engine.session_state(S) as state:
            await self.engine._execute_actions(state, self.engine.node("study"), TickOutcome(session_id=S),
                [PlannedAction(type="read", duration=60)], depth=0, autonomous=True)
            action = dict(state.current_action)
        self.assertEqual(action.get("persona_key"), sha256(original.encode()).hexdigest()[:24])
        self.persona.text = "另一位完全不同的园艺少女。"
        action["silent_followup"] = True
        async with self.engine.session_state(S) as state:
            # Test late memory ownership independently: execution now correctly
            # refuses to complete another character's running activity.
            await self.engine._remember_action(state, self.engine.world.action_map()["read"], action)
        items, _ = await self.engine.lookup_life_memories(S, "", "看书")
        self.assertEqual(items, [])
        self.persona.text = original
        items, _ = await self.engine.lookup_life_memories(S, "", "看书")
        self.assertTrue(items)

    async def test_completed_activity_does_not_enter_another_characters_automatic_context(self):
        self.clock._now = time.time()  # Activity rows and context use the same time domain.
        await self.idle()
        async with self.engine.session_state(S) as state:
            await self.engine._finish_action(state, self.engine.node("study"), TickOutcome(session_id=S),
                {"type": "read", "duration_ticks": 1, "elapsed_ticks": 1, "silent_followup": True})
            request = self.engine.context_request(state, S, text="看书", rows=[])
            request.now = time.time()
            await self.engine.prepare_context_memory(request)
            self.assertTrue(self.engine.context_memories(state, request))
        self.persona.text = "另一位完全不同的园艺少女。"
        async with self.engine.session_state(S) as state:
            request = self.engine.context_request(state, S, text="看书", rows=[])
            request.now = time.time()
            await self.engine.prepare_context_memory(request)
            self.assertEqual(self.engine.context_memories(state, request), [])

    async def test_other_characters_fleeting_idea_is_not_expired_in_the_current_life(self):
        old_key = sha256(self.persona.text.encode()).hexdigest()[:24]
        async with self.engine.session_state(S) as state:
            idea = self.engine.add_life_matter(state, "想画一幅蓝色风车", kind="idea", persona_key=old_key)
            old_id, old_at = idea["id"], idea["at"]
        self.persona.text = "另一位完全不同的园艺少女。"
        async with self.engine.session_state(S) as state:
            self.engine._decay_own_topics(state, now=old_at + 31 * 86400)
            self.assertNotEqual(next(row for row in state.life_matters if row["id"] == old_id)["status"], "expired")
        self.persona.text = self.persona.text + "仍然是新角色。"
        items, _ = await self.engine.lookup_life_memories(S, "", "蓝色风车")
        self.assertEqual(items, [])

    async def test_expired_idea_archive_keeps_its_matter_link_and_character(self):
        key = sha256(self.persona.text.encode()).hexdigest()[:24]
        async with self.engine.session_state(S) as state:
            idea = self.engine.add_life_matter(state, "想画一幅蓝色风车", kind="idea", persona_key=key)
            self.engine._decay_own_topics(state, now=idea["at"] + 31 * 86400)
        row = self.engine.memory.db.query_memories(session_id=S, memory_type="inner")[0]
        self.assertEqual(row["source"], "life_matter")
        self.assertEqual(life_memory_metadata(row["source"], row["context"]).get("matter_id"), idea["id"])
        self.assertEqual(life_memory_metadata(row["source"], row["context"]).get("persona_key"), key)

    async def test_detached_state_without_current_character_cannot_expire_tagged_work(self):
        state = WorldState(S, node_id="study")
        await self.engine.note_own_topic(state, "想画一幅蓝色风车")
        state.life_matters[0]["kind"] = "idea"
        self.engine._decay_own_topics(state, now=state.life_matters[0]["at"] + 31 * 86400)
        self.assertNotEqual(state.life_matters[0]["status"], "expired")


class DreamMemoryContinuityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = consolidate.ConsolidateTestCase.asyncSetUp

    async def dream(self, persona):
        self.memory.remember(session_id=consolidate.SESSION, persona_id="", node_id="study",
            content="今天读了风车的故事", source="manual")
        result = await self.consolidator.run(session_id=consolidate.SESSION, persona_text=persona,
            llm=AsyncMock(return_value=json.dumps({"dream": "风车在云间转动"}, ensure_ascii=False)))
        self.assertTrue(result.ok)
        self.assertEqual(result.dream, "风车在云间转动")
        return self.db.query_memories(session_id=consolidate.SESSION, memory_type="dream")[0]

    async def test_sleep_review_dream_has_character_provenance_and_readable_original(self):
        persona = "修理风车的少女。"
        row = await self.dream(persona)
        meta = life_memory_metadata(row["source"], row["context"])
        self.assertEqual(meta.get("persona_key"), sha256(persona.encode()).hexdigest()[:24])
        rendered = self.memory._to_recalled(1, row).render()
        self.assertIn("风车在云间转动", rendered)
        self.assertIn("梦境，非现实经历", rendered)
        self.assertNotIn("persona_key", rendered)

    async def test_dream_is_recalled_only_by_its_original_character(self):
        persona = "修理风车的少女。"
        row = await self.dream(persona)
        for current, expected in (("另一位园艺少女。", False), (persona, True)):
            with self.subTest(persona=current):
                items = self.memory.recall(session_id=consolidate.SESSION, persona_id="", keyword="云间",
                    mode="group", include_reading=True, life_persona_key=sha256(current.encode()).hexdigest()[:24])
                self.assertEqual(row["id"] in [item.id for item in items], expected)

    def test_existing_unmarked_dreams_remain_compatible(self):
        self.memory.remember(session_id=consolidate.SESSION, persona_id="", node_id="study",
            content="做了个梦：旧版的月亮", memory_type="dream", source="dream", context="旧版的月亮")
        items = self.memory.recall(session_id=consolidate.SESSION, persona_id="", mode="group",
            life_persona_key="current_character", now=time.time())
        self.assertTrue(any("旧版的月亮" in item.content for item in items))

    async def test_sleep_review_uses_current_character_activity_before_candidate_limit(self):
        persona = "园艺少女。"
        key = sha256(persona.encode()).hexdigest()[:24]
        for index in range(125):
            self.memory.remember(session_id=consolidate.SESSION, persona_id="", node_id="study",
                content=f"旧角色的风车笔记{index}", persona_key="old_character")
        self.memory.remember(session_id=consolidate.SESSION, persona_id="", node_id="study",
            content="新角色的园艺笔记", persona_key=key)
        result = await self.consolidator.run(session_id=consolidate.SESSION, persona_text=persona,
            llm=AsyncMock(return_value="{}"), dry_run=True)
        self.assertIn("新角色的园艺笔记", str(result.preview))
        self.assertNotIn("旧角色的风车笔记", str(result.preview))

    def test_fold_preserves_long_original_and_character_metadata(self):
        original = "旧风车的读书笔记。" * 90 + "结尾仍有疑问。"
        mid = self.memory.remember(session_id=consolidate.SESSION, persona_id="", node_id="study",
            content=original, persona_key="old_character")
        self.memory.fold(mid, text="风车的笔记还没有确认完")
        row = self.db.memory_by_id(mid)
        meta = life_memory_metadata(row["source"], row["context"])
        self.assertEqual(meta.get("persona_key"), "old_character")
        self.assertEqual(meta.get("original"), original)
        self.assertNotIn("persona_key", self.memory._to_recalled(1, row).render())
        self.assertEqual(self.memory.recall(session_id=consolidate.SESSION, persona_id="", mode="group",
            life_persona_key="new_character"), [])

    def test_old_plain_runtime_context_is_still_rendered_as_text(self):
        mid = self.memory.remember(session_id=consolidate.SESSION, persona_id="", node_id="study",
            content="旧版读书要点", context="原先那段普通文字", tier="gist")
        row = self.db.memory_by_id(mid)
        self.assertIn("原先那段普通文字", self.memory._to_recalled(1, row).render())

    def test_matter_gist_does_not_treat_internal_metadata_as_original_experience(self):
        mid = self.memory.remember(session_id=consolidate.SESSION, persona_id="", node_id="study",
            content="风车的草图还没有画完", source="life_matter", tier="gist",
            context=json.dumps({"persona_key": "internal_character_key", "matter_id": "internal_matter_key",
                                "goal": "画一幅蓝色风车"}))
        text = self.memory._to_recalled(1, self.db.memory_by_id(mid)).render()
        self.assertIn("风车的草图还没有画完", text)
        self.assertNotIn("persona_key", text)
        self.assertNotIn("internal_matter_key", text)

