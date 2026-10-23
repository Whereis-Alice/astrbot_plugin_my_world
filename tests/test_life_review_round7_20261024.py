"""Private emotions and explicit recollection keep their actual source."""
import copy
import json
import unittest
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.engine import TickOutcome
from core.json_actions import PlannedAction
from core.context_sources import life_memory_metadata, memory_context_items
from tests import test_life_audit as audit
from tests import test_relationship_api as contact_api
from tests import test_social_directory as directory_api

S, P = audit.S, audit.P


class EmotionalContinuityTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = audit.LifeAuditTests.asyncSetUp
    add_group = audit.LifeAuditTests.add_group
    idle = audit.LifeAuditTests.idle

    async def reflect(self, session, **fields):
        self.engine._ask_judge = AsyncMock(return_value=json.dumps(fields, ensure_ascii=False))
        result = await self.engine.reflect_turn(session, user_id="42", user_name="小明",
            user_text="我有点担心，只和你说。", reply_text="嗯，我听着。")
        self.assertIsNotNone(result)

    async def emotions(self, session, *, task="reply"):
        async with self.engine.session_state(session) as state:
            request = self.engine.context_request(state, session, task=task, rows=[], focus="42")
            await self.engine.prepare_context_memory(request)
            return self.engine.context_emotions(state, request)

    async def test_private_reflection_keeps_the_worry_in_its_actual_conversation(self):
        self.add_group(sessions=[S, P], main=S)
        await self.idle()
        await self.reflect(P, heart_knot="只在私聊说过的担心")
        for session, expected in ((P, True), (S, False)):
            with self.subTest(session=session):
                self.assertEqual("只在私聊说过的担心" in await self.emotions(session), expected)

    async def test_another_character_does_not_inherit_the_old_feelings(self):
        await self.idle()
        original = self.persona.text
        await self.reflect(S, heart_knot="旧角色担心风车", grudge="旧角色还气着迟到")
        self.assertIn("旧角色担心风车", await self.emotions(S))
        self.persona.text = "另一位完全不同的园艺少女。"
        block = await self.emotions(S)
        self.assertNotIn("旧角色担心风车", block)
        self.assertNotIn("旧角色还气着迟到", block)
        self.persona.text = original
        self.assertIn("旧角色还气着迟到", await self.emotions(S))

    async def test_explicit_recall_does_not_reintroduce_another_characters_experience(self):
        await self.idle()
        async with self.engine.session_state(S) as state:
            await self.engine._execute_actions(state, self.engine.node("study"), TickOutcome(session_id=S),
                [PlannedAction(type="think", content="旧角色想把风车涂成蓝色")], depth=0, autonomous=True)
        self.persona.text = "另一位完全不同的园艺少女。"
        self.engine._parse_recall_query = AsyncMock(return_value={"keyword": "风车", "nodes": [], "limit": 3})
        self.engine._llm_followup = AsyncMock(return_value=False)
        async with self.engine.session_state(S) as state:
            await self.engine._execute_actions(state, self.engine.node("study"), TickOutcome(session_id=S),
                [PlannedAction(type="recall", intent="回想风车的事")], depth=0, autonomous=True)
        events = await self.db.call("query_events", session_id=S, event_type="recall_done")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["detail"]["count"], 0)
        self.assertNotIn("旧角色想把风车", self.engine._llm_followup.call_args.args[-1])

    async def test_slow_emotion_decay_does_not_disappear_with_frequent_ticks(self):
        await self.idle()
        self.engine.world.state_dynamics.heart_knot_decay_per_hour = .001
        async with self.engine.session_state(S) as state:
            self.engine.note_heart_knot(state, "慢慢放下的担心")
            original = copy.deepcopy(state.heart_knots)
            start = self.clock.now()
            for offset in range(60, 3601, 60):
                self.engine._decay_heart_knots(state, now=start + offset)
            self.assertAlmostEqual(state.heart_knots[0]["strength"], .699, places=8)
            state.heart_knots = original
            self.engine._decay_heart_knots(state, now=start + 3600)
            self.assertAlmostEqual(state.heart_knots[0]["strength"], .699, places=8)

    async def test_expired_private_worry_keeps_its_source_when_archived(self):
        self.add_group(sessions=[S, P], main=S)
        await self.idle()
        await self.reflect(P, heart_knot="只在私聊说过的担心")
        self.clock.advance(6 * 86400)
        async with self.engine.session_state(S) as state:
            self.engine._decay_heart_knots(state, now=self.clock.now())
        private, _ = await self.engine.lookup_life_memories(P, "", "只在私聊说过的担心")
        group, _ = await self.engine.lookup_life_memories(S, "", "只在私聊说过的担心")
        self.assertEqual(len(private), 1)
        self.assertEqual(group, [])

    async def test_a_new_character_cannot_forgive_the_previous_characters_grudge(self):
        await self.idle()
        original = self.persona.text
        await self.reflect(S, grudge="旧角色还气着迟到")
        self.persona.text = "另一位完全不同的园艺少女。"
        await self.reflect(S, forgive=True)
        self.persona.text = original
        self.assertIn("旧角色还气着迟到", await self.emotions(S))

    async def test_same_character_recall_still_finds_its_own_experience(self):
        await self.idle()
        async with self.engine.session_state(S) as state:
            await self.engine._execute_actions(state, self.engine.node("study"), TickOutcome(session_id=S),
                [PlannedAction(type="think", content="想把风车涂成蓝色")], depth=0, autonomous=True)
            self.engine._parse_recall_query = AsyncMock(return_value={"keyword": "风车", "limit": 3})
            self.engine._llm_followup = AsyncMock(return_value=False)
            await self.engine._execute_actions(state, self.engine.node("study"), TickOutcome(session_id=S),
                [PlannedAction(type="recall", intent="回想风车的事")], depth=0, autonomous=True)
        events = await self.db.call("query_events", session_id=S, event_type="recall_done")
        self.assertEqual(events[0]["detail"]["count"], 1)
        self.assertIn("想把风车涂成蓝色", self.engine._llm_followup.call_args.args[-1])

    async def test_current_character_does_not_expire_another_characters_emotions(self):
        await self.idle()
        original = self.persona.text
        await self.reflect(S, heart_knot="旧角色担心风车", grudge="旧角色还气着迟到")
        before = await self.engine.load_state(S)
        self.persona.text = "另一位完全不同的园艺少女。"
        self.clock.advance(8 * 86400)
        async with self.engine.session_state(S) as state:
            self.engine._decay_heart_knots(state, now=self.clock.now())
            self.engine._decay_grudges(state, now=self.clock.now())
            self.assertEqual(state.heart_knots, before.heart_knots)
            self.assertEqual(state.grudges, before.grudges)
        self.assertEqual(self.engine.memory.db.query_memories(memory_type="inner"), [])
        self.persona.text = original
        async with self.engine.session_state(S) as state:
            self.engine._decay_heart_knots(state, now=self.clock.now())
            self.engine._decay_grudges(state, now=self.clock.now())
            self.assertEqual((state.heart_knots, state.grudges), ([], []))
        rows = self.engine.memory.db.query_memories(memory_type="inner")
        self.assertEqual(len(rows), 2)
        self.assertEqual({life_memory_metadata(row["source"], row["context"])["persona_key"] for row in rows},
                         {sha256(original.encode()).hexdigest()[:24]})

    async def test_new_character_cannot_replace_identical_emotions_of_the_old_one(self):
        await self.idle()
        original = self.persona.text
        self.engine.world.profile.grudge_daily_max = 10
        await self.reflect(S, heart_knot="担心那件事", grudge="还气着迟到")
        self.persona.text = "另一位完全不同的园艺少女。"
        await self.reflect(S, heart_knot="担心那件事", grudge="还气着迟到")
        state = await self.engine.load_state(S)
        self.assertEqual((len(state.heart_knots), len(state.grudges)), (2, 2))
        self.engine.reload_config()
        self.persona.text = original
        async with self.engine.session_state(S) as state:
            self.assertIsNotNone(self.engine.grudge_for(state, "42"))
            self.assertIn("担心那件事", self.engine.extra_reminders(state,
                ctx=SimpleNamespace(session_id=S, user_id="42")))

    async def test_archived_private_emotions_stay_out_of_other_people_and_automatic_work(self):
        self.add_group(sessions=[S, P], main=S)
        await self.idle()
        await self.reflect(P, heart_knot="只在私聊说过的担心")
        self.clock.advance(6 * 86400)
        async with self.engine.session_state(P) as state:
            self.engine._decay_heart_knots(state, now=self.clock.now())
            items, _ = await self.engine.lookup_life_memories(P, "", "只在私聊说过的担心")
            self.assertEqual(len(items), 1)
            self.assertTrue(items[0].private_emotion)
            for task, session, person, expected in (("reply", P, "42", True), ("reply", P, "7", False),
                    ("reply", S, "42", False), ("plan", P, "42", False), ("sleep", P, "42", False),
                    ("diary", P, "42", False)):
                with self.subTest(task=task, session=session, person=person):
                    request = self.engine.context_request(state, session, task=task, rows=[], focus=person,
                        text="只在私聊说过的担心")
                    await self.engine.prepare_context_memory(request)
                    self.assertEqual(bool(memory_context_items(state, request, items)), expected)
            self.engine.memory.schedule_review(items[0].id, next_at=self.clock.now())
            self.assertEqual(self.engine.memory.due_reviews(session_ids=[S, P], now=self.clock.now() + 86400), [])
            public = self.engine.memory.db.query_memories(session_ids=[S, P], exclude_source_scoped=True)
            self.assertFalse(any("只在私聊说过的担心" in item["content"] for item in public))

    async def test_legacy_runtime_notes_survive_sql_source_filters(self):
        await self.idle()
        for context in ("", "原来的普通文字", '{"persona_key":""}'):
            self.engine.memory.remember(session_id=S, persona_id="", node_id="study",
                content="原来的普通记录" + context, memory_type="inner", context=context)
        rows = self.engine.memory.db.query_memories(session_ids=[S], source_session=P,
            exclude_source_scoped=True, life_persona_key="new_character", memory_type="inner")
        self.assertEqual(len(rows), 3)

    async def test_same_worry_for_different_people_is_not_merged_on_archiving(self):
        await self.idle()
        async with self.engine.session_state(S) as state:
            for person in ("42", "7"):
                self.engine.note_heart_knot(state, "担心明天那件事", about=person)
            self.assertEqual(len(state.heart_knots), 2)
            self.engine._decay_heart_knots(state, now=self.clock.now() + 6 * 86400)
        rows = self.engine.memory.db.query_memories(session_id=S, memory_type="inner")
        self.assertEqual(len(rows), 2)
        self.assertEqual({tuple(row["participants"]) for row in rows}, {("42",), ("7",)})

    async def test_original_reply_path_keeps_private_worries_in_the_private_chat(self):
        self.add_group(sessions=[S, P], main=S)
        await self.idle()
        self.llm.replies = ['{"heart_knot":"只在私聊说过的担心","actions":[{"type":"say","messages":["嗯"]}]}']
        ctx = audit.fixture.EngineTestCase.ctx(self, session_id=P, user_id="42", text="我有点担心，只和你说。")
        await self.engine.handle_incoming(ctx)
        result = await self.engine.handle_reply(ctx)
        self.assertTrue(result.ok)
        self.assertIn("只在私聊说过的担心", await self.emotions(P))
        self.assertNotIn("只在私聊说过的担心", await self.emotions(S))

    async def test_disabling_emotions_hides_current_feelings_and_preserves_other_characters(self):
        await self.idle()
        await self.reflect(S, heart_knot="旧角色担心风车", grudge="旧角色还气着迟到")
        self.persona.text = "另一位完全不同的园艺少女。"
        self.engine.world.profile.grudge_daily_max = 10
        await self.reflect(S, heart_knot="当前角色担心花圃", grudge="当前角色还气着迟到")
        self.engine.world.profile.grudge_enabled = False
        self.engine.world.state_dynamics.heart_knot_enabled = False
        self.assertEqual(await self.emotions(S), "")
        async with self.engine.session_state(S) as state:
            self.engine._decay_heart_knots(state, now=self.clock.now())
            self.engine._decay_grudges(state, now=self.clock.now())
            self.assertEqual([row["text"] for row in state.heart_knots], ["旧角色担心风车"])
            self.assertEqual([row["reason"] for row in state.grudges], ["旧角色还气着迟到"])

    async def test_reading_emotions_does_not_rewrite_sources_or_decay_them(self):
        await self.idle()
        await self.reflect(S, heart_knot="担心风车", grudge="还气着迟到")
        async with self.engine.session_state(S) as state:
            before = copy.deepcopy((state.heart_knots, state.grudges))
            for _ in range(3):
                request = self.engine.context_request(state, S, rows=[], focus="42")
                await self.engine.prepare_context_memory(request)
                self.assertIn("担心风车", self.engine.context_emotions(state, request))
                self.engine.extra_reminders(state, ctx=SimpleNamespace(session_id=S, user_id="42"))
            self.assertEqual((state.heart_knots, state.grudges), before)


class EmotionContactApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = contact_api.RelationshipApiTests.asyncSetUp
    call = contact_api.RelationshipApiTests.call

    async def test_contact_detail_and_manual_clear_respect_the_selected_character(self):
        engine = self.plugin.engine
        engine._persona_text = AsyncMock(return_value="角色甲")
        engine.profiles.touch(self.session, "42")
        created = await self.call("profile/grudge", method="POST", session=self.session,
            user_id="42", action="add", reason="角色甲还气着迟到")
        self.assertEqual(created["status_code"], 200)
        detail = await self.call("profile/contact", session=self.session, user_id="42")
        self.assertIn("角色甲还气着迟到", detail["data"]["grudge"]["reason"])
        engine._persona_text.return_value = "角色乙"
        detail = await self.call("profile/contact", session=self.session, user_id="42")
        self.assertIsNone(detail["data"]["grudge"])
        await self.call("profile/grudge", method="POST", session=self.session, user_id="42", action="delete")
        engine._persona_text.return_value = "角色甲"
        detail = await self.call("profile/contact", session=self.session, user_id="42")
        self.assertIn("角色甲还气着迟到", detail["data"]["grudge"]["reason"])
        await self.call("profile/grudge", method="POST", session=self.session, user_id="42", action="resolve")
        detail = await self.call("profile/contact", session=self.session, user_id="42")
        self.assertIsNone(detail["data"]["grudge"])


class EmotionDirectoryApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = directory_api.SocialDirectoryApiTests.asyncSetUp
    call = directory_api.SocialDirectoryApiTests.call
    add_session = directory_api.SocialDirectoryApiTests.add_session
    other = directory_api.SocialDirectoryApiTests.other
    uid = directory_api.SocialDirectoryApiTests.uid

    async def test_directory_cards_use_the_actual_session_and_current_character_without_writes(self):
        self.add_session()
        raw = self.plugin.store.raw_sessions()
        raw["groups"] = [{"id": "together", "name": "同一个她", "sessions": [self.session, self.other],
                          "main_session": self.session}]
        self.plugin.store.save_sessions(raw)
        engine = self.plugin.engine
        engine.reload_config()
        engine._persona_text = AsyncMock(return_value="角色甲")
        for sid in (self.session, self.other):
            engine.profiles.touch(sid, self.uid)
        async with engine.session_state(self.other) as state:
            engine.note_grudge(state, "角色甲还气着迟到", user_id=self.uid, force=True)
        before = copy.deepcopy(self.plugin.db.raw.get_state(engine.state_key(self.other)))
        for persona, expected in (("角色甲", True), ("角色乙", False), ("角色甲", True)):
            engine._persona_text.return_value = persona
            for endpoint in ("relationships/person", "relationships"):
                with self.subTest(persona=persona, endpoint=endpoint):
                    response = await self.call(endpoint, user_id=self.uid, q=self.uid)
                    data = response["data"]
                    person = data["people"][0] if endpoint == "relationships" else data
                    rows = {row["session"]: row for row in person["sessions"]}
                    self.assertEqual(rows[self.other]["cold"], expected)
                    self.assertFalse(rows[self.session]["cold"])
        self.assertEqual(self.plugin.db.raw.get_state(engine.state_key(self.other)), before)

    async def test_relationship_edit_responses_keep_current_character_emotions(self):
        engine = self.plugin.engine
        engine._persona_text = AsyncMock(return_value="角色甲")
        engine.profiles.touch(self.session, self.uid)
        await self.call("profile/grudge", method="POST", session=self.session,
            user_id=self.uid, action="add", reason="角色甲还气着迟到")
        response = await self.call("relationships/apply", method="POST", user_id=self.uid,
            affinity=42, relations=["主人"], revision=0)
        self.assertTrue(response["data"]["person"]["sessions"][0]["cold"])
        response = await self.call("relationships/baseline", method="POST", user_id=self.uid, revision=1)
        self.assertTrue(response["data"]["person"]["sessions"][0]["cold"])
        for persona, expected in (("角色乙", False), ("角色甲", True)):
            engine._persona_text.return_value = persona
            response = await self.call("relationships/session", method="POST", session=self.session,
                user_id=self.uid, affinity=42, relations=["主人"])
            self.assertEqual(response["data"]["person"]["sessions"][0]["cold"], expected)


if __name__ == "__main__":
    unittest.main()
