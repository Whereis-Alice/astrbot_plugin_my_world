"""Cross-module regressions reproduced by the 2026-10-23 life review."""
import copy
import json
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.engine.life_inference import LIFE_WORK, LifeWorkDiscarded, released_life_lock
from core.models import ActionDef
from core.recovery import recovery_gain
from core import life_matters as matters
from core.ports import LLMReply
from tests import test_engine as fixture
from tests import test_recovery as recovery

S = fixture.SESSION


class LifeRecoveryReviewTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = recovery.RecoveryTests.asyncSetUp
    set_state = recovery.RecoveryTests.set_state
    get_state = recovery.RecoveryTests.get_state
    start = recovery.RecoveryTests.start
    outcome = recovery.RecoveryTests.outcome

    async def test_net_consumption_cannot_become_recovery_at_low_energy(self):
        self.engine.world.state_dynamics.daily_mood_enabled = False
        self.engine.world.state_dynamics.energy_decay_per_min = .005
        self.action.duration_mode, self.action.duration = "fixed", 600
        self.action.on_complete.effects_per_minute = {"energy": "+0.003"}
        async with self.engine.session_state(S) as state:
            state.energy = .02
            gain = recovery_gain(self.action, state.energy, self.engine.world, 600, state)
            selected = self.engine.decider.recovery_plan(state)
            await self.start(state)
            for _ in range(10):
                self.clock.advance(60)
                self.engine.dynamics.tick(state, node=self.engine.node(state.node_id), elapsed_seconds=60,
                                          world=self.engine.world, now=self.clock.now())
                await self.engine._tick_continuous(state, self.engine.node(state.node_id), self.outcome())
            self.assertLess(state.energy, .02, "real execution confirms net consumption")
            self.assertLessEqual(gain, 0)
            self.assertIsNone(selected)

    async def test_exhausting_hobby_yields_to_available_emergency_charge(self):
        self.engine.world.state_dynamics.daily_mood_enabled = False
        self.engine.world.state_dynamics.energy_decay_per_min = .005
        hobby = ActionDef(id="long_hobby", name="看书", category="continuous", duration=10800,
            recovery_kind="none", on_complete={"effects_per_minute": {"energy": "+0.001"}})
        self.engine.world.actions.append(hobby)
        async with self.engine.session_state(S) as state:
            state.energy = .04
            await self.start(state, hobby, seconds=10800)
            self.assertIsNotNone(self.engine.decider.recovery_plan(state))
            await self.engine.tick_activity_recovery(state, self.outcome())
            self.assertEqual(state.current_action["type"], "charge")

    async def test_completion_bonus_recovery_is_not_interrupted(self):
        self.engine.world.state_dynamics.energy_decay_per_min = .005
        self.action.on_complete.effects_per_minute = {}
        self.action.on_complete.effects = {"energy": "=0.8"}
        async with self.engine.session_state(S) as state:
            state.energy = .04
            await self.start(state)
            await self.engine.tick_activity_recovery(state, self.outcome())
            self.assertEqual(state.current_action["type"], "charge")
            self.assertEqual(state.current_action["elapsed_ticks"], 0)

    async def test_normal_charging_progress_merges_without_discard_or_double_reward(self):
        async with self.engine.session_state(S) as state:
            state.energy, state.valence_offset = .42, .123
            await self.start(state, seconds=3600)
            self.engine.dynamics.apply_effects(state, {}, world=self.engine.world, now=self.clock.now())
            state.life_updated_at = self.clock.now()
        token = LIFE_WORK.set(True)
        try:
            async with self.engine.session_state(S) as state:
                try:
                    async with released_life_lock(self.engine):
                        self.clock.advance(60)
                        await self.engine._tick_while_thinking(S)
                except LifeWorkDiscarded:
                    self.fail("a normal charging minute must not discard inference")
                self.assertEqual(state.current_action["effects_settled_minutes"], 1)
                self.assertAlmostEqual(state.energy, .43)
                self.engine.settle_recovery_progress(state, state.current_action)
                self.assertAlmostEqual(state.energy, .43, "merged accounting must not pay the same minute twice")
        finally:
            LIFE_WORK.reset(token)

    async def test_offline_completion_precedes_remaining_awake_interval(self):
        self.engine.world.state_dynamics.daily_mood_enabled = False
        self.engine.world.state_dynamics.energy_decay_per_min = .005
        self.action.on_complete.effects_per_minute = {}
        self.action.on_complete.effects = {"energy": "+0.5"}
        async with self.engine.session_state(S) as state:
            await self.start(state)
            state.life_updated_at = self.clock.now()
        self.clock.advance(7200)
        initial = copy.deepcopy(await self.get_state())
        # Both paths use the actual character lease; an unowned state copy may
        # not complete a marked action or receive its recovery reward.
        async with self.engine.session_state(S) as control:
            awake_seconds = self.engine.reconcile_life_time(control)
            await self.engine._tick_continuous(control, self.engine.node(control.node_id), self.outcome())
            self.engine.dynamics.tick(control, node=self.engine.node(control.node_id), elapsed_seconds=awake_seconds,
                                      world=self.engine.world, now=self.clock.now())
            expected_energy = control.energy
        await self.engine.save_state(initial)
        await self.engine._tick_session(S)
        result = await self.get_state()
        self.assertAlmostEqual(result.energy, expected_energy)
        self.assertIsNone(result.current_action)
        energy = result.energy
        await self.engine._tick_continuous(result, self.engine.node(result.node_id), self.outcome())
        self.assertAlmostEqual(result.energy, energy)

    async def test_activity_schedule_rejects_builtin_and_custom_sleep(self):
        self.engine.world.actions.append(ActionDef(id="custom_sleep", name="长眠", recovery_kind="sleep",
                                                   category="continuous", duration=600))
        for action_id in ("sleep", "nap", "custom_sleep"):
            with self.subTest(action=action_id):
                self.engine._background_generate = AsyncMock(return_value=LLMReply(ok=True, text=json.dumps({"schedules": [{
                    "id": "incompatible_rest", "time": "23:00", "action_chain": [{"type": action_id}]}]})))
                result = await self.engine.generate_schedules(1)
                request = self.engine._background_generate.call_args.kwargs
                self.assertEqual(result["schedules"], [])
                self.assertTrue(result["problems"])
                self.assertNotIn(f"- {action_id}｜", request["prompt"])
                self.assertIn("你不需要睡眠", request["system_prompt"])

    async def test_reply_followup_prepares_images_without_reentering_proactive_tools(self):
        calls = []
        @asynccontextmanager
        async def prepare(target, rows, *, images=None):
            calls.append((target, list(rows), images))
            yield {"text": "图片说明", "images": ["prepared.png"]}
        self.engine.media = SimpleNamespace(context=prepare)
        self.engine._ask_llm = AsyncMock(return_value='{"actions":[]}')
        async with self.engine.session_state(S) as state:
            outcome = self.outcome()
            outcome.speech_kind = "reply"
            await self.engine._llm_followup(state, self.engine.node(state.node_id), outcome,
                "工具完成", "图片结果", image_urls=["original.gif"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.engine._ask_llm.call_args.kwargs["image_urls"], ["prepared.png"])
        self.assertIn("图片说明", self.engine._ask_llm.call_args.args[2])


class LifeProgressNotesTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = recovery.RecoveryTests.asyncSetUp
    set_state = recovery.RecoveryTests.set_state
    get_state = recovery.RecoveryTests.get_state

    async def seed(self):
        self.engine.schedules.schedules = []
        self.engine.world.limits.life_matter_notes_per_day = 2
        async with self.engine.session_state(S) as state:
            state.state, state.energy = "idle", .9
            state.current_action = state.current_plan = None
            item = self.engine.add_life_matter(state, "写一个关于风车的故事")
            item.update(status="waiting", resume_at=self.clock.now() + 86400, next_step="明天接着写",
                        steps=[{"id":"opening", "text":"写开头", "status":"done", "worked_minutes":20,
                                "result":{"ok":True,"kind":"life_simulation","minutes":20,"result":"世界内写了开头"}},
                               {"id":"next", "text":"明天接着写", "status":"pending", "action":"read",
                                "node":"study", "minutes":20, "not_before":self.clock.now()+86400}])
            self.matter_id = item["id"]
            before = copy.deepcopy(item)
        self.engine._ask_judge = AsyncMock(return_value=json.dumps({"outcome":"completed", "inner":"那封信还没写署名，明天想想是谁留下的。",
            "stage":{"summary":"留下了故事开头", "content":"风车停转后，塔底出现一封没有署名的信；写信人尚未确定。",
                     "next_focus":"想一想写信人的动机", "basis_step_ids":["opening"]}}))
        return before

    async def test_note_keeps_tomorrows_plan_and_records_real_content_once(self):
        before = await self.seed()
        await self.engine.advance_life_matters(S)
        state = await self.get_state()
        item = matters.find(state, self.matter_id)
        self.assertEqual(len(item.get("stage_notes", [])), 1)
        self.assertIn("没有署名的信", item["stage_notes"][0]["content"])
        for key in ("status", "resume_at", "next_step"):
            self.assertEqual(item[key], before[key])
        self.assertEqual(item["steps"][1], before["steps"][1])
        self.assertFalse(item.get("needs_replan"))
        self.assertTrue(any(t.get("source") == "生活小结" for t in state.thoughts))
        self.assertIsNone(state.current_action)
        self.clock.advance(3601)
        await self.engine.advance_life_matters(S)
        self.assertEqual(self.engine._ask_judge.await_count, 1)

    async def test_disabled_notes_do_not_make_a_request(self):
        await self.seed()
        self.engine.world.limits.life_matter_notes_per_day = 0
        await self.engine.advance_life_matters(S)
        self.engine._ask_judge.assert_not_awaited()

    async def test_identical_stage_artifact_is_not_injected_twice(self):
        await self.seed()
        await self.engine.advance_life_matters(S)
        state = await self.get_state()
        item = matters.find(state, self.matter_id)
        content = item["stage_notes"][0]["content"]
        block = matters.context_block(state, session_id=S, now=self.clock.now())
        self.assertEqual(block.count(content), 1)
        state.life_artifacts[item["id"]]["content"] = "额外保留的另一个世界内草稿"
        block = matters.context_block(state, session_id=S, now=self.clock.now())
        self.assertIn(content, block)
        self.assertIn("额外保留的另一个世界内草稿", block)
        item["status"] = "completed"
        self.assertIn("额外保留的另一个世界内草稿", matters.context_block(state, session_id=S))

    async def test_failed_note_is_bounded_and_does_not_block_other_work(self):
        await self.seed()
        self.engine._ask_judge.return_value = "invalid"
        self.engine.world.limits.life_matter_notes_per_day = 1
        await self.engine.advance_life_matters(S)
        self.clock.advance(7200)
        await self.engine.advance_life_matters(S)
        self.assertEqual(self.engine._ask_judge.await_count, 1)
        item = matters.find(await self.get_state(), self.matter_id)
        self.assertFalse(item.get("stage_notes"))
        self.assertEqual(item["status"], "waiting")

    async def test_edited_project_rejects_pending_note(self):
        await self.seed()
        output = self.engine._ask_judge.return_value
        async def edit(*args):
            async with self.engine.session_state(S) as state:
                item = matters.find(state, self.matter_id)
                item["status"] = "paused"
                matters.history(item, "paused", "用户暂停", self.clock.now())
            return output
        self.engine._ask_judge.side_effect = edit
        await self.engine.advance_life_matters(S)
        item = matters.find(await self.get_state(), self.matter_id)
        self.assertEqual(item["status"], "paused")
        self.assertFalse(item.get("stage_notes"))

    async def test_changed_receipt_cannot_support_a_pending_note(self):
        await self.seed()
        output = self.engine._ask_judge.return_value
        async def replace_receipt(*args):
            async with self.engine.session_state(S) as state:
                item = matters.find(state, self.matter_id)
                item["steps"][0]["result"]["result"] = "已更正的活动记录"
            return output
        self.engine._ask_judge.side_effect = replace_receipt
        await self.engine.advance_life_matters(S)
        item = matters.find(await self.get_state(), self.matter_id)
        self.assertFalse(item.get("stage_notes"))
        self.assertFalse(item["steps"][0].get("stage_noted"))

    async def test_note_exception_does_not_prevent_ready_work(self):
        await self.seed()
        async with self.engine.session_state(S) as state:
            ready = self.engine.add_life_matter(state, "继续读昨天的书")
            ready["steps"] = [{"id":"read_next", "text":"接着读", "status":"pending",
                "action":"read", "node":"study", "minutes":20, "not_before":0}]
            ready_id = ready["id"]
        self.engine._ask_judge.side_effect = RuntimeError("model unavailable")
        await self.engine.advance_life_matters(S)
        state = await self.get_state()
        self.assertEqual((state.current_action or {}).get("matter_id"), ready_id)
        self.assertFalse(matters.find(state, self.matter_id).get("stage_notes"))

    async def test_daily_note_allowance_is_shared_between_projects(self):
        await self.seed()
        async with self.engine.session_state(S) as state:
            original = matters.find(state, self.matter_id)
            for index in range(2):
                other = copy.deepcopy(original)
                other["id"] = f"another_{index}"
                state.life_matters.append(other)
        for _ in range(3):
            await self.engine.advance_life_matters(S)
        state = await self.get_state()
        self.assertEqual(self.engine._ask_judge.await_count, 2)
        self.assertEqual(sum(len(item.get("stage_notes", [])) for item in state.life_matters), 2)
        self.assertEqual(sum(item.get("note_review", {}).get("attempts", 0) for item in state.life_matters), 2)

    async def test_other_persona_and_private_session_do_not_supply_notes(self):
        await self.seed()
        async with self.engine.session_state(S) as state:
            item = matters.find(state, self.matter_id)
            item["persona_key"] = "different_persona"
        await self.engine.advance_life_matters(S)
        self.engine._ask_judge.assert_not_awaited()
        async with self.engine.session_state(S) as state:
            item = matters.find(state, self.matter_id)
            item.update(persona_key="", who="someone", session="another_private_chat")
        await self.engine.advance_life_matters(S)
        self.engine._ask_judge.assert_not_awaited()

    async def test_concurrent_maintenance_cannot_duplicate_a_note(self):
        await self.seed()
        output = self.engine._ask_judge.return_value
        async def other_scan(*args):
            await self.engine.advance_life_matters(S)
            return output
        self.engine._ask_judge.side_effect = other_scan
        await self.engine.advance_life_matters(S)
        self.assertEqual(self.engine._ask_judge.await_count, 1)
        self.assertEqual(len(matters.find(await self.get_state(), self.matter_id)["stage_notes"]), 1)

    async def test_unperformed_steps_and_promises_cannot_create_notes(self):
        await self.seed()
        async with self.engine.session_state(S) as state:
            item = matters.find(state, self.matter_id)
            item["steps"][0]["result"]["minutes"] = 0
        await self.engine.advance_life_matters(S)
        self.engine._ask_judge.assert_not_awaited()
        async with self.engine.session_state(S) as state:
            item = matters.find(state, self.matter_id)
            item["steps"][0]["result"]["minutes"] = 20
            item["kind"] = "promise"
        await self.engine.advance_life_matters(S)
        self.engine._ask_judge.assert_not_awaited()
