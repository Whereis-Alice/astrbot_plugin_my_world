"""Ended encounters reach ordinary replies without losing date or life ownership."""
from __future__ import annotations

import copy
from datetime import datetime
from hashlib import sha256
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from tests import test_engine as fixture
from tests import test_visits_actor as actors
from tests import test_visits_memory_review_o as long_fixture


class ReplyVisitRecall30Tests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    long_visit = long_fixture.VisitRecallReviewTests.long_visit

    async def asyncSetUp(self):
        await actors.VisitActorTests.asyncSetUp(self)
        self.engine.world.timezone = "Asia/Shanghai"
        self.clock._now = self.at(10, 22, 43)
        self.engine.world.memory_scope_mode = "group"
        self.engine.world.memory.semantic_enabled = False
        self.engine.world.memory.recall_max_chars = 1200
        self.engine.world.limits.max_think_memory = 5
        self.persona_key = sha256(self.persona.text.encode()).hexdigest()[:24]
        self.question = "爱丽丝你今天有去哪串门吗"
        self.user_id = "current-qq-speaker"

    @staticmethod
    def at(day, hour=12, minute=0):
        return datetime(2026, 11, day, hour, minute, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()

    def stored_visit(self, content, *, start=None, end=None, session=fixture.SESSION,
                     persona=None, created=None, extra_meta=None):
        meta = {"kind": "world_visit", "visit_id": "trip-" + str(self.db.raw.memory_stats()["total"]),
                "peer_id": "friend:leaf", "peer_world": "圣森", "confirmed": True,
                "status": "completed", "persona_key": persona or self.persona_key}
        if start is not None:
            meta["occurred_start"] = start
        if end is not None:
            meta["occurred_end"] = end
        meta.update(extra_meta or {})
        return self.db.raw.add_memory(session_id=session, persona_id=persona or self.persona_key,
            scope="group_persona", node_id="study", content=content, source="life_event",
            memory_type="event", tier="gist", context=json.dumps(meta, ensure_ascii=False),
            participants=["world_actor:friend:leaf"], keywords=["串门", "莉耶芙"],
            created_at=self.clock.now() if created is None else created)

    async def rendered_reply(self, question=None, *, rows=None):
        state = await self.engine.load_state(fixture.SESSION, cold_start=False)
        request = self.engine.context_request(state, fixture.SESSION, task="reply",
            text=self.question if question is None else question, rows=rows or [], focus=self.user_id)
        await self.engine.prepare_context_memory(request, state=state)
        memories = self.engine.context_memories(state, request, rows or [])
        _, dynamic = self.engine.prompts.build_dialogue_prompt(persona_text=self.persona.text,
            state=state, node=self.engine.node(state.node_id), memories=memories,
            current_session=fixture.SESSION, context_request=request)
        self.assertEqual(self.llm.calls, [])
        self.assertEqual(self.messenger.sent, [])
        return memories, dynamic, request

    async def test_current_visit_gist_survives_old_speaker_notes_and_contextual_query(self):
        trip = self.stored_visit("与圣森的莉耶芙串门，沿湖散步后告别回家，心情平静。",
                                 start=self.at(10, 18), end=self.at(10, 19))
        for index in range(8):
            self.db.raw.add_memory(session_id=fixture.SESSION, persona_id="", scope="group_persona",
                node_id="study", content=f"服务器技术笔记{index}：嵌入模型配置、接口令牌和插件重载的排查记录。",
                source="runtime", weight=1., participants=[self.user_id], related_users=[self.user_id],
                created_at=self.at(9, 20))
        rows = [{"origin": fixture.SESSION, "user_id": self.user_id,
                 "text": "服务器技术笔记、嵌入模型配置和接口令牌，插件重载后继续排查。"}]
        memories, dynamic, request = await self.rendered_reply(rows=rows)
        self.assertIn(trip, [row.id for row in memories])
        self.assertIn("沿湖散步后告别回家", dynamic)
        self.assertNotIn("服务器技术笔记", dynamic)
        self.assertEqual(request.current_text, self.question)
        self.assertIn("服务器技术笔记", request.query)

    async def test_long_ended_visit_reaches_both_reply_budget_layers_with_confirmed_ending(self):
        visit = copy.deepcopy(self.long_visit())
        visit.update(arrived_at=self.at(10, 18), ended_at=self.at(10, 19))
        await self.port.remember(fixture.SESSION, visit)
        row = next(row for row in self.db.raw.query_memories(session_id=fixture.SESSION)
                   if row["source"] == "life_event")
        self.assertGreater(len(row["content"]), 6000)
        self.db.raw._execute("UPDATE node_memory SET created_at=? WHERE id=?", (self.at(10, 19), row["id"]))
        memories, dynamic, request = await self.rendered_reply()
        self.assertIn(row["id"], [item.id for item in memories])
        for text in ("[我说]", "扶稳了差点倾倒的樱桃蛋糕", "送你到门口", "这次已告别归家"):
            self.assertIn(text, dynamic)
        self.assertNotIn("聊了很长一段旧事。" * 20, dynamic)
        self.assertEqual(self.db.raw.memory_by_id(row["id"])["content"], row["content"])
        self.assertLessEqual(request.trace["memories"]["selected_chars"], 1200)

    async def test_today_and_yesterday_use_local_occurrence_including_midnight_overlap(self):
        today = self.stored_visit("与朋友串门完成今日樱桃。", start=self.at(10, 8), end=self.at(10, 9))
        yesterday = self.stored_visit("与朋友串门完成昨日纸船。", start=self.at(9, 8), end=self.at(9, 9),
                                      created=self.at(10, 20))
        crossing = self.stored_visit("与朋友串门完成跨夜风铃。", start=self.at(9, 23, 55), end=self.at(10, 0, 5))
        self.stored_visit("与朋友串门完成未来灯笼。", start=self.at(11, 8), end=self.at(11, 9))
        self.stored_visit("与朋友串门完成未知松饼。")
        memories, dynamic, _ = await self.rendered_reply()
        self.assertEqual({row.id for row in memories}, {today, crossing})
        self.assertNotIn("昨日纸船", dynamic)
        self.assertNotIn("未来灯笼", dynamic)
        self.assertNotIn("未知松饼", dynamic)
        memories, _, _ = await self.rendered_reply("你昨天有去哪串门吗")
        self.assertEqual({row.id for row in memories}, {yesterday, crossing})

    async def test_other_session_and_persona_never_enter_ordinary_reply(self):
        own = self.stored_visit("与朋友串门看过本群星光。", start=self.at(10, 8), end=self.at(10, 9))
        self.stored_visit("与朋友串门看过隔群暗号。", session=fixture.OTHER_SESSION,
                          start=self.at(10, 8), end=self.at(10, 9))
        self.stored_visit("与朋友串门看过异角私事。", persona="another-life-persona",
                          start=self.at(10, 8), end=self.at(10, 9))
        memories, dynamic, _ = await self.rendered_reply()
        self.assertEqual([row.id for row in memories], [own])
        self.assertNotIn("隔群暗号", dynamic)
        self.assertNotIn("异角私事", dynamic)

    async def test_unrelated_greeting_does_not_dump_trip_and_recall_switches_stay_off(self):
        self.stored_visit("与圣森的莉耶芙串门，沿湖散步后告别回家。",
                          start=self.at(10, 18), end=self.at(10, 19))
        memories, dynamic, _ = await self.rendered_reply("晚上好")
        self.assertEqual(memories, [])
        self.assertNotIn("沿湖散步", dynamic)
        for disabled in ("manual", "zero_chars", "zero_items"):
            with self.subTest(disabled=disabled):
                self.engine.world.memory.automatic_recall = "manual" if disabled == "manual" else "auto"
                self.engine.world.memory.recall_max_chars = 0 if disabled == "zero_chars" else 1200
                self.engine.world.limits.max_think_memory = 0 if disabled == "zero_items" else 5
                memories, dynamic, _ = await self.rendered_reply()
                self.assertEqual(memories, [])
                self.assertNotIn("沿湖散步", dynamic)

    async def test_embedding_uses_current_question_without_old_speaker_query_scores(self):
        trip = self.stored_visit("与莉耶芙串门沿湖散步后回家。", start=self.at(10, 18), end=self.at(10, 19))
        old = self.db.raw.add_memory(session_id=fixture.SESSION, persona_id="", node_id="study", scope="group_persona",
            content="修复技术配置记录。", source="runtime", participants=[self.user_id], created_at=self.at(9))
        self.engine.world.memory.semantic_enabled = True
        semantic = self.engine.memory.semantic
        semantic.provider = SimpleNamespace(describe=lambda *_: {"ready": True}, embed=AsyncMock())
        rows = [{"origin": fixture.SESSION, "user_id": self.user_id, "text": "旧话题：技术配置记录。"}]
        with patch.object(semantic, "prepare_many", new_callable=AsyncMock) as prepare, patch.object(
                semantic, "cached", side_effect=lambda _s, _p, query, **kw: {trip: .9} if query == self.question else {old: 1.}):
            memories, dynamic, request = await self.rendered_reply(rows=rows)
        self.assertEqual(prepare.await_args.args[2], [self.question])
        self.assertEqual([row.id for row in memories], [trip])
        self.assertNotIn("修复技术配置记录", dynamic)
        self.assertNotEqual(request.query, self.question)
        semantic.provider.embed.assert_not_called()

    async def test_short_followup_retains_context_when_embedding_cache_is_empty(self):
        trip = self.stored_visit("与莉耶芙串门沿湖散步后回家。", start=self.at(10, 18), end=self.at(10, 19))
        rows = [{"origin": fixture.SESSION, "user_id": self.user_id, "text": "和莉耶芙串门了吗"},
                {"origin": fixture.SESSION, "user_id": "other-speaker", "text": "服务器技术配置记录。"}]
        self.engine.world.memory.semantic_enabled = True
        # Provider unavailability must not change the lexical continuation query.
        memories, dynamic, request = await self.rendered_reply("后来呢", rows=rows)
        self.assertNotEqual(request.retrieval_query, "后来呢")
        self.assertGreater(len(request.retrieval_queries), 1)
        self.assertIn(trip, [row.id for row in memories])
        self.assertIn("沿湖散步", dynamic)

    async def test_today_plan_does_not_date_filter_a_separate_previous_visit_question(self):
        trip = self.stored_visit("上次和莉耶芙串门沿湖散步后回家。", start=self.at(9, 18), end=self.at(9, 19))
        memories, dynamic, _ = await self.rendered_reply("今天有什么打算？上次和莉耶芙串门怎么样？")
        self.assertIn(trip, [row.id for row in memories])
        self.assertIn("沿湖散步", dynamic)
