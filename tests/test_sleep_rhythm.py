"""自主作息的状态机回归，使用真实存储与模型替身。"""

import asyncio
import unittest
from datetime import datetime

from core.defaults import default_world
from core.engine import MessageContext, MyWorldEngine, TickOutcome
from core.models import parse_world
from tests import test_engine as fixture


class SleepRhythmTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    set_state = fixture.EngineTestCase.set_state
    get_state = fixture.EngineTestCase.get_state

    async def asyncSetUp(self):
        await fixture.EngineTestCase.asyncSetUp(self)
        raw = self.store.raw_world()
        raw["sleep"].update(mode="autonomous", goodnight=False, goodmorning=False, drowsy=False)
        raw.setdefault("events", {})["enabled"] = False
        self.store.save_world(raw)
        self.engine.reload_config()
        self.clock.set_struct(datetime(2026, 10, 19, 21, 30))
        self.llm.replies = []
        await self.set_state(state="idle", cold_start_done=True, node_id="study", world_time=10,
            energy=0.5, sleep_pressure=0.7, sleep_rhythm={"awake_since": self.clock.now() - 14 * 3600})

    async def assess(self):
        outcome = TickOutcome(session_id=fixture.SESSION)
        async with self.engine.session_state(fixture.SESSION) as state:
            await self.engine._tick_sleep_rhythm(state, outcome)
        return outcome

    def add_due_schedule(self, *, chain=None):
        self.store.save_schedules({"schedules":[{"id":"evening_activity", "time":"21:30",
            "smart":False, "auto_travel":False,
            "action_chain":chain or [{"type":"walk_to", "target_node":"window"}]}]})
        self.engine.reload_config()

    async def test_automatic_activity_does_not_replace_the_walk_to_bed(self):
        self.add_due_schedule()
        self.llm.replies = ['{"decision":"sleep","hours":7,"reason":"准备休息"}']
        await self.assess()
        before = await self.get_state()
        self.assertEqual(before.current_action["target_node"], "bedroom")
        await self.engine.run_schedules()
        state = await self.get_state()
        self.assertEqual(state.current_action, before.current_action)
        self.assertEqual(state.current_plan, before.current_plan)

    async def test_automatic_activity_does_not_replace_drowsiness_or_repeat_skip_logs(self):
        self.add_due_schedule()
        await self.prepare_pending_sleep(drowsy=True)
        before = await self.get_state()
        await self.engine.run_schedules()
        state = await self.get_state()
        self.assertEqual(state.state, "drowsy")
        self.assertIsNone(state.current_action)
        self.assertEqual(state.drowsy_sleep_step, before.drowsy_sleep_step)
        rows = await self.db.call("query_events", session_id=fixture.SESSION, limit=30)
        await self.engine.run_schedules()
        self.assertEqual(await self.db.call("query_events", session_id=fixture.SESSION, limit=30), rows)

    async def test_an_instant_reminder_also_yields_to_a_pending_sleep_decision(self):
        self.add_due_schedule(chain=[{"type":"say", "messages":["日程提醒"]}])
        await self.prepare_pending_sleep()
        await self.set_state(current_plan=None)
        self.assertEqual(await self.engine.run_schedules(), [])
        self.assertEqual(self.messenger.sent, [])

    async def test_manual_activity_can_still_run_while_bedtime_is_pending(self):
        self.add_due_schedule()
        await self.prepare_pending_sleep()
        result = await self.engine.run_schedule_now(fixture.SESSION, "evening_activity")
        self.assertTrue(result["ok"])
        self.assertEqual((await self.get_state()).current_action["target_node"], "window")

    async def test_a_started_schedule_does_not_claim_the_whole_chain_is_finished(self):
        self.add_due_schedule(chain=[{"type":"walk_to", "target_node":"window"},
            {"type":"say", "messages":["这里光线好"]}])
        await self.set_state(sleep_rhythm={"awake_since":self.clock.now()})
        await self.engine.run_schedules()
        state = await self.get_state()
        self.assertEqual(state.current_action["type"], "walk_to")
        self.assertTrue(state.current_plan)
        digest = " ".join(state.event_digest)
        self.assertNotIn("按日程做完了", digest)
        self.assertIn("日程已启动", digest)

    async def test_manual_schedule_summary_also_describes_progress(self):
        self.add_due_schedule()
        await self.engine.run_schedule_now(fixture.SESSION, "evening_activity")
        self.assertIn("日程已启动", " ".join((await self.get_state()).event_digest))

    async def test_skipped_continuous_step_does_not_discard_later_valid_steps(self):
        self.add_due_schedule(chain=[{"type":"walk_to", "target_node":"missing_place"},
            {"type":"say", "messages":["先在这里待着吧"]}])
        outcomes = await self.engine.run_schedules()
        self.assertTrue(any("先在这里待着吧" in outcome.messages for outcome in outcomes))
        state = await self.get_state()
        self.assertIsNone(state.current_action)
        self.assertIn("有步骤跳过", " ".join(state.event_digest))

    async def test_skipped_step_preserves_current_activity_and_keeps_processing(self):
        self.add_due_schedule(chain=[{"type":"walk_to", "target_node":"missing_place"},
            {"type":"say", "messages":["这里也挺好"]}])
        await self.set_state(current_action={"type":"read", "duration_ticks":30, "elapsed_ticks":5})
        before = await self.get_state()
        outcomes = await self.engine.run_schedules()
        self.assertEqual((await self.get_state()).current_action, before.current_action)
        self.assertTrue(any("这里也挺好" in item.messages for item in outcomes))

    async def test_fixed_mode_schedule_still_runs_when_a_manual_sleep_is_pending(self):
        self.add_due_schedule()
        await self.switch_sleep_mode("fixed")
        await self.set_state(node_id="bedroom", state="drowsy", current_plan=None,
            drowsy_sleep_step={"type":"sleep", "duration":25200, "plan_source":"manual"},
            sleep_rhythm={"mode":"fixed", "awake_since":self.clock.now()})
        self.assertTrue(await self.engine.run_schedules())
        self.assertEqual((await self.get_state()).current_action["type"], "walk_to")

    async def test_schedule_log_saves_trigger_progress_for_instant_and_continuous_chains(self):
        for chain, status in (([{"type":"say", "messages":["提醒一下"]}], "completed"),
                ([{"type":"walk_to", "target_node":"window"}], "running"),
                ([{"type":"walk_to", "target_node":"missing"}], "skipped")):
            with self.subTest(status=status):
                self.add_due_schedule(chain=chain)
                await self.set_state(state="idle", node_id="study", current_action=None, current_plan=None,
                    schedule_checked_at=0, sleep_rhythm={"awake_since":self.clock.now()})
                result = await self.engine.run_schedule_now(fixture.SESSION, "evening_activity")
                self.assertEqual(result["status"], status)
                rows = await self.db.call("query_events", session_id=fixture.SESSION, limit=30)
                runs = [row for row in rows if row["event_type"] == "schedule"]
                self.assertEqual(runs[0]["detail"]["status"], status)

    async def test_full_sleep_after_a_yielded_schedule_records_only_the_actual_sleep(self):
        self.add_due_schedule()
        self.engine.world.sleep.drowsy = True
        self.llm.replies = ['{"decision":"sleep","hours":7,"reason":"休息"}']
        await self.assess()
        await self.engine.run_schedules()
        for _ in range(8):
            self.clock.advance(60)
            await self.engine._tick_session(fixture.SESSION)
        state = await self.get_state()
        self.assertEqual(state.current_action["type"], "sleep")
        self.assertEqual(state.node_id, "bedroom")
        self.assertNotIn("按日程做完了", " ".join(state.event_digest))

    async def prepare_pending_sleep(self, *, drowsy=False):
        from core.planner import create_plan
        await self.set_state(node_id="bedroom" if drowsy else "study",
            state="drowsy" if drowsy else "idle", current_action=None,
            current_plan=create_plan(steps=[{"action":"sleep", "duration":25200}],
                world_time=10, source="sleep_rhythm"),
            drowsy_sleep_step={"type":"sleep", "duration":25200} if drowsy else {},
            sleep_rhythm={"mode":"autonomous", "decision":"sleep", "source":"llm",
                "duration_seconds":25200, "awake_since":self.clock.now()-14*3600})

    async def switch_sleep_mode(self, mode):
        raw = self.store.raw_world()
        raw["sleep"]["mode"] = mode
        self.store.save_world(raw)
        self.engine.reload_config()

    async def test_clearing_a_pending_sleep_decision_without_a_plan_is_effective(self):
        await self.prepare_pending_sleep()
        await self.set_state(current_plan=None)
        self.assertTrue(await self.engine.clear_plan(fixture.SESSION))
        await self.assess()
        state = await self.get_state()
        self.assertIsNone(state.current_plan)
        self.assertIsNone(state.current_action)
        self.assertEqual(state.sleep_rhythm["decision"], "continue")
        self.assertAlmostEqual(self.engine._rhythm_awake_hours(state), 14)
        self.assertEqual(state.sleep_history, [])
        self.assertEqual(len(self.llm.calls), 0)

    async def test_clearing_the_bedtime_queue_allows_the_current_walk_to_finish(self):
        await self.prepare_pending_sleep()
        await self.set_state(current_action={"type":"walk_to", "target_node":"bedroom",
            "elapsed_ticks":0, "duration_ticks":2, "interruptible":True})
        self.assertTrue(await self.engine.clear_plan(fixture.SESSION))
        for _ in range(2):
            self.clock.advance(60)
            await self.engine._tick_session(fixture.SESSION)
        state = await self.get_state()
        self.assertEqual(state.node_id, "bedroom")
        self.assertNotEqual((state.current_action or {}).get("type"), "sleep")
        self.assertNotEqual((state.current_plan or {}).get("source"), "sleep_rhythm")

    async def test_clearing_a_drowsy_queue_cancels_its_detached_sleep_step(self):
        await self.prepare_pending_sleep(drowsy=True)
        self.assertTrue(await self.engine.clear_plan(fixture.SESSION))
        self.clock.advance(360)
        await self.engine._tick_session(fixture.SESSION)
        state = await self.get_state()
        self.assertEqual(state.drowsy_sleep_step, {})
        self.assertEqual(state.state, "idle")
        self.assertIsNone(state.current_action)
        self.assertEqual(state.sleep_history, [])

    async def test_model_cancellation_clears_drowsiness_and_pending_decisions(self):
        for mode in ("queue", "now"):
            with self.subTest(mode=mode):
                await self.prepare_pending_sleep(drowsy=True)
                async with self.engine.session_state(fixture.SESSION) as state:
                    note = await self.engine.apply_cancel(state, mode, "先别睡",
                        TickOutcome(session_id=fixture.SESSION))
                    self.assertIn("入睡", note)
                    self.assertEqual(state.drowsy_sleep_step, {})
                    self.assertEqual(state.state, "idle")
                await self.assess()
                self.assertIsNone((await self.get_state()).current_action)

    async def test_interrupt_button_can_cancel_drowsiness(self):
        await self.prepare_pending_sleep(drowsy=True)
        self.assertTrue(await self.engine.interrupt(fixture.SESSION, force=True))
        state = await self.get_state()
        self.assertEqual(state.state, "idle")
        self.assertEqual(state.drowsy_sleep_step, {})
        self.assertIsNone(state.current_plan)

    async def test_cancellation_expires_and_does_not_reset_sleep_debt(self):
        await self.prepare_pending_sleep()
        await self.engine.clear_plan(fixture.SESSION)
        self.clock.advance(3601)
        await self.set_state(world_time=71)
        self.llm.replies = ['{"decision":"sleep","hours":7,"reason":"重新评估还是困"}']
        await self.assess()
        self.assertEqual((await self.get_state()).current_action["type"], "walk_to")
        self.assertEqual(len(self.llm.calls), 1)

    async def test_cancelling_a_queue_keeps_an_already_started_sleep(self):
        for cancel in ("clear", "queue"):
            with self.subTest(cancel=cancel):
                await self.prepare_pending_sleep()
                await self.set_state(node_id="bedroom", state="sleeping",
                    current_action={"type":"sleep", "elapsed_ticks":20, "duration_ticks":420},
                    sleep_rhythm={"mode":"autonomous", "decision":"sleeping",
                        "planned_wake_at":self.clock.now()+24000, "awake_since":self.clock.now()-14*3600})
                before = await self.get_state()
                if cancel == "clear":
                    await self.engine.clear_plan(fixture.SESSION)
                else:
                    async with self.engine.session_state(fixture.SESSION) as state:
                        await self.engine.apply_cancel(state, "queue", "放弃之后的安排", TickOutcome(fixture.SESSION))
                state = await self.get_state()
                self.assertEqual(state.current_action, before.current_action)
                self.assertEqual(state.sleep_rhythm, before.sleep_rhythm)
                self.assertEqual(state.sleep_history, [])

    async def test_cancellation_does_not_disable_overwork_protection(self):
        await self.prepare_pending_sleep()
        await self.engine.clear_plan(fixture.SESSION)
        async with self.engine.session_state(fixture.SESSION) as state:
            state.sleep_rhythm["awake_since"] = self.clock.now()-21*3600
        await self.assess()
        state = await self.get_state()
        self.assertEqual(state.sleep_rhythm["source"], "safety")
        self.assertEqual(state.current_action["type"], "walk_to")

    async def test_pending_decision_survives_engine_restart_without_being_cancelled(self):
        await self.prepare_pending_sleep()
        other = MyWorldEngine(store=self.store, db=self.db, llm=self.llm,
            messenger=self.messenger, persona=self.persona, clock=self.clock,
            tick_seconds=60, decider_interval=0)
        async with other.session_state(fixture.SESSION) as state:
            self.assertEqual(state.sleep_rhythm["decision"], "sleep")
            self.assertEqual(state.current_plan["source"], "sleep_rhythm")

    async def test_old_pending_decision_without_mode_is_cancelled_in_fixed_mode(self):
        await self.prepare_pending_sleep()
        async with self.engine.session_state(fixture.SESSION) as state:
            state.sleep_rhythm.pop("mode", None)
        await self.switch_sleep_mode("fixed")
        async with self.engine.session_state(fixture.SESSION) as state:
            self.assertEqual(state.sleep_rhythm["decision"], "continue")
            self.assertIsNone(state.current_plan)

    async def test_mode_switch_preserves_existing_wake_and_delay_protection(self):
        await self.prepare_pending_sleep()
        await self.set_state(no_sleep_until=300, sleep_delay_until=400)
        await self.switch_sleep_mode("fixed")
        async with self.engine.session_state(fixture.SESSION) as state:
            self.assertEqual(state.no_sleep_until, 300)
            self.assertEqual(state.sleep_delay_until, 400)

    async def test_switching_to_fixed_discards_autonomous_queue_but_finishes_walk(self):
        await self.prepare_pending_sleep()
        await self.set_state(current_action={"type":"walk_to", "target_node":"bedroom",
            "elapsed_ticks":0, "duration_ticks":2, "interruptible":True})
        await self.switch_sleep_mode("fixed")
        async with self.engine.session_state(fixture.SESSION) as state:
            self.assertIsNone(state.current_plan)
            self.assertEqual(state.current_action["type"], "walk_to")
            self.assertNotEqual(state.sleep_rhythm["decision"], "sleep")
        await self.switch_sleep_mode("autonomous")
        self.llm.replies = ['{"decision":"continue","reason":"先整理一下"}']
        await self.assess()
        state = await self.get_state()
        self.assertIsNone(state.current_plan)
        self.assertEqual(state.sleep_rhythm["source"], "llm")

    async def test_switching_to_fixed_cancels_autonomous_drowsiness(self):
        await self.prepare_pending_sleep(drowsy=True)
        await self.switch_sleep_mode("fixed")
        async with self.engine.session_state(fixture.SESSION) as state:
            self.assertEqual(state.state, "idle")
            self.assertEqual(state.drowsy_sleep_step, {})
            self.assertIsNone(state.current_plan)
            self.assertEqual(state.sleep_history, [])

    async def test_switching_to_autonomy_discards_an_unstarted_fixed_sleep_schedule(self):
        from core.planner import create_plan
        await self.set_state(sleep_rhythm={"mode":"fixed", "awake_since":self.clock.now()},
            current_plan=create_plan(steps=[{"action":"walk_to", "target_node":"bedroom"},
                {"action":"sleep"}], world_time=10, source="schedule"))
        # 当前已为自主模式，模拟上次固定作息留下的状态即可。
        async with self.engine.session_state(fixture.SESSION) as state:
            self.assertIsNone(state.current_plan)
            self.assertEqual(state.sleep_rhythm["mode"], "autonomous")

    async def test_switching_to_autonomy_cancels_detached_fixed_schedule_sleep_step(self):
        from core.planner import create_plan
        plan = create_plan(steps=[{"action":"sleep"}, {"action":"read"}],
            world_time=10, source="schedule")
        plan["steps"][0]["status"] = "done"
        plan["current_step"] = 1
        await self.set_state(state="drowsy", node_id="bedroom", current_plan=plan,
            drowsy_sleep_step={"type":"sleep", "duration":25200},
            sleep_rhythm={"mode":"fixed", "awake_since":self.clock.now()})
        async with self.engine.session_state(fixture.SESSION) as state:
            self.assertIsNone(state.current_plan)
            self.assertEqual(state.drowsy_sleep_step, {})
            self.assertEqual(state.state, "idle")

    async def test_switching_modes_preserves_actual_sleep_and_manual_queue(self):
        from core.planner import create_plan
        await self.set_state(node_id="bedroom")
        self.llm.replies = ['{"decision":"sleep","hours":7,"reason":"休息"}']
        await self.assess()
        before = await self.get_state()
        await self.switch_sleep_mode("fixed")
        async with self.engine.session_state(fixture.SESSION) as state:
            self.assertEqual(state.current_action, before.current_action)
            self.assertEqual(state.sleep_rhythm["decision"], "sleeping")
            self.assertEqual(state.sleep_history, [])
        await self.set_state(state="idle", current_action=None,
            current_plan=create_plan(steps=[{"action":"read"}], world_time=10, source="manual"),
            sleep_rhythm={"mode":"fixed", "decision":"awake", "awake_since":self.clock.now()})
        await self.switch_sleep_mode("autonomous")
        async with self.engine.session_state(fixture.SESSION) as state:
            self.assertEqual(state.current_plan["source"], "manual")

    async def test_manual_schedule_trial_keeps_its_queue_across_a_mode_switch(self):
        raw = self.store.raw_schedules()
        raw["schedules"] = [{"id":"manual_test", "time":"22:00", "smart":False,
            "action_chain":[{"type":"walk_to", "target_node":"bedroom"},
                {"type":"sleep", "duration":25200}]}]
        self.store.save_schedules(raw)
        self.engine.reload_config()
        await self.engine.run_schedule_now(fixture.SESSION, "manual_test")
        self.assertTrue((await self.get_state()).current_plan["manual"])
        await self.switch_sleep_mode("fixed")
        async with self.engine.session_state(fixture.SESSION) as state:
            self.assertTrue(state.current_plan["manual"])
            self.assertEqual(state.current_plan["source"], "schedule")

    async def test_drowsy_schedule_without_a_queue_has_a_source_for_mode_switches(self):
        from core.models import ChainStep
        for source in ("schedule", "manual"):
            with self.subTest(source=source):
                await self.switch_sleep_mode("fixed")
                self.engine.world.sleep.drowsy = True
                self.clock.set_struct(datetime(2026,10,19,23,30))
                await self.set_state(node_id="bedroom", current_action=None, current_plan=None,
                    state="idle", drowsy_day="", drowsy_sleep_step={},
                    sleep_rhythm={"mode":"fixed", "awake_since":self.clock.now()})
                async with self.engine.session_state(fixture.SESSION) as state:
                    await self.engine._run_chain(state, [ChainStep(type="sleep", duration=25200)],
                        TickOutcome(fixture.SESSION), depth=0, plan_source=source)
                    self.assertEqual(state.state, "drowsy")
                    self.assertIsNone(state.current_plan)
                await self.switch_sleep_mode("autonomous")
                async with self.engine.session_state(fixture.SESSION) as state:
                    self.assertEqual(state.state, "idle" if source == "schedule" else "drowsy")
                    self.assertEqual(bool(state.drowsy_sleep_step), source == "manual")

    async def test_goodnight_still_runs_when_drowsiness_is_disabled(self):
        self.engine.world.sleep.goodnight = True
        await self.set_state(node_id="bedroom")
        self.llm.replies = ['{"decision":"sleep","hours":7,"reason":"今天累了"}',
            '{"plan":[{"action":"say","messages":["晚安，明天见"]}]}']
        outcome = await self.assess()
        self.assertIn("晚安，明天见", outcome.messages)
        self.assertEqual((await self.get_state()).current_action["type"], "sleep")
        self.assertEqual(len(self.llm.calls), 2)

    async def test_overwork_sleep_does_not_wait_for_a_goodnight_model_call(self):
        self.engine.world.sleep.goodnight = True
        await self.set_state(node_id="bedroom",
            sleep_rhythm={"awake_since":self.clock.now()-21*3600})
        await self.assess()
        self.assertEqual((await self.get_state()).current_action["type"], "sleep")
        self.assertEqual(len(self.llm.calls), 0)

    async def test_shared_life_cancellation_from_a_member_clears_the_same_pending_sleep(self):
        fixture.EngineTestCase.add_group(self, sessions=[fixture.SESSION, fixture.OTHER_SESSION])
        await self.prepare_pending_sleep(drowsy=True)
        self.assertTrue(await self.engine.clear_plan(fixture.OTHER_SESSION))
        state = await self.get_state()
        shared = await self.engine.load_state(fixture.OTHER_SESSION, cold_start=False)
        self.assertEqual(state.sleep_rhythm["decision"], "continue")
        self.assertEqual(shared.sleep_rhythm, state.sleep_rhythm)
        self.assertEqual(shared.drowsy_sleep_step, {})

    async def test_drowsy_goodnight_is_not_repeated_when_sleep_actually_starts(self):
        self.engine.world.sleep.goodnight = True
        self.engine.world.sleep.drowsy = True
        await self.set_state(node_id="bedroom")
        self.llm.replies = ['{"decision":"sleep","hours":7,"reason":"累了"}',
            '{"plan":[{"action":"say","messages":["先睡了"]}]}']
        await self.assess()
        self.clock.advance(360)
        await self.engine._tick_session(fixture.SESSION)
        self.assertEqual((await self.get_state()).current_action["type"], "sleep")
        self.assertEqual(len(self.llm.calls), 2)

    async def test_arrival_for_sleep_does_not_plan_another_activity_during_drowsiness(self):
        self.engine.world.sleep.drowsy = True
        self.llm.replies = ['{"decision":"sleep","hours":7,"reason":"今天累了"}']
        await self.assess()
        for _ in range(2):
            self.clock.advance(60)
            await self.engine._tick_session(fixture.SESSION)
        state = await self.get_state()
        self.assertEqual(state.node_id, "bedroom")
        self.assertEqual(state.state, "drowsy")
        self.assertFalse(state.pending_arrival)
        self.assertEqual(len(self.llm.calls), 1)

    async def test_goodnight_is_spoken_before_sleep_without_replacing_queued_activity(self):
        from core.planner import create_plan
        self.engine.world.sleep.goodnight = True
        self.engine.world.sleep.drowsy = True
        await self.set_state(node_id="bedroom")
        async with self.engine.session_state(fixture.SESSION) as state:
            state.current_plan = create_plan(steps=[{"action":"read","duration":900}],
                world_time=state.world_time, source="schedule", reason="睡醒后看书")
            queued = state.current_plan
            self.llm.replies = ['{"plan":[{"action":"say","messages":["晚安，我先睡啦"]},'
                '{"action":"walk_to","target_node":"window"}],"reason":"睡前问候"}']
            outcome = TickOutcome(session_id=fixture.SESSION)
            await self.engine._enter_drowsy(state, self.engine.node("bedroom"), outcome,
                self.engine.world.action_map()["sleep"],
                self.engine._step_to_action({"action":"sleep","duration":25200}), 0)
            self.assertIn("晚安，我先睡啦", outcome.messages)
            self.assertIs(state.current_plan, queued)
            self.assertEqual(state.state, "drowsy")
            self.assertIsNone(state.current_action)

    async def test_goodmorning_is_spoken_at_actual_wake_and_keeps_queued_plan(self):
        from core.planner import create_plan
        self.engine.world.sleep.goodmorning = True
        # 排队动作在卧室即可开始，单独验证问候不会替换或跳过该计划。
        self.engine.world.action_map()["read"].scope = "global"
        await self.set_state(node_id="bedroom")
        self.llm.replies = ['{"decision":"sleep","hours":7,"reason":"早些休息"}']
        await self.assess()
        async with self.engine.session_state(fixture.SESSION) as state:
            state.current_action["elapsed_ticks"] = 419
            state.current_action["from_plan"] = False
            state.current_plan = create_plan(steps=[{"action":"read","duration":900}],
                world_time=state.world_time, source="schedule", reason="醒来后读书")
        self.llm.replies = ['{"plan":[{"action":"say","messages":["早呀，睡醒了"]}],"reason":"起床问候"}']
        outcome = await self.engine._tick_session(fixture.SESSION)
        self.assertIn("早呀，睡醒了", outcome.messages)
        state = await self.get_state()
        self.assertEqual(state.current_action["type"], "read")
        self.assertEqual(state.current_plan["source"], "schedule")

    async def test_wake_words_in_an_incoming_message_cancel_drowsiness(self):
        await self.set_state(node_id="bedroom", state="drowsy",
            drowsy_sleep_step={"type":"sleep","duration":25200},
            sleep_rhythm={"decision":"sleep", "duration_seconds":25200,
                "awake_since":self.clock.now()-14*3600})
        await self.engine.handle_incoming(MessageContext(session_id=fixture.SESSION,
            user_id="test", user_name="测试用户", text="醒醒，先听我说", is_mentioned=True, is_wake=True))
        state = await self.get_state()
        self.assertEqual(state.state, "idle")
        self.assertEqual(state.drowsy_sleep_step, {})
        self.assertEqual(state.sleep_rhythm["source"], "wake")
        self.assertGreater(state.no_sleep_until, state.world_time)

    async def test_drowsy_phase_with_empty_plan_stays_quiet_under_manual_decision(self):
        await self.set_state(node_id="bedroom", state="drowsy", current_plan=None,
            drowsy_sleep_step={"type":"sleep", "duration":25200})
        self.assertIsNone(await self.engine.maybe_decide(fixture.SESSION, force=True))
        async with self.engine.session_state(fixture.SESSION) as state:
            await self.engine.decide_after_arrival(state, self.engine.node("bedroom"), TickOutcome(fixture.SESSION))
        self.assertEqual(len(self.llm.calls), 0)
        self.assertIsNone((await self.get_state()).current_plan)

    async def test_greeting_routes_each_message_without_changing_sleep_plan(self):
        fixture.EngineTestCase.add_group(self, sessions=[fixture.SESSION, fixture.OTHER_SESSION])
        self.engine.world.sleep.goodnight = True
        await self.set_state(state="drowsy", node_id="bedroom")
        self.llm.replies = ['{"plan":[{"action":"say","messages":["晚安啦"],'
            '"send_to":"群 1001"},{"action":"say","messages":["我也跟这边说声晚安"],'
            '"send_to":"群 2002"}],"reason":"睡前问候"}']
        async with self.engine.session_state(fixture.SESSION) as state:
            outcome = TickOutcome(session_id=fixture.SESSION)
            self.assertTrue(await self.engine._maybe_say_goodnight(state, self.engine.node("bedroom"), outcome))
            self.assertEqual(outcome.messages, ["晚安啦"])
            self.assertEqual(outcome.routed, {fixture.OTHER_SESSION:["我也跟这边说声晚安"]})
            self.assertEqual(state.state, "drowsy")
            self.assertIsNone(state.current_plan)
        await self.engine._deliver(outcome)
        self.assertIn((fixture.OTHER_SESSION, ["我也跟这边说声晚安"]), self.messenger.sent)

    async def test_shared_life_evaluates_and_records_sleep_once_for_both_sessions(self):
        fixture.EngineTestCase.add_group(self, sessions=[fixture.SESSION, fixture.OTHER_SESSION])
        self.llm.replies = ['{"decision":"sleep","hours":7,"reason":"休息"}']

        async def tick_and_settle():
            await self.engine.tick()
            # tick deliberately returns early for slow life work. Verify the
            # completed shared cycle rather than racing its background commit.
            pending = list(getattr(self.engine, "_life_clock_jobs", {}).values())
            if pending:
                await asyncio.wait_for(asyncio.gather(*pending), 2)

        before = (await self.get_state()).world_time
        await tick_and_settle()
        shared = await self.engine.load_state(fixture.OTHER_SESSION, cold_start=False)
        self.assertEqual(shared.world_time, before+1)
        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual(shared.sleep_rhythm, (await self.get_state()).sleep_rhythm)
        await tick_and_settle()
        await tick_and_settle()
        async with self.engine.session_state(fixture.OTHER_SESSION) as state:
            self.assertEqual(state.current_action["type"], "sleep")
            self.assertEqual(state.current_action["duration_ticks"], 420)
            state.current_action["elapsed_ticks"] = 419
        await tick_and_settle()
        self.assertEqual(len((await self.get_state()).sleep_history), 1)
        self.assertEqual((await self.engine.load_state(fixture.OTHER_SESSION)).sleep_history,
            (await self.get_state()).sleep_history)

    async def test_greeting_respects_pause_and_disabled_say_without_replacing_state(self):
        self.engine.world.sleep.goodnight = True
        await self.set_state(state="drowsy", node_id="bedroom")
        self.engine.speech_allowed = lambda *args: False
        async with self.engine.session_state(fixture.SESSION) as state:
            outcome = TickOutcome(fixture.SESSION)
            self.assertFalse(await self.engine._maybe_say_goodnight(state, self.engine.node("bedroom"), outcome))
        self.assertEqual(len(self.llm.calls), 0)
        self.engine.speech_allowed = lambda *args: True
        self.engine.world.action_map()["say"].enabled = False
        self.llm.replies = ['{"plan":[{"action":"say","messages":["晚安"]}]}']
        async with self.engine.session_state(fixture.SESSION) as state:
            outcome = TickOutcome(fixture.SESSION)
            self.assertFalse(await self.engine._maybe_say_goodnight(state, self.engine.node("bedroom"), outcome))
            self.assertEqual(outcome.messages, [])
            self.assertEqual(state.state, "drowsy")

    async def test_new_emergency_defers_sleep_after_the_drowsy_phase(self):
        await self.set_state(node_id="bedroom", state="drowsy", current_plan=None,
            drowsy_sleep_step={"type":"sleep", "duration":25200},
            event_threads=[{"id":"urgent", "status":"open", "root":{"critical":True}}])
        async with self.engine.session_state(fixture.SESSION) as state:
            state.drowsy_started_at = self.clock.now()-360
            outcome = TickOutcome(fixture.SESSION)
            await self.engine._drowsy_tick(state, self.engine.node("bedroom"), outcome)
            self.assertEqual(state.state, "drowsy")
            self.assertIsNone(state.current_action)
            self.assertTrue(state.drowsy_sleep_step)
            state.event_threads = []
            await self.engine._drowsy_tick(state, self.engine.node("bedroom"), outcome)
            self.assertEqual(state.current_action["type"], "sleep")

    async def test_two_days_without_chat_keep_sleep_cycles_and_variable_durations(self):
        import json
        from core.ports import LLMReply
        from tests.stub_ports import StubClock
        self.clock = StubClock(now=datetime(2026,10,19,8).timestamp())
        self.engine.clock = self.clock
        self.engine.tick_seconds = 600
        self.engine.schedules.schedules = []
        self.engine.world.decider.enabled = False
        self.engine.world.profile.consolidate_enabled = False
        # 固定本次模拟的疲劳曲线，避免随机日基调提前触发另有测试的过劳兜底。
        self.engine.world.state_dynamics.daily_mood_enabled = False
        self.engine.world.state_dynamics.energy_decay_per_min = 0.0005
        await self.set_state(node_id="bedroom", energy=1, sleep_pressure=0,
            sleep_rhythm={"awake_since":self.clock.now()})
        sleeps = []
        async def decide(**kwargs):
            context = kwargs["prompt"].split("\n作息偏好：", 1)[1].split("\n", 1)[1]
            payload, _ = json.JSONDecoder().raw_decode(context)
            should_sleep = payload["清醒小时"] >= 14
            hours = 7 if not sleeps else 8
            if should_sleep:
                sleeps.append(hours)
            return LLMReply(ok=True, text=json.dumps({"decision":"sleep" if should_sleep else "continue",
                "hours":hours, "review_minutes":60, "reason":"结合当天疲劳休息"}))
        self.llm.generate = decide
        for _ in range(288):
            self.clock.advance(600)
            await self.engine._tick_session(fixture.SESSION)
        state = await self.get_state()
        self.assertEqual([item["hours"] for item in state.sleep_history], [7, 8])
        self.assertTrue(all(item["rested"] and not item["interrupted"] for item in state.sleep_history))
        self.assertEqual(self.messenger.sent, [])
        self.assertEqual(state.node_id, "bedroom")

    async def test_model_can_sleep_before_fixed_night_and_choose_duration(self):
        self.llm.replies = ['{"decision":"sleep","hours":7.5,"reason":"今天走累了，早些休息"}']
        await self.assess()
        state = await self.get_state()
        self.assertEqual(state.current_action["type"], "walk_to")
        self.assertEqual(state.current_action["target_node"], "bedroom")
        self.assertEqual(state.current_plan["steps"][-1]["duration"], 27000)
        self.assertEqual(state.sleep_rhythm["source"], "llm")
        await self.engine.tick()
        await self.engine.tick()
        state = await self.get_state()
        self.assertEqual(state.node_id, "bedroom")
        self.assertEqual(state.current_action["type"], "sleep")
        self.assertEqual(state.current_action["duration_ticks"], 450)
        self.assertEqual(len(self.llm.calls), 1)

    async def test_continue_is_persisted_throttled_and_independent_of_chat_cooldown(self):
        self.llm.replies = ['{"decision":"continue","review_minutes":30,"reason":"看完这一章再休息"}']
        await self.set_state(cooldown_until=10000)
        await self.assess()
        self.assertEqual(len(self.llm.calls), 1)
        other = MyWorldEngine(store=self.store, db=self.db, clock=self.clock, llm=self.llm, tick_seconds=60)
        async with other.session_state(fixture.SESSION) as state:
            self.assertIn("这一章", state.sleep_rhythm["reason"])
            await other._tick_sleep_rhythm(state, TickOutcome(session_id=fixture.SESSION))
        self.assertEqual(len(self.llm.calls), 1)
        self.clock.advance(1801)
        self.llm.replies = ['{"decision":"sleep","hours":8,"reason":"现在休息"}']
        await self.assess()
        self.assertEqual(len(self.llm.calls), 2)

    async def test_failure_uses_fallback_and_overwork_preempts_a_long_action(self):
        await self.set_state(energy=0.2, sleep_rhythm={"awake_since": self.clock.now() - 18 * 3600})
        self.llm.default_reply = "不是合法 JSON"
        await self.assess()
        self.assertEqual((await self.get_state()).sleep_rhythm["source"], "fallback")
        await self.set_state(current_action={"type":"read", "desc":"看书", "interruptible":False},
            current_plan=None, sleep_delay_until=10000,
            sleep_rhythm={"awake_since":self.clock.now()-21*3600})
        await self.assess()
        state = await self.get_state()
        self.assertEqual(state.sleep_rhythm["source"], "safety")
        self.assertEqual(state.current_action["type"], "walk_to")
        self.assertFalse(state.sleep_delayed)

    async def test_sleep_decision_waits_for_activity_then_starts_without_another_llm_call(self):
        self.llm.replies = ['{"decision":"sleep","hours":8,"reason":"把手头事情收尾就去睡"}']
        await self.set_state(current_action={"type":"read", "desc":"看书"})
        await self.assess()
        self.assertEqual((await self.get_state()).current_action["type"], "read")
        await self.set_state(current_action=None)
        await self.assess()
        self.assertEqual((await self.get_state()).current_action["type"], "walk_to")
        self.assertEqual(len(self.llm.calls), 1)

    async def test_duration_is_bounded_and_wake_records_actual_sleep_once(self):
        await self.set_state(node_id="bedroom")
        self.llm.replies = ['{"decision":"sleep","hours":999,"reason":"休息"}']
        await self.assess()
        async with self.engine.session_state(fixture.SESSION) as state:
            self.assertEqual(state.current_action["duration_ticks"], 600)
            state.current_action["elapsed_ticks"] = 120
            self.engine._wake_up_state(state, MessageContext(session_id=fixture.SESSION, user_id="test"))
            self.assertEqual(state.sleep_history[-1]["hours"], 2)
            self.assertTrue(state.sleep_history[-1]["interrupted"])
            self.assertGreater(self.engine._rhythm_awake_hours(state), 8)
        snapshot = await self.engine.snapshot(fixture.SESSION)
        self.assertEqual(snapshot["sleep"]["mode"], "autonomous")
        self.assertEqual(len(snapshot["sleep"]["history"]), 1)

    async def test_full_sleep_resets_awake_time_but_a_nap_only_eases_it(self):
        async with self.engine.session_state(fixture.SESSION) as state:
            self.engine._start_sleep_rhythm(state, "nap", 30)
            action = {"type":"nap", "elapsed_ticks":30}
            self.engine._finish_sleep_rhythm(state, action, interrupted=False)
            self.engine._finish_sleep_rhythm(state, action, interrupted=False)
            self.assertAlmostEqual(self.engine._rhythm_awake_hours(state), 13)
            self.assertEqual(len(state.sleep_history), 1)
            self.engine._start_sleep_rhythm(state, "sleep", 480)
            self.engine._finish_sleep_rhythm(state, {"type":"sleep", "elapsed_ticks":480}, interrupted=False)
            self.assertEqual(self.engine._rhythm_awake_hours(state), 0)

    async def test_automatic_sleep_schedule_yields_but_manual_run_still_works(self):
        self.clock.set_struct(datetime(2026, 10, 19, 23, 30))
        await self.engine.run_schedules()
        self.assertIsNone((await self.get_state()).current_action)
        rows = await self.db.call("query_events", session_id=fixture.SESSION, limit=30)
        self.assertTrue(any("自主作息接管" in str(row) for row in rows))
        result = await self.engine.run_schedule_now(fixture.SESSION, "night_sleep")
        self.assertTrue(result["ok"], result)
        self.assertEqual((await self.get_state()).current_action["type"], "walk_to")

    async def test_pressure_keeps_building_after_sunrise_and_rest_does_not_force_another_night(self):
        async with self.engine.session_state(fixture.SESSION) as state:
            state.sleep_pressure = 0.8
            self.engine.dynamics._tick_sleep_pressure(state, minutes=60, world=self.engine.world,
                now=datetime(2026,10,19,9).timestamp())
            self.assertGreater(state.sleep_pressure, 0.8)
        await self.set_state(energy=0.1,sleep_pressure=0.1,sleep_rhythm={"awake_since":self.clock.now()-2*3600})
        await self.assess()
        self.assertEqual(len(self.llm.calls), 0)

    async def test_malformed_model_numbers_do_not_break_ticks(self):
        self.llm.replies = ['{"decision":"sleep","hours":"NaN","review_minutes":"oops"}']
        await self.assess()
        self.assertEqual((await self.get_state()).current_plan["steps"][-1]["duration"], 28800)

    async def test_sleeping_does_not_get_woken_by_a_morning_schedule(self):
        await self.set_state(node_id="bedroom")
        self.llm.replies = ['{"decision":"sleep","hours":8,"reason":"休息"}']
        await self.assess()
        schedule = self.engine.schedules.schedules[0]
        self.clock.set_struct(datetime(2026,10,19,7,0))
        schedule.time = "07:00"
        await self.engine.run_schedules()
        self.assertEqual((await self.get_state()).current_action["type"], "sleep")

    async def test_drowsy_overwork_cannot_be_extended_forever(self):
        await self.set_state(node_id="bedroom", state="drowsy", sleep_delay_until=10000,
            drowsy_sleep_step={"type":"sleep","duration":25200},
            sleep_rhythm={"awake_since":self.clock.now()-21*3600})
        await self.assess()
        state = await self.get_state()
        self.assertEqual(state.current_action["type"], "sleep")
        self.assertFalse(state.sleep_delayed)

    async def test_waking_during_drowsiness_cancels_pending_sleep_without_recording_sleep(self):
        await self.set_state(node_id="bedroom", state="drowsy", cooldown_until=10000,
            drowsy_sleep_step={"type":"sleep", "duration":25200},
            sleep_rhythm={"decision":"sleep", "duration_seconds":25200,
                "awake_since":self.clock.now()-14*3600})
        self.assertTrue(await self.engine.wake_up(fixture.SESSION))
        state = await self.get_state()
        self.assertEqual(state.sleep_rhythm["decision"], "continue")
        self.assertEqual(state.sleep_history, [])
        self.assertAlmostEqual(self.engine._rhythm_awake_hours(state), 14)
        await self.engine.maybe_decide(fixture.SESSION, force=True)
        await self.assess()
        state = await self.get_state()
        self.assertNotEqual((state.current_action or {}).get("type"), "sleep")
        self.assertEqual(len(self.llm.calls), 0)

    async def test_pending_sleep_respects_wake_and_delay_protection_before_walking(self):
        for protection in ("no_sleep_until", "sleep_delay_until"):
            with self.subTest(protection=protection):
                await self.set_state(current_action=None, current_plan=None,
                    sleep_rhythm={"decision":"sleep", "duration_seconds":28800,
                        "awake_since":self.clock.now()-14*3600},
                    **{"no_sleep_until":0, "sleep_delay_until":0, protection:100})
                await self.engine.maybe_decide(fixture.SESSION, force=True)
                state = await self.get_state()
                self.assertIsNone(state.current_action)
                self.assertIsNone(state.current_plan)
                await self.set_state(**{protection:0})
                await self.engine.maybe_decide(fixture.SESSION, force=True)
                self.assertEqual((await self.get_state()).current_action["type"], "walk_to")

    async def test_sleep_notes_do_not_repeat_an_old_bedtime_reason_after_waking(self):
        async with self.engine.session_state(fixture.SESSION) as state:
            state.sleep_rhythm.update(decision="awake", reason="准备去睡觉")
            self.assertNotIn("准备去睡觉", "".join(self.engine.sleep_notes(state)))

    async def test_overwork_with_disabled_sleep_backs_off_instead_of_logging_every_tick(self):
        self.engine.world.action_map()["sleep"].enabled = False
        await self.set_state(sleep_rhythm={"awake_since":self.clock.now()-21*3600})
        await self.assess()
        events = await self.db.call("query_events", session_id=fixture.SESSION, limit=30)
        await self.assess()
        await self.assess()
        self.assertEqual(await self.db.call("query_events", session_id=fixture.SESSION, limit=30), events)
        self.assertIsNone((await self.get_state()).current_action)

    async def test_disabling_sleep_records_actual_duration_and_clears_sleeping_status(self):
        await self.set_state(node_id="bedroom")
        self.llm.replies = ['{"decision":"sleep","hours":8,"reason":"休息"}']
        await self.assess()
        async with self.engine.session_state(fixture.SESSION) as state:
            state.current_action["elapsed_ticks"] = 90
        self.engine.world.action_map()["sleep"].enabled = False
        await self.engine._tick_session(fixture.SESSION)
        state = await self.get_state()
        self.assertEqual(state.sleep_history[-1]["hours"], 1.5)
        self.assertTrue(state.sleep_history[-1]["interrupted"])
        self.assertEqual(state.sleep_rhythm["decision"], "awake")

    async def test_interrupting_a_nap_when_gate_is_disabled_still_records_sleep(self):
        self.engine.world.sleep.applies_to_nap = False
        async with self.engine.session_state(fixture.SESSION) as state:
            self.engine._start_sleep_rhythm(state, "nap", 30)
            state.state = "napping"
            state.current_action = {"type":"nap", "elapsed_ticks":20, "interruptible":True}
        self.assertTrue(await self.engine.interrupt(fixture.SESSION))
        state = await self.get_state()
        self.assertAlmostEqual(state.sleep_history[-1]["hours"], 0.33)
        self.assertTrue(state.sleep_history[-1]["interrupted"])

    async def test_natural_sleep_completion_records_duration_and_does_not_repeat(self):
        await self.set_state(node_id="bedroom")
        self.llm.replies = ['{"decision":"sleep","hours":7,"reason":"明早想早点起"}']
        await self.assess()
        async with self.engine.session_state(fixture.SESSION) as state:
            state.current_action["elapsed_ticks"] = 419
        await self.engine._tick_session(fixture.SESSION)
        state = await self.get_state()
        self.assertIsNone(state.current_action)
        self.assertEqual(state.sleep_history[-1]["hours"],7)
        self.assertFalse(state.sleep_history[-1]["interrupted"])
        self.assertEqual(state.sleep_rhythm["decision"],"awake")
        await self.assess()
        self.assertEqual(len(self.llm.calls),1)

    async def test_fixed_mode_restores_existing_automatic_sleep_schedule(self):
        raw = self.store.raw_world()
        raw["sleep"]["mode"] = "fixed"
        self.store.save_world(raw)
        self.engine.reload_config()
        self.clock.set_struct(datetime(2026,10,19,23,30))
        await self.engine.run_schedules()
        self.assertEqual((await self.get_state()).current_action["type"],"walk_to")


class SleepConfigTests(unittest.TestCase):
    def test_default_and_old_configs_use_autonomy_with_fixed_opt_out(self):
        raw = default_world()
        raw["sleep"].pop("mode")
        world, warnings = parse_world(raw)
        self.assertFalse(warnings)
        self.assertEqual(world.sleep.mode, "autonomous")
        self.assertTrue(world.sleep_allowed_now(21))
        world.sleep.mode = "fixed"
        self.assertFalse(world.sleep_allowed_now(21))
