"""Execution evidence keeps present activity distinct from dialogue and future plans."""
import unittest
from types import SimpleNamespace

from core.action_facts import action_facts
from core.engine import TickOutcome
from core.models import ActionDef
from core.state import WorldState
from tests import test_engine as fixture


class ActionFactTimelineTests(unittest.TestCase):
    def setUp(self):
        definitions = {
            "charge": ActionDef(id="charge", name="充电", llm_level="single"),
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
