"""Incoming QQ messages distinguish a saved home sleep from current visiting life."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from tests import fake_astrbot

fake_astrbot.install()

from core.models import ActionDef  # noqa: E402 - install the SDK fixture before importing the engine
from core.planner import create_plan  # noqa: E402
from tests import test_engine as fixture  # noqa: E402
from tests import test_visits_actor as actors  # noqa: E402
from tests.test_web_routes import load_plugin_module  # noqa: E402


class VisitHomeSleepMessageTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = actors.VisitActorTests.asyncSetUp
    ctx = fixture.EngineTestCase.ctx

    async def home_nap(self):
        self.engine.world.sleep.applies_to_nap = True
        self.engine.world.sleep.wake_words = ["醒醒"]
        self.engine.world.sleep.clear_plan_on_wake = True
        self.engine.world.sleep.reply_mode = "silent"
        self.engine.world.actions.append(ActionDef(
            id="saved_home_nap", name="家中的小睡", category="continuous",
            recovery_kind="nap", enabled=True,
        ))
        async with self.engine.session_state(fixture.SESSION) as state:
            state.state, state.cold_start_done = "napping", True
            state.current_action = {"type": "saved_home_nap", "duration_ticks": 20,
                "elapsed_ticks": 2, "persona_key": self.local["persona_key"]}
            state.current_plan = create_plan(steps=[{"action": "read"}],
                now=self.clock.now(), world_time=state.world_time, valid_for=3600,
                persona_key=self.local["persona_key"])
            return state.current_plan["id"]

    async def assert_home_kept(self, plan_id):
        state = await self.engine.load_state(fixture.SESSION, cold_start=False)
        self.assertEqual("napping", state.state)
        self.assertEqual("saved_home_nap", state.current_action["type"])
        self.assertEqual(2, state.current_action["elapsed_ticks"])
        self.assertEqual(plan_id, state.current_plan["id"])
        self.assertFalse(self.llm.calls)
        self.assertEqual([], self.messenger.sent)
        return state

    async def test_wake_word_during_visit_keeps_home_sleep_and_real_message(self):
        self.active = self.visit
        plan_id = await self.home_nap()
        ctx = self.ctx(text="醒醒，你在朋友那里干嘛呢？", is_mentioned=True)
        self.assertIsNone(await self.engine.sleep_gate(ctx))
        await self.engine.handle_incoming(ctx)
        plan = await self.engine.handle_reply(ctx, inject_only=True)
        self.assertTrue(plan.ok)
        self.assertIn("港湾", plan.runtime_part)
        self.assertIn("一起下棋", plan.runtime_part)
        self.assertNotIn("你刚被叫醒", plan.runtime_part)
        state = await self.assert_home_kept(plan_id)
        self.assertTrue(any(row["text"] == ctx.text for row in state.recent_chat))
        events = await self.db.call("query_events", session_id=fixture.SESSION, event_type="wake_up")
        self.assertEqual([], events)

    async def test_paused_home_sleep_does_not_apply_delay_or_old_home_sleep_hints(self):
        self.active = self.visit
        plan_id = await self.home_nap()
        self.engine.world.sleep.delay_words = ["等会儿再睡"]
        async with self.engine.session_state(fixture.SESSION) as state:
            state.wake_note = "你刚被叫醒，这是家中那段睡眠的旧提示"
            state.wake_note_until = state.world_time + 10
            state.startled_note = "家中半睡半醒的旧提示"
            state.startled_until = state.world_time + 10
        ctx = self.ctx(text="等会儿再睡，你们刚才在玩什么？", is_mentioned=True)
        await self.engine.handle_incoming(ctx)
        plan = await self.engine.handle_reply(ctx, inject_only=True)
        self.assertTrue(plan.ok)
        self.assertIn("一起下棋", plan.runtime_part)
        self.assertNotIn("家中那段睡眠的旧提示", plan.runtime_part)
        self.assertNotIn("家中半睡半醒的旧提示", plan.runtime_part)
        state = await self.assert_home_kept(plan_id)
        self.assertEqual("你刚被叫醒，这是家中那段睡眠的旧提示", state.wake_note)
        self.assertFalse(state.sleep_delayed)

    async def test_inactive_unarrived_or_foreign_visit_keeps_normal_home_wake(self):
        for visit in (None, {**self.visit, "status": "offered", "arrived_at": 0},
                      {**self.visit, "status": "completed"},
                      {**self.visit, "local": {**self.local, "persona_key": "previous-character"}},
                      {**self.visit, "scope": fixture.OTHER_SESSION}):
            with self.subTest(visit=visit and (visit["status"], visit["scope"], visit["local"]["persona_key"])):
                self.active = visit
                await self.home_nap()
                ctx = self.ctx(text="醒醒", is_mentioned=True)
                await self.engine.handle_incoming(ctx)
                state = await self.engine.load_state(fixture.SESSION, cold_start=False)
                self.assertEqual("idle", state.state)
                self.assertIsNone(state.current_action)
                self.assertIsNone(state.current_plan)
                self.assertIn("你刚被叫醒", state.wake_note)
                self.assertFalse(self.llm.calls)
                self.assertEqual([], self.messenger.sent)

    async def test_finishing_visit_still_keeps_its_paused_home_sleep(self):
        self.active = {**self.visit, "status": "finishing"}
        plan_id = await self.home_nap()
        await self.engine.handle_incoming(self.ctx(text="醒醒", is_mentioned=True))
        await self.assert_home_kept(plan_id)

    async def social_snapshot(self):
        # Exercise the real public method without constructing a host or
        # starting background tasks; its engine and all ports are the fixture.
        method = load_plugin_module().MyWorldPlugin.social_snapshot
        return await method(SimpleNamespace(enabled=True, engine=self.engine), fixture.SESSION)

    async def test_social_snapshot_reports_current_waking_visit_and_keeps_home_record(self):
        self.active = self.visit
        plan_id = await self.home_nap()
        snapshot = await self.social_snapshot()
        self.assertIsNotNone(snapshot)
        self.assertFalse(snapshot["sleeping"])
        self.assertEqual("napping", snapshot["state"])
        await self.assert_home_kept(plan_id)

    async def test_social_snapshot_keeps_real_home_sleep_for_invalid_or_finished_visits(self):
        for visit in (None, {**self.visit, "status": "offered", "arrived_at": 0},
                      {**self.visit, "status": "completed"},
                      {**self.visit, "local": {**self.local, "persona_key": "previous-character"}}):
            with self.subTest(visit=visit and visit["status"]):
                self.active = visit
                plan_id = await self.home_nap()
                snapshot = await self.social_snapshot()
                self.assertIsNotNone(snapshot)
                self.assertTrue(snapshot["sleeping"])
                await self.assert_home_kept(plan_id)
