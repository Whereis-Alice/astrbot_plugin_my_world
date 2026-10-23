"""Character ownership and executable appointments across recovery modes."""
import copy
import unittest
from datetime import datetime
from hashlib import sha256
from unittest.mock import AsyncMock

from core import life_matters as matters
from core.models import ActionDef, ChainStep, ScheduleDef
from core.state import WorldState
from tests import test_life_audit as audit

S = audit.S


class CharacterLifeTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = audit.LifeAuditTests.asyncSetUp
    idle = audit.LifeAuditTests.idle

    async def project(self, *, planned=True, persona_key="another_character"):
        await self.idle()
        async with self.engine.session_state(S) as state:
            item = self.engine.add_life_matter(state, "旧角色的私人阅读项目", persona_key=persona_key)
            if planned:
                item["steps"] = [{"id": "ready", "text": "接着旧角色的书读", "action": "read",
                                  "node": "study", "minutes": 10, "status": "pending"}]
            return copy.deepcopy(item)

    async def test_other_character_ready_step_is_not_started_or_mutated(self):
        original = await self.project()
        await self.engine.advance_life_matters(S)
        state = await self.engine.load_state(S)
        self.assertIsNone(state.current_action)
        self.assertIsNone(state.current_plan)
        self.assertEqual(matters.find(state, original["id"]), original)

    async def test_other_character_goal_never_reaches_planning_model(self):
        await self.project(planned=False)
        judge = self.engine._ask_judge = AsyncMock(return_value='{"steps":[]}')
        await self.engine.advance_life_matters(S)
        judge.assert_not_awaited()

    async def test_other_character_project_does_not_block_new_idea(self):
        await self.project()
        self.engine.world.limits.life_matter_self_start = True
        judge = self.engine._ask_judge = AsyncMock(return_value='{"text":"新角色自己的园艺计划"}')
        await self.engine.seed_life_matter(S)
        judge.assert_awaited_once()
        self.assertNotIn("旧角色的私人阅读项目", judge.call_args.args[2])
        state = await self.engine.load_state(S)
        self.assertEqual(len(state.life_matters), 2)

    async def test_foreign_projects_do_not_consume_current_character_capacity(self):
        await self.idle()
        async with self.engine.session_state(S) as state:
            for i in range(self.engine.world.limits.life_matter_active_max):
                self.assertIsNotNone(self.engine.add_life_matter(state, f"旧角色的事项{i}", persona_key="old"))
            key = sha256(self.persona.text.encode()).hexdigest()[:24]
            self.assertIsNotNone(self.engine.add_life_matter(state, "当前角色自己的事项", persona_key=key))

    async def test_foreign_matter_schedule_does_not_reserve_current_life(self):
        original = await self.project()
        self.clock.set_struct(datetime(2026, 10, 19, 21, 30))
        self.engine.schedules.schedules = [ScheduleDef(id="old_appointment", time="21:35", matter_id=original["id"])]
        async with self.engine.session_state(S) as state:
            self.assertEqual(self.engine.life_choice(state)["kind"], "idle")

    async def test_foreign_stage_is_not_sent_to_current_model(self):
        original = await self.project()
        judge = self.engine._ask_judge = AsyncMock(return_value='{"outcome":"continue"}')
        await self.engine._review_matter_stage(S, original, world=self.engine.world,
            state_key=S, persona=self.persona.text)
        judge.assert_not_awaited()

    async def test_switching_character_during_work_preserves_receipts_and_stops_old_work(self):
        key = sha256(self.persona.text.encode()).hexdigest()[:24]
        original = await self.project(persona_key=key)
        await self.engine.advance_life_matters(S)
        async with self.engine.session_state(S) as state:
            state.current_action["elapsed_ticks"] = 2
            self.engine.settle_matter_work(state, state.current_action)
            before = copy.deepcopy(matters.find(state, original["id"]))
        self.persona.text = "现在切换成完全不同的角色。"
        await self.engine._tick_session(S)
        state = await self.engine.load_state(S)
        self.assertNotEqual((state.current_action or {}).get("matter_id"), original["id"])
        self.assertNotEqual((state.current_plan or {}).get("matter_id"), original["id"])
        item = matters.find(state, original["id"])
        self.assertEqual(item["worked_minutes"], before["worked_minutes"])
        self.assertEqual(item["steps"][0]["worked_minutes"], before["steps"][0]["worked_minutes"])
        self.assertEqual(item["history"][:len(before["history"])], before["history"])

    async def test_foreign_appointment_cannot_interrupt_current_life(self):
        original = await self.project()
        async with self.engine.session_state(S) as state:
            current = {"type": "read", "duration_ticks": 30, "elapsed_ticks": 2}
            state.current_action = copy.deepcopy(current)
            schedule = ScheduleDef(id="old_project", matter_id=original["id"])
            self.assertFalse(await self.engine.run_matter_appointment(state, schedule, session=S, force=True))
            self.assertEqual(state.current_action, current)
            self.assertEqual(matters.find(state, original["id"]), original)

    async def test_legacy_unowned_project_and_returning_character_can_continue(self):
        original = await self.project(persona_key="")
        await self.engine.advance_life_matters(S)
        state = await self.engine.load_state(S)
        self.assertEqual(state.current_action["matter_id"], original["id"])
        await self.idle()
        self.persona.text = "另一位角色。"
        other_key = sha256(self.persona.text.encode()).hexdigest()[:24]
        async with self.engine.session_state(S) as state:
            state.life_matters = []
        original = await self.project(persona_key=other_key)
        await self.engine.advance_life_matters(S)
        state = await self.engine.load_state(S)
        self.assertEqual(state.current_action["matter_id"], original["id"])


class ExecutableScheduleTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = audit.LifeAuditTests.asyncSetUp
    idle = audit.LifeAuditTests.idle

    async def setup_schedule(self, kinds, *, mode="activity"):
        await self.idle()
        self.engine.world.sleep.mode = mode
        self.clock.set_struct(datetime(2026, 10, 19, 21, 30))
        self.engine.world.actions.append(ActionDef(id="custom_sleep", name="休眠",
            recovery_kind="sleep", category="continuous", duration=600))
        self.engine.world.actions.append(ActionDef(id="charge", name="充电",
            recovery_kind="restore", category="continuous", duration=600))
        schedule = ScheduleDef(id="old_rest", time="21:35", action_chain=[ChainStep(type=k) for k in kinds])
        self.engine.schedules.schedules = [schedule,
            ScheduleDef(id="reading", time="22:00", action_chain=[ChainStep(type="read")])]
        return schedule

    async def test_activity_mode_sleep_only_does_not_reserve_free_time(self):
        for kind in ("sleep", "nap", "custom_sleep"):
            with self.subTest(kind=kind):
                await self.setup_schedule([kind])
                self.assertEqual(self.engine._next_schedule(S)["id"], "reading")
                state = WorldState(S, node_id="study", energy=.9)
                self.assertEqual(self.engine.life_choice(state)["kind"], "idle")

    async def test_activity_mode_mixed_chain_keeps_valid_activity(self):
        await self.setup_schedule(["sleep", "read"])
        self.assertEqual(self.engine._next_schedule(S)["id"], "old_rest")
        self.assertEqual(self.engine._next_schedule(S)["actions"], self.engine.world.action_map()["read"].name)

    async def test_mixed_chain_gate_uses_only_executable_steps(self):
        schedule = await self.setup_schedule(["sleep", "read"])
        self.assertEqual(self.engine._schedule_gate_kind(schedule), "")
        self.engine.world.sleep.mode = "fixed"
        self.assertEqual(self.engine._schedule_gate_kind(schedule), "heavy")

    async def test_charging_and_fixed_sleep_remain_real_appointments(self):
        for kind, mode in (("charge", "activity"), ("sleep", "fixed"), ("nap", "autonomous")):
            with self.subTest(kind=kind, mode=mode):
                await self.setup_schedule([kind], mode=mode)
                self.assertEqual(self.engine._next_schedule(S)["id"], "old_rest")

    async def test_activity_sleep_only_schedule_does_not_interrupt_live_action(self):
        await self.setup_schedule(["sleep"])
        async with self.engine.session_state(S) as state:
            current = {"type": "read", "duration_ticks": 30, "elapsed_ticks": 2}
            state.current_action = copy.deepcopy(current)
        self.clock.set_struct(datetime(2026, 10, 19, 21, 35))
        self.engine._schedule_reset_pending = True
        await self.engine.run_schedules()
        state = await self.engine.load_state(S)
        self.assertEqual(state.current_action, current)
        self.assertIsNone(state.current_plan)
        self.assertEqual(state.life_resume, {})

    async def test_mixed_schedule_runs_valid_steps_without_sleep(self):
        await self.setup_schedule(["sleep", "read"])
        self.clock.set_struct(datetime(2026, 10, 19, 21, 35))
        self.engine._schedule_reset_pending = True
        await self.engine.run_schedules()
        state = await self.engine.load_state(S)
        queued = (state.current_plan or {}).get("steps", [])
        self.assertEqual((state.current_action or {}).get("type"), "read")
        self.assertNotIn("sleep", [row["action"] for row in queued])

    async def test_manual_sleep_only_does_not_interrupt_valid_work(self):
        await self.setup_schedule(["sleep"])
        async with self.engine.session_state(S) as state:
            current = {"type": "read", "duration_ticks": 30, "elapsed_ticks": 2}
            state.current_action = copy.deepcopy(current)
        result = await self.engine.run_schedule_now(S, "old_rest", force=True)
        self.assertFalse(result["ok"])
        state = await self.engine.load_state(S)
        self.assertEqual(state.current_action, current)
