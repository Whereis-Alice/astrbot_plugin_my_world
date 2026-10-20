"""Behavioral regressions for blind evaluation, scoped recall and reversible learning."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from tests import (
    test_web_routes as _web_fixture,
)  # Install the host fixture before request types import.

from core.db import Database
from core.defaults import default_world
from core.evidence import direct_text, source_snapshot
from core.life_policy import choose_life
from core.memory import MemoryEngine, SCENE
from core.models import parse_world
from core.prompt import PromptBuilder
from core.request_context import _split_prompt_injection
from core.state import WorldState
from tests import test_engine as fixture
from tests import test_consolidate as consolidate_fixture

S = fixture.SESSION


class SocialStoreTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.path = Path(root.name) / "test.db"
        self.db = Database(self.path)
        self.addCleanup(self.db.close)

    def record(self, key="a", user="42", session=S, delta=0.1):
        evidence = [
            source_snapshot(
                {"user_id": user, "text": "谢谢你认真听我说", "message_id": key}, session
            )
        ]
        return self.db.record_interaction(session, user, key, delta, "认真倾听", evidence, now=100)

    def test_replay_and_overlapping_source_are_not_recounted(self):
        self.assertTrue(self.record())
        self.assertFalse(self.record())
        source = source_snapshot(
            {"user_id": "42", "text": "谢谢你认真听我说", "message_id": "a"}, S
        )
        self.assertFalse(
            self.db.record_interaction(
                S, "42", "different-batch", 0.2, "重复来源", [source], now=200
            )
        )
        self.assertEqual(len(self.db.interaction_detail(S, "42")["history"]), 1)

    def test_same_message_in_other_conversation_is_independent(self):
        self.assertTrue(self.record())
        self.assertTrue(self.record(session="other"))
        self.assertEqual(len(self.db.interaction_detail("other", "42")["history"]), 1)

    def test_neutral_turn_has_receipt_but_does_not_trigger_attitude(self):
        for i in range(8):
            self.record(str(i), delta=0)
        self.assertEqual(self.db.pending_interaction_styles(S, 100), [])
        self.assertFalse(self.record("0"))

    def test_attitude_threshold_backoff_unchanged_cursor_and_stale_edit(self):
        for i in range(5):
            self.record(str(i))
        self.assertEqual(len(self.db.pending_interaction_styles(S, 100)), 1)
        self.db.defer_interaction_style(S, "42", 100)
        self.assertEqual(self.db.pending_interaction_styles(S, 399), [])
        detail = self.db.interaction_detail(S, "42")
        through = detail["history"][0]["id"]
        self.assertTrue(self.db.save_interaction_style(S, "42", [], revision=0, through=through))
        self.assertEqual(self.db.pending_interaction_styles(S, 500), [])
        self.assertFalse(
            self.db.save_interaction_style(S, "42", [{"text": "旧请求覆盖"}], revision=0)
        )

    def test_manual_style_and_vocabulary_survive_automatic_updates(self):
        self.assertTrue(self.db.save_interaction_style(S, "42", [], revision=0, manual=True))
        self.assertFalse(
            self.db.save_interaction_style(S, "42", [{"text": "自动覆盖"}], revision=1)
        )
        self.assertTrue(self.db.set_vocabulary(S, "夜航", "读书闲聊", manual=True))
        self.assertFalse(self.db.set_vocabulary(S, "夜航", "改变身份", evidence=[{"text": "x"}]))
        self.assertEqual(self.db.vocabulary("other"), [])
        self.db.set_vocabulary(S, "夜航", "读书闲聊", manual=True, status="disabled")
        self.assertFalse(self.db.set_vocabulary(S, "夜航", "重新学习", evidence=[{"text": "x"}]))

    def test_manual_correction_and_disable_preserve_original_vocabulary_source(self):
        evidence = [{"text": "夜航就是晚上一起安静读书", "user_id": "42"}]
        self.db.set_vocabulary(S, "夜航", "读书", evidence=evidence)
        self.db.set_vocabulary(S, "夜航", "一起读书和闲聊", manual=True)
        self.db.set_vocabulary(S, "夜航", "一起读书和闲聊", manual=True, status="disabled")
        self.assertEqual(self.db.vocabulary(S)[0]["evidence"], evidence)

    def test_backlog_reads_oldest_batch_without_skipping_evaluations(self):
        for i in range(61):
            self.record(str(i))
        first = self.db.pending_interactions(S, "42", 0)
        self.assertEqual(len(first), 50)
        self.assertEqual(first[0]["source_key"], "0")
        self.db.save_interaction_style(S, "42", [], revision=0, through=first[-1]["id"])
        remaining = self.db.pending_interactions(S, "42", first[-1]["id"])
        self.assertEqual([r["source_key"] for r in remaining], [str(i) for i in range(50, 61)])

    def test_pruning_preserves_receipts_current_evidence_and_unprocessed_work(self):
        for i in range(550):
            self.record(str(i))
        self.assertEqual(len(self.db._query("SELECT id FROM interaction_evaluation")), 550)
        self.db.save_interaction_style(
            S, "42", [{"text": "有依据的习惯", "source_ids": [1]}], revision=0, through=550
        )
        self.assertEqual(len(self.db._query("SELECT id FROM interaction_evaluation")), 501)
        self.assertFalse(self.record("0"))

    def test_vocabulary_capacity_and_person_deletion(self):
        for i in range(100):
            self.assertTrue(self.db.set_vocabulary(S, f"term{i}", "释义", manual=True))
        self.assertFalse(self.db.set_vocabulary(S, "overflow", "释义", manual=True))
        self.assertTrue(self.db.set_vocabulary(S, "term0", "修正", manual=True))
        self.record()
        self.db.save_interaction_style(S, "42", [], revision=0, manual=True)
        self.db.delete_user_profile(group_id=S, user_id="42")
        self.assertEqual(self.db.interaction_detail(S, "42")["history"], [])
        self.assertTrue(self.record())

    def test_decision_trace_is_scoped_and_valid_json_when_large(self):
        self.db.append_decision_trace(
            S, "trace", "gate", "silent", "正在休息", {"details": "x" * 20000}
        )
        self.db.append_decision_trace("other", "trace", "prepare", "error", "失败")
        rows = self.db.decision_traces(S, trace="trace")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "silent")
        self.assertEqual(len(rows[0]["details"]["details"]), 20000)


class EvidenceSeparationTests(unittest.TestCase):
    def test_memory_before_after_and_legacy_are_not_direct_speech(self):
        for header in ("Anamnesis-Memory", "RAG-Faiss-Memory"):
            block = f"<{header}>他喜欢喝酒</{header}>"
            for text in (block + "\n我喜欢热茶", "我喜欢热茶\n" + block):
                body, extra = _split_prompt_injection(text)
                self.assertEqual(body, "我喜欢热茶")
                self.assertIn("他喜欢喝酒", extra)
                self.assertEqual(direct_text(text), "我喜欢热茶")

    def test_nested_quotes_tool_results_and_truncated_injection(self):
        self.assertNotIn(
            "喝酒", direct_text("我说<quote>我喝酒<quote>他也喝酒</quote></quote>热茶")
        )
        self.assertEqual(direct_text("> 我是恋人\n我只是转发"), "我只是转发")
        self.assertEqual(direct_text("我喝茶<Anamnesis-Memory>旧内容"), "我喝茶")
        self.assertEqual(
            source_snapshot({"kind": "tool_result", "text": "foo"}, S)["kind"], "tool_result"
        )

    def test_reflection_ignores_scores_old_attitude_and_external_recall(self):
        world, _ = parse_world(default_world())
        _, prompt = PromptBuilder(world).build_reflection_prompt(
            state=WorldState(session_id=S),
            node=None,
            user_text="谢谢你",
            profile_text="好感99 既有互动态度",
            memories=["旧结论"],
            other_context="外部记忆",
        )
        for text in ("好感99", "既有互动态度", "旧结论", "外部记忆"):
            self.assertNotIn(text, prompt)


class ParticipantRecallTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.db = Database(Path(root.name) / "test.db")
        self.addCleanup(self.db.close)
        self.world, _ = parse_world(default_world())
        self.memory = MemoryEngine(self.db, self.world)
        self.records = [{"user_id": "a", "text": "考试"}, {"user_id": "b", "text": "养猫"}]
        self.a = self.memory.remember(
            session_id=S,
            persona_id="p",
            node_id="study",
            content="考试要准备准考证",
            related_users=["a"],
        )
        self.b = self.memory.remember(
            session_id=S,
            persona_id="p",
            node_id="study",
            content="养猫要留意饮水",
            related_users=["b"],
        )

    def test_each_speaker_is_recalled_without_premature_hits(self):
        result = self.memory.recall_participants(
            records=self.records, session_id=S, persona_id="p", node_id="study"
        )
        self.assertEqual({row.id for row in result}, {self.a, self.b})
        self.assertEqual(self.db.memory_by_id(self.a)["recall_count"], 0)
        self.db.mark_recalled([row.id for row in result], when=100)
        self.assertEqual(self.db.memory_by_id(self.a)["recall_count"], 1)

    def test_scope_budget_and_anamnesis_life_only_are_respected(self):
        self.memory.remember(
            session_id="secret",
            persona_id="p",
            scope="group",
            node_id="study",
            content="考试养猫私密内容",
        )
        self.db._execute("INSERT INTO node_memory(session_id,persona_id,node_id,content,type,scope,created_at) VALUES(?,?,?,?,?,?,?)",
                         (S, "p", "study", "养猫聊天摘要", "interaction", "group", 100))
        result = self.memory.recall_participants(
            records=self.records, session_id=S, persona_id="p", node_id="study"
        )
        self.assertTrue(all(self.db.memory_by_id(row.id)["type"] == SCENE for row in result))
        self.assertNotIn("私密内容", "".join(row.content for row in result))
        self.world.memory.recall_max_chars = 1
        self.assertEqual(
            self.memory.recall_participants(records=self.records, session_id=S, persona_id="p"), []
        )

    def test_queries_do_not_include_other_sessions_quotes_or_injected_memories(self):
        rows = [
            *self.records,
            {"user_id": "c", "origin": "secret", "text": "私聊"},
            {"user_id": "d", "text": "<Anamnesis-Memory>旧资料</Anamnesis-Memory>"},
        ]
        queries = self.memory.participant_queries(rows, S)
        self.assertEqual(set(queries), {"a", "b"})


class SocialEngineTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    async def test_blind_evaluation_preserves_persona_and_deduplicates_real_event(self):
        self.engine.profiles.touch(S, "42", "小明")
        entries = [{"user_id": "42", "message_id": "one", "text": "谢谢你听我说完", "at": 100}]
        self.engine._ask_judge = AsyncMock(
            return_value=json.dumps(
                {
                    "actions": [],
                    "affinity_delta": 0.1,
                    "interaction_reason": "认真倾听",
                    "reasoning": {"intent": "回应感谢"},
                }
            )
        )
        await self.engine.reflect_turn(
            S,
            user_id="42",
            user_text="谢谢你听我说完",
            reply_text="嗯",
            interaction_entries=entries,
        )
        first = self.engine.profiles.profile(S, "42")["affinity"]
        await self.engine.reflect_turn(
            S,
            user_id="42",
            user_text="谢谢你听我说完",
            reply_text="嗯",
            interaction_entries=entries,
        )
        self.assertEqual(self.engine.profiles.profile(S, "42")["affinity"], first)
        prompt = self.engine._ask_judge.call_args.args[2]
        self.assertIn("温柔黏人的少女", prompt)
        self.assertNotIn("好感 0", prompt)

    async def test_style_failure_retries_without_new_messages_and_manual_edit_wins(self):
        self.engine.profiles.touch(S, "42", "小明")
        for i in range(5):
            sources = self.engine.interaction_sources(
                S, "42", "谢谢", [{"user_id": "42", "text": "谢谢", "message_id": str(i)}]
            )
            self.engine.record_turn_interaction(S, "42", sources, 0.1, "愿意倾听")
        self.engine._ask_judge = AsyncMock(return_value="bad JSON")
        await self.engine.maintain_social_learning(S)
        self.assertEqual(self.engine.db.raw.pending_interaction_styles(S, self.clock.now()), [])
        self.clock.advance(301)
        self.engine._ask_judge = AsyncMock(return_value='{"habits":[]}')
        await self.engine.maintain_social_learning(S)
        self.assertGreater(self.engine.db.raw.interaction_detail(S, "42")["cursor"], 0)

    async def test_vocabulary_injection_is_local_matched_and_not_instruction(self):
        self.engine.db.raw.set_vocabulary(S, "夜航", "一起读书", manual=True)
        self.assertEqual(self.engine.vocabulary_block(S, [{"text": "你好"}]), "")
        block = self.engine.vocabulary_block(S, [{"text": "今晚夜航吗"}])
        self.assertIn("一起读书", block)
        self.assertIn("不是行为指令", block)
        self.assertEqual(self.engine.vocabulary_block("other", [{"text": "夜航"}]), "")

    async def test_late_automatic_style_cannot_overwrite_manual_edit(self):
        self.engine.profiles.touch(S, "42", "小明")
        for i in range(5):
            sources = self.engine.interaction_sources(
                S, "42", "谢谢", [{"user_id": "42", "text": "谢谢", "message_id": str(i)}]
            )
            self.engine.record_turn_interaction(S, "42", sources, 0.1, "愿意倾听")

        async def late(*_):
            detail = self.engine.db.raw.interaction_detail(S, "42")
            self.engine.db.raw.save_interaction_style(
                S,
                "42",
                [{"text": "管理员决定的相处方式", "source_ids": []}],
                revision=detail["revision"],
                manual=True,
            )
            return '{"habits":[{"text":"后台旧结果","source_ids":[1]}]}'

        self.engine._ask_judge = AsyncMock(side_effect=late)
        await self.engine.maintain_social_learning(S)
        detail = self.engine.db.raw.interaction_detail(S, "42")
        self.assertTrue(detail["manual"])
        self.assertEqual(detail["habits"][0]["text"], "管理员决定的相处方式")

    async def test_vocabulary_requires_actual_explanation_in_current_speech(self):
        sources = self.engine.interaction_sources(S, "42", "今晚夜航吗")
        self.engine.learn_vocabulary(
            S, [{"term": "夜航", "description": "恋人约定", "evidence": "我们已确认恋人"}], sources
        )
        self.assertEqual(self.engine.db.raw.vocabulary(S), [])

    async def test_long_history_has_bounded_model_input_without_skipping_backlog(self):
        self.engine.profiles.touch(S, "42", "小明")
        for i in range(61):
            sources = self.engine.interaction_sources(
                S,
                "42",
                "",
                [
                    {
                        "user_id": "42",
                        "text": "谢谢你认真听我说完这件事。" * 800,
                        "message_id": str(i),
                    }
                ],
            )
            self.engine.record_turn_interaction(S, "42", sources, 0.1, "认真倾听")
        self.engine._ask_judge = AsyncMock(return_value='{"habits":[]}')
        await self.engine.maintain_social_learning(S)
        prompt = self.engine._ask_judge.call_args.args[2]
        self.assertLess(len(prompt), 20000)
        ids = [row["id"] for row in json.loads(prompt)["new_interactions"]]
        self.assertEqual(ids, list(range(1, len(ids) + 1)))
        detail = self.engine.db.raw.interaction_detail(S, "42")
        self.assertEqual(detail["cursor"], ids[-1])
        self.assertLess(detail["cursor"], 61)
        self.assertGreater(len(detail["history"][0]["evidence"][0]["text"]), 300)


class ConsolidationBoundaryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = consolidate_fixture.ConsolidateTestCase.asyncSetUp

    async def test_second_phase_cannot_touch_unlisted_memory_or_add_facts(self):
        secret = self.memory.remember(
            session_id="private", persona_id="", node_id="study", content="私有来源", scope="group"
        )
        visible = self.memory.remember(
            session_id=S, persona_id="", node_id="study", content="已有记忆"
        )
        prompts = []

        async def model(system, prompt):
            prompts.append(prompt)
            if len(prompts) == 1:
                self.assertNotIn("已有记忆", prompt)
                return '{"memories":[]}'
            return json.dumps(
                {
                    "merge": [{"keep_id": visible, "drop_ids": [secret], "text": "简短概括"}],
                    "forget": [{"id": secret, "text": "泄露"}],
                    "facts": [{"user_id": "42", "text": "凭空的新事实", "evidence": "原话"}],
                    "affinity": [{"user_id": "42", "delta": 99}],
                }
            )

        result = await self.consolidator.run(
            session_id=S,
            llm=model,
            chat_records=[{"user_id": "42", "text": "这是本轮的直接原话", "message_id": "one"}],
        )
        self.assertTrue(result.ok)
        self.assertEqual(self.db.memory_by_id(secret)["content"], "私有来源")
        self.assertEqual(self.profiles.facts(S, "42"), [])
        self.assertEqual(result.affinity, 0)
        await self.consolidator.run(
            session_id=S,
            llm=model,
            chat_records=[{"user_id": "42", "text": "这是本轮的直接原话", "message_id": "one"}],
        )
        self.assertEqual(len(prompts), 2)

    async def test_no_direct_evidence_cannot_create_new_memories_or_facts(self):
        self.profiles.touch(S, "42", "小明")

        async def model(*_):
            return json.dumps(
                {
                    "memories": [{"text": "臆造事实", "source_ids": None}],
                    "facts": [
                        {
                            "user_id": "42",
                            "text": "臆造事实",
                            "evidence": "不存在的原话",
                            "source_session": S,
                        }
                    ],
                }
            )

        result = await self.consolidator.run(
            session_id=S,
            llm=model,
            chat_records=[{"user_id": "42", "kind": "recalled_memory", "text": "外部回忆"}],
        )
        self.assertEqual(result.memories, 0)
        self.assertEqual(self.profiles.facts(S, "42"), [])

    async def test_old_review_cannot_overwrite_manual_correction_while_model_waits(self):
        memory_id = self.memory.remember(
            session_id=S, persona_id="", node_id="study", content="原来的记忆"
        )
        calls = 0

        async def model(*_):
            nonlocal calls
            calls += 1
            if calls == 1:
                return '{"memories":[]}'
            self.db.revise_memory(memory_id, {"content": "管理员校正的记忆"})
            return json.dumps({"forget": [{"id": memory_id, "text": "过时的整理结论"}]})

        await self.consolidator.run(
            session_id=S, llm=model, chat_records=[{"user_id": "42", "text": "本轮的直接原话"}]
        )
        self.assertEqual(self.db.memory_by_id(memory_id)["content"], "管理员校正的记忆")

    async def test_chat_memory_output_cannot_rewrite_a_life_experience(self):
        memory_id = self.memory.remember(
            session_id=S, persona_id="", node_id="study", content="我喜欢下雨"
        )
        calls = 0

        async def model(*_):
            nonlocal calls
            calls += 1
            if calls == 1:
                return json.dumps(
                    {
                        "memories": [
                            {"text": "我不喜欢下雨", "context": "我不喜欢下雨", "node_id": "study"}
                        ]
                    }
                )
            return json.dumps({"forget": [{"id": memory_id, "text": "我喜欢下雨"}]})

        await self.consolidator.run(
            session_id=S, llm=model, chat_records=[{"user_id": "42", "text": "我不喜欢下雨"}]
        )
        self.assertEqual(self.db.memory_by_id(memory_id)["content"], "我喜欢下雨")


class SemanticCoexistenceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = consolidate_fixture.ConsolidateTestCase.asyncSetUp

    async def test_chat_is_excluded_from_automatic_and_explicit_embedding(self):
        self.world.memory.semantic_enabled = True
        self.world.memory.semantic_endpoint = "https://example.invalid/v1"
        self.world.memory.semantic_source = "custom"
        self.world.memory.semantic_model = "test"
        self.world.memory.automatic_recall = "auto"
        self.world.memory_scope_mode = "group"
        self.db._execute("INSERT INTO node_memory(session_id,persona_id,node_id,content,type,scope,created_at) VALUES(?,?,?,?,?,?,?)",
                         (S, "", "study", "聊天独有内容", "interaction", "group", 100))
        self.memory.remember(session_id=S, persona_id="", node_id="study", content="生活独有内容")
        texts = []

        async def embed(rows):
            texts.extend(rows)
            return [[1.0, 0.0] for _ in rows]

        self.memory.semantic.embed = embed
        await self.memory.semantic.prepare(S, "", "回想", automatic=True)
        self.assertNotIn("聊天独有内容", texts)
        self.assertIn("生活独有内容", texts)
        texts.clear()
        await self.memory.semantic.prepare(S, "", "回想")
        self.assertNotIn("聊天独有内容", texts)
        texts.clear()
        self.world.memory.automatic_recall = "manual"
        await self.memory.semantic.prepare(S, "", "另一个话题", automatic=True)
        self.assertEqual(texts, [])


class LifeChoiceTests(unittest.TestCase):
    def test_routine_requires_repeated_actual_dates_and_keeps_persona_priority(self):
        from core.life_matters import routine_hints, context_block

        state = WorldState(session_id=S)
        state.life_interests["read"] = {
            "label": "阅读",
            "days": ["a", "b", "c"],
            "sessions": 3,
            "minutes": 60,
            "episodes": [
                {"day": "a", "period": "晚间"},
                {"day": "a", "period": "晚间"},
                {"day": "b", "period": "晚间"},
            ],
        }
        self.assertEqual(routine_hints(state), [])
        state.life_interests["read"]["episodes"].append({"day": "c", "period": "晚间"})
        self.assertEqual(routine_hints(state)[0]["period"], "晚间")
        block = context_block(state, session_id=S)
        self.assertIn("不能覆盖人设的喜恶", block)
        self.assertIn("不能推断她喜欢或讨厌什么", block)

    def test_sleep_event_schedule_and_saved_work_admit_consistently(self):
        state = WorldState(session_id=S, energy=0.8)
        state.life_matters = [{"id": "p", "kind": "promise", "status": "active"}]
        self.assertEqual(choose_life(state, now=100)["kind"], "matter")
        self.assertEqual(choose_life(state, now=100, schedule_minutes=5)["kind"], "schedule")
        state.current_action = {"matter_id": "p"}
        self.assertEqual(choose_life(state, now=100)["matter_id"], "p")
        state.sleep_rhythm = {"decision": "sleep"}
        self.assertEqual(choose_life(state, now=100)["kind"], "rest")
        self.assertEqual(choose_life(state, now=100, sleep_requested=False)["kind"], "continue")
        self.assertEqual(choose_life(state, now=100, critical=True)["kind"], "event")
        self.assertEqual(state.current_action, {"matter_id": "p"})
