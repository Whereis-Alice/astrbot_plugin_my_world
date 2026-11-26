"""The editor and dialogue use the same source and status for feelings."""
import json
import unittest
from unittest.mock import AsyncMock, patch

from core.engine.emotion_records import EmotionRecordConflict
from core.engine.legacy_emotion_context import legacy_emotion_evidence
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
        memory_id = self.engine.memory.remember(session_id=P, persona_id="", node_id="study", memory_type="inner",
            content="（跟他算清了一笔账）小明：折断了蓝风车", source="runtime",
            related_users=["77"], participants=[],
            context=json.dumps({"kind": "grudge", "original": "折断了蓝风车", "emotion_user": "42"}))
        self.engine.memory.db.update_memory(memory_id, created_at=self.clock.now())
        async with self.engine.session_state(P) as state:
            [row] = self.engine.emotion_records(state, P)
            self.assertEqual((row["status"], row["user_name"]), ("resolved", "小明"))
            self.assertEqual(self.engine.emotion_records(state, S), [])
            with self.assertRaises(ValueError):
                self.engine.update_emotion_record(state, S, row["id"], "delete", revision=row["revision"])
            self.assertEqual(self.engine.memory.db.query_memories(session_id=P, exclude_source_scoped=True), [])
            for focus, expected in (("42", [memory_id]), ("77", [])):
                request = self.engine.context_request(state, P, rows=[], focus=focus, text="折断了蓝风车")
                self.assertEqual([item.id for item in self.engine.context_memories(state, request)], expected)
        for origin, focus, topic, expected in ((P, "42", "折断了蓝风车", [memory_id]),
                (P, "77", "折断了蓝风车", []), (P, "", "折断了蓝风车", []),
                (P, "42", "", []), (S, "42", "折断了蓝风车", [])):
            with self.subTest(origin=origin, focus=focus, topic=topic):
                items, _ = await self.engine.lookup_life_memories(origin, "", topic, focus_user=focus)
                self.assertEqual([item.id for item in items], expected)

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

    async def test_archive_survives_a_failed_world_save_without_reactivating_or_reappearing(self):
        await self.idle()
        async with self.engine.session_state(S) as state:
            self.engine.note_grudge(state, "我被撕掉了纸风车", user_id="42")
            [original] = self.engine.emotion_records(state, S)
        db = self.engine.db.raw
        with patch.object(db, "save_state", side_effect=RuntimeError("world write failed")):
            with self.assertRaisesRegex(RuntimeError, "world write failed"):
                async with self.engine.session_state(S) as state:
                    self.assertTrue(self.engine.resolve_grudge(state, user_id="42"))

        async with self.engine.session_state(S) as state:
            self.assertEqual(len(state.grudges), 1)  # Stale world snapshot after the failed save.
            [saved] = self.engine.emotion_records(state, S)
            self.assertEqual((saved["id"], saved["status"]), (original["id"], "resolved"))
            self.assertIsNone(self.engine.grudge_for(state, "42"))
            request = self.engine.context_request(state, S, rows=[], focus="42", text="你还在意纸风车吗？")
            self.assertIsNone(self.engine.context_emotion(state, request))
            self.engine.update_emotion_record(state, S, saved["id"], "delete", revision=saved["revision"])

        state = await self.engine.load_state(S, cold_start=False)
        self.assertEqual(state.grudges, [])
        self.assertEqual(self.engine.emotion_records(state, S), [])

    async def test_deleted_archive_receipt_hides_stale_live_row_until_tick_can_clean_it(self):
        await self.idle()
        async with self.engine.session_state(S) as state:
            self.engine.note_heart_knot(state, "我还惦记那只风车", about="42")
            [original] = self.engine.emotion_records(state, S)
        db = self.engine.db.raw
        with patch.object(db, "save_state", side_effect=RuntimeError("world write failed")):
            with self.assertRaisesRegex(RuntimeError, "world write failed"):
                async with self.engine.session_state(S) as state:
                    self.engine.update_emotion_record(state, S, original["id"], "resolve", revision=original["revision"])
            with self.assertRaisesRegex(RuntimeError, "world write failed"):
                async with self.engine.session_state(S) as state:
                    [saved] = self.engine.emotion_records(state, S)
                    self.engine.update_emotion_record(state, S, saved["id"], "delete", revision=saved["revision"])

        async with self.engine.session_state(S) as state:
            self.assertEqual(len(state.heart_knots), 1)
            self.assertEqual(self.engine.emotion_records(state, S), [])
            self.engine._decay_heart_knots(state, now=self.clock.now())
            self.assertEqual(state.heart_knots, [])

    async def test_native_dialogue_uses_only_current_relevant_record_once(self):
        await self.idle()
        async with self.engine.session_state(S) as state:
            self.engine.note_heart_knot(state, "有点担心纸风车被踩坏", about="42")
        for query, expected in (("吃面还是吃饭？", 0), ("她只问了‘你怎么了’，我转达一下。", 0),
                ("他说‘纸风车被踩坏’，只是举个例子。", 0), ("你还担心纸风车吗？", 1)):
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
        apology = "折断了你的纸风车，对不起。"
        entries = [{"user_id": "42", "text": apology, "message_id": "apology", "at": self.clock.now()}]
        [source] = self.engine.interaction_sources(S, "42", apology, entries)

        async def edit_during_generation(session, system, prompt):
            self.assertIn("折断了纸风车", prompt)
            async with self.engine.session_state(S) as state:
                [record] = self.engine.emotion_records(state, S)
                self.engine.update_emotion_record(state, S, record["id"], "edit",
                    text="弄坏了另一只纸风车", revision=record["revision"])
            return json.dumps({"addressing": "me", "forgive": {"record_id": record["id"],
                "kind": "apology", "source_id": source["id"], "quote": apology}}, ensure_ascii=False)

        self.engine._ask_judge = AsyncMock(side_effect=edit_during_generation)
        await self.engine.reflect_turn(S, user_id="42", user_text=apology, reply_text="好吧。",
                                       interaction_entries=entries)
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

    async def test_same_direct_source_does_not_record_a_second_headline(self):
        await self.idle()
        quote = "那只风车的叶片已经泡软了"
        entries = [{"user_id": "42", "text": quote, "at": self.clock.now(), "message_id": "windmill"}]
        for title in ("雨里的纸风车", "那把泡软的叶片"):
            self.engine._ask_judge = AsyncMock(return_value=json.dumps({"heart_knot": {
                "summary": title, "narrative": "听见叶片坏了，我还舍不得。", "evidence": [quote]}, "addressing": "me"}))
            await self.engine.reflect_turn(S, user_id="42", user_text=quote, reply_text="我想看看能不能修。",
                interaction_entries=entries)
            self.clock.advance(30)
        state = await self.engine.load_state(S, cold_start=False)
        [row] = self.engine.emotion_records(state, S)
        self.assertEqual(row["text"], "雨里的纸风车")
        self.assertEqual(row["created_at"], entries[0]["at"])

    async def test_words_addressed_to_someone_else_do_not_ground_a_record(self):
        await self.idle()
        quote = "那只风车的叶片已经泡软了"
        self.engine._ask_judge = AsyncMock(return_value=json.dumps({"heart_knot": {
            "summary": "担心那只风车", "narrative": "我听了很舍不得。", "evidence": [quote]}, "addressing": "me"}))
        await self.engine.reflect_turn(S, user_id="42", user_text=quote, reply_text="我听见了。",
            interaction_entries=[{"user_id": "42", "text": quote, "at": self.clock.now(), "addressing": "others"}])
        state = await self.engine.load_state(S, cold_start=False)
        self.assertEqual(self.engine.emotion_records(state, S), [])

    async def test_legacy_editor_recovers_only_untampered_direct_evidence(self):
        await self.idle()
        now = self.clock.now()
        async with self.engine.session_state(S) as state:
            for speaker, quote, flags in (("42", "那只风车的叶片泡软了", {}),
                    ("43", "他说过‘风车已经坏了’", {}),
                    ("44", "那只风车在屋里", {"text_truncated": True}),
                    ("45", "风车已经修好了", {"kind": "quote"})):
                state.recent_chat.append({"user_id": speaker, "name": speaker, "text": quote,
                                          "origin": S, "at": now - 5, **flags})
                await self.engine._log_event(state, "reflection", {"at": now, "user": speaker, "user_text": quote,
                    "heart_knot": f"关于风车的事 {speaker}"}, place=S)
            items = [{"id": speaker, "created_at": now, "user_id": speaker, "evidence": [],
                      "kind": "heart_knot", "text": f"关于风车的事 {speaker}"}
                     for speaker in ("42", "43", "44", "45")]
            items[0].update(text="我改了这个标题", source_summary="关于风车的事 42")
            recovered = await legacy_emotion_evidence(self.engine, state, S, items)
            self.assertEqual(recovered, {"42": [{"text": "那只风车的叶片泡软了", "at": now - 5,
                "user_name": "42", "recovered": True}]})

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
