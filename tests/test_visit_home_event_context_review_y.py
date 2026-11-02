"""A home event remains unfinished while the current shared scene takes precedence."""
from __future__ import annotations

import copy
from hashlib import sha256
import unittest

from tests.fake_astrbot import install

install()

from core.context_sources import paused_home_event_context  # noqa: E402
from core.engine import TickOutcome  # noqa: E402
from core.events import event_from_payload  # noqa: E402
from tests import test_engine as fixture  # noqa: E402
from tests import test_visits_actor as actors  # noqa: E402


class VisitHomeEventContextTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = actors.VisitActorTests.asyncSetUp
    ctx = fixture.EngineTestCase.ctx

    async def prepare(self, *, role="guest", status="active", confirmed=True, pending_help=False):
        self.engine.world.events.enabled = True
        now = self.clock.now()
        self.home_event = {"id": "home-event", "title": "拆书桌旁的快递", "tier": "small",
            "status": "open", "opened_at": now - 300, "updated_at": now - 60,
            "closed_at": 0, "place": "study", "place_name": "家中的工作间",
            "line": "刚拆开外面的包装，里面还没看清。",
            "root": {"title": "拆书桌旁的快递", "hook": "桌边的包裹已经拆了一半。"},
            "steps": [], "pending_followup": "把纸箱里的书拿出来看看", "next_step_at": now + 60,
            "persona_key": self.local["persona_key"]}
        self.pending_help = {"state": "active", "thread_id": "home-event", "title": "拆书桌旁的快递",
                             "persona_key": self.local["persona_key"]} if pending_help else {}
        async with self.engine.session_state(fixture.SESSION) as state:
            state.event_threads = [copy.deepcopy(self.home_event)]
            state.pending_help = copy.deepcopy(self.pending_help)
            state.cold_start_done = True
        self.active = {**copy.deepcopy(self.visit), "role": role, "status": status, "outing_version": 1,
            "scene": {"place": "河边小公园", "activity": "沿河散步", "event": ""}}
        if confirmed:
            self.active["outing"] = {"version": 1, "stage": 0, "place": "河边小公园",
                "activity": "沿河散步", "started_at": now - 120, "until": now + 1800}

    async def reply(self, text="拆书桌旁的快递怎么样了？"):
        ctx = self.ctx(text=text, is_mentioned=True)
        await self.engine.handle_incoming(ctx)
        plan = await self.engine.handle_reply(ctx, inject_only=True)
        self.assertTrue(plan.ok, plan.reason)
        state = await self.engine.load_state(fixture.SESSION, cold_start=False)
        self.assertEqual([self.home_event], state.event_threads)
        self.assertEqual(self.pending_help, state.pending_help)
        self.assertEqual([], self.llm.calls)
        self.assertEqual([], self.tools.calls)
        return plan.runtime_part

    def assert_paused(self, runtime):
        self.assertIn("拆书桌旁的快递", runtime)
        self.assertIn("把纸箱里的书拿出来看看", runtime)
        self.assertIn("串门期间暂停", runtime)
        self.assertIn("游玩期间没有在家继续执行", runtime)
        self.assertIn("归家后再接续", runtime)
        self.assertNotIn("# 我正在经历", runtime)

    async def test_guest_reply_does_not_present_unfinished_home_event_as_current_work(self):
        await self.prepare()
        runtime = await self.reply()
        self.assert_paused(runtime)
        self.assertIn("当前动作：与另一个她在河边小公园，沿河散步", runtime)

    async def test_host_reply_keeps_the_home_event_paused_during_joint_activity(self):
        await self.prepare(role="host")
        runtime = await self.reply()
        self.assert_paused(runtime)
        self.assertIn("正在自己的世界接待", runtime)

    async def test_arrived_guest_before_joint_agreement_retains_pause_without_claiming_proposal(self):
        await self.prepare(confirmed=False)
        runtime = await self.reply()
        self.assert_paused(runtime)
        self.assertIn("尚未共同确认", runtime)
        self.assertNotIn("河边小公园", runtime)

    async def test_arrived_host_before_joint_agreement_also_pauses_home_event(self):
        await self.prepare(role="host", confirmed=False)
        self.assert_paused(await self.reply())

    async def test_pending_invitation_leaves_current_home_event_semantics_unchanged(self):
        await self.prepare(status="offered", confirmed=False)
        self.active["arrived_at"] = 0
        runtime = await self.reply()
        self.assertIn("# 我正在经历", runtime)
        self.assertIn("拆书桌旁的快递", runtime)
        self.assertNotIn("串门期间暂停", runtime)

    async def test_returned_visit_restores_original_event_context_without_completing_it(self):
        await self.prepare()
        self.assert_paused(await self.reply())
        self.active["status"] = "completed"
        self.active["ended_at"] = self.clock.now()
        runtime = await self.reply()
        self.assertIn("# 我正在经历", runtime)
        self.assertIn("拆书桌旁的快递", runtime)
        self.assertNotIn("串门期间暂停", runtime)
        self.assertNotIn("正在港湾拜访", runtime)

    async def test_old_personas_visit_and_home_event_stay_out_of_new_personas_reply(self):
        await self.prepare()
        self.persona.text = "现在我是刚搬来这里的新角色。"
        runtime = await self.reply()
        self.assertNotIn("拆书桌旁的快递", runtime)
        self.assertNotIn("正在港湾拜访", runtime)
        self.assertNotIn("沿河散步", runtime)

    async def test_pending_home_help_keeps_actual_request_but_marks_execution_paused(self):
        await self.prepare(pending_help=True)
        runtime = await self.reply()
        self.assert_paused(runtime)
        self.assertIn("还在等人回话", runtime)
        self.assertIn("家中挂着的未完事件", runtime)

    def help_replies(self):
        self.llm.replies = [
            '{"related":[{"from":"小明","kind":"suggestion","point":"用卡片打开纸箱"}],"irrelevant":[]}',
            '{"pick":1,"reason":"照小明的建议打开","say":{}}',
            '{"outcome":"纸箱打开，书已经拿出来了。","followup":""}',
        ]

    async def test_human_advice_during_visit_is_not_consumed_or_settled_by_home_event(self):
        await self.prepare(pending_help=True)
        self.help_replies()
        runtime = await self.reply("那个快递用卡片从上面撬一下就能打开。")
        self.assert_paused(runtime)
        state = await self.engine.load_state(fixture.SESSION, cold_start=False)
        self.assertIn("用卡片从上面撬一下", state.recent_chat[-1]["text"])
        self.assertEqual("open", state.event_threads[0]["status"])
        self.assertEqual([], state.event_threads[0]["steps"])

    async def test_unrelated_human_message_does_not_classify_old_help_during_visit(self):
        await self.prepare(pending_help=True)
        runtime = await self.reply("你们在朋友那边一起干什么呢？")
        self.assertIn("河边小公园", runtime)
        self.assertIn("游玩期间没有在家继续执行", runtime)

    async def test_after_return_the_original_help_response_can_continue_the_home_event(self):
        await self.prepare(status="completed", pending_help=True)
        self.help_replies()
        ctx = self.ctx(text="那个快递用卡片从上面撬一下就能打开。", is_mentioned=True)
        await self.engine.handle_incoming(ctx)
        result = await self.engine.handle_reply(ctx, inject_only=True)
        self.assertTrue(result.ok)
        state = await self.engine.load_state(fixture.SESSION, cold_start=False)
        self.assertEqual("closed", state.event_threads[0]["status"])
        self.assertEqual({}, state.pending_help)
        self.assertEqual(3, len(self.llm.calls))
        self.assertEqual([], self.tools.calls)

    async def test_finishing_visit_still_keeps_help_and_event_waiting(self):
        await self.prepare(status="finishing", pending_help=True)
        self.help_replies()
        self.assert_paused(await self.reply())

    async def test_editor_cannot_change_followup_deadline_or_advance_event_during_visit(self):
        await self.prepare()
        self.home_event["next_step_at"] = self.clock.now() + 3600
        async with self.engine.session_state(fixture.SESSION) as state:
            state.event_threads = [copy.deepcopy(self.home_event)]
        result = await self.engine.advance_thread(fixture.SESSION, "home-event")
        self.assertIn("家中事件暂停", result)
        state = await self.engine.load_state(fixture.SESSION, cold_start=False)
        self.assertEqual([self.home_event], state.event_threads)
        self.assertEqual([], self.llm.calls)
        self.assertEqual([], self.tools.calls)

    async def test_old_event_callbacks_cannot_modify_help_or_followup_while_away(self):
        for callback in ("followup", "pending_help", "event_step"):
            with self.subTest(callback=callback):
                await self.prepare(pending_help=True)
                self.home_event["next_step_at"] = self.clock.now() - 1
                async with self.engine.session_state(fixture.SESSION) as state:
                    state.event_threads = [copy.deepcopy(self.home_event)]
                    node = self.engine.node(state.node_id)
                    outcome = TickOutcome(fixture.SESSION)
                    if callback == "followup":
                        self.assertFalse(await self.engine._continue_thread(state, node, outcome, self.clock.now()))
                    elif callback == "pending_help":
                        await self.engine._advance_pending_help(state, node, outcome, self.clock.now())
                    else:
                        package = event_from_payload(self.home_event["root"])
                        await self.engine._run_event_step(state, node, outcome, state.event_threads[0], package)
                    self.assertEqual([self.home_event], state.event_threads)
                    self.assertEqual(self.pending_help, state.pending_help)
                self.assertEqual([], self.llm.calls)
                self.assertEqual([], self.tools.calls)

    async def test_manual_home_event_submission_does_not_create_an_event_while_away(self):
        await self.prepare()
        async with self.engine.session_state(fixture.SESSION) as state:
            state.event_threads = []
        result = await self.engine.submit_event(fixture.SESSION, "书桌旁的纸箱突然打开了")
        self.assertIn("家中事件暂停", result)
        state = await self.engine.load_state(fixture.SESSION, cold_start=False)
        self.assertEqual([], state.event_threads)
        self.assertEqual([], self.llm.calls)
        self.assertEqual([], self.tools.calls)

    async def test_old_personas_visit_does_not_block_new_personas_home_help(self):
        await self.prepare(pending_help=True)
        self.persona.text = "我是在家整理书桌的新角色。"
        key = sha256(self.persona.text.encode("utf-8")).hexdigest()[:24]
        async with self.engine.session_state(fixture.SESSION) as state:
            state.event_threads[0]["persona_key"] = key
            state.pending_help["persona_key"] = key
        self.help_replies()
        ctx = self.ctx(text="用卡片从上面打开纸箱", is_mentioned=True)
        await self.engine.handle_incoming(ctx)
        result = await self.engine.handle_reply(ctx, inject_only=True)
        self.assertTrue(result.ok)
        state = await self.engine.load_state(fixture.SESSION, cold_start=False)
        self.assertEqual("closed", state.event_threads[0]["status"])
        self.assertEqual(3, len(self.llm.calls))
        self.assertEqual([], self.tools.calls)

    async def test_runtime_notes_also_protect_legacy_prompt_callers(self):
        await self.prepare()
        notes = await self.engine.runtime_notes(fixture.SESSION)
        self.assertIn("串门期间暂停", notes["event_journal"])
        self.assertNotIn("# 我正在经历", notes["event_journal"])
        state = await self.engine.load_state(fixture.SESSION, cold_start=False)
        _, runtime = self.engine.prompts.build_dialogue_prompt(state=state,
            persona_text=self.persona.text, node=self.engine.node(state.node_id), **notes)
        self.assert_paused(runtime)


class PausedHomeEventMaterialTests(unittest.TestCase):
    def test_closed_history_is_unchanged_and_overlay_is_idempotent(self):
        recent = "# 最近发生在我身上的事\n- 昨天　拆完一本书的快递"
        material = "# 我正在经历（今天 · 工作间）\n尚未拆完\n\n" + recent
        rendered = paused_home_event_context(material)
        self.assertIn(recent, rendered)
        self.assertEqual(rendered, paused_home_event_context(rendered))
        self.assertEqual(recent, paused_home_event_context(recent))
        self.assertEqual("", paused_home_event_context(""))
