"""Explicit provenance stays attached across budgets, goals and privacy scopes."""
import copy
import json
import unittest

from core import life_matters as matters
from core.context_selection import ContextItem, ContextRequest, relevance, request_for, select_context
from core.context_sources import life_items, memory_context_items
from core.memory import RecalledMemory
from core.state import WorldState

S = "test:GroupMessage:1"
OTHER = "test:PrivateMessage:2"


class LinkedContextTests(unittest.TestCase):
    def setUp(self):
        self.state = WorldState(S)
        self.matter = matters.make("读完风车的故事", now=100, session=S, persona_key="alice")
        self.matter.update(status="completed", result="确认两封信的日期不同，章节笔记已收尾", next_step="")
        self.state.life_matters = [self.matter]

    def request(self, **changes):
        return ContextRequest(task=changes.pop("task", "reply"), session=changes.pop("session", S),
            now=1000, query=changes.pop("query", "风车的故事后来怎么样"), life_persona="alice", **changes)

    def memory(self, **changes):
        meta = {"matter_id": self.matter["id"], "goal": self.matter["text"], "persona_key": "alice"}
        return RecalledMemory(id=1, content=changes.pop("content", "风车的故事才读了一半，信封日期还是个疑问"),
            node_id="study", memory_type="scene", emotion="", weight=.5, score=1, related_users=[],
            source="life_matter", session_id=S, created_at=200,
            context=changes.pop("context", json.dumps(meta, ensure_ascii=False)), **changes)

    def test_old_progress_cannot_be_injected_without_latest_result_even_with_small_budget(self):
        request = self.request()
        material = memory_context_items(self.state, request, [self.memory()])
        for budget in (1, 90, 220, 500, 1500):
            result = select_context(request, material, budget=budget)
            if any(item.key == "memory:1" for item, _ in result.rows):
                self.assertIn("章节笔记已收尾", result.render())
                self.assertTrue(any(item.key.startswith("matter:") for item, _ in result.rows))
        result = select_context(request, material, budget=1500)
        self.assertIn("才读了一半", result.render())
        self.assertIn("章节笔记已收尾", result.render())

    def test_manual_correction_survives_atomic_summary(self):
        self.matter.update(status="active", result="", text="重新核对风车故事的结局")
        matters.history(self.matter, "manual_edit", "原先认为两封信同日寄出有误", 900, manual=True)
        text = select_context(self.request(), memory_context_items(self.state, self.request(),
            [self.memory(context=json.dumps({"matter_id": self.matter["id"], "goal": "读完风车的故事"}))])).render()
        self.assertIn("有误", text)
        self.assertIn("历史目标", text)
        self.assertIn("重新核对", text)

    def test_private_or_deleted_matter_does_not_leave_orphan_progress(self):
        memory = self.memory()
        self.matter.update(who="private-user", session=OTHER)
        self.assertEqual(memory_context_items(self.state, self.request(), [memory]), [])
        self.state.life_matters = []
        self.assertEqual(memory_context_items(self.state, self.request(), [memory]), [])

    def test_other_persona_filtered_before_selection_trace(self):
        request = self.request()
        items = memory_context_items(self.state, request,
            [self.memory(context=json.dumps({"matter_id": self.matter["id"], "persona_key": "another"}))])
        self.assertEqual(items, [])
        select_context(request, items)
        self.assertNotIn("memory:1", json.dumps(request.trace))

    def test_unlinked_legacy_memory_is_not_guessed_as_current_progress(self):
        items = memory_context_items(self.state, self.request(), [self.memory(context="旧记录")])
        self.assertEqual(len(items), 1)
        self.assertFalse(items[0].dependencies)
        self.assertIn("不能证明当前状态", items[0].summary)

    def test_cancelled_matter_remains_attached_as_cancelled(self):
        self.matter.update(status="abandoned", result="", obstacle="换一个主题")
        text = select_context(self.request(), memory_context_items(self.state, self.request(), [self.memory()])).render()
        self.assertIn("abandoned", text)
        self.assertIn("换一个主题", text)

    def test_unrelated_participation_never_gains_life_report_from_fresh_memory(self):
        request = self.request(task="participation", query="晚饭的咖喱有点辣")
        self.assertEqual(memory_context_items(self.state, request, [self.memory()]), [])
        self.assertTrue(memory_context_items(self.state, self.request(task="participation"), [self.memory()]))

    def test_life_switch_removes_memories_as_well_as_current_matters(self):
        self.assertEqual(memory_context_items(self.state, self.request(), [self.memory()], life_enabled=False), [])

    def test_retrieval_ignores_external_memory_without_dropping_real_quote(self):
        request = request_for(task="reply", session=S, now=1000, query="怎么会呢",
            rows=[{"origin": S, "user_id": "a", "text": '她说“风车后来又转了”\n<Anamnesis-Memory>秘密旧剧情</Anamnesis-Memory>'}])
        self.assertIn("风车后来又转了", request.query)
        self.assertNotIn("秘密旧剧情", request.query)
        self.assertNotIn("Anamnesis", request.query)

    def test_stage_and_bookmark_are_available_without_a_second_detail_expansion(self):
        self.matter.update(status="active", result="", steps=[{"id": "s", "text": "核对邮戳", "minutes": 20,
            "worked_minutes": 5, "status": "pending", "focus": "看信封背面", "open_question": "是否为同一封信"}],
            stage_notes=[{"summary": "两封信的寄信人可能不同", "content": "这段详细推导可以按预算展开",
                          "next_focus": "比较两封信署名"}])
        before = copy.deepcopy(self.matter)
        summary = life_items(self.state, self.request())[0].summary
        self.assertIn("两封信署名", summary)
        self.assertIn("看信封背面", summary)
        self.assertIn("同一封信", summary)
        self.assertIn("步骤尚未做完", summary)
        self.assertNotIn("分钟", summary)
        self.assertIn("计时依据（不是成果）", life_items(self.state, self.request())[0].detail)
        self.assertIn("详细推导", summary, "stage conditions must survive even without a detail expansion")
        self.assertEqual(self.matter, before)

    def test_unverified_stage_keeps_its_question_at_every_budget(self):
        self.matter.update(status="active", result="", next_step="比较署名并整理时间线",
            stage_notes=[{"summary": "完成日期比较", "content": "邮戳与手写日期可能不同，仍需核实。",
                          "next_focus": "比较署名并整理时间线"}])
        material = life_items(self.state, self.request())
        for budget in (1, 90, 250, 500, 1000):
            with self.subTest(budget=budget):
                text = select_context(self.request(), material, budget=budget).render()
                if "完成日期比较" in text:
                    self.assertIn("仍需核实", text)
                    self.assertIn("目标（尚未完成）", text)
                    self.assertIn("后续待做：比较署名", text)

    def test_timed_stage_awaiting_review_includes_its_limited_scope(self):
        self.matter.update(status="active", result="", needs_review=True,
            text="比较日期和署名，最后整理时间线", next_step="这一阶段已做完，核对结果后再安排",
            steps=[{"id": "dates", "text": "比较日期，署名留待下一阶段", "status": "done"}])
        text = life_items(self.state, self.request())[0].summary
        self.assertIn("目标（尚未完成）", text)
        self.assertIn("待复核的活动：比较日期，署名留待下一阶段", text)
        self.assertIn("尚无该阶段的内容结论", text)
        self.assertIn("不是整个目标的完成度", text)

    def test_last_unconfirmed_thought_survives_timer_end_and_atomic_selection(self):
        self.matter.update(status="active", result="", needs_review=True,
            steps=[{"id": "dates", "text": "比较日期", "status": "done", "minutes": 35,
                "worked_minutes": 35, "bookmark": {"goal": self.matter["text"],
                    "note": "第一封只写了黎明，尚不能确定日期关系", "open_question": "邮戳是否与手写日期一致"}}])
        before = copy.deepcopy(self.matter)
        material = life_items(self.state, self.request())
        for budget in (90, 400, 800, 1500):
            text = select_context(self.request(), material, budget=budget).render()
            if "比较日期" in text:
                self.assertIn("尚不能确定日期关系", text)
                self.assertIn("邮戳是否与手写日期一致", text)
                self.assertIn("计时结束没有自动解决", text)
        self.assertEqual(self.matter, before)
        self.matter["steps"][0]["stage_noted"] = True
        self.assertNotIn("第一封只写了黎明", life_items(self.state, self.request())[0].summary)

    def test_old_planning_and_legacy_paths_preserve_qualifications_and_persona(self):
        self.matter.update(status="active", result="", stage_notes=[{
            "summary": "完成日期比较", "content": "核对的过程。" * 85 + "末尾仍未确认邮戳是哪一天。",
            "next_focus": "继续核实日期", "at": 900}])
        for text in (matters.activity_basis(self.state, now=1000, persona_key="alice"),
                     matters.context_block(self.state, session_id=S, now=1000, persona_key="alice")):
            self.assertIn("末尾仍未确认", text)
            self.assertIn("不是独立的结论依据", text)
        self.state.life_artifacts[self.matter['id']] = {"label": self.matter['text'],
            "result": "完成日期比较", "content": "仍未确认邮戳是哪一天", "session": S}
        self.assertNotIn("日期比较", matters.context_block(self.state, session_id=S, now=1000, persona_key="other"))
        self.assertNotIn("日期比较", matters.activity_basis(self.state, now=1000, persona_key="other"))
        self.matter.update(who="private-user")
        self.assertNotIn("日期比较", matters.context_block(self.state, session_id=S, now=1000, persona_key="alice"))

    def test_oversized_stage_is_omitted_whole_instead_of_losing_its_question(self):
        self.matter.update(stage_notes=[{"summary": "核对日期", "content": "仍需核实。" * 360,
            "next_focus": "等到找到依据再下结论", "at": 900}])
        text = matters.activity_basis(self.state, now=1000, persona_key="alice")
        self.assertNotIn("核对日期", text)

    def test_cleared_bookmark_question_is_not_reintroduced_from_the_plan(self):
        self.matter.update(status="active", result="", steps=[{"id": "dates", "text": "比较日期", "status": "pending",
            "minutes": 35, "worked_minutes": 20, "open_question": "邮戳与手写日期是否矛盾",
            "bookmark": {"open_question": "", "note": "先保留两个时间参照，接着看署名"}}])
        text = life_items(self.state, self.request())[0].summary
        self.assertNotIn("邮戳与手写日期是否矛盾", text)
        self.assertIn("先保留两个时间参照", text)
        self.assertIn("步骤尚未做完", text)

    def test_labels_and_generic_interruption_do_not_create_a_topic_match(self):
        self.matter.update(status="active", result="", obstacle="上次的小步被打断，稍后接着做",
            steps=[{"id": "dates", "text": "比较两封信的日期", "status": "pending",
                    "worked_minutes": 20, "minutes": 35, "focus": "看蓝色邮戳"}])
        item = life_items(self.state, self.request())[0]
        for text in ("练了好多天的小提琴片段，录音时又拉错了，真有点泄气。", "我的工作也被打断了", "步骤尚未做完"):
            with self.subTest(text=text):
                self.assertEqual(relevance(text, item.topic_text), 0)
        self.assertGreater(relevance("那两封信的邮戳呢", item.topic_text), 0)

    def test_source_matching_does_not_score_presentation_labels(self):
        request = self.request(query="当时的事")
        selection = select_context(request, [ContextItem("one", "memory", "# 当时的事\n风车已修好",
            match_text="风车已修好")])
        self.assertEqual(selection.rows, [])

    def test_particles_are_not_a_reason_to_recall_unrelated_content(self):
        self.assertEqual(relevance("好多天的小提琴", "上次的小步被打断"), 0)
        self.assertGreater(relevance("小提琴练习", "换了小提琴的琴弦"), 0)
