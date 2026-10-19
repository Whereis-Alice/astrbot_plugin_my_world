"""Acceptance for life continuity, optimistic inference and grounded corrections."""
import asyncio
import json
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from core import life_matters as matters
from core.engine.life_inference import life_work
from core.models import ChainStep, ScheduleDef
from core.planner import create_plan
from core.ports import LLMReply
from core.state import WorldState
from tests import test_engine as fixture
from tests import test_life_continuity as continuity
from tests import test_life_matters as fixtures

S = fixture.SESSION


class LifeUnificationTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixtures.MattersAcceptanceTests.asyncSetUp
    prepare = continuity.LifeContinuityTests.prepare

    async def asyncTearDown(self):
        await self.engine.close_background()

    async def test_schedule_and_critical_event_save_the_same_partial_step(self):
        for kind in ("schedule", "event", "rest"):
            state, item = await self.prepare()
            choice = self.engine.admit_life(state, kind, critical=kind == "event", force=kind == "rest")
            self.assertTrue(choice["allow"])
            self.assertEqual(item["steps"][0]["worked_minutes"], 2)
            self.assertEqual(item["status"], "active")
            self.assertIsNone(state.current_action)
            self.assertEqual(item["history"][-1]["source"], kind)
            await self.engine._start_matter_step(state, item)
            self.assertEqual(state.current_action["duration_ticks"], 18)

    async def test_ordinary_experience_does_not_replace_current_work(self):
        state, item = await self.prepare()
        action = state.current_action
        self.assertTrue(self.engine.admit_life(state, "event")["allow"])
        self.assertIs(state.current_action, action)
        self.assertEqual(item["status"], "working")

    async def test_quiet_activity_resumes_remaining_time_and_explicit_cancel_drops_it(self):
        state = WorldState(S, node_id="study", energy=.9)
        self.engine.world.action_map()["read"].on_complete.trigger = "none"
        state.current_plan = create_plan(steps=[{"action": "read", "duration": 1200}],
            world_time=0, now=self.clock.now(), valid_for=7200, reason="读完这一章")
        state.current_action = {"type": "read", "duration_ticks": 20, "elapsed_ticks": 5}
        self.engine.suspend_life(state, reason="出门前先停一下", source="schedule")
        self.assertEqual(state.life_resume["plan"]["steps"][0]["duration"], 900)
        self.assertTrue(self.engine.resume_life(state))
        self.engine.suspend_life(state, reason="再等一会儿", source="event")
        await self.engine.apply_cancel(state, "queue", "今天不读了", fixture.TickOutcome(S))
        self.assertFalse(state.life_resume)
        self.assertFalse(self.engine.resume_life(state))

    async def test_tool_and_speech_plans_are_not_replayed_after_interruption(self):
        state = WorldState(S)
        for action in ("search_web", "say"):
            state.current_plan = create_plan(steps=[{"action": action}], world_time=0, now=self.clock.now())
            self.engine.suspend_life(state, reason="临时有事", source="event")
            self.assertFalse(state.life_resume)

    async def test_event_has_optional_bounded_continuation_not_a_mandatory_task(self):
        state = WorldState(S)
        thread = {"id": "event_one"}
        self.engine.link_event_matter(state, thread, {})
        self.assertEqual(state.life_matters, [])
        proposal = {"text": "慢慢画出刚才见到的天空", "reason": "散步时想留住这片晚霞"}
        self.engine.link_event_matter(state, thread, proposal)
        self.engine.link_event_matter(state, thread, proposal)
        self.assertEqual(len(state.life_matters), 1)
        self.assertEqual(state.life_matters[0]["kind"], "idea")
        self.assertEqual(state.life_matters[0]["origin_event"], thread["id"])
        self.assertEqual(thread["matter_linked"], state.life_matters[0]["id"])

    async def test_appointment_uses_same_matter_and_respects_daily_budget(self):
        state, item = await self.prepare(running=False)
        schedule = ScheduleDef(id="appointment", matter_id=item["id"])
        self.assertTrue(await self.engine.run_matter_appointment(state, schedule, session=S))
        self.assertEqual(state.current_action["matter_id"], item["id"])
        self.assertEqual(len(state.life_matters), 1)
        self.engine.suspend_life(state, reason="先休息", source="rest")
        item["worked_minutes"] = self.engine.world.limits.life_matter_daily_minutes
        self.assertFalse(await self.engine.run_matter_appointment(state, schedule, session=S))

    async def test_delayed_appointment_claims_a_new_slot_and_actually_retries(self):
        now = datetime.fromtimestamp(self.clock.now()).replace(second=0, microsecond=0)
        self.clock._now = now.timestamp()
        self.clock.set_struct(now)
        schedule = ScheduleDef(id="retry", time=now.strftime("%H:%M"), action_chain=[ChainStep(type="read", duration=300)])
        self.engine.schedules.schedules = [schedule]
        self.engine.world.events.schedule_delay_minutes = 5
        self.engine._schedule_gate = AsyncMock(side_effect=[("delay", "先处理手上的事"), ("do", "")])
        async with self.engine.session_state(S) as state:
            state.state, state.energy = "idle", .9
        await self.engine.run_schedules()
        saved = await self.engine.load_state(S, cold_start=False)
        self.assertTrue(saved.schedule_delays)
        self.clock.advance(300)
        self.clock.set_struct(now + timedelta(minutes=5))
        rows = await self.engine.run_schedules()
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.engine._schedule_gate.await_count, 2)
        saved = await self.engine.load_state(S, cold_start=False)
        self.assertFalse(saved.schedule_delays)
        self.assertEqual(saved.current_action["type"], "read")

    async def test_week_rhythm_requires_distinct_dates_and_fades(self):
        state = WorldState(S)
        now = datetime(2026, 10, 19, 20).timestamp()
        episodes = [{"day": (datetime(2026, 10, 19) - timedelta(days=age)).date().isoformat(),
                     "period": "晚间", "node": "study", "minutes": 20, "at": now - age * 86400}
                    for age in (0, 7, 14)]
        state.life_interests["read"] = {"label": "阅读", "sessions": 3, "days": [e["day"] for e in episodes],
                                         "minutes": 60, "episodes": episodes, "last_at": now}
        hints = matters.routine_hints(state, now=now)
        self.assertEqual(hints[0]["weekdays"][0]["weekday"], 0)
        self.assertEqual(hints[0]["weight"], 1.75)
        self.assertEqual(len(hints[0]["evidence"]), 3)
        self.assertEqual(matters.routine_hints(state, now=now + 31 * 86400), [])
        for row in episodes:
            row["day"] = "2026-10-19"
        self.assertEqual(matters.routine_hints(state, now=now), [])

    async def test_fixed_fact_conflict_is_visible_and_resolved_only_by_current_human_choice(self):
        profiles = self.engine.profiles
        profiles.touch(S, "42", "小明")
        original = profiles.note_fact(S, "42", text="喜欢辣椒", kind="like", pinned=True, evidence="人工固定")
        candidate = profiles.note_fact(S, "42", text="不喜欢辣椒", kind="like", evidence="用户明确纠正")
        self.assertEqual(candidate["action"], "candidate")
        again = profiles.note_fact(S, "42", text="不喜欢辣椒", kind="like", evidence="再次说明")
        self.assertEqual(again["id"], candidate["id"])
        self.assertEqual(profiles.dedupe_facts(S, "42"), 0)
        old = self.engine.db.raw.get_user_fact(fact_id=original["id"])
        new = self.engine.db.raw.get_user_fact(fact_id=candidate["id"])
        self.assertEqual(old["status"], "active")
        self.assertFalse(profiles.resolve_fact(S, "42", new["id"], expected=-1))
        self.assertFalse(profiles.resolve_fact("other", "42", new["id"], expected=new["updated_at"]))
        self.assertTrue(profiles.resolve_fact(S, "42", new["id"], expected=new["updated_at"]))
        self.assertEqual(self.engine.db.raw.get_user_fact(fact_id=old["id"])["status"], "past")
        changes = self.engine.db.raw.learning_changes(S, "42")
        self.assertEqual(len(changes), 2)

    async def test_unexplained_habit_omission_keeps_it_but_grounded_correction_records_why(self):
        profiles = self.engine.profiles
        profiles.touch(S, "42", "小明")
        self.engine.db.raw.save_interaction_style(S, "42", [{"text": "可以提醒他早睡", "source_ids": [999]}], revision=0)
        for i in range(5):
            sources = self.engine.interaction_sources(S, "42", "我今天不想被反复催睡", [{"user_id": "42", "text": "我今天不想被反复催睡", "message_id": str(i)}])
            self.engine.record_turn_interaction(S, "42", sources, -.1, "明确表达边界")
        self.engine._ask_judge = AsyncMock(return_value='{"habits":[]}')
        await self.engine.maintain_social_learning(S)
        detail = self.engine.db.raw.interaction_detail(S, "42")
        self.assertEqual(detail["habits"][0]["text"], "可以提醒他早睡")
        for i in range(5, 10):
            sources = self.engine.interaction_sources(S, "42", "以后也不要催睡了", [{"user_id": "42", "text": "以后也不要催睡了", "message_id": str(i)}])
            self.engine.record_turn_interaction(S, "42", sources, -.1, "再次明确纠正")
        latest = self.engine.db.raw.interaction_detail(S, "42")["history"][0]["id"]
        self.engine._ask_judge = AsyncMock(return_value=json.dumps({"habits": [], "retire": [{"text": "可以提醒他早睡", "reason": "他明确要求停止催睡", "source_ids": [latest]}]}))
        await self.engine.maintain_social_learning(S)
        detail = self.engine.db.raw.interaction_detail(S, "42")
        self.assertEqual(detail["habits"], [])
        self.assertIn("明确要求", detail["changes"][0]["reason"])

    async def infer_with_mutation(self, mutate):
        entered, release = asyncio.Event(), asyncio.Event()
        async def generate(**kwargs):
            entered.set()
            await release.wait()
            return LLMReply(ok=True, text="新决定")
        self.llm.generate = generate
        @life_work()
        async def infer(engine):
            async with engine.session_state(S) as state:
                text = await engine._ask_llm(S, "人设优先", "考虑下一步")
                state.external_state["inference_result"] = {"text": text}
            return text
        work = asyncio.create_task(infer(self.engine))
        await asyncio.wait_for(entered.wait(), 2)
        try:
            self.assertFalse(self.engine.is_busy(S))
            await mutate()
        finally:
            release.set()
        return await asyncio.wait_for(work, 2)

    async def test_slow_model_releases_lock_and_rejects_concurrent_manual_change(self):
        async def mutate():
            async with self.engine.session_state(S) as state:
                state.node_id = "bedroom"
        self.assertIsNone(await self.infer_with_mutation(mutate))
        saved = await self.engine.load_state(S, cold_start=False)
        self.assertEqual(saved.node_id, "bedroom")
        self.assertNotIn("inference_result", saved.external_state)

    async def test_slow_model_preserves_new_clock_without_overwriting_it(self):
        async def mutate():
            async with self.engine.session_state(S) as state:
                state.world_time += 2
        before = (await self.engine.load_state(S, cold_start=False)).world_time
        self.assertEqual(await self.infer_with_mutation(mutate), "新决定")
        self.assertEqual((await self.engine.load_state(S, cold_start=False)).world_time, before + 2)

    async def test_slow_model_drops_result_after_persona_change(self):
        async def mutate():
            self.persona.text = "另一位角色"
        self.assertIsNone(await self.infer_with_mutation(mutate))

    async def test_tick_returns_while_event_thinks_and_clock_keeps_running(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def generate(**kwargs):
            entered.set()
            await release.wait()
            return LLMReply(ok=True, text="已想好")
        async def event(state, node, outcome):
            text = await self.engine._ask_llm(S, "事件", "想想")
            state.external_state["event_result"] = {"text": text}
        self.llm.generate = generate
        self.engine.queue_background = Mock(return_value=None)
        self.engine.run_schedules = AsyncMock(return_value=[])
        self.engine._maybe_run_event = event
        async with self.engine.session_state(S) as state:
            state.state, state.energy = "idle", .9
        await asyncio.wait_for(self.engine.tick(), 1)
        await asyncio.wait_for(entered.wait(), 1)
        before = (await self.engine.load_state(S, cold_start=False)).world_time
        self.clock.advance(60)
        await asyncio.wait_for(self.engine.tick(), 1)
        self.assertGreater((await self.engine.load_state(S, cold_start=False)).world_time, before)
        release.set()
        await asyncio.gather(*self.engine._life_delivery_jobs)
        self.assertFalse(self.engine.is_busy(S))
        saved = await self.engine.load_state(S, cold_start=False)
        self.assertEqual(saved.external_state["event_result"]["text"], "已想好")

    async def test_slow_result_is_discarded_after_reload_disable_and_day_change(self):
        for change in ("reload", "disable", "midnight"):
            with self.subTest(change=change):
                async def mutate():
                    if change == "reload":
                        self.engine.reload_config()
                    elif change == "disable":
                        self.engine.is_enabled = Mock(return_value=False)
                    else:
                        self.clock.set_struct(self.engine.local_now() + timedelta(days=1))
                self.assertIsNone(await self.infer_with_mutation(mutate))
                if change == "disable":
                    del self.engine.is_enabled

    async def test_cancellation_during_rejoin_preserves_the_other_writer_and_unlocks(self):
        entered = asyncio.Event()
        async def generate(**kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.llm.generate = generate
        @life_work()
        async def infer(engine):
            async with engine.session_state(S):
                await engine._ask_llm(S, "", "下一步")
        task = asyncio.create_task(infer(self.engine))
        await asyncio.wait_for(entered.wait(), 2)
        async with self.engine.session_state(S) as state:
            state.external_state["human"] = {"text": "保留人工修改"}
            task.cancel()
            await asyncio.sleep(.02)
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        saved = await self.engine.load_state(S, cold_start=False)
        self.assertEqual(saved.external_state["human"]["text"], "保留人工修改")
        self.assertFalse(self.engine.is_busy(S))

    async def test_disabled_or_paused_appointment_does_not_interrupt_current_activity(self):
        state, item = await self.prepare(running=False)
        state.current_action = {"type": "read", "duration_ticks": 20, "elapsed_ticks": 4}
        before = dict(state.current_action)
        schedule = ScheduleDef(id="appointment", matter_id=item["id"])
        item["status"] = "paused"
        self.assertFalse(await self.engine.run_matter_appointment(state, schedule, session=S, force=True))
        self.assertEqual(state.current_action, before)
        item["status"] = "active"
        self.engine.world.limits.life_matter_enabled = False
        self.assertFalse(await self.engine.run_matter_appointment(state, schedule, session=S, force=True))
        self.assertEqual(state.current_action, before)

    async def test_unknown_event_matter_id_does_not_create_a_replacement(self):
        state = WorldState(S)
        self.engine.link_event_matter(state, {"id": "event_unknown"}, {"matter_id": "missing", "text": "误建事项", "reason": "模型引用错误"})
        self.assertEqual(state.life_matters, [])

    async def test_once_appointment_beyond_a_week_is_still_the_next_schedule(self):
        day = (self.engine.local_now() + timedelta(days=20)).date().isoformat()
        self.engine.schedules.schedules = [ScheduleDef(id="later", time="20:00", once=True, date=day, matter_id="lm_future")]
        self.assertEqual(self.engine._next_schedule(S)["id"], "later")

    async def test_private_appointment_runs_in_its_origin_with_shared_life(self):
        fixture.EngineTestCase.add_group(self, sessions=[S, fixture.PRIVATE_SESSION], main=S)
        state, item = await self.prepare(running=False)
        item.update(who="42", session=fixture.PRIVATE_SESSION)
        async with self.engine.session_state(S) as saved:
            saved.__dict__.update(state.__dict__)
        now = self.engine.local_now().replace(second=0, microsecond=0)
        self.clock.set_struct(now)
        self.engine.schedules.schedules = [ScheduleDef(id="private_appointment", time=now.strftime("%H:%M"),
            sessions=[fixture.PRIVATE_SESSION], matter_id=item["id"])]
        await self.engine.run_schedules()
        saved = await self.engine.load_state(S, cold_start=False)
        self.assertEqual(saved.current_action["matter_id"], item["id"])

    async def test_schedule_reload_preserves_new_cursor_reset_and_appointments(self):
        async def run_scope(session, once_done):
            once_done.add(("", "reused_id"))
            self.engine.reload_config()
            self.engine._schedule_reset_pending = True
            return []
        self.engine._run_schedule_scope = run_scope
        self.engine._drop_schedule = Mock()
        await self.engine.run_schedules()
        self.assertTrue(self.engine._schedule_reset_pending)
        self.engine._drop_schedule.assert_not_called()
