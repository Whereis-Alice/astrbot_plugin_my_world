"""Sleep reviews must preserve source identity and concurrent human corrections."""

import asyncio
import json
import unittest
from unittest.mock import patch

from core.evidence import source_snapshot
from tests import test_consolidate as fixture

SESSION = fixture.SESSION
USER = fixture.USER


class SleepReviewIntegrityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.ConsolidateTestCase.asyncSetUp

    async def test_delayed_digest_cannot_replace_manual_edit(self):
        self.profiles.touch(SESSION, USER, "小明")
        self.profiles.set_digest(SESSION, USER, "喜欢画画")

        async def model(*_):
            self.profiles.set_digest(SESSION, USER, "管理员核实：只喜欢油画")
            return json.dumps({"digests": {USER: "喜欢水彩和油画"}})

        result = await self.consolidator.run(
            session_id=SESSION, llm=model,
            chat_records=[{"user_id": USER, "text": "最近在画画"}],
        )
        self.assertEqual(self.profiles.profile(SESSION, USER)["digest"], "管理员核实：只喜欢油画")
        self.assertEqual(result.digests, 0)
        self.assertTrue(result.skipped)
        self.assertFalse(result.retry_needed)

    async def test_overlapping_batches_only_present_new_sources(self):
        self.profiles.touch(SESSION, USER, "小明")
        old = {"user_id": USER, "text": "我喜欢猫", "message_id": "old"}
        new = {"user_id": USER, "text": "我习惯早起", "message_id": "new"}
        calls = []

        async def model(_system, prompt):
            calls.append(prompt)
            return "{}"

        await self.consolidator.run(session_id=SESSION, llm=model, chat_records=[old])
        await self.consolidator.run(session_id=SESSION, llm=model, chat_records=[old, new])
        await self.consolidator.run(session_id=SESSION, llm=model, chat_records=[old])
        self.assertEqual(len(calls), 2)
        self.assertNotIn("我喜欢猫", calls[1])
        self.assertIn("我习惯早起", calls[1])

    async def test_immutable_source_keeps_its_original_person_and_session(self):
        self.profiles.touch(SESSION, USER, "真正的发言人")
        self.profiles.touch(SESSION, "99", "另一人")
        original = {"user_id": USER, "text": "我喜欢猫", "origin": SESSION, "message_id": "one"}
        source = source_snapshot(original, SESSION)
        self.db.observe_source(source, eligible=True)
        seen = []

        async def model(_system, prompt):
            seen.append(prompt)
            return json.dumps({"facts": [{"user_id": "99", "text": "喜欢猫", "evidence": "我喜欢猫"}]})

        await self.consolidator.run(session_id=SESSION, llm=model,
                                    chat_records=[{**original, "source_id": source["id"], "user_id": "99"}])
        self.assertIn(f"（QQ {USER}）: 我喜欢猫", seen[0])
        self.assertEqual(self.profiles.facts(SESSION, "99"), [])

    async def test_dreams_do_not_hide_older_lived_material_before_limit(self):
        mid = self.memory.remember(session_id=SESSION, persona_id="", node_id="study", content="修好了风车")
        for i in range(125):
            self.db.add_memory(session_id=SESSION, persona_id="", node_id="study",
                               content=f"第{i}个梦", memory_type="dream", source="dream", scope="group")
        lines = self.consolidator.memory_lines(SESSION)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith(f"#{mid} "))

    async def test_late_review_cannot_erase_a_corrected_location(self):
        mid = self.memory.remember(session_id=SESSION, persona_id="", node_id="study", content="修好了风车")

        async def model(*_):
            self.db.revise_memory(mid, {"node_id": "garden"})
            return json.dumps({"forget": [{"id": mid, "text": "在书房修好了风车"}], "dream": "书房飞起来"})

        result = await self.consolidator.run(session_id=SESSION, llm=model)
        self.assertEqual(self.db.memory_by_id(mid)["content"], "修好了风车")
        self.assertFalse(result.dream)
        self.assertTrue(result.skipped)

    async def test_stable_contact_edit_does_not_discard_another_person(self):
        for uid in (USER, "99"):
            self.profiles.touch(SESSION, uid, uid)

        async def model(*_):
            self.profiles.set_note(SESSION, USER, "管理员核实的备注")
            # Seeing a message is ordinary presence, not a contact correction.
            self.profiles.touch(SESSION, "99", "新名片")
            return json.dumps({"digests": {USER: "过时结论", "99": "喜欢画画"}})

        result = await self.consolidator.run(session_id=SESSION, llm=model, chat_records=[
            {"user_id": USER, "text": "聊一点近况"},
            {"user_id": "99", "text": "我喜欢画画"},
        ])
        self.assertEqual(self.profiles.profile(SESSION, USER)["digest"], "")
        self.assertEqual(self.profiles.profile(SESSION, "99")["digest"], "喜欢画画")
        self.assertEqual(result.digests, 1)

    async def test_bystander_digest_cannot_change_without_direct_material(self):
        self.profiles.touch(SESSION, "99", "未发言的人")
        self.profiles.set_digest(SESSION, "99", "已核实的资料")

        async def model(*_):
            return json.dumps({"digests": {"99": "没有依据的新资料"}})

        result = await self.consolidator.run(session_id=SESSION, llm=model,
                                            chat_records=[{"user_id": USER, "text": "我今天去画画"}])
        self.assertEqual(self.profiles.profile(SESSION, "99")["digest"], "已核实的资料")
        self.assertEqual(result.digests, 0)

    async def test_same_persons_digests_remain_independent_in_shared_life(self):
        self.consolidator._sessions_of = lambda _: [SESSION, fixture.PRIVATE]
        for sid in (SESSION, fixture.PRIVATE):
            self.profiles.touch(sid, USER)

        async def model(_system, prompt):
            self.assertIn("完整会话标识", prompt)
            return json.dumps({"digests": [
                {"user_id": USER, "source_session": SESSION, "text": "在群里习惯聊画画"},
                {"user_id": USER, "source_session": fixture.PRIVATE, "text": "私下喜欢聊猫"},
            ]})

        result = await self.consolidator.run(session_id=SESSION, llm=model, chat_records=[
            {"user_id": USER, "origin": SESSION, "text": "我喜欢油画"},
            {"user_id": USER, "origin": fixture.PRIVATE, "text": "我喜欢猫"},
        ])
        self.assertEqual(result.digests, 2)
        self.assertEqual(self.profiles.profile(SESSION, USER)["digest"], "在群里习惯聊画画")
        self.assertEqual(self.profiles.profile(fixture.PRIVATE, USER)["digest"], "私下喜欢聊猫")

    async def test_receipts_survive_restart_and_legacy_batch_is_recognized(self):
        record = {"user_id": USER, "text": "我喜欢猫"}
        canonical = {**record, "source_id": source_snapshot(record, SESSION)["id"]}
        self.db.kv_set(self.consolidator._batch_key(SESSION, "full", [canonical]), {"at": 1})
        calls = []

        async def model(*_):
            calls.append(True)
            return "{}"

        await self.consolidator.run(session_id=SESSION, llm=model, chat_records=[record], dry_run=True)
        self.assertEqual(self.db._query("SELECT * FROM profile_review_source"), [])
        await self.consolidator.run(session_id=SESSION, llm=model, chat_records=[record])
        self.assertEqual(len(self.db._query("SELECT * FROM profile_review_source")), 1)
        self.db.close()
        # The normal reopen path reloads receipts from SQLite rather than RAM.
        await self.consolidator.run(session_id=SESSION, llm=model, mode="nap", chat_records=[record])
        self.assertEqual(calls, [])

    async def test_stored_source_session_and_role_override_a_mutated_window(self):
        self.consolidator._sessions_of = lambda _: [SESSION, fixture.PRIVATE]
        original = {"user_id": USER, "origin": fixture.PRIVATE, "text": "我喜欢猫", "is_self": False}
        source = source_snapshot(original, SESSION)
        self.db.observe_source(source, eligible=True)
        prompts = []

        async def model(_system, prompt):
            prompts.append(prompt)
            return json.dumps({"facts": [{"user_id": USER, "text": "喜欢猫", "evidence": "我喜欢猫",
                                          "source_session": fixture.PRIVATE}]})

        await self.consolidator.run(session_id=SESSION, llm=model, chat_records=[
            {**original, "source_id": source["id"], "origin": SESSION, "is_self": True},
        ])
        self.assertIn(f"session={fixture.PRIVATE}", prompts[0])
        self.assertNotIn("（她自己）", prompts[0])
        self.assertEqual(self.profiles.facts(SESSION, USER), [])
        self.assertEqual(len(self.profiles.facts(fixture.PRIVATE, USER)), 1)

    async def test_quote_source_cannot_be_relabelled_as_direct_speech(self):
        original = {"user_id": USER, "text": "别人说的偏好", "kind": "quote"}
        source = source_snapshot(original, SESSION)
        self.db.observe_source(source, eligible=True)
        calls = []

        async def model(*_):
            calls.append(True)
            return "{}"

        await self.consolidator.run(session_id=SESSION, llm=model, chat_records=[
            {**original, "kind": "direct_speech", "source_id": source["id"]},
        ])
        self.assertEqual(calls, [])

    async def test_revoked_source_eligibility_invalidates_delayed_output(self):
        self.profiles.touch(SESSION, USER)
        record = {"user_id": USER, "text": "我喜欢猫"}
        source = source_snapshot(record, SESSION)
        self.db.observe_source(source, eligible=True)

        async def model(*_):
            self.db._execute("UPDATE memory_source SET eligible=0 WHERE id=?", (source["id"],))
            return json.dumps({"facts": [{"user_id": USER, "text": "喜欢猫", "evidence": "我喜欢猫"}]})

        result = await self.consolidator.run(session_id=SESSION, llm=model, chat_records=[record])
        self.assertEqual(self.profiles.facts(SESSION, USER), [])
        self.assertFalse(self.db.reviewed_profile_sources([source["id"]], "full"))
        self.assertTrue(result.skipped)

    async def test_nap_does_not_consume_a_full_reviews_fact_sources(self):
        self.profiles.touch(SESSION, USER)
        record = {"user_id": USER, "text": "我喜欢猫"}
        calls = []

        async def model(*_):
            calls.append(True)
            return json.dumps({"digests": {USER: "喜欢猫"}, "facts": [
                {"user_id": USER, "text": "喜欢猫", "evidence": "我喜欢猫"}]})

        await self.consolidator.run(session_id=SESSION, llm=model, mode="nap", chat_records=[record])
        self.assertEqual(self.profiles.facts(SESSION, USER), [])
        await self.consolidator.run(session_id=SESSION, llm=model, mode="full", chat_records=[record])
        self.assertEqual(len(self.profiles.facts(SESSION, USER)), 1)
        await self.consolidator.run(session_id=SESSION, llm=model, mode="nap", chat_records=[record])
        self.assertEqual(len(calls), 2)

    async def test_prompt_limit_leaves_unseen_sources_for_the_next_review(self):
        records = [{"user_id": USER, "text": f"直接发言编号{i:04d}", "message_id": str(i)} for i in range(405)]
        prompts = []

        async def model(_system, prompt):
            prompts.append(prompt)
            return "{}"

        await self.consolidator.run(session_id=SESSION, llm=model, chat_records=records)
        self.assertIn("直接发言编号0399", prompts[0])
        self.assertNotIn("直接发言编号0400", prompts[0])
        await self.consolidator.run(session_id=SESSION, llm=model, chat_records=records)
        self.assertIn("直接发言编号0400", prompts[1])
        self.assertNotIn("直接发言编号0399", prompts[1])
        await self.consolidator.run(session_id=SESSION, llm=model, chat_records=records)
        self.assertEqual(len(prompts), 2)

    async def test_failed_output_and_preview_do_not_consume_sources(self):
        record = {"user_id": USER, "text": "我喜欢猫"}
        replies = iter(("模型暂时不可用", "{}", "{}"))

        async def model(*_):
            return next(replies)

        await self.consolidator.run(session_id=SESSION, llm=model, chat_records=[record])
        await self.consolidator.run(session_id=SESSION, llm=model, chat_records=[record], dry_run=True)
        self.assertEqual(self.db._query("SELECT * FROM profile_review_source"), [])
        self.assertEqual(self.db._query("SELECT * FROM memory_source"), [])
        self.assertTrue((await self.consolidator.run(session_id=SESSION, llm=model, chat_records=[record])).ok)
        self.assertEqual(len(self.db._query("SELECT * FROM profile_review_source")), 1)

    async def test_receipts_and_fact_writes_roll_back_together(self):
        self.profiles.touch(SESSION, USER)
        record = {"user_id": USER, "text": "我喜欢猫"}

        async def model(*_):
            return json.dumps({"facts": [{"user_id": USER, "text": "喜欢猫", "evidence": "我喜欢猫"}]})

        with patch.object(self.db, "mark_profile_reviewed", side_effect=RuntimeError("write failed")):
            with self.assertRaisesRegex(RuntimeError, "write failed"):
                await self.consolidator.run(session_id=SESSION, llm=model, chat_records=[record])
        self.assertEqual(self.profiles.facts(SESSION, USER), [])
        self.assertEqual(self.db._query("SELECT * FROM memory_source"), [])
        self.assertEqual(self.db._query("SELECT * FROM profile_review_source"), [])
        await self.consolidator.run(session_id=SESSION, llm=model, chat_records=[record])
        self.assertEqual(len(self.profiles.facts(SESSION, USER)), 1)

    async def test_concurrent_overlap_discards_stale_result_and_leaves_new_sources(self):
        old = {"user_id": USER, "text": "我喜欢猫", "message_id": "old"}
        new = {"user_id": USER, "text": "我喜欢油画", "message_id": "new"}
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed(*_):
            entered.set()
            await release.wait()
            return json.dumps({"digests": {USER: "过时的整批结论"}})

        prompts = []

        async def quick(_system, prompt):
            prompts.append(prompt)
            return "{}"

        task = asyncio.create_task(self.consolidator.run(session_id=SESSION, llm=delayed, chat_records=[old, new]))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await self.consolidator.run(session_id=SESSION, llm=quick, chat_records=[old])
        finally:
            release.set()
        result = await asyncio.wait_for(task, 2)
        self.assertTrue(result.skipped)
        self.assertEqual(result.digests, 0)
        await self.consolidator.run(session_id=SESSION, llm=quick, chat_records=[old, new])
        self.assertIn("我喜欢油画", prompts[1])
        self.assertNotIn("我喜欢猫", prompts[1])

    async def test_recall_weight_change_does_not_trigger_another_life_call(self):
        mid = self.memory.remember(session_id=SESSION, persona_id="", node_id="study", content="修好了风车")
        calls = []

        async def model(*_):
            calls.append(True)
            return "{}"

        await self.consolidator.run(session_id=SESSION, llm=model)
        self.db.mark_recalled([mid])
        await self.consolidator.run(session_id=SESSION, llm=model)
        self.assertEqual(len(calls), 1)

    async def test_life_material_includes_location_and_does_not_merge_across_it(self):
        ids = [self.memory.remember(session_id=SESSION, persona_id="", node_id=node, content=text)
               for node, text in (("study", "修好了风车"), ("garden", "修剪了花枝"))]

        async def model(_system, prompt):
            self.assertIn("地点=", prompt)
            self.assertIn("scope=", prompt)
            return json.dumps({"merge": [{"keep_id": ids[0], "drop_ids": [ids[1]], "text": "在书房修好了风车"}]})

        await self.consolidator.run(session_id=SESSION, llm=model)
        self.assertEqual(self.db.memory_by_id(ids[0])["content"], "修好了风车")
        self.assertEqual(self.db.memory_by_id(ids[1])["content"], "修剪了花枝")
        self.assertEqual(self.db.memory_by_id(ids[1])["tier"], "raw")

    async def test_invalid_merge_cannot_partly_rewrite_a_protected_record_group(self):
        first = self.memory.remember(session_id=SESSION, persona_id="", node_id="study", content="做过一次实验")
        second = self.memory.remember(session_id=SESSION, persona_id="", node_id="study", content="实验失败了", pinned=True)

        async def model(*_):
            return json.dumps({"merge": [{"keep_id": first, "drop_ids": [second], "text": "实验全部成功了"}]})

        result = await self.consolidator.run(session_id=SESSION, llm=model)
        self.assertEqual(self.db.memory_by_id(first)["content"], "做过一次实验")
        self.assertEqual(self.db.memory_by_id(second)["content"], "实验失败了")
        self.assertEqual(result.merged, 0)
        self.assertTrue(result.skipped)
        self.assertFalse(result.retry_needed)
