"""Scoped semantic recall and candidate ranking through production entrypoints."""
from __future__ import annotations

import json
import types
import unittest
from unittest.mock import AsyncMock

from core.semantic_memory import cosine, revision
from tests import test_engine as fixture

SESSION = fixture.SESSION


class SemanticReviewTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    def enable_embedding(self):
        self.engine.world.memory.semantic_enabled = True
        self.provider = types.SimpleNamespace(
            describe=lambda *_: {"ready": True, "id": "synthetic", "fingerprint": "test"},
            embed=AsyncMock(side_effect=lambda texts, *_: [[1., 0.] for _ in texts]),
        )
        self.engine.memory.semantic.provider = self.provider
        return self.engine.memory.semantic

    def remember(self, text, **kwargs):
        mid = self.engine.memory.remember(session_id=SESSION, persona_id="",
                                          node_id="study", content=text, **kwargs)
        self.db.raw._execute("UPDATE node_memory SET created_at=? WHERE id=?", (self.clock.now(), mid))
        return mid

    async def test_context_filters_character_before_embedding(self):
        self.enable_embedding()
        _, owner = await self.engine._journal_persona(SESSION)
        self.remember("当前角色的薄荷", persona_key=owner)
        self.remember("另一角色的秘密", persona_key="foreign-character")
        state = await self.engine.load_state(SESSION, cold_start=False)
        request = self.engine.context_request(state, SESSION, text="薄荷")
        await self.engine.prepare_context_memory(request)
        texts = self.provider.embed.await_args.args[0]
        self.assertIn("当前角色的薄荷", texts)
        self.assertNotIn("另一角色的秘密", texts)

    async def test_repeated_query_continues_bounded_index_rebuild(self):
        semantic = self.enable_embedding()
        for number in range(45):
            self.remember(f"安静的生活片段编号{number:03d}")
        await semantic.prepare(SESSION, "", "回想风景")
        await semantic.prepare(SESSION, "", "回想风景")
        self.assertEqual(2, self.provider.embed.await_count)
        self.assertTrue(all(len(call.args[0]) <= 33 for call in self.provider.embed.await_args_list))
        self.assertEqual(45, len(semantic.cached(SESSION, "", "回想风景")))

    async def test_same_query_notices_new_memory_without_waiting_five_minutes(self):
        semantic = self.enable_embedding()
        self.remember("在窗前磨豆")
        await semantic.prepare(SESSION, "", "咖啡")
        added = self.remember("调试手冲壶")
        await semantic.prepare(SESSION, "", "咖啡")
        self.assertIn(added, semantic.cached(SESSION, "", "咖啡"))

    async def test_one_corrupt_vector_does_not_disable_valid_semantic_candidates(self):
        semantic = self.enable_embedding()
        good, bad = self.remember("在窗前磨豆"), self.remember("调试手冲壶")
        model = revision(semantic.model_identity())
        with self.db.raw.atomic():
            for mid, vector in ((good, "[1.0,0.0]"), (bad, "not-json")):
                row = self.db.raw.memory_by_id(mid)
                self.db.raw._execute(
                    "INSERT INTO memory_vector(memory_id,model_key,revision,vector) VALUES(?,?,?,?)",
                    (mid, model, revision(row["content"]), vector),
                )
        await semantic.prepare(SESSION, "", "咖啡")
        self.assertIn(good, semantic.cached(SESSION, "", "咖啡"))
        self.assertEqual(0, semantic.retry_at)

    async def test_candidate_rerank_marks_only_final_returned_memories(self):
        self.engine.world.memory.life_rerank = True
        ids = [self.remember(f"阅读不同的书，第{number}册") for number in range(8)]
        self.engine._ask_judge = AsyncMock(return_value=json.dumps({"ids": ids}))
        items, _ = await self.engine.lookup_life_memories(SESSION, "", "阅读")
        self.assertEqual(ids[:5], [item.id for item in items])
        counts = {mid: self.db.raw.memory_by_id(mid)["recall_count"] for mid in ids}
        self.assertEqual({mid: int(mid in ids[:5]) for mid in ids}, counts)

    async def test_context_ranking_uses_memory_weight_and_recall_penalty(self):
        weak = self.remember("薄荷盆放在窗边", weight=0.05)
        strong = self.remember("薄荷枝叶需要避开直晒", weight=0.9)
        _, owner = await self.engine._journal_persona(SESSION)
        with self.db.raw.atomic():
            self.db.raw._execute("UPDATE node_memory SET created_at=?", (self.clock.now(),))
        state = await self.engine.load_state(SESSION, cold_start=False)
        request = self.engine.context_request(state, SESSION, text="薄荷", persona="")
        request.life_persona = owner
        selected = self.engine.memory.recall_context(request, limit=1)
        self.assertEqual([strong], [item.id for item in selected])
        self.assertNotEqual(weak, strong)

    async def test_recent_recall_penalty_affects_actual_context_selection(self):
        first = self.remember("薄荷盆放在窗边", weight=0.5)
        second = self.remember("薄荷枝叶避开直晒", weight=0.5)
        with self.db.raw.atomic():
            self.db.raw._execute("UPDATE node_memory SET created_at=?", (self.clock.now(),))
        state = await self.engine.load_state(SESSION, cold_start=False)
        request = self.engine.context_request(state, SESSION, text="薄荷")
        selected = self.engine.memory.recall_context(request, limit=1)
        initial = selected[0].id
        self.db.raw.mark_recalled([initial], when=self.clock.now())
        selected = self.engine.memory.recall_context(request, limit=1)
        self.assertEqual([{first, second}.difference({initial}).pop()], [item.id for item in selected])

    async def test_unrelated_presets_do_not_fill_a_role_filtered_recall(self):
        self.remember("另一角色在窗台种薄荷", persona_key="foreign-character")
        self.remember("书桌前查过很多奇怪的问题", source="preset", weight=0.9)
        state = await self.engine.load_state(SESSION, cold_start=False)
        request = self.engine.context_request(state, SESSION, text="薄荷")
        await self.engine.prepare_context_memory(request)
        self.assertEqual([], self.engine.memory.recall_context(request))

    async def test_synonym_only_semantic_match_can_beat_recent_unrelated_memory(self):
        self.enable_embedding()
        self.provider.embed.side_effect = lambda texts, *_: [
            [1., 0.] if text in {"充电", "接上电源补足电量"} else [0., 1.] for text in texts]
        match = self.remember("接上电源补足电量")
        self.remember("窗前读小说")
        with self.db.raw.atomic():
            self.db.raw._execute("UPDATE node_memory SET created_at=?", (self.clock.now(),))
            self.db.raw._execute("UPDATE node_memory SET created_at=? WHERE id=?",
                                 (self.clock.now() - 86400 * 3, match))
        state = await self.engine.load_state(SESSION, cold_start=False)
        request = self.engine.context_request(state, SESSION, text="充电")
        await self.engine.prepare_context_memory(request)
        self.assertEqual([match], [item.id for item in self.engine.memory.recall_context(request, limit=1)])

    async def test_character_switch_does_not_reuse_other_characters_query_cache(self):
        self.enable_embedding()
        _, old = await self.engine._journal_persona(SESSION)
        old_id = self.remember("前一角色的薄荷", persona_key=old)
        state = await self.engine.load_state(SESSION, cold_start=False)
        request = self.engine.context_request(state, SESSION, text="薄荷")
        await self.engine.prepare_context_memory(request)
        self.persona.text = "现在是另一个角色。"
        _, new = await self.engine._journal_persona(SESSION)
        new_id = self.remember("当前角色的薄荷", persona_key=new)
        request = self.engine.context_request(state, SESSION, text="薄荷")
        await self.engine.prepare_context_memory(request)
        self.assertEqual([new_id], [item.id for item in self.engine.memory.recall_context(request)])
        self.assertNotIn(old_id, self.engine.memory.semantic.cached(SESSION, "", "薄荷", automatic=True,
                                                                   life_persona_key=new))
        self.assertNotIn("前一角色的薄荷", self.provider.embed.await_args.args[0])

    async def test_finished_index_reuses_cache_and_corrupt_entry_is_rebuilt(self):
        semantic = self.enable_embedding()
        mid = self.remember("接上电源")
        await semantic.prepare(SESSION, "", "电量")
        await semantic.prepare(SESSION, "", "电量")
        self.assertEqual(1, self.provider.embed.await_count)
        self.db.raw._execute("UPDATE memory_vector SET vector='not-json' WHERE memory_id=?", (mid,))
        await semantic.prepare(SESSION, "", "另一查询")
        await semantic.prepare(SESSION, "", "另一查询")
        self.assertIn(mid, semantic.cached(SESSION, "", "另一查询"))
        self.assertEqual(3, self.provider.embed.await_count)

    async def test_rerank_sees_context_and_does_not_count_changed_candidates(self):
        self.engine.world.memory.life_rerank = True
        first = self.remember("阅读札记第一册", context="关于如何修理风车")
        second = self.remember("阅读札记第二册", context="关于料理")

        async def judge(_session, _system, prompt):
            candidates = json.loads(prompt)["candidates"]
            self.assertIn("关于如何修理风车", str(candidates))
            self.db.raw.update_memory(first, content="已经修订的新札记")
            return json.dumps({"ids": [first, second, second, True, 999999]})

        self.engine._ask_judge = AsyncMock(side_effect=judge)
        items, _ = await self.engine.lookup_life_memories(SESSION, "", "阅读")
        self.assertEqual([second], [item.id for item in items])
        self.assertEqual(0, self.db.raw.memory_by_id(first)["recall_count"])
        self.assertEqual(1, self.db.raw.memory_by_id(second)["recall_count"])


class CosineBoundaryTests(unittest.TestCase):
    def test_finite_extreme_vectors_remain_stable(self):
        self.assertAlmostEqual(1., cosine([1e308, 1e308], [1e308, 1e308]))
        self.assertAlmostEqual(1., cosine([1e-308, 1e-308], [1e-308, 1e-308]))
        self.assertAlmostEqual(-1., cosine([1., 1.], [-1., -1.]))
        for bad in ([float("nan"), 0.], [True, 0.], ["1", 0.], []):
            with self.subTest(vector=bad):
                self.assertEqual(0, cosine([1., 0.], bad))
