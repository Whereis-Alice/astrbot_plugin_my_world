"""Execution evidence keeps present activity distinct from dialogue and future plans."""
import unittest
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.action_facts import action_facts, state_execution_view
from core.engine import TickOutcome
from core.models import ActionDef
from core.json_actions import PlannedAction
from core.planner import create_plan
from core.state import WorldState
from tests import test_engine as fixture


class ActionFactTimelineTests(unittest.TestCase):
    def setUp(self):
        definitions = {
            "charge": ActionDef(id="charge", name="充电", llm_level="single", category="continuous"),
            "read": ActionDef(id="read", name="翻图鉴", llm_level="single"),
            "say": ActionDef(id="say", name="说话", llm_level="single"),
            "tool": ActionDef(id="tool", name="下载资料", llm_level="tool"),
        }
        self.world = SimpleNamespace(action_map=lambda: definitions)

    def render(self, state):
        return action_facts(state, self.world, persona_key="isla", now=600)

    def test_new_charge_is_pending_after_previous_charge_finished(self):
        state = WorldState("s", recent_events=[{
            "kind": "action_done", "detail": {"type": "charge"}, "at": 300,
        }], current_plan={"steps": [{"action": "say"}, {"action": "charge"}], "current_step": 0})
        text = self.render(state)
        self.assertIn("约 5 分钟前完成：充电", text)
        self.assertIn("尚未开始的安排：充电", text)
        self.assertNotIn("正在进行", text)

    def test_running_step_does_not_appear_as_pending_again(self):
        state = WorldState("s", current_action={"type": "charge"}, current_plan={
            "steps": [{"action": "charge"}, {"action": "read"}], "current_step": 0,
        })
        text = self.render(state)
        self.assertIn("正在进行，尚未结束：充电", text)
        self.assertIn("尚未开始的安排：翻图鉴", text)
        self.assertNotIn("尚未开始的安排：充电", text)

    def test_speech_and_tool_clock_cannot_establish_physical_state_or_success(self):
        state = WorldState("s", recent_events=[
            {"kind": "action", "detail": {"type": "say", "action_name": "我闭眼了"}},
            {"kind": "action_done", "detail": {"type": "tool", "action_name": "下载成功"}},
        ])
        self.assertEqual(self.render(state), "")

    def test_start_record_alone_does_not_count_as_completed(self):
        state = WorldState("s", recent_events=[{
            "kind": "action_start", "detail": {"type": "charge"},
        }])
        self.assertEqual(self.render(state), "")

    def test_foreign_character_records_and_plans_are_hidden(self):
        state = WorldState("s", current_action={"type": "charge", "persona_key": "alice"},
            current_plan={"persona_key": "alice", "steps": [{"action": "read"}]},
            recent_events=[{"kind": "action_done", "persona_key": "alice", "detail": {"type": "read"}}])
        self.assertEqual(self.render(state), "")

    def test_old_records_are_not_renamed_as_just_completed(self):
        state = WorldState("s", recent_events=[{
            "kind": "action_done", "detail": {"type": "charge"}, "world_time": 7,
        }])
        self.assertIn("较早的执行记录完成：充电", self.render(state))

    def test_non_finite_or_malformed_event_times_remain_undated(self):
        for at in ("bad", {}, float("nan"), float("inf"), -3, 700):
            with self.subTest(at=at):
                state = WorldState("s", recent_events=[{
                    "kind": "action_done", "detail": {"type": "charge"}, "at": at,
                }])
                self.assertIn("较早的执行记录完成：充电", self.render(state))

    def test_missing_now_never_converts_non_finite_time_to_age(self):
        state = WorldState("s", recent_events=[{
            "kind": "action_done", "detail": {"type": "charge"}, "at": 300,
        }])
        for now in (None, "bad", float("nan"), float("inf")):
            with self.subTest(now=now):
                self.assertIn("较早的执行记录", action_facts(state, self.world, now=now))

    def test_invalid_pointers_and_step_containers_do_not_replay_a_plan_tail(self):
        for current in (-1, "bad", 2, .5, float("inf"), True):
            with self.subTest(current=current):
                state = WorldState("s", current_plan={"steps": [{"action": "charge"}], "current_step": current})
                self.assertEqual(self.render(state), "")
        for steps in ("bad", {}, 3):
            with self.subTest(steps=steps):
                self.assertEqual(self.render(WorldState("s", current_plan={"steps": steps})), "")

    def test_expired_future_and_bad_deadline_plans_are_not_current_arrangements(self):
        for fields in ({"expires_at": 500}, {"expires_at": "bad"}, {"expires_at": float("inf")},
                       {"created_ts": 700}, {"valid_until": 9}, {"status": "paused"}):
            with self.subTest(fields=fields):
                state = WorldState("s", world_time=10, current_plan={"steps": [{"action": "charge"}], **fields})
                self.assertEqual(self.render(state), "")

    def test_skipped_deferred_and_pending_records_are_not_completion_evidence(self):
        for status in ("skipped", "deferred", "pending", "running", "cancelled", ["bad"]):
            with self.subTest(status=status):
                state = WorldState("s", recent_events=[{
                    "kind": "action_done", "detail": {"type": "charge", "status": status},
                }], current_plan={"steps": [{"action": "charge", "status": "deferred"}]})
                self.assertEqual(self.render(state), "")

    def test_ambiguous_continuous_record_without_completion_status_is_hidden(self):
        state = WorldState("s", recent_events=[{"kind": "action", "detail": {"type": "charge"}}])
        self.assertEqual(self.render(state), "")
        state.recent_events[0]["detail"]["status"] = "completed"
        self.assertIn("完成：充电", self.render(state))

    def test_bad_fields_are_sanitised_without_editing_original_records(self):
        state = WorldState("s", current_action={"type": "charge", "duration_ticks": "bad"},
            current_plan={"steps": [{"action": "read", "status": "deferred"}]},
            recent_events=[None, ["bad"], {"kind": "action_done", "detail": "bad", "at": "bad"}],
            idle_activity={"until_tick": "bad"})
        view = state_execution_view(state, self.world, now=600)
        self.assertIsNone(view.current_action)
        self.assertEqual(view.current_plan["steps"], [{}])
        self.assertEqual(view.idle_activity, {})
        self.assertEqual(state.current_action["duration_ticks"], "bad")
        self.assertEqual(state.current_plan["steps"][0]["status"], "deferred")
        self.assertEqual(state.recent_events[2]["at"], "bad")

    def test_unfiltered_legacy_path_keeps_marked_persona_evidence(self):
        state = WorldState("s", current_action={"type": "charge", "persona_key": "any_character"})
        self.assertIn("正在进行", action_facts(state, self.world, persona_key=None, now=600))

    def test_instant_execution_replaces_only_its_own_pending_step_in_display(self):
        state = WorldState("s", current_plan={"id": "plan", "steps": [{"action": "read"}, {"action": "read"}]})
        instant = {"type": "read", "from_plan": True, "plan_id": "plan", "plan_index": 0}
        text = action_facts(state, self.world, instant_action=instant, now=600)
        self.assertIn("本轮正在做，结果尚未确认：翻图鉴", text)
        self.assertEqual(text.count("翻图鉴"), 2, "the second identical future activity remains pending")
        self.assertIn("尚未开始的安排：翻图鉴", text)
        self.assertEqual(state.current_plan["steps"][0], {"action": "read"})

    def test_unrelated_instant_execution_does_not_hide_pending_plan(self):
        state = WorldState("s", current_plan={"id": "plan", "steps": [{"action": "read"}]})
        for fields in ({"from_plan": False}, {"from_plan": True, "plan_id": "other", "plan_index": 0},
                       {"from_plan": True, "plan_id": "plan", "plan_index": 1}):
            with self.subTest(fields=fields):
                text = action_facts(state, self.world, instant_action={"type": "read", **fields})
                self.assertIn("尚未开始的安排：翻图鉴", text)

    def test_instant_execution_preserves_long_running_activity(self):
        state = WorldState("s", current_action={"type": "charge"})
        text = action_facts(state, self.world, instant_action={"type": "read"})
        self.assertIn("正在进行，尚未结束：充电", text)
        self.assertIn("本轮正在做，结果尚未确认：翻图鉴", text)
        self.assertNotIn("完成：", text)

    def test_malformed_or_foreign_instant_evidence_does_not_create_execution_fact(self):
        state = WorldState("s")
        for instant in (None, [], {"type": "tool"}, {"type": "charge"}, {"type": "say"},
                        {"type": "read", "persona_key": "other"}):
            with self.subTest(instant=instant):
                self.assertEqual(action_facts(state, self.world, persona_key="isla", instant_action=instant), "")

    def test_zero_duration_continuous_definition_can_have_explicit_instant_execution(self):
        text = action_facts(WorldState("s"), self.world,
            instant_action={"type": "charge", "execution_kind": "instant"})
        self.assertIn("本轮正在做，结果尚未确认：充电", text)
        self.assertNotIn("完成：", text)


class ActionFactCompletionTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    async def test_completed_activity_remains_available_after_current_action_is_cleared(self):
        state = await self.engine.load_state(fixture.SESSION, cold_start=False)
        state.state, state.energy = "idle", .9
        definition = self.engine.world.action_map()["read"]
        definition.on_complete.trigger = "none"
        action = {"type": "read", "duration_ticks": 10, "elapsed_ticks": 10,
                  "session": fixture.SESSION, "from_plan": False}
        state.current_action = action
        await self.engine._finish_action(state, self.engine.node(state.node_id),
                                        TickOutcome(fixture.SESSION), action)
        self.assertIsNone(state.current_action)
        record = next(event for event in reversed(state.recent_events) if event["kind"] == "action_done")
        self.assertEqual(record["detail"]["type"], "read")
        self.assertEqual(record["at"], self.clock.now())
        text = action_facts(state, self.engine.world, persona_key=record["persona_key"], now=self.clock.now())
        self.assertIn("完成：" + definition.name, text)
        self.assertNotIn("正在进行", text)
        self.assertEqual(self.llm.calls, [])

    async def test_runtime_context_tolerates_old_invalid_execution_fields(self):
        state = await self.engine.load_state(fixture.SESSION, cold_start=False)
        state.current_action = ["bad"]
        state.current_plan = {"steps": [None, {"action": "read"}], "current_step": "bad"}
        state.last_plan = {"steps": "bad", "at": "bad"}
        state.recent_events = [None, ["bad"], {"kind": "action", "detail": ["bad"], "created_at": "bad"}]
        state.idle_activity = {"until_tick": "bad"}
        request = self.engine.context_request(state, fixture.SESSION, task="reply", text="在做什么？", rows=[])
        text = self.engine.prompts.runtime_layer(state, node=self.engine.node(state.node_id),
                                                 context_request=request, current_session=fixture.SESSION)
        self.assertIn("你的状态", text)
        self.assertNotIn("尚未开始的安排", text)
        self.assertEqual(state.current_action, ["bad"])
        self.assertEqual(state.current_plan["current_step"], "bad")
        self.assertEqual(self.llm.calls, [])

    async def instant_context(self):
        definition = ActionDef(id="flip_book", name="翻开图鉴", category="instant", llm_level="single", visible=True)
        self.engine.world.actions.append(definition)
        state = await self.engine.load_state(fixture.SESSION, cold_start=False)
        state.state, state.energy = "idle", .9
        state.current_plan = create_plan(steps=[{"action": "flip_book"}], world_time=state.world_time,
                                          now=self.clock.now())
        state.current_action = {"type": "read", "duration_ticks": 20, "elapsed_ticks": 4}
        return state, definition

    async def test_single_generator_receives_original_state_and_transient_execution_evidence(self):
        state, definition = await self.instant_context()
        original_action, original_plan = state.current_action, copy.deepcopy(state.current_plan)
        self.llm.replies = [json.dumps({"reasoning": {"inner": "这片叶子的纹路不同"},
            "actions": [{"type": "say", "messages": ["这页的叶子尖一点。"]}]}, ensure_ascii=False)]
        original_generate = self.engine._generate_text_actions
        passed = []

        async def capture(received, *args, **kwargs):
            passed.append(received)
            self.assertIs(received, state)
            self.assertEqual(state.current_plan, original_plan)
            self.assertFalse(any(row.get("kind") == "action_done" for row in state.recent_events))
            return await original_generate(received, *args, **kwargs)

        self.engine._generate_text_actions = capture
        result = await self.engine._instant_output(state, self.engine.node(state.node_id), definition,
            PlannedAction(type=definition.id), session_id=fixture.SESSION, from_plan=True)
        self.assertEqual(result, ["这页的叶子尖一点。"])
        self.assertEqual(passed, [state])
        self.assertIs(state.current_action, original_action)
        self.assertEqual(state.current_plan, original_plan)
        self.assertGreater(state.llm_text_count_hour, 0)
        self.assertTrue(any(row.get("content") == "这片叶子的纹路不同" for row in state.thoughts))
        prompt = self.llm.calls[-1]["system_prompt"]
        self.assertIn("本轮正在做，结果尚未确认：翻开图鉴", prompt)
        self.assertNotIn("尚未开始的安排：翻开图鉴", prompt)
        self.assertNotIn("接下来排队（**一件都还没做**）：翻开图鉴", prompt)
        self.assertFalse(any(row.get("kind") == "action_done" for row in state.recent_events))

    async def test_single_generation_failure_does_not_persist_transient_execution_as_completion(self):
        state, definition = await self.instant_context()
        original_plan = copy.deepcopy(state.current_plan)
        self.engine._ask_llm = AsyncMock(side_effect=RuntimeError("synthetic generation failure"))
        with self.assertRaises(RuntimeError):
            await self.engine._instant_output(state, self.engine.node(state.node_id), definition,
                PlannedAction(type=definition.id), session_id=fixture.SESSION, from_plan=True)
        self.assertEqual(state.current_plan, original_plan)
        self.assertFalse(any(row.get("kind") in {"action", "action_done"} for row in state.recent_events))
        self.assertFalse(hasattr(state, "instant_action"))

    async def test_real_start_path_passes_plan_identity_then_records_actual_completion(self):
        state, definition = await self.instant_context()
        self.llm.replies = [json.dumps({"actions": [{"type": "say", "messages": ["找到这一页了。"]}]})]
        original_output = self.engine._instant_output
        passed = []

        async def capture(received, *args, **kwargs):
            self.assertIs(received, state)
            passed.append(kwargs.get("from_plan"))
            self.assertFalse(any(row.get("kind") == "action" and row.get("detail", {}).get("type") == definition.id
                                 for row in state.recent_events))
            return await original_output(received, *args, **kwargs)

        self.engine._instant_output = capture
        await self.engine._start_action(state, self.engine.node(state.node_id),
            TickOutcome(fixture.SESSION, speech_kind="reply"), definition,
            PlannedAction(type=definition.id), 0, True, from_plan=True)
        self.assertEqual(passed, [True])
        self.assertIn("本轮正在做，结果尚未确认：翻开图鉴", self.llm.calls[-1]["system_prompt"])
        self.assertTrue(any(row.get("kind") == "action" and row.get("detail", {}).get("type") == definition.id
                            for row in state.recent_events))
