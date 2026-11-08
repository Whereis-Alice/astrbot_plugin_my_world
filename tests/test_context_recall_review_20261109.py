"""Recall keeps semantic relevance and the unfinished topic's actual addressee."""
from __future__ import annotations

import types
import unittest
from unittest.mock import AsyncMock

from tests import test_engine as fixture


class ContextRecallReviewTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    def enable_embedding(self):
        self.engine.world.memory.semantic_enabled = True
        provider = types.SimpleNamespace(
            describe=lambda *_: {"ready": True, "fingerprint": "context-review"},
            embed=AsyncMock(side_effect=lambda texts, *_: [[1., 0.] for _ in texts]),
        )
        self.engine.memory.semantic.provider = provider
        return provider

    async def test_several_semantic_matches_are_retained_as_related_memories(self):
        provider = self.enable_embedding()
        provider.embed.side_effect = lambda texts, *_: [
            [0., 1.] if text == "窗前种着一盆薄荷" else [1., 0.] for text in texts]
        ids = set()
        for text in ("接上电源补足电量", "适配器的接口曾经松动", "窗前种着一盆薄荷"):
            mid = self.engine.memory.remember(session_id=fixture.SESSION, persona_id="",
                node_id=self.node_id, content=text)
            self.db.raw._execute("UPDATE node_memory SET created_at=? WHERE id=?",
                                 (self.clock.now() - 3 * 86400, mid))
            if text != "窗前种着一盆薄荷":
                ids.add(mid)
        state = await self.engine.load_state(fixture.SESSION, cold_start=False)
        request = self.engine.context_request(state, fixture.SESSION, text="充电", rows=[])

        await self.engine.prepare_context_memory(request, state=state)
        memories = self.engine.context_memories(state, request)

        self.assertEqual(ids, {item.id for item in memories})
        self.assertTrue(all(item.semantic_score == 1. for item in memories))
        _, runtime = self.engine.prompts.build_dialogue_prompt(state=state,
            node=self.engine.node(state.node_id), persona_text=self.persona.text,
            memories=memories, context_request=request)
        self.assertIn("接上电源补足电量", runtime)
        self.assertIn("适配器的接口曾经松动", runtime)
        self.assertNotIn("窗前种着一盆薄荷", runtime)
        self.assertEqual(1, len(self.engine.memory.recall_context(request, limit=1)))
        self.engine.world.memory.recall_max_chars = 1
        self.assertEqual([], self.engine.context_memories(state, request))
        self.assertEqual(1, provider.embed.await_count)

    async def test_reply_only_embeds_and_presents_the_addressees_unfinished_topics(self):
        provider = self.enable_embedding()
        state = await self.engine.load_state(fixture.SESSION, cold_start=False)
        _, owner = await self.engine._journal_persona(fixture.SESSION)
        state.open_topics = [
            {"text": "还在等风车工坊的零件", "who": "42", "session": fixture.SESSION,
             "persona_key": owner, "at": self.clock.now() - 3600, "next_ask_at": self.clock.now() - 1},
            {"text": "另一个人的相机送修结果", "who": "99", "session": fixture.SESSION,
             "persona_key": owner, "at": self.clock.now() - 3600, "next_ask_at": self.clock.now() - 1},
        ]
        request = self.engine.context_request(state, fixture.SESSION,
            text="你在吗", rows=[], focus="42")

        await self.engine.prepare_context_memory(request, state=state)
        embedded = "\n".join(provider.embed.await_args.args[0])
        _, runtime = self.engine.prompts.build_dialogue_prompt(state=state,
            node=self.engine.node(state.node_id), persona_text=self.persona.text,
            context_request=request)

        for material in (embedded, runtime):
            self.assertIn("还在等风车工坊的零件", material)
            self.assertNotIn("另一个人的相机送修结果", material)

    async def test_legacy_user_id_topics_keep_their_addressee(self):
        state = await self.engine.load_state(fixture.SESSION, cold_start=False)
        state.open_topics = [
            {"text": "旧版自己的未完话题", "user_id": "42", "at": self.clock.now()},
            {"text": "旧版其他人的未完话题", "user_id": "99", "at": self.clock.now()},
        ]

        topics = self.engine.prompts._due_open_topics(state, fixture.SESSION, focus_user="42")

        self.assertEqual(["旧版自己的未完话题"], [item["text"] for item in topics])
