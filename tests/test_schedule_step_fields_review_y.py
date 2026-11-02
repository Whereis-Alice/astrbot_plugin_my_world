"""Natural-language schedules keep their specific task through saving and travel."""
from __future__ import annotations

import json
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from core.engine import MyWorldEngine, TickOutcome
from core.json_actions import PlannedAction
from core.models import ChainStep
from tests import test_engine as fixture

DETAILS = {"intent": "只查京都明天的天气", "queries": ["京都 明日 天気"],
           "search_depth": "quick", "read_pages": 0}


class ScheduleStepFieldsTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    async def rebuild(self):
        await self.engine.close_background()
        self.engine = MyWorldEngine(store=self.store, db=self.db, llm=self.llm,
            messenger=self.messenger, tools=self.tools, persona=self.persona,
            clock=self.clock, tick_seconds=60, decider_interval=0)
        self.addAsyncCleanup(self.engine.close_background)

    def assert_details(self, action, expected=DETAILS):
        actual = {key: action[key] if isinstance(action, dict) else getattr(action, key)
                  for key in expected}
        self.assertEqual(actual, expected)

    async def test_natural_language_add_keeps_specific_task_after_save_and_rebuild(self):
        payload = {"id": "kyoto_weather", "time": "12:00",
                   "action_chain": [{"type": "search_web", **DETAILS}]}
        self.llm.replies = [json.dumps(payload, ensure_ascii=False)]
        async with self.engine.session_state(fixture.SESSION) as state:
            with patch.object(self.engine, "_llm_followup", AsyncMock()):
                await self.engine._run_schedule_action(state, self.engine.node(state.node_id),
                    TickOutcome(fixture.SESSION), self.engine.world.action_map()["schedule_add"],
                    PlannedAction(type="schedule_add", intent="明天中午只快速查京都天气，不读网页"))
        self.assertEqual(len(self.llm.calls), 1)
        self.assertIn("她想加一条日程", self.llm.calls[0]["prompt"])
        saved = next(item for item in self.store.raw_schedules()["schedules"] if item["id"] == payload["id"])
        self.assert_details(saved["action_chain"][0])
        await self.rebuild()
        schedule = next(item for item in self.engine.schedules.schedules if item.id == payload["id"])
        self.assert_details(schedule.action_chain[0])
        self.assertFalse(self.messenger.sent)

    async def test_explicit_zero_pages_survives_even_without_other_search_fields(self):
        ok, note = await self.engine.schedule_add({"id": "no_pages", "time": "12:00",
            "action_chain": [{"type": "search_web", "read_pages": 0}]}, session_id=fixture.SESSION)
        self.assertTrue(ok, note)
        await self.rebuild()
        step = next(item for item in self.engine.schedules.schedules if item.id == "no_pages").action_chain[0]
        self.assertEqual(step.read_pages, 0)
        self.assertEqual(step.queries, [])
        self.assertEqual(step.intent, "")

    async def test_legacy_missing_fields_use_the_same_defaults(self):
        ok, note = await self.engine.schedule_add({"id": "legacy", "time": "12:00",
            "action_chain": [{"type": "search_web"}]}, session_id=fixture.SESSION)
        self.assertTrue(ok, note)
        await self.rebuild()
        step = next(item for item in self.engine.schedules.schedules if item.id == "legacy").action_chain[0]
        self.assert_details(step, dict(intent="", queries=[], search_depth="", read_pages=-1))

    async def test_only_declared_step_fields_are_saved(self):
        ok, note = await self.engine.schedule_add({"id": "declared", "time": "12:00",
            "action_chain": [{"type": "search_web", **DETAILS,
                              "plan_id": "foreign-plan", "elapsed_ticks": 999,
                              "send_to": "unselected-session"}]}, session_id=fixture.SESSION)
        self.assertTrue(ok, note)
        step = next(item for item in self.store.raw_schedules()["schedules"] if item["id"] == "declared")["action_chain"][0]
        self.assertEqual(set(step), {"type", *DETAILS})

    async def test_malformed_declared_step_is_rejected_without_saving(self):
        original = self.store.raw_schedules()
        ok, note = await self.engine.schedule_add({"id": "broken", "time": "12:00",
            "action_chain": [{"type": "search_web", "read_pages": "many"}]}, session_id=fixture.SESSION)
        self.assertFalse(ok)
        self.assertIn("参数格式不正确", note)
        self.assertEqual(self.store.raw_schedules(), original)

    async def run_chain(self, chain):
        async with self.engine.session_state(fixture.SESSION) as state:
            state.state, state.current_action, state.current_plan = "idle", None, None
            state.energy, state.sleep_pressure = .9, 0
            return await self.engine._run_chain(state, chain, TickOutcome(fixture.SESSION), depth=0)

    async def test_continuous_activity_preserves_all_tail_fields_through_state_reload(self):
        status = await self.run_chain([ChainStep(type="read", duration=300), ChainStep(type="search_web", **DETAILS)])
        self.assertEqual(status, "running")
        await self.rebuild()
        state = await self.engine.load_state(fixture.SESSION, cold_start=False)
        self.assertEqual(state.current_action["type"], "read")
        self.assert_details(state.current_plan["steps"][0])
        action = self.engine._step_to_action(state.current_plan["steps"][0])
        self.assert_details(action)
        self.assertFalse(self.llm.calls)
        self.assertFalse(self.messenger.sent)

    async def test_legacy_tail_keeps_defaults_after_continuous_activity(self):
        self.assertEqual(await self.run_chain([ChainStep(type="read", duration=300),
            ChainStep(type="search_web")]), "running")
        state = await self.engine.load_state(fixture.SESSION, cold_start=False)
        self.assert_details(state.current_plan["steps"][0],
                            dict(intent="", queries=[], search_depth="", read_pages=-1))

    async def test_auto_travel_retains_the_task_until_actual_arrival(self):
        self.clock.set_struct(datetime(2026, 11, 3, 11, 59))
        raw = self.store.raw_schedules()
        raw["schedules"] = [{"id": "travel_search", "time": "12:00", "auto_travel": True,
                             "action_chain": [{"type": "search_web", **DETAILS}]}]
        self.store.save_schedules(raw)
        self.engine.reload_config()
        async with self.engine.session_state(fixture.SESSION) as state:
            state.node_id, state.state, state.energy = "bedroom", "idle", .9
            state.current_action, state.current_plan = None, None
            state.sleep_pressure, state.cold_start_done = 0, True
        self.assertEqual(await self.engine.run_schedules(), [])
        self.clock.set_struct(datetime(2026, 11, 3, 12, 0))
        rows = await self.engine.run_schedules()
        self.assertEqual(len(rows), 1)
        state = await self.engine.load_state(fixture.SESSION, cold_start=False)
        self.assertEqual(state.current_action["type"], "walk_to")
        self.assertEqual(state.current_action["target_node"], "study")
        self.assert_details(state.current_plan["steps"][0])
        ticks = state.current_action["duration_ticks"]
        captured = []

        async def execute(state, node, outcome, actions, **kwargs):
            captured.extend((state.node_id, action) for action in actions)
            return len(actions)

        with patch.object(self.engine, "_execute_actions", side_effect=execute):
            for _ in range(ticks):
                self.clock.advance(60)
                async with self.engine.session_state(fixture.SESSION) as state:
                    state.world_time += 1
                    outcome = TickOutcome(fixture.SESSION)
                    await self.engine._tick_continuous(state, self.engine.node(state.node_id), outcome)
                    await self.engine._tick_plan(state, self.engine.node(state.node_id), outcome, depth=0)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][0], "study")
        self.assertEqual(captured[0][1].type, "search_web")
        self.assert_details(captured[0][1])
        self.assertFalse(self.llm.calls)
        self.assertFalse(self.messenger.sent)
