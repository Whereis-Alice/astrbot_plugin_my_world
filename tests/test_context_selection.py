"""Behavioural regressions for task-aware context, using no real chat transport."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core.config_store import ConfigStore
from core.context_selection import ContextItem, ContextRequest, request_for, select_chat_rows, select_context
from core.context_sources import life_items
from core.db import AsyncDatabase
from core.engine import MyWorldEngine, MessageContext, TickOutcome
from core.life_matters import make
from core.memory import DREAM
from tests.stub_ports import StubClock, StubLLM, StubMessenger, StubPersona


GROUP = "aiocqhttp:GroupMessage:1001"
OTHER = "aiocqhttp:GroupMessage:2002"
PERSONA = "你是爱丽丝，喜欢风车。独立、温柔，说话简短；不认识的人不能当成亲人。"


class ContextSelectionTests(unittest.TestCase):
    def request(self, **kwargs):
        return ContextRequest("participation", GROUP, 10000, **kwargs)

    def test_private_or_expired_candidates_never_enter_trace(self):
        req = self.request(query="秘密")
        result = select_context(req, [ContextItem("private-secret", "memory", "秘密", session=OTHER),
            ContextItem("old", "memory", "过期", expires=9999),
            ContextItem("other-persona", "memory", "秘密", persona="someone-else")])
        self.assertEqual(result.rows, [])
        self.assertEqual(result.trace["filtered"], 3)
        self.assertNotIn("private-secret", json.dumps(result.trace))

    def test_dependencies_remain_together_even_when_required_chain_exceeds_budget(self):
        req = self.request(query="好多了")
        result = select_context(req, [ContextItem("before", "memory", "此前说过面试失利"),
            ContextItem("now", "memory", "今天好多了", required=True, dependencies=("before",))], budget=3)
        self.assertEqual([item.key for item, _ in result.rows], ["before", "now"])

    def test_missing_cause_is_not_presented_as_a_standalone_fact(self):
        result = select_context(self.request(), [ContextItem("effect", "event", "已经成功", dependencies=("missing",))])
        self.assertEqual(result.rows, [])

    def test_correction_retires_old_fact_before_selection(self):
        result = select_context(self.request(query="周末风车"), [
            ContextItem("old", "memory", "周末去风车山"),
            ContextItem("new", "memory", "周末风车出行已取消", supersedes=("old",))])
        self.assertNotIn("去风车山", result.render())
        self.assertIn("已取消", result.render())

    def test_small_budget_keeps_atomic_summary_instead_of_half_a_condition(self):
        req = self.request(query="寄出")
        result = select_context(req, [ContextItem("promise", "matter", "只是准备，尚未寄出",
            "准备步骤完成，但必须拿到实际投递回执才能说已经寄出。", continuing=True)], budget=15)
        self.assertEqual(result.render(), "只是准备，尚未寄出")

    def test_related_material_gets_detail_when_space_allows(self):
        result = select_context(self.request(query="风车模型的叶片"), [
            ContextItem("work", "matter", "风车模型", "风车模型的叶片已完成，但底座尚未安装。")])
        self.assertIn("底座尚未安装", result.render())

    def test_identical_words_by_different_people_are_not_merged(self):
        result = select_context(self.request(query="失落", people=("a", "b")), [
            ContextItem("a", "memory", "失落", people=("a",)),
            ContextItem("b", "memory", "失落", people=("b",))])
        self.assertEqual(len(result.rows), 2)

    def test_priority_selects_records_before_chronological_presentation(self):
        result = select_context(self.request(query="风车"), [
            ContextItem("unrelated", "memory", "旧风车传闻", at=100),
            ContextItem("before", "memory", "风车叶片尚未安装", at=200, priority=2),
            ContextItem("after", "memory", "风车叶片现已安装", at=300, priority=3)], max_items=2)
        self.assertEqual([item.key for item, _ in result.rows], ["before", "after"])

    def test_short_continuation_keeps_its_speakers_previous_context(self):
        req = request_for(task="reply", session=GROUP, now=10000, query="今天好多了", focus="a", rows=[
            {"origin": GROUP, "user_id": "a", "text": "面试失利后我一直睡不好"},
            {"origin": GROUP, "user_id": "b", "text": "我在修风车"},
            {"origin": GROUP, "user_id": "a", "text": "今天好多了"},
            {"origin": OTHER, "user_id": "secret", "text": "别处的私事"}])
        self.assertIn("面试失利", req.people_queries["a"])
        self.assertNotIn("修风车", req.people_queries["a"])
        self.assertNotIn("别处", req.query)

    def test_quote_parent_outside_recent_tail_is_preserved(self):
        rows = [{"message_id": str(i), "text": f"消息{i}", "origin": GROUP} for i in range(30)]
        rows[-1]["reply_to"] = {"id": "sender", "message_id": "1"}
        chosen = select_chat_rows(self.request(), rows, maximum=8)
        self.assertIn(rows[1], chosen)
        self.assertIn(rows[-1], chosen)

    def test_sender_id_is_never_treated_as_a_message_id(self):
        rows = [{"message_id": str(i), "text": f"消息{i}", "origin": GROUP} for i in range(30)]
        rows[-1]["reply_to"] = {"id": "1"}
        chosen = select_chat_rows(self.request(), rows, maximum=8)
        self.assertNotIn(rows[1], chosen)

    def test_quote_chain_can_recover_ancestors_outside_the_recent_window(self):
        archive = [{"origin": GROUP, "message_id": str(i), "text": f"消息{i}"} for i in range(50)]
        archive[20]["reply_to"] = {"message_id": "1"}
        archive[-1]["reply_to"] = {"message_id": "20"}
        chosen = select_chat_rows(self.request(), archive[-8:], maximum=8, archive=archive)
        self.assertEqual([row["message_id"] for row in chosen], ["1", "20", *map(str, range(42, 50))])
        self.assertNotIn(archive[2], chosen)

    def test_quote_recovery_cannot_read_another_sessions_same_message_id(self):
        archive = [{"origin": OTHER, "message_id": "1", "text": "别处的秘密"}]
        rows = [{"origin": GROUP, "message_id": "2", "text": "后来呢", "reply_to": {"message_id": "1"}}]
        self.assertEqual(select_chat_rows(self.request(), rows, archive=archive), rows)

    def test_live_context_is_rebuilt_when_permission_or_fact_changes(self):
        req = self.request(query="风车")
        first = select_context(req, [ContextItem("fact", "memory", "风车可以去")]).render()
        second = select_context(req, [ContextItem("fact", "memory", "风车不开放", session=OTHER)]).render()
        self.assertIn("可以去", first)
        self.assertEqual(second, "")


class ContextIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        store = ConfigStore(Path(folder.name))
        store.ensure_files()
        for sid in (GROUP, OTHER):
            store.add_session(sid, session_type="group", platform="aiocqhttp", cold_start_node="lobby")
        raw = store.raw_world()
        raw["proactive_session_whitelist"] = [GROUP]
        raw["life_share_session_whitelist"] = [GROUP]
        raw["calendar"]["auto_generate"] = False
        raw["diary"]["automatic"] = False
        store.save_world(raw)
        self.db = AsyncDatabase(store.db_path)
        self.addAsyncCleanup(self.db.close)
        self.clock = StubClock()
        self.llm = StubLLM(['{"say":"","reply_to":""}'] * 8)
        self.messenger = StubMessenger()
        self.engine = MyWorldEngine(store=store, db=self.db, llm=self.llm, creator_llm=self.llm,
            persona=StubPersona(PERSONA), clock=self.clock, messenger=self.messenger)
        self.addAsyncCleanup(self.engine.close_background)
        self.state = await self.engine.load_state(GROUP, cold_start=False)
        self.state.current_action = None
        self.state.current_plan = None
        self.state.energy = .9

    def remember(self, text, *, node="lobby", session=GROUP, **kwargs):
        return self.db.raw.add_memory(session_id=session, persona_id="", node_id=node, scope="group_persona",
            content=text, memory_type=kwargs.pop("memory_type", "scene"),
            created_at=kwargs.pop("now", self.clock.now()), **kwargs)

    def row(self, text, uid="alice", **kwargs):
        return {"origin": GROUP, "user_id": uid, "name": uid, "text": text,
                "at": self.clock.now(), **kwargs}

    def prompt(self, text="周末还去看风车吗？"):
        return self.engine.participation_prompt(self.state, GROUP, PERSONA,
            rows=[self.row(text, message_id="live-1")])

    async def test_chat_recall_can_cross_location_but_explicit_spatial_recall_cannot(self):
        mid = self.remember("昨天在公园一起看过风车，叶片坏了一片", node="park_gate")
        request = self.engine.context_request(self.state, GROUP, text="还记得公园的风车吗？", rows=[])
        memories = self.engine.context_memories(self.state, request)
        self.assertIn(mid, [item.id for item in memories])
        spatial = self.engine.memory.recall(session_id=GROUP, persona_id="", node_id="lobby", keyword="风车")
        self.assertNotIn(mid, [item.id for item in spatial])

    async def test_old_chinese_topic_survives_many_unrelated_recent_memories(self):
        mid = self.remember("在公园见过风车的木制叶片", now=self.clock.now() - 60 * 86400)
        for i in range(520):
            self.db.raw.add_memory(session_id=GROUP, persona_id="", node_id="lobby", content=f"今天煮了米饭{i}",
                memory_type="scene", emotion="", weight=.5, scope="group_persona", related_users=[],
                created_at=self.clock.now() - i)
        request = self.engine.context_request(self.state, GROUP, text="公园那架风车叶片后来怎么样了", rows=[])
        self.assertIn(mid, [item.id for item in self.engine.context_memories(self.state, request)])

    async def test_source_scoped_promise_never_leaks_even_in_global_memory_mode(self):
        self.engine.world.memory_scope_mode = "global"
        self.remember("风车礼物是私下约定的秘密", session=OTHER, source="life_matter", participants=["alice"])
        system, prompt, _ = self.prompt()
        self.assertNotIn("私下约定的秘密", system + prompt)

    async def test_participation_recovers_related_experience_and_uses_activity_only_when_asked(self):
        self.engine.profiles.touch(GROUP, "alice", name="阿梨", now=self.clock.now())
        self.remember("昨天和阿梨一起修过风车的叶片", related_users=["alice"])
        self.state.current_action = {"type": "read_book", "desc": "正在读航空史"}
        system, prompt, _ = self.prompt()
        self.assertIn("昨天和阿梨一起修过风车", system)
        self.assertNotIn("正在读航空史", system)
        self.assertIn("alice", system + prompt)
        self.assertNotIn("可达地点", system)
        self.assertNotIn('"plan"', system)
        asked_system, _, _ = self.prompt("你现在在做什么？")
        self.assertIn("正在读航空史", asked_system)

    async def test_dream_cannot_be_injected_as_a_real_experience(self):
        self.remember("风车飞上天空", memory_type=DREAM)
        system, _, _ = self.prompt()
        self.assertIn("梦境，非现实经历", system)

    async def test_one_activity_is_not_an_invented_stable_interest(self):
        self.state.life_interests = {"read_book": {"label": "刚读过航空史", "days": ["2026-10-21"],
            "sessions": 1, "minutes": 15, "last_at": self.clock.now()}}
        system, _, _ = self.prompt("今天吃什么")
        self.assertNotIn("刚读过航空史", system)

    async def test_foreign_persona_matter_is_not_selected(self):
        self.state.life_matters = [make("修好风车的叶片", now=self.clock.now(), session=GROUP,
            persona_key="a-different-character")]
        system, _, _ = self.prompt()
        self.assertNotIn("修好风车的叶片", system)

    async def test_private_promise_is_not_a_shared_sleep_or_planning_motivation(self):
        self.state.life_matters = [make("给阿梨的风车礼物", now=self.clock.now(), session=GROUP,
            who="alice", kind="promise")]
        for task in ("sleep", "plan", "diary"):
            request = self.engine.context_request(self.state, GROUP, task=task, rows=[])
            self.assertEqual(life_items(self.state, request), [])
        system, _, _ = self.prompt()
        self.assertIn("给阿梨的风车礼物", system)

    async def test_private_promise_requires_the_person_to_be_present(self):
        self.state.life_matters = [make("给阿梨的风车礼物", now=self.clock.now(), session=GROUP,
            who="alice", kind="promise")]
        for people in ((), ("bob",)):
            request = ContextRequest("expression", GROUP, self.clock.now(), people=people)
            self.assertEqual(life_items(self.state, request), [])

    async def test_recalled_private_promise_requires_its_person_and_cannot_drive_shared_planning(self):
        mid = self.remember("给阿梨准备的风车礼物，尚未寄出", source="life_matter", participants=["alice"])
        for task, people in (("expression", ()), ("participation", ("bob",)), ("plan", ("alice",))):
            request = ContextRequest(task, GROUP, self.clock.now(), query="风车礼物", people=people)
            self.assertNotIn(mid, [memory.id for memory in self.engine.context_memories(self.state, request)])
        system, _, _ = self.prompt("风车礼物寄出了吗")
        self.assertIn("准备与模拟不等于现实兑现", system)
        self.assertIn("尚未寄出", system)

    async def test_legacy_chat_origin_stays_with_the_archive_owner(self):
        self.state.recent_chat = [{"user_id": "alice", "text": "代表会话的旧私事", "at": self.clock.now()}]
        request = self.engine.context_request(self.state, OTHER)
        self.assertNotIn("代表会话的旧私事", request.query)
        self.assertEqual(request.people, ())
        self.assertEqual(self.engine.context_chat(self.state, request), [])

    async def test_participation_can_select_the_current_speakers_relevant_profile_facts(self):
        self.engine.profiles.touch(GROUP, "alice", name="阿梨", now=self.clock.now())
        self.engine.profiles.note_fact(GROUP, "alice", text="风车模型是阿梨爷爷留下的", kind="经历")
        system, _, _ = self.prompt("今天修风车时又想起爷爷了")
        self.assertIn("风车模型是阿梨爷爷留下的", system)

    async def test_selection_and_preview_do_not_change_memory_recall_counters(self):
        mid = self.remember("昨天修过风车叶片")
        before = copy.deepcopy(self.db.raw.memory_by_id(mid))
        self.prompt()
        self.prompt()
        await self.engine.preview_autonomous_prompt(GROUP)
        self.assertEqual(self.db.raw.memory_by_id(mid), before)

    async def test_ambient_evaluation_uses_production_builder_and_does_not_mutate_world(self):
        self.remember("昨天和阿梨一起看过风车", related_users=["alice"])
        async with self.engine.session_state(GROUP) as state:
            state.recent_chat = [self.row("明天去看风车吗？")]
        before = (await self.engine.load_state(GROUP)).to_payload()
        result = await self.engine.run_eval(GROUP, [{"user_text": "好啊", "user_id": "alice"}], variant="participation")
        self.assertFalse(result["results"][0]["error"])
        self.assertIn("昨天和阿梨", self.llm.calls[-1]["system_prompt"])
        self.assertIn("context_selection", result["results"][0])
        self.assertEqual((await self.engine.load_state(GROUP)).to_payload(), before)
        self.assertEqual(self.messenger.sent, [])

    async def test_semantic_failure_keeps_lexical_facts_without_an_extra_decision_model(self):
        self.remember("和阿梨一起看过风车", related_users=["alice"])
        self.state.recent_chat = [self.row("还记得风车吗？")]
        self.engine.memory.semantic.prepare = AsyncMock(return_value=None)
        await self.engine.participation_text(self.state, GROUP)
        self.assertEqual(len(self.llm.calls), 1)
        self.assertIn("一起看过风车", self.llm.calls[0]["system_prompt"])

    async def test_expression_uses_destination_context_and_does_not_send(self):
        self.remember("本群一起修过风车", related_users=["alice"])
        self.state.recent_chat = [self.row("风车修好了吗？"), self.row("别处私事", origin=OTHER)]
        await self.engine._generate_text_actions(self.state, self.engine.node(self.state.node_id),
            "想说说风车", session_id=GROUP, focus_user="alice")
        self.assertIn("本群一起修过风车", self.llm.calls[-1]["system_prompt"])
        self.assertNotIn("别处私事", self.llm.calls[-1]["system_prompt"])
        self.assertEqual(self.messenger.sent, [])

    async def test_event_background_retains_causal_chain_above_optional_material(self):
        required = "最初风车叶片断了；已经拆下旧叶片；尚未安装新叶片；不能说已经修好。"
        text = await self.engine.task_background(self.state, task="event", query="修理风车", required=required)
        self.assertIn(required, text)

    async def test_adaptive_dialogue_has_one_matter_copy_and_preserves_host_boundary(self):
        self.state.life_matters = [make("修好风车的底座", now=self.clock.now(), session=GROUP)]
        request = self.engine.context_request(self.state, GROUP, text="风车进展怎么样", rows=[])
        system, dynamic = self.engine.prompts.build_dialogue_prompt(persona_text=PERSONA, state=self.state,
            node=self.engine.node(self.state.node_id), context_request=request, current_session=GROUP)
        self.assertIn(PERSONA, system)
        self.assertEqual(dynamic.count("修好风车的底座"), 1)
        self.assertNotIn("walk_to", dynamic)

    async def test_reference_extraction_and_archive_keep_message_id_distinct_from_sender(self):
        from core.messages import _reply_target
        Reply = type("Reply", (), {})
        component = Reply()
        component.id, component.sender_id, component.sender_nickname = "quote-42", "alice", "阿梨"
        event = SimpleNamespace(message_obj=SimpleNamespace(message=[component]))
        reference = _reply_target(event)
        row = self.state.note_chat(user_id="bob", name="小波", text="后来呢", now=self.clock.now(), reply_to=reference)
        self.assertEqual(row["reply_to"], {"id": "alice", "name": "阿梨", "message_id": "quote-42"})

    async def test_manual_memory_setting_and_whitelist_remain_effective(self):
        self.remember("昨天修过风车")
        self.engine.world.memory.automatic_recall = "manual"
        system, _, _ = self.prompt()
        self.assertNotIn("昨天修过风车", system)
        self.assertFalse(self.engine.proactive_chat_allowed(OTHER))

    async def test_production_plan_passes_the_task_request_into_the_shared_builder(self):
        with patch.object(self.engine.prompts, "build_autonomous_system_prompt", wraps=self.engine.prompts.build_autonomous_system_prompt) as build:
            await self.engine._ask_llm_for_plan(self.state, self.engine.node(self.state.node_id),
                TickOutcome(GROUP), quiet=True, force=True, hint="继续修风车")
        self.assertEqual(build.call_args.kwargs["context_request"].task, "plan")
        self.assertEqual(self.messenger.sent, [])
