"""Seven synthetic days exercise real work accounting, restart and stage review."""
import copy
import json
import unittest
from hashlib import sha256

from core import life_matters as matters
from core.engine import TickOutcome
from core.state import WorldState
from tests import test_engine as fixture
from tests import test_life_matters as fixtures

S = fixture.SESSION


class LifeBookmarkTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixtures.MattersAcceptanceTests.asyncSetUp
    ctx = fixture.EngineTestCase.ctx
    add_group = fixture.EngineTestCase.add_group

    async def prepare(self):
        state = await self.engine.load_state(S, cold_start=False)
        state.state, state.energy, state.world_time = "idle", .9, 10
        item = self.engine.add_life_matter(state, "读完风车的故事并留下章节笔记")
        item["steps"] = [{"id": "chapter", "text": "对照第二章与第一封信", "goal": item["text"],
                          "action": "read", "node": "study", "minutes": 35, "status": "pending",
                          "focus": "第二章的风车为何在黎明停转", "open_question": "信封上的日期是否矛盾"}]
        return state, item

    async def test_seven_days_resume_one_step_then_review_without_restarting(self):
        state, item = await self.prepare()
        mid = item["id"]
        notes = ["第一封信比停转记录早一天", "第二章只写了黎明没有日期", "两段叙述的时间参照不同",
                 "先保留日期矛盾而非认定作者写错", "重读信封邮戳", "还要对照末页手写日期"]
        for day in range(7):
            with self.subTest(day=day + 1):
                self.assertIsNone(state.current_action)
                await self.engine._start_matter_step(state, item)
                self.assertEqual(state.current_action["matter_step_id"], "chapter")
                self.assertEqual(state.current_action["duration_ticks"], 35 - day * 5)
                state.current_action["elapsed_ticks"] = 5
                self.clock.advance(300)
                self.engine.settle_matter_work(state, state.current_action)
                self.engine.pause_matter_action(state, state.current_action)
                self.engine.pause_matter_action(state, state.current_action)
                self.assertEqual(item["steps"][0]["worked_minutes"], (day + 1) * 5)
                self.assertEqual(item["worked_minutes"], 5, "restart or repeated pause cannot count minutes twice")
                if day < 6:
                    basis = {**matters.current_bookmark(item), "revision": item["revision"]}
                    self.assertTrue(matters.record_bookmark_note(item,
                        {"matter_id": mid, "step_id": "chapter", "note": notes[day],
                         "next_focus": f"从第{day + 2}处日期线索继续", "open_question": "邮戳与手写日期是否一致"},
                        basis, now=self.clock.now()))
                    self.assertEqual(item["progress"], 0)
                    self.assertFalse(state.life_artifacts)
                    state.current_action = state.current_plan = None
                    state.state = "idle"
                    await self.engine.save_state(state)
                    state = WorldState.from_payload(await self.db.call("get_state", S), S)
                    item = matters.find(state, mid)
                    self.assertEqual(matters.current_bookmark(item)["note"], notes[day])
                    self.assertEqual(matters.current_bookmark(item)["next_focus"], f"从第{day + 2}处日期线索继续")
                    self.clock.advance(86400 - 300)
                else:
                    self.engine._record_matter_action(state, state.current_action)
                    self.assertEqual(item["steps"][0]["status"], "done")
                    self.assertTrue(item["needs_review"])
                    self.assertFalse(matters.current_bookmark(item))
        state.current_action = state.current_plan = None
        state.state = "idle"
        await self.engine.save_state(state)
        self.llm.replies = [json.dumps({"outcome": "continue", "reason": "还需要对照结尾",
            "stage": {"summary": "确认邮戳与手写日期属于两次寄信", "content": "信封并未矛盾，第二章保留叙述视角的差别。",
                      "next_focus": "核对结尾第二封信的署名", "basis_step_ids": ["chapter"]}})]
        await self.engine._review_matter_stage(S, copy.deepcopy(item), world=self.engine.world,
            state_key=self.engine.state_key(S), persona=await self.engine._persona_text(S))
        state = await self.engine.load_state(S, cold_start=False)
        item = matters.find(state, mid)
        self.assertEqual(item["next_step"], "核对结尾第二封信的署名")
        self.assertEqual(len(item["stage_notes"]), 1)
        self.llm.replies = ['{"steps":[{"text":"核对第二封信署名","focus":"比较结尾与第一章笔迹","action":"read","node":"study","minutes":10}]}']
        await self.engine.advance_life_matters(S)
        self.assertIn("两次寄信", self.llm.calls[-1]["prompt"])
        self.assertIn("第二封信的署名", self.llm.calls[-1]["prompt"])
        self.assertEqual(self.messenger.sent, [])

    async def test_reading_a_bookmark_cannot_invalidate_an_inflight_result(self):
        state, item = await self.prepare()
        item["steps"][0]["worked_minutes"] = 3
        before = copy.deepcopy(item)
        for _ in range(5):
            self.assertEqual(matters.current_bookmark(item)["worked_minutes"], 3)
        self.assertEqual(item, before)

    async def review_fixture(self):
        state = await self.engine.load_state(S, cold_start=False)
        state.life_matters = []
        await self.engine.save_state(state)
        state, item = await self.prepare()
        step = item["steps"][0]
        step.update(status="done", worked_minutes=35, bookmark={"goal": item["text"],
            "note": "黎明不是完整日期，尚不能确认两封信同一天", "open_question": "邮戳指哪一天"},
            result={"ok": True, "kind": "in_world_action", "minutes": 35})
        item.update(needs_review=True, progress=99)
        await self.engine.save_state(state)
        return item

    async def review(self, item, payload):
        self.llm.replies = [json.dumps(payload)]
        await self.engine._review_matter_stage(S, copy.deepcopy(item), world=self.engine.world,
            state_key=self.engine.state_key(S), persona=await self.engine._persona_text(S))
        return matters.find(await self.engine.load_state(S, cold_start=False), item["id"])

    async def test_review_cannot_drop_last_question_without_replacement_content(self):
        for outcome in ("continue", "blocked", "completed"):
            for stage in (None, {}, ""):
                with self.subTest(outcome=outcome, stage=stage):
                    item = await self.review_fixture()
                    before = copy.deepcopy(item)
                    payload = {"outcome": outcome, "reason": "这一阶段先告一段落"}
                    if stage is not None:
                        payload["stage"] = stage
                    updated = await self.review(item, payload)
                    self.assertEqual(updated, before)
                    self.assertIn("邮戳指哪一天", matters.context_block(
                        await self.engine.load_state(S, cold_start=False), session_id=S))
                    self.assertEqual(self.messenger.sent, [])

    async def test_partial_stage_cannot_discard_another_unreviewed_bookmark(self):
        item = await self.review_fixture()
        state = await self.engine.load_state(S, cold_start=False)
        item = matters.find(state, item["id"])
        item["steps"].append({**copy.deepcopy(item["steps"][0]), "id": "second", "text": "检查署名",
            "bookmark": {"goal": item["text"], "note": "第二封信署名尚未看清", "open_question": "谁写的第二封信"}})
        await self.engine.save_state(state)
        updated = await self.review(item, {"outcome": "completed", "stage": {
            "summary": "日期仍待核对", "content": "黎明不是完整日期，尚不能确认同一天。",
            "basis_step_ids": ["chapter"]}})
        self.assertEqual(updated["status"], "active")
        self.assertTrue(updated["needs_review"])
        self.assertTrue(updated["steps"][0]["stage_noted"])
        self.assertFalse(updated["steps"][1].get("stage_noted"))
        self.assertEqual(matters.pending_review_bookmarks(updated)[0]["open_question"], "谁写的第二封信")
        from core.context_sources import life_items
        from core.context_selection import ContextRequest
        state = await self.engine.load_state(S, cold_start=False)
        text = life_items(state, ContextRequest(task="reply", session=S, now=self.clock.now(), query="信"))[0].summary
        self.assertIn("待复核的活动：检查署名", text)
        self.assertNotIn("待复核的活动：对照第二章", text)
        self.assertIn("谁写的第二封信", text)
        self.engine.reload_config()
        state = await self.engine.load_state(S, cold_start=False)
        updated = await self.review(matters.find(state, item["id"]), {"outcome": "continue", "stage": {
            "summary": "署名还有疑问", "content": "第二封信署名尚未看清，不能确定作者。",
            "next_focus": "从署名继续", "basis_step_ids": ["second"]}})
        self.assertFalse(updated["needs_review"])
        self.assertTrue(updated["needs_replan"])
        self.assertEqual(len(updated["stage_notes"]), 2)

    async def test_complete_stage_hands_off_question_and_can_continue_after_reload(self):
        item = await self.review_fixture()
        content = "黎明不是完整日期，尚不能确认两封信同一天；邮戳指哪一天仍待核对。"
        updated = await self.review(item, {"outcome": "continue", "stage": {
            "summary": "整理日期疑问", "content": content, "next_focus": "从邮戳继续核对",
            "basis_step_ids": ["chapter"]}})
        self.assertFalse(updated["needs_review"])
        self.assertTrue(updated["needs_replan"])
        self.assertEqual(updated["stage_notes"][-1]["content"], content)
        self.engine.reload_config()
        state = await self.engine.load_state(S, cold_start=False)
        self.assertIn(content, matters.context_block(state, session_id=S))
        self.assertEqual(matters.pending_review_bookmarks(matters.find(state, item["id"])), [])

    async def test_legacy_unreviewed_batches_remain_visible_until_every_step_is_noted(self):
        from unittest.mock import AsyncMock
        item = await self.review_fixture()
        state = await self.engine.load_state(S, cold_start=False)
        item = matters.find(state, item["id"])
        item["steps"] = [{**copy.deepcopy(item["steps"][0]), "id": f"chapter_{index}"} for index in range(14)]
        await self.engine.save_state(state)
        batches = []

        async def review_batch(_session, _system, prompt):
            snapshot = json.loads(prompt)["matter"]
            ids = [step["id"] for step in snapshot["steps"]]
            batches.append(ids)
            return json.dumps({"outcome": "completed", "stage": {"summary": "保存这一批未解线索",
                "content": "黎明不是完整日期，邮戳所指日期仍未确认。", "basis_step_ids": ids}})

        self.engine._ask_judge = AsyncMock(side_effect=review_batch)
        for index in range(3):
            await self.engine.advance_life_matters(S)
            state = await self.engine.load_state(S, cold_start=False)
            item = matters.find(state, item["id"])
            self.assertEqual(item["worked_minutes"], 0)
            self.assertEqual(item["plan_attempts"], index + 1)
            if index < 2:
                self.assertTrue(item["needs_review"])
                self.assertNotEqual(item["status"], "completed")
            self.engine.reload_config()
            self.clock.advance(3600)
        self.assertEqual([len(batch) for batch in batches], [6, 6, 2])
        self.assertEqual(len(set(sid for batch in batches for sid in batch)), 14)
        self.assertEqual(item["status"], "completed")
        self.assertTrue(all(step["stage_noted"] for step in item["steps"]))
        self.assertEqual(self.messenger.sent, [])

    async def test_overlong_stage_cannot_save_only_a_claim_and_drop_its_qualification(self):
        item = await self.review_fixture()
        before = copy.deepcopy(item)
        updated = await self.review(item, {"outcome": "completed", "stage": {
            "summary": "日期核对进展", "content": "日期资料。" * 240 + "仍无依据确认两封信同一天。",
            "basis_step_ids": ["chapter"]}})
        self.assertEqual(updated, before)

    async def test_repeated_bookmark_notes_do_not_displace_history_or_change_revision(self):
        _state, item = await self.prepare()
        item["steps"][0]["worked_minutes"] = 3
        payload = {"matter_id": item["id"], "step_id": "chapter", "note": "两封信尚需对照",
                   "next_focus": "看蓝色邮戳"}
        basis = {**matters.current_bookmark(item), "revision": item["revision"]}
        self.assertTrue(matters.record_bookmark_note(item, payload, basis, now=self.clock.now()))
        basis = {**matters.current_bookmark(item), "revision": item["revision"]}
        before = copy.deepcopy(item)
        self.clock.advance(3600)
        self.assertFalse(matters.record_bookmark_note(item, payload, basis, now=self.clock.now()))
        self.assertEqual(item, before)
        self.assertTrue(matters.record_bookmark_note(item, {**payload, "next_focus": "再看手写日期"},
            basis, now=self.clock.now()))

    async def test_copying_a_planned_step_cannot_replace_an_actual_thought(self):
        _state, item = await self.prepare()
        step = item["steps"][0]
        step.update(worked_minutes=5, bookmark={"goal": item["text"],
            "note": "第一封信写黎明，但第二章未写日期，两者还不能认作同一天"})
        basis = {**matters.current_bookmark(item), "revision": item["revision"]}
        before = copy.deepcopy(item)
        payload = {"matter_id": item["id"], "step_id": step["id"], "note": step["text"]}
        for note in (step["text"], step["text"] + "。", " “" + step["text"] + "！” ",
                     step["text"].replace("与", " 与 ")):
            with self.subTest(note=note):
                self.assertFalse(matters.record_bookmark_note(item, {**payload, "note": note}, basis, now=self.clock.now()))
                self.assertEqual(item, before)
        payload["next_focus"] = "先看信封边缘的邮戳"
        self.assertTrue(matters.record_bookmark_note(item, payload, basis, now=self.clock.now()))
        self.assertEqual(matters.current_bookmark(item)["note"], basis["note"])
        self.assertEqual(matters.current_bookmark(item)["next_focus"], payload["next_focus"])

    async def test_finished_timer_hands_off_last_thought_until_review_without_reopening(self):
        _state, item = await self.prepare()
        step = item["steps"][0]
        step.update(status="done", worked_minutes=35, bookmark={"goal": item["text"],
            "note": "黎明不是完整日期，尚不能确认两封信同一天", "open_question": "邮戳指哪一天"})
        item["needs_review"] = True
        before = copy.deepcopy(item)
        self.assertFalse(matters.current_bookmark(item))
        notes = matters.pending_review_bookmarks(item)
        self.assertEqual(notes[0]["note"], step["bookmark"]["note"])
        self.assertEqual(notes[0]["remaining_minutes"], 0)
        self.assertEqual(item, before)
        step["stage_noted"] = True
        self.assertEqual(matters.pending_review_bookmarks(item), [])
        step["stage_noted"] = False
        step["goal"] = "过去的目标"
        self.assertEqual(matters.pending_review_bookmarks(item), [])

    async def test_only_changed_fields_are_needed_to_update_a_bookmark(self):
        _state, item = await self.prepare()
        step = item["steps"][0]
        step.update(worked_minutes=5, bookmark={"goal": item["text"],
            "note": "两封信是否同一天仍未确认", "next_focus": "看邮戳", "open_question": "邮戳是哪天"})
        basis = {**matters.current_bookmark(item), "revision": item["revision"]}
        payload = {"matter_id": item["id"], "step_id": step["id"], "next_focus": "看背面手写日期"}
        self.assertTrue(matters.record_bookmark_note(item, payload, basis, now=self.clock.now()))
        bookmark = matters.current_bookmark(item)
        self.assertEqual(bookmark["note"], basis["note"])
        self.assertEqual(bookmark["open_question"], basis["open_question"])
        self.assertEqual(bookmark["next_focus"], payload["next_focus"])
        self.assertFalse(matters.record_bookmark_note(item,
            {"matter_id": item["id"], "step_id": step["id"], "open_question": ""}, basis, now=self.clock.now()),
            "a result based on an older revision cannot overwrite a newer bookmark")
        basis = {**matters.current_bookmark(item), "revision": item["revision"]}
        self.assertTrue(matters.record_bookmark_note(item,
            {"matter_id": item["id"], "step_id": step["id"], "open_question": ""}, basis, now=self.clock.now()))
        basis = {**matters.current_bookmark(item), "revision": item["revision"]}
        self.assertEqual(matters.current_bookmark(item)["open_question"], "")
        before = copy.deepcopy(item)
        for fields in ({}, {"note": ""}, {"note": None}, {"open_question": None}):
            self.assertFalse(matters.record_bookmark_note(item,
                {"matter_id": item["id"], "step_id": step["id"], **fields}, basis, now=self.clock.now()))
            self.assertEqual(item, before)
        self.assertEqual(step["worked_minutes"], 5)
        self.assertEqual(item["progress"], 0)

    async def test_old_notes_rejected_after_human_edits_pause_or_completion(self):
        for operation, changes in [("pause", {}), ("edit", {"text": "换一本书"}),
                                   ("complete", {"reason": "管理员确认已读完"}), ("abandon", {})]:
            state, item = await self.prepare()
            item["steps"][0]["worked_minutes"] = 3
            basis = {**matters.current_bookmark(item), "revision": item["revision"]}
            self.engine.manage_life_matter(state, item["id"], operation, **changes)
            before = copy.deepcopy(item)
            self.assertFalse(matters.record_bookmark_note(item,
                {"matter_id": item["id"], "step_id": "chapter", "note": "旧想法"}, basis, now=self.clock.now()))
            self.assertEqual(item, before)
            state.life_matters = []

    async def test_cleared_question_does_not_restore_the_original_planning_question(self):
        _state, item = await self.prepare()
        step = item["steps"][0]
        step["worked_minutes"] = 5
        basis = {**matters.current_bookmark(item), "revision": item["revision"]}
        self.assertTrue(basis["open_question"])
        self.assertTrue(matters.record_bookmark_note(item,
            {"matter_id": item["id"], "step_id": step["id"], "note": step["text"], "open_question": ""},
            basis, now=self.clock.now()))
        self.assertEqual(matters.current_bookmark(item)["open_question"], "")
        self.assertNotEqual(item["history"][-1]["text"], "None")
        self.assertNotEqual(step["status"], "done")

    async def test_private_promises_and_another_persona_never_enter_sleep_bookmarks(self):
        state, item = await self.prepare()
        item["steps"][0]["worked_minutes"] = 3
        for patch in ({"who": "private-user"}, {"kind": "promise"}, {"persona_key": "another-persona"}):
            original = copy.deepcopy(item)
            item.update(patch)
            self.assertEqual(matters.bookmark_basis(state, persona_key="current-persona"), [])
            item.clear()
            item.update(original)
        self.assertEqual(len(matters.bookmark_basis(state, persona_key="current-persona")), 1)

    async def test_nan_or_malformed_effort_cannot_become_a_bookmark(self):
        _state, item = await self.prepare()
        for value in (float("nan"), float("inf"), "broken", None, -1):
            item["steps"][0]["worked_minutes"] = value
            self.assertEqual(matters.current_bookmark(item), {})

    async def test_existing_sleep_call_can_save_thought_without_completing_or_speaking(self):
        self.engine.world.sleep.mode = "autonomous"
        async with self.engine.session_state(S) as state:
            state.state, state.energy, state.sleep_pressure = "idle", .3, .9
            state.sleep_rhythm = {"awake_since": self.clock.now() - 17 * 3600}
            item = self.engine.add_life_matter(state, "接着看风车的故事")
            item["steps"] = [{"id": "s1", "text": "阅读第二章", "minutes": 20,
                              "worked_minutes": 5, "status": "pending", "focus": "对照信封日期"}]
            self.llm.replies = [json.dumps({"decision": "continue", "hours": 8, "reason": "再核对一小处就休息",
                "life_bookmark": {"matter_id": item["id"], "step_id": "s1", "note": "两封信的日期还需要对照",
                                  "next_focus": "从信封背面继续", "open_question": "邮戳是否同一天"}})]
            await self.engine._tick_sleep_rhythm(state, TickOutcome(S))
            self.assertEqual(matters.current_bookmark(item)["note"], "两封信的日期还需要对照")
            self.assertEqual(item["steps"][0]["worked_minutes"], 5)
            self.assertEqual(item["progress"], 0)
        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual(self.messenger.sent, [])

    async def test_persona_key_is_deterministic_for_bookmark_filter(self):
        state, item = await self.prepare()
        key = sha256((await self.engine._persona_text(S)).encode()).hexdigest()[:24]
        item["persona_key"] = key
        item["steps"][0]["worked_minutes"] = 2
        self.assertEqual(len(matters.bookmark_basis(state, persona_key=key)), 1)
