"""Deliberate recall uses semantic meaning without losing its requested scope."""
from __future__ import annotations

import json
import types
import unittest
from unittest.mock import AsyncMock

from core.engine.models import TickOutcome
from core.json_actions import PlannedAction
from core.memory import SCOPE_NODE
from tests import test_engine as fixture


class ActiveRecallReviewTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    def enable_embedding(self):
        self.engine.world.memory.semantic_enabled = True
        self.provider = types.SimpleNamespace(
            describe=lambda *_: {"ready": True, "fingerprint": "active-recall-review"},
            embed=AsyncMock(side_effect=lambda texts, *_: [[1., 0.] for _ in texts]),
        )
        self.engine.memory.semantic.provider = self.provider

    def remember(self, content, **kwargs):
        kwargs.setdefault("node_id", self.node_id)
        return self.engine.memory.remember(session_id=fixture.SESSION, persona_id="",
                                          content=content, **kwargs)

    async def recall(self, intent="想起港湾散心的经历", **query):
        parsed = {"keyword": "港湾散心", "nodes": [], "node_names": [], "type": "", "limit": 3}
        parsed.update(query)
        self.engine._parse_recall_query = AsyncMock(return_value=parsed)
        self.engine._llm_followup = AsyncMock()
        async with self.engine.session_state(fixture.SESSION) as state:
            await self.engine._run_recall(state, self.engine.node(self.node_id),
                TickOutcome(session_id=fixture.SESSION), PlannedAction(type="recall", intent=intent))
        rows = await self.db.call("query_events", session_id=fixture.SESSION, limit=30)
        return next(row["detail"] for row in rows if row["event_type"] == "recall_done")

    async def test_explicit_action_recalls_paraphrases_even_in_manual_only_mode(self):
        self.enable_embedding()
        self.engine.world.memory.automatic_recall = "manual"
        self.engine.world.memory.recall_max_chars = 0
        self.remember("海风拂过旧码头，远处亮起了灯塔")
        self.remember("其他角色留下的私事", persona_key="another-persona")

        result = await self.recall()
        again = await self.recall()

        self.assertEqual(1, result["count"])
        self.assertIn("旧码头", result["detail"])
        self.assertEqual(result["detail"], again["detail"])
        self.assertEqual(1, self.provider.embed.await_count)
        self.assertNotIn("其他角色留下的私事", str(self.provider.embed.await_args_list))
        self.assertFalse(self.llm.calls)

    async def test_zone_and_type_filters_apply_before_embedding_node_scoped_memories(self):
        self.enable_embedding()
        self.remember("柳枝擦过长椅，湖面映着落日", node_id="park_bench", scope=SCOPE_NODE)
        self.remember("枕边做的一场梦", node_id="park_lake", scope=SCOPE_NODE, memory_type="dream")
        self.remember("厨房的水壶响了", node_id="kitchen", scope=SCOPE_NODE)

        result = await self.recall(nodes=["park_bench", "park_lake"], type="scene")

        self.assertEqual(1, result["count"])
        self.assertIn("柳枝", result["detail"])
        embedded = str(self.provider.embed.await_args_list)
        self.assertIn("柳枝", embedded)
        self.assertNotIn("枕边做的一场梦", embedded)
        self.assertNotIn("厨房的水壶响了", embedded)

    async def test_action_does_not_embed_sources_that_it_cannot_recall(self):
        self.enable_embedding()
        self.remember("海风拂过旧码头")
        self.remember("翻聊天时留下的感受", source="chat_browse")
        self.remember("只属于某次交谈的心结", context=json.dumps(
            {"kind": "heart_knot", "source_scoped": True}))
        self.remember("私下约定的进度", source="life_matter", participants=["42"])

        result = await self.recall()

        self.assertEqual(1, result["count"])
        embedded = str(self.provider.embed.await_args_list)
        self.assertIn("海风拂过旧码头", embedded)
        for private in ("翻聊天时留下的感受", "只属于某次交谈的心结", "私下约定的进度"):
            self.assertNotIn(private, embedded)

    async def test_embedding_failure_retains_keyword_matches_without_another_chat_call(self):
        self.enable_embedding()
        self.provider.embed.side_effect = RuntimeError("embedding service unavailable")
        self.remember("窗台的薄荷长高了")

        result = await self.recall(keyword="薄荷")

        self.assertEqual(1, result["count"])
        self.assertIn("薄荷", result["detail"])
        self.provider.embed.assert_awaited_once()
        self.assertFalse(self.llm.calls)

    async def test_named_person_is_filtered_before_the_result_limit_and_embedding(self):
        self.enable_embedding()
        await self.engine.note_presence(self.ctx(text="你好", user_id="42", user_name="小明"))
        self.remember("小明那次晚饭吃了鱼", participants=["42"], weight=.1)
        self.remember("阿May那次晚饭吃了面", participants=["7"], weight=1.)

        result = await self.recall("回想小明那次晚饭", keyword="晚饭", limit=1)

        self.assertEqual(1, result["count"])
        self.assertIn("小明那次晚饭", result["detail"])
        self.assertNotIn("阿May", result["detail"])
        self.assertNotIn("阿May那次晚饭", str(self.provider.embed.await_args_list))

    async def test_missing_named_person_memory_never_falls_back_to_somebody_else(self):
        await self.engine.note_presence(self.ctx(text="你好", user_id="42", user_name="小明"))
        self.remember("阿May那次晚饭吃了面", participants=["7"])

        result = await self.recall("回想小明那次晚饭", keyword="晚饭", limit=1)

        self.assertEqual(0, result["count"])
        self.assertNotIn("阿May", result["detail"])

    async def test_malformed_helper_limit_cannot_abort_deliberate_recall(self):
        self.remember("窗台的薄荷长高了")
        for invalid in ("几条", [], {}, float("inf"), float("nan")):
            with self.subTest(limit=invalid):
                result = await self.recall(keyword="薄荷", limit=invalid)
                self.assertEqual(1, result["count"])

    def test_participant_filter_precedes_database_limit_and_matches_whole_ids(self):
        wanted = self.remember("早先与小明的经历", related_users=["42"])
        self.remember("同号前缀的其他人", participants=["142"])
        self.remember("更新的其他人的经历", participants=["7"])

        rows = self.db.raw.query_memories(session_id=fixture.SESSION, participants=["42"], limit=1)

        self.assertEqual([wanted], [row["id"] for row in rows])

    def test_scalar_and_malformed_participant_payloads_do_not_prove_identity(self):
        wanted = self.remember("与小明的旧经历", participants=["42"])
        other = self.remember("字段损坏的旧记录", participants=["7"])
        for invalid in ('"42"', '{"42":"another person"}', 'broken json', 'null'):
            with self.subTest(payload=invalid):
                self.db.raw._execute("UPDATE node_memory SET participants=?,related_users=? WHERE id=?",
                                     (invalid, invalid, other))
                rows = self.db.raw.query_memories(session_id=fixture.SESSION, participants=["42"], limit=1)
                self.assertEqual([wanted], [row["id"] for row in rows])
        self.db.raw._execute("UPDATE node_memory SET participants=? WHERE id=?", ('[42]', wanted))
        rows = self.db.raw.query_memories(session_id=fixture.SESSION, participants=["42"], limit=1)
        self.assertEqual([wanted], [row["id"] for row in rows])

    async def test_changing_search_locations_refreshes_scores_and_reuses_query_vectors(self):
        self.enable_embedding()
        self.remember("小灶旁看着水汽升起", node_id="kitchen", scope=SCOPE_NODE)
        self.remember("柳枝擦过长椅", node_id="park_bench", scope=SCOPE_NODE)

        first = await self.recall(nodes=["kitchen"])
        second = await self.recall(nodes=["park_bench"])
        third = await self.recall(nodes=["kitchen"])

        self.assertIn("水汽", first["detail"])
        self.assertIn("柳枝", second["detail"])
        self.assertNotIn("水汽", second["detail"])
        self.assertEqual(first["detail"], third["detail"])
        self.assertEqual(2, self.provider.embed.await_count)
        self.assertEqual(["柳枝擦过长椅"], self.provider.embed.await_args.args[0])

    async def test_location_only_lookup_does_not_request_an_embedding(self):
        self.enable_embedding()
        self.remember("小灶旁看着水汽升起", node_id="kitchen", scope=SCOPE_NODE)
        self.remember("柳枝擦过长椅", node_id="park_bench", scope=SCOPE_NODE)

        result = await self.recall(nodes=["kitchen"], keyword="")

        self.assertEqual(1, result["count"])
        self.assertIn("水汽", result["detail"])
        self.provider.embed.assert_not_awaited()

    ctx = fixture.EngineTestCase.ctx
