"""Scoped semantic recall and candidate ranking through production entrypoints."""
from __future__ import annotations

import json
import types
import unittest
from unittest.mock import AsyncMock, Mock

from core.semantic_memory import cosine, revision
from core.memory import SCENE, EVENT, INNER, DREAM, SCOPE_NODE
from core.context_selection import context_identity, select_context
from core.context_sources import life_items
from core.life_matters import make
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

    async def test_unavailable_provider_description_cannot_break_lexical_context(self):
        self.enable_embedding()
        self.provider.describe = Mock(side_effect=RuntimeError("provider is reloading"))
        match = self.remember("窗台的薄荷要浇水")
        state = await self.engine.load_state(SESSION, cold_start=False)
        request = self.engine.context_request(state, SESSION, text="薄荷", rows=[])
        await self.engine.prepare_context_memory(request, state=state)
        self.assertIn(match, [item.id for item in self.engine.context_memories(state, request)])
        self.provider.embed.assert_not_awaited()

    async def test_context_query_and_speakers_share_one_bounded_embedding_batch(self):
        self.enable_embedding()
        self.remember("一段关于咖啡的往事")
        state = await self.engine.load_state(SESSION, cold_start=False)
        rows = [
            {"origin": SESSION, "user_id": "42", "text": "想喝咖啡", "raw_text": "想喝咖啡"},
            {"origin": SESSION, "user_id": "84", "text": "我记得上次那家店", "raw_text": "我记得上次那家店"},
        ]
        request = self.engine.context_request(state, SESSION, task="reply", rows=rows,
                                              text="今天还想去", focus="42")
        await self.engine.prepare_context_memory(request)
        self.assertEqual(1, self.provider.embed.await_count)
        embedded = self.provider.embed.await_args.args[0]
        self.assertEqual(4, len(embedded))
        self.assertIn("想喝咖啡", embedded)
        self.assertIn("我记得上次那家店", embedded)
        await self.engine.prepare_context_memory(request)
        self.assertEqual(1, self.provider.embed.await_count)

    async def test_multiline_query_uses_the_same_cache_key_during_actual_recall(self):
        semantic = self.enable_embedding()
        match = self.remember("接上电源补足电量")
        state = await self.engine.load_state(SESSION, cold_start=False)
        request = self.engine.context_request(state, SESSION, text="先看看近况\n再给电池充电", rows=[])
        await self.engine.prepare_context_memory(request)
        self.assertIn(match, semantic.cached(SESSION, "", request.query, automatic=True,
                                            life_persona_key=request.life_persona, node_id=request.node_id))

    async def test_specialized_tasks_still_prepare_character_ownership_without_embedding(self):
        self.enable_embedding()
        state = await self.engine.load_state(SESSION, cold_start=False)
        _, owner = await self.engine._journal_persona(SESSION)
        for task in ("sleep", "diary", "review", "generation"):
            with self.subTest(task=task):
                request = self.engine.context_request(state, SESSION, task=task, text="自己的生活", rows=[])
                await self.engine.prepare_context_memory(request)
                self.assertEqual(owner, request.life_persona)
        self.provider.embed.assert_not_awaited()

    async def test_relevance_check_keeps_latest_query_and_reuses_the_short_lived_cache(self):
        semantic = self.enable_embedding()
        query = "之前的话题" * 600 + "后来怎么样了"
        self.assertTrue(await semantic.related(SESSION, query, ["与朋友一起收好茶具"]))
        embedded_query = self.provider.embed.await_args.args[0][0]
        self.assertEqual(query[-2000:], embedded_query)
        self.assertTrue(await semantic.related(SESSION, query, ["与朋友一起收好茶具"]))
        self.assertEqual(1, self.provider.embed.await_count)

    async def test_all_durable_life_memory_types_share_the_semantic_index(self):
        semantic = self.enable_embedding()
        rows = [self.remember(f"{kind} 记忆内容", memory_type=kind)
                for kind in (SCENE, EVENT, INNER, DREAM)]
        await semantic.prepare(SESSION, "", "回想生活")
        indexed = {int(row["memory_id"]) for row in self.db.raw._query("SELECT memory_id FROM memory_vector")}
        self.assertEqual(set(rows), indexed)

    async def test_semantic_only_memory_survives_the_final_runtime_selection(self):
        self.enable_embedding()
        self.provider.embed.side_effect = lambda texts, *_: [
            [1., 0.] if text in {"充电", "接上电源补足电量"} else [0., 1.] for text in texts]
        match = self.remember("接上电源补足电量")
        self.db.raw._execute("UPDATE node_memory SET created_at=? WHERE id=?",
                             (self.clock.now() - 86400 * 3, match))
        state = await self.engine.load_state(SESSION, cold_start=False)
        request = self.engine.context_request(state, SESSION, text="充电", rows=[])
        await self.engine.prepare_context_memory(request, state=state)
        memories = self.engine.context_memories(state, request)
        _, runtime = self.engine.prompts.build_dialogue_prompt(
            state=state, node=self.engine.node(state.node_id), persona_text=self.persona.text,
            memories=memories, context_request=request)
        self.assertIn("接上电源补足电量", runtime)
        self.assertIn("语义相关", str(request.trace["background"]))

    async def test_context_only_matters_and_thoughts_use_one_cached_embedding_batch(self):
        self.enable_embedding()
        self.provider.embed.side_effect = lambda texts, *_: [
            [1., 0.] if "电源" in text or text == "充电" else [0., 1.] for text in texts]
        state = await self.engine.load_state(SESSION, cold_start=False)
        _, owner = await self.engine._journal_persona(SESSION)
        match = make("接上电源补足电量", now=self.clock.now() - 86400 * 3,
                     session=SESSION, persona_key=owner)
        match.update(status="paused", next_step="先检查接口", obstacle="等适配器")
        state.life_matters = [match, make("读完窗前的小说", now=self.clock.now(),
            session=SESSION, persona_key=owner)]
        state.thoughts = [{"content": "电源接口需要再看一眼", "at": self.clock.now(), "persona_key": owner}]
        request = self.engine.context_request(state, SESSION, text="充电", rows=[])
        await self.engine.prepare_context_memory(request, state=state)
        self.assertEqual(1, self.provider.embed.await_count)
        self.assertTrue(request.semantic_context)
        items = life_items(state, request)
        matter = next(item for item in items if item.key == "matter:" + match["id"])
        self.assertTrue(any(item.kind == "thought" and context_identity(item) in request.semantic_context for item in items))
        selected = select_context(request, [item for item in items if item.kind == "matter"],
                                  budget=len(matter.summary) + 2)
        self.assertIn("接上电源补足电量", selected.render())
        self.assertIn("目前未执行", selected.render())
        self.assertNotIn("窗前的小说", selected.render())
        self.assertEqual([], self.db.raw._query("SELECT * FROM memory_vector"))
        await self.engine.prepare_context_memory(request, state=state)
        self.assertEqual(1, self.provider.embed.await_count)
        match["text"] = "已经改成别的生活目标"
        new_items = life_items(state, request)
        new_matter = next(item for item in new_items if item.key == matter.key)
        self.assertNotIn(context_identity(new_matter), request.semantic_context)
        await self.engine.prepare_context_memory(request, state=state)
        self.assertEqual(2, self.provider.embed.await_count)
        self.assertNotIn("充电", self.provider.embed.await_args.args[0])

    async def test_semantic_context_includes_events_due_topics_inner_voice_and_chat_note(self):
        self.enable_embedding()
        state = await self.engine.load_state(SESSION, cold_start=False)
        _, owner = await self.engine._journal_persona(SESSION)
        now = self.clock.now()
        state.recent_events = [
            {"kind": "event", "detail": {"title": "旧钟的齿轮卡住了"}, "at": now, "persona_key": owner},
            {"kind": "event", "detail": {"title": "另一角色的秘密"}, "at": now,
             "persona_key": "foreign-character"},
            {"kind": "conversation", "detail": {"title": "没有发生的闲聊"}, "at": now,
             "persona_key": owner},
        ]
        state.open_topics = [
            {"text": "上次说的相机送修结果", "who": "42", "who_name": "小明", "session": SESSION,
             "persona_key": owner, "at": now - 3600, "next_ask_at": now - 1},
            {"text": "其他会话的私事", "who": "42", "session": fixture.OTHER_SESSION,
             "persona_key": owner, "at": now - 3600, "next_ask_at": now - 1},
        ]
        state.note_reasoning({"mood": "惦记", "inner": "旧钟还没有修好"},
                            source="reply", session_id=SESSION, persona_key=owner)
        state.chat_notes[SESSION] = {"text": "正在聊小镇的旧钟", "at": now}
        request = self.engine.context_request(state, SESSION, text="旧钟后来怎样了", rows=[], focus="42")

        await self.engine.prepare_context_memory(request, state=state)

        embedded = "\n".join(self.provider.embed.await_args.args[0])
        for visible in ("旧钟的齿轮卡住了", "相机送修结果", "旧钟还没有修好", "正在聊小镇的旧钟"):
            self.assertIn(visible, embedded)
        for hidden in ("另一角色的秘密", "没有发生的闲聊", "其他会话的私事"):
            self.assertNotIn(hidden, embedded)

    async def test_repeated_context_preparation_skips_cached_cosine_scoring(self):
        semantic = self.enable_embedding()
        self.remember("留作语义评分基准的生活记忆")
        state = await self.engine.load_state(SESSION, cold_start=False)
        _, owner = await self.engine._journal_persona(SESSION)
        state.life_matters = [make("想继续修旧钟", now=self.clock.now(), session=SESSION,
                                   persona_key=owner)]
        request = self.engine.context_request(state, SESSION, text="旧钟维修", rows=[])
        await self.engine.prepare_context_memory(request, state=state)
        self.assertEqual(1, self.provider.embed.await_count)

        from unittest.mock import patch
        with patch("core.semantic_memory.cosine", wraps=cosine) as score:
            await self.engine.prepare_context_memory(request, state=state)

        self.assertEqual(0, score.call_count)
        self.assertEqual(1, self.provider.embed.await_count)
        self.assertTrue(semantic.cached_context(SESSION, "", [request.query],
                                                life_persona_key=request.life_persona,
                                                node_id=request.node_id))

    async def test_context_embedding_excludes_foreign_private_and_expired_sources(self):
        self.enable_embedding()
        state = await self.engine.load_state(SESSION, cold_start=False)
        _, owner = await self.engine._journal_persona(SESSION)
        state.life_matters = [
            make("visible life project", now=self.clock.now(), session=SESSION, persona_key=owner),
            make("foreign persona secret", now=self.clock.now(), session=SESSION, persona_key="foreign"),
            make("another speaker promise", now=self.clock.now(), session=SESSION, persona_key=owner, who="999"),
        ]
        state.thoughts = [
            {"content": "expired thought", "at": self.clock.now() - 1000,
             "expires_at": self.clock.now() - 1, "persona_key": owner},
            {"content": "another chat feeling", "at": self.clock.now(),
             "dialogue_session": fixture.OTHER_SESSION, "persona_key": owner},
        ]
        request = self.engine.context_request(state, SESSION, text="回想进展", rows=[], focus="42")
        await self.engine.prepare_context_memory(request, state=state)
        embedded = "\n".join(self.provider.embed.await_args.args[0])
        self.assertIn("visible life project", embedded)
        for hidden in ("foreign persona secret", "another speaker promise", "expired thought", "another chat feeling"):
            self.assertNotIn(hidden, embedded)

    async def test_profile_fact_ranking_uses_semantics_only_for_the_actual_speaker(self):
        self.enable_embedding()
        self.provider.embed.side_effect = lambda texts, *_: [
            [1., 0.] if "电源" in text or text == "充电" else [0., 1.] for text in texts]
        self.engine.profiles.touch(SESSION, "42", name="当前说话人", now=self.clock.now())
        self.engine.profiles.note_fact(SESSION, "42", text="出门需要携带电源适配器", kind="经历")
        self.engine.profiles.touch(SESSION, "999", name="不在场的人", now=self.clock.now())
        self.engine.profiles.note_fact(SESSION, "999", text="不在场人的私密往事", kind="经历")
        state = await self.engine.load_state(SESSION, cold_start=False)
        request = self.engine.context_request(state, SESSION, text="充电", rows=[], focus="42")
        await self.engine.prepare_context_memory(request, state=state)
        embedded = self.provider.embed.await_args.args[0]
        self.assertIn("出门需要携带电源适配器", embedded)
        self.assertNotIn("不在场人的私密往事", embedded)
        profile = self.engine.context_profiles(state, request)
        self.assertIn("出门需要携带电源适配器", profile)
        self.assertIn("语义相关", str(request.trace["profile:42"]))

    async def test_current_node_memories_are_indexed_and_cache_is_node_scoped(self):
        semantic = self.enable_embedding()
        study = self.engine.memory.remember(session_id=SESSION, persona_id="", node_id="study",
            scope=SCOPE_NODE, content="study node only: a windmill diary")
        kitchen = self.engine.memory.remember(session_id=SESSION, persona_id="", node_id="kitchen",
            scope=SCOPE_NODE, content="kitchen node only: a kettle diary")
        state = await self.engine.load_state(SESSION, cold_start=False)
        state.node_id = "study"
        request = self.engine.context_request(state, SESSION, text="windmill memory", rows=[])
        await self.engine.prepare_context_memory(request)
        self.assertIn(study, semantic.cached(SESSION, "", request.query, automatic=True,
                                             life_persona_key=request.life_persona, node_id="study"))
        self.assertNotEqual(
            semantic._key(SESSION, "", request.query, automatic=True,
                          life_persona_key=request.life_persona, node_id="study"),
            semantic._key(SESSION, "", request.query, automatic=True,
                          life_persona_key=request.life_persona, node_id="kitchen"),
        )
        self.assertNotIn(kitchen, [item.id for item in self.engine.context_memories(state, request)])
        self.assertIn(study, {int(row["memory_id"]) for row in self.db.raw._query(
            "SELECT memory_id FROM memory_vector")})

    async def test_automatic_embedding_filters_private_memories_for_other_people(self):
        semantic = self.enable_embedding()
        private = self.remember("another person's private promise", source="life_matter",
                                participants=["999"], related_users=["999"])
        visible = self.remember("this speaker's unfinished promise", source="life_matter",
                                participants=["42"], related_users=["42"])
        state = await self.engine.load_state(SESSION, cold_start=False)
        request = self.engine.context_request(state, SESSION, text="what happened next",
                                              rows=[], focus="42")
        await self.engine.prepare_context_memory(request)
        embedded = self.provider.embed.await_args.args[0]
        self.assertNotIn("another person's private promise", embedded)
        self.assertIn("this speaker's unfinished promise", embedded)
        indexed = {int(row["memory_id"]) for row in self.db.raw._query("SELECT memory_id FROM memory_vector")}
        self.assertNotIn(private, indexed)
        self.assertIn(visible, indexed)

    async def test_query_vectors_are_reused_when_new_memory_arrives(self):
        semantic = self.enable_embedding()
        self.remember("第一次散步")
        await semantic.prepare(SESSION, "", "回想户外")
        added = self.remember("后来的海边散步")
        await semantic.prepare(SESSION, "", "回想户外")
        self.assertNotIn("回想户外", self.provider.embed.await_args.args[0])
        self.assertIn("后来的海边散步", self.provider.embed.await_args.args[0])
        self.assertIn(added, semantic.cached(SESSION, "", "回想户外"))

    async def test_new_memory_indexed_by_another_query_refreshes_existing_scores_without_network(self):
        semantic = self.enable_embedding()
        self.remember("第一次散步")
        await semantic.prepare(SESSION, "", "回想户外")
        added = self.remember("后来的海边散步")
        await semantic.prepare(SESSION, "", "回想海边")
        calls = self.provider.embed.await_count
        await semantic.prepare(SESSION, "", "回想户外")
        self.assertIn(added, semantic.cached(SESSION, "", "回想户外"))
        self.assertEqual(calls, self.provider.embed.await_count)

    async def test_speaker_recall_reuses_the_queries_prepared_on_the_request(self):
        self.enable_embedding()
        self.provider.embed.side_effect = lambda texts, *_: [
            [1., 0.] if text in {"接上电源补足电量", "想充电"} else [0., 0.] for text in texts]
        match = self.remember("接上电源补足电量")
        self.remember("其他话题")
        self.db.raw._execute("UPDATE node_memory SET created_at=? WHERE id=?",
                             (self.clock.now() - 86400 * 3, match))
        state = await self.engine.load_state(SESSION, cold_start=False)
        request = self.engine.context_request(state, SESSION, text="别的事", rows=[])
        request.people_queries = {"42": "想充电"}
        await self.engine.prepare_context_memory(request)
        selected = self.engine.context_memories(state, request)
        self.assertIn(match, [item.id for item in selected])

    async def test_long_context_query_keeps_latest_text_with_a_fixed_embedding_limit(self):
        semantic = self.enable_embedding()
        self.remember("保存一条用于检索的生活记忆")
        query = "旧对话" * 1000 + "最新问题是风车去哪了"
        await semantic.prepare(SESSION, "", query)
        embedded_query = self.provider.embed.await_args.args[0][0]
        self.assertLessEqual(len(embedded_query), 2000)
        self.assertTrue(embedded_query.endswith("最新问题是风车去哪了"))

    async def test_repeated_query_continues_bounded_index_rebuild(self):
        semantic = self.enable_embedding()
        for number in range(45):
            self.remember(f"安静的生活片段编号{number:03d}")
        await semantic.prepare(SESSION, "", "回想风景")
        await semantic.prepare(SESSION, "", "回想风景")
        self.assertEqual(2, self.provider.embed.await_count)
        self.assertTrue(all(len(call.args[0]) <= 33 for call in self.provider.embed.await_args_list))
        self.assertEqual(45, len(semantic.cached(SESSION, "", "回想风景")))

    async def test_durable_and_temporary_sources_share_the_rebuild_budget(self):
        semantic = self.enable_embedding()
        for number in range(45):
            self.remember(f"持久生活片段 {number:03d}")
        sources = {f"matter:{number}": f"阶段正文 {number:03d}" for number in range(40)}
        for _ in range(3):
            await semantic.prepare_many(SESSION, "", ["回想生活"], automatic=True,
                                         life_persona_key="", context_texts=sources)
        self.assertTrue(all(len(call.args[0]) <= 33 for call in self.provider.embed.await_args_list))
        self.assertEqual(45, len(self.db.raw._query("SELECT * FROM memory_vector")))
        self.assertEqual(40, len(semantic.cached_context(SESSION, "", ["回想生活"])))

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
