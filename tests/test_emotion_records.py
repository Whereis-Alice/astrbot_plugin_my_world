"""The editor and dialogue use the same source and status for feelings."""
import json
import unittest
from unittest.mock import AsyncMock, patch

from core.engine.emotion_records import EmotionRecordConflict
from tests import test_life_audit as audit

S, P = audit.S, audit.P


class EmotionRecordTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = audit.LifeAuditTests.asyncSetUp
    add_group = audit.LifeAuditTests.add_group
    idle = audit.LifeAuditTests.idle

    async def test_legacy_edit_pause_resume_resolve_and_delete_keep_one_identity(self):
        await self.idle()
        async with self.engine.session_state(S) as state:
            now = self.clock.now()
            state.heart_knots = [{"text": "担心原来的纸风车", "about": "42", "since": now,
                                  "strength": .7, "at": now}]
            [first] = self.engine.emotion_records(state, S)
            self.assertEqual(first["status"], "active")
            self.assertTrue(first["affects_dialogue"])
        async with self.engine.session_state(S) as state:
            self.engine._decay_heart_knots(state, now=now + 60)
            [still] = self.engine.emotion_records(state, S)
            self.assertEqual(still["revision"], first["revision"])
            record_id = first["id"]
            edited = self.engine.update_emotion_record(state, S, record_id, "edit",
                text="担心蓝色纸风车", revision=first["revision"])
            self.assertEqual(edited["id"], record_id)
            self.assertEqual(edited["created_at"], now)
            with self.assertRaises(EmotionRecordConflict):
                self.engine.update_emotion_record(state, S, record_id, "delete", revision=first["revision"])
            paused = self.engine.update_emotion_record(state, S, record_id, "pause", revision=edited["revision"])
            self.assertEqual(paused["status"], "paused")
            self.assertFalse(paused["affects_dialogue"])
            self.engine.note_heart_knot(state, paused["text"], about="42")
            self.assertEqual(len(state.heart_knots), 1)
            resumed = self.engine.update_emotion_record(state, S, record_id, "resume", revision=paused["revision"])
            self.assertTrue(resumed["affects_dialogue"])
            closed = self.engine.update_emotion_record(state, S, record_id, "resolve", revision=resumed["revision"])
            self.assertEqual((closed["status"], closed["archived"]), ("resolved", True))
            self.assertEqual(closed["id"], record_id)
            self.assertEqual(state.heart_knots, [])
        async with self.engine.session_state(S) as state:
            [saved] = self.engine.emotion_records(state, S)
            saved = self.engine.update_emotion_record(state, S, record_id, "edit",
                text="已经放下纸风车那件事", revision=saved["revision"])
            self.assertIn("已经放下纸风车那件事", self.engine.memory.db.query_emotion_memories(session_id=S)[0]["content"])
            self.engine.update_emotion_record(state, S, record_id, "delete", revision=saved["revision"])
            self.assertEqual(self.engine.emotion_records(state, S), [])

    async def test_archived_legacy_feeling_stays_in_the_original_conversation(self):
        self.add_group(sessions=[S, P], main=S)
        await self.idle()
        self.engine.memory.remember(session_id=P, persona_id="", node_id="study", memory_type="inner",
            content="（跟他算清了一笔账）小明：折断了蓝风车", source="runtime",
            context=json.dumps({"kind": "grudge", "original": "折断了蓝风车", "emotion_user": "42"}))
        async with self.engine.session_state(P) as state:
            [row] = self.engine.emotion_records(state, P)
            self.assertEqual((row["status"], row["user_name"]), ("resolved", "小明"))
            self.assertEqual(self.engine.emotion_records(state, S), [])
            with self.assertRaises(ValueError):
                self.engine.update_emotion_record(state, S, row["id"], "delete", revision=row["revision"])
            self.assertEqual(self.engine.memory.db.query_memories(session_id=P, exclude_source_scoped=True), [])

    async def test_disabled_and_expired_live_rows_remain_editable_without_dialogue_effect(self):
        await self.idle()
        async with self.engine.session_state(S) as state:
            self.engine.note_grudge(state, "忘了修纸风车", user_id="42")
            [row] = self.engine.emotion_records(state, S)
            self.engine.world.profile.grudge_enabled = False
            self.engine._decay_grudges(state, now=self.clock.now())
            [disabled] = self.engine.emotion_records(state, S)
            self.assertEqual(disabled["id"], row["id"])
            self.assertFalse(disabled["feature_enabled"])
            self.assertIsNone(self.engine.grudge_for(state, "42"))
            self.engine.world.profile.grudge_enabled = True
            self.clock.advance(8 * 86400)
            [expired] = self.engine.emotion_records(state, S)
            self.assertEqual((expired["status"], expired["archived"]), ("expired", False))
            self.assertIsNone(self.engine.grudge_for(state, "42"))
            self.engine._decay_grudges(state, now=self.clock.now())
            [archived] = self.engine.emotion_records(state, S)
            self.assertEqual((archived["id"], archived["status"], archived["archived"]), (row["id"], "expired", True))

    async def test_failed_archive_retains_the_original_record(self):
        await self.idle()
        async with self.engine.session_state(S) as state:
            self.engine.note_grudge(state, "忘了修纸风车", user_id="42")
            [row] = self.engine.emotion_records(state, S)
            with patch.object(self.engine.memory, "remember", return_value=None):
                with self.assertRaisesRegex(ValueError, "原记录仍保留"):
                    self.engine.update_emotion_record(state, S, row["id"], "resolve", revision=row["revision"])
            self.assertEqual(self.engine.emotion_records(state, S), [row])

    async def test_native_dialogue_uses_only_current_relevant_record_once(self):
        await self.idle()
        async with self.engine.session_state(S) as state:
            self.engine.note_heart_knot(state, "有点担心纸风车被踩坏", about="42")
        for query, expected in (("吃面还是吃饭？", 0), ("你还担心纸风车吗？", 1)):
            ctx = audit.fixture.EngineTestCase.ctx(self, session_id=S, text=query, user_id="42", is_mentioned=True)
            await self.engine.handle_incoming(ctx)
            result = await self.engine.handle_reply(ctx, inject_only=True)
            self.assertTrue(result.ok)
            self.assertEqual(result.runtime_part.count("有点担心纸风车被踩坏"), expected)
            self.assertNotIn("有点担心纸风车被踩坏", result.system_prompt)

    async def test_reflection_cannot_forgive_a_record_admin_edited_during_the_request(self):
        await self.idle()
        async with self.engine.session_state(S) as state:
            self.engine.note_grudge(state, "折断了纸风车", user_id="42")

        async def edit_during_generation(session, system, prompt):
            self.assertIn("折断了纸风车", prompt)
            async with self.engine.session_state(S) as state:
                [record] = self.engine.emotion_records(state, S)
                self.engine.update_emotion_record(state, S, record["id"], "edit",
                    text="弄坏了另一只纸风车", revision=record["revision"])
            return '{"forgive":true}'

        self.engine._ask_judge = AsyncMock(side_effect=edit_during_generation)
        await self.engine.reflect_turn(S, user_id="42", user_text="折断了你的纸风车，对不起。", reply_text="好吧。")
        async with self.engine.session_state(S) as state:
            [current] = self.engine.emotion_records(state, S)
            self.assertEqual((current["status"], current["text"]), ("active", "弄坏了另一只纸风车"))

    async def test_first_person_reflection_and_direct_evidence_persist_through_archive(self):
        await self.idle()
        quote = "那只纸风车的叶片被雨泡软了"
        narrative = "他说风车的叶片被雨泡软了。我做了好久，听见以后还是舍不得。\n\n现在只想先把它晾干看看。"
        self.engine._ask_judge = AsyncMock(return_value=json.dumps({"heart_knot": {
            "summary": "雨里的纸风车", "narrative": narrative, "evidence": [quote]}, "addressing": "me"}))
        now = self.clock.now()
        await self.engine.reflect_turn(S, user_id="42", user_name="小明", user_text=quote, reply_text="我想先晾晾它。",
            interaction_entries=[{"user_id": "42", "name": "小明", "text": quote, "origin": S, "at": now}])
        self.assertIn("第一人称自述", self.engine._ask_judge.call_args.args[1])
        async with self.engine.session_state(S) as state:
            [live] = self.engine.emotion_records(state, S)
            evidence = [{"text": quote, "at": now, "user_name": "小明"}]
            self.assertEqual((live["text"], live["narrative"], live["evidence"]), ("雨里的纸风车", narrative, evidence))
            edited = self.engine.update_emotion_record(state, S, live["id"], "edit", text=live["text"],
                narrative="我想等天晴了看看还能不能修。", revision=live["revision"])
            archived = self.engine.update_emotion_record(state, S, live["id"], "resolve", revision=edited["revision"])
            self.assertEqual(archived["narrative"], edited["narrative"])
            self.assertEqual(archived["evidence"], evidence)
        state = await self.engine.load_state(S, cold_start=False)
        [saved] = self.engine.emotion_records(state, S)
        self.assertEqual((saved["narrative"], saved["evidence"]), (edited["narrative"], evidence))

    async def test_structured_record_rejects_invented_or_indirect_quotes(self):
        await self.idle()
        for quote in ("风车已经修好了", "整只风车都踩坏了"):
            self.engine._ask_judge = AsyncMock(return_value=json.dumps({"heart_knot": {
                "summary": "担心风车", "narrative": "我听见以后有点在意。", "evidence": [quote]}, "addressing": "me"}))
            await self.engine.reflect_turn(S, user_id="42", user_text="他说过‘整只风车都踩坏了’，我还没有看到。",
                reply_text="那得等亲眼看见再说。")
        state = await self.engine.load_state(S, cold_start=False)
        self.assertEqual(self.engine.emotion_records(state, S), [])

    async def test_new_automatic_heart_knot_requires_a_complete_account(self):
        await self.idle()
        self.engine._ask_judge = AsyncMock(return_value='{"heart_knot":"风车的事","addressing":"me"}')
        await self.engine.reflect_turn(S, user_id="42", user_text="那只风车的叶片淋坏了", reply_text="我知道了。")
        state = await self.engine.load_state(S, cold_start=False)
        self.assertEqual(self.engine.emotion_records(state, S), [])

    async def test_bulk_delete_is_atomic_across_live_and_archived_origins(self):
        self.add_group(sessions=[S, P], main=S)
        await self.idle()
        async with self.engine.session_state(P) as state:
            self.engine.note_heart_knot(state, "那只纸风车", about="42", session_id=P)
            [private] = self.engine.emotion_records(state, P)
            archived = self.engine.update_emotion_record(state, P, private["id"], "resolve", revision=private["revision"])
        async with self.engine.session_state(S) as state:
            self.engine.note_grudge(state, "忘了修纸风车", user_id="42")
            [live] = self.engine.emotion_records(state, S)
        batch = [{key: row[key] for key in ("session", "id", "revision")} for row in (archived, live)]

        async with self.engine.session_state(S) as state:
            with self.assertRaises(EmotionRecordConflict):
                await self.engine.delete_emotion_records(state, [batch[0], {**batch[1], "revision": "stale"}])
        self.assertEqual(len(self.engine.memory.db.query_emotion_memories(session_id=P)), 1)

        with patch.object(self.engine.memory.db, "save_state", side_effect=RuntimeError("write failed")):
            async with self.engine.session_state(S) as state:
                with self.assertRaisesRegex(RuntimeError, "write failed"):
                    await self.engine.delete_emotion_records(state, batch)
        self.assertEqual(len(self.engine.memory.db.query_emotion_memories(session_id=P)), 1)

        async with self.engine.session_state(S) as state:
            self.assertEqual(await self.engine.delete_emotion_records(state, batch), 2)
            self.assertEqual(state.grudges, [])
        saved = await self.engine.load_state(S, cold_start=False)
        self.assertEqual(saved.grudges, [])
        self.assertEqual(self.engine.memory.db.query_emotion_memories(session_id=P), [])
