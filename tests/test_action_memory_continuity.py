"""Executed personal activities remain recallable after short event context expires."""
import json
import time
import unittest

from tests import test_action_record_boundaries as records
from tests import test_engine as fixture


class ActionMemoryContinuityTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    configure_action = records.ActionRecordBoundaryTests.configure_action
    execute = records.ActionRecordBoundaryTests.execute

    async def asyncSetUp(self):
        await fixture.EngineTestCase.asyncSetUp(self)
        # Runtime memory timestamps and requests must use the same calendar.
        self.clock.advance(time.time() - self.clock.now())

    async def memories(self):
        rows = await self.db.call("query_memories", session_id=fixture.SESSION, limit=100)
        # New worlds also load map presets; these assertions concern execution.
        return [row for row in rows if row["source"] == "runtime"]

    async def assert_instant_recall(self, level):
        label = "整理桌上的书签"
        action_id = self.configure_action(name=label, llm_level=level, template="")
        self.llm.replies = [json.dumps({"intent": "listen", "actions": []})]
        await self.execute(action_id)
        self.assertEqual([], self.messenger.flat_messages)
        # The bounded event window really evicts the executed activity.
        async with self.engine.session_state(fixture.SESSION) as state:
            for _ in range(41):
                self.engine._note_event(state, "bot_spoke", {})
            self.assertFalse(any(row["kind"] == "action" for row in state.recent_events))
        self.engine.reload_config()
        self.llm.replies = [json.dumps({"intent": "listen", "actions": []})]
        await self.execute("recall", intent=label)
        events = await self.db.call("query_events", session_id=fixture.SESSION, limit=20)
        recalled = next(row["detail"] for row in events if row["event_type"] == "recall_done")
        self.assertEqual(1, recalled["count"])
        self.assertIn(label, recalled["detail"])
        self.assertIn(label, self.llm.calls[-1]["prompt"])
        request = self.engine.context_request(state, fixture.SESSION, text=label)
        await self.engine.prepare_context_memory(request)
        self.assertTrue(any(label in row.content for row in self.engine.context_memories(state, request)))

    async def test_template_activity_survives_event_eviction_and_explicit_recall(self):
        await self.assert_instant_recall("template")

    async def test_listening_single_activity_survives_event_eviction_and_explicit_recall(self):
        await self.assert_instant_recall("single")

    async def start_and_rename(self):
        action_id = self.configure_action(name="整理书签", category="continuous", llm_level="template", template="")
        await self.execute(action_id, duration=60)
        self.configure_action(name="浇花")
        return action_id

    async def test_continuous_completion_remembers_name_from_execution(self):
        await self.start_and_rename()
        async with self.engine.session_state(fixture.SESSION) as state:
            action = dict(state.current_action)
            action["elapsed_ticks"] = action["duration_ticks"]
            await self.engine._finish_action(state, self.engine.node(state.node_id),
                fixture.TickOutcome(fixture.SESSION), action)
        rows = await self.memories()
        self.assertTrue(any(row["content"] == "在这里整理书签" for row in rows), rows)
        self.assertFalse(any("浇花" in row["content"] for row in rows))
        events = await self.db.call("query_events", session_id=fixture.SESSION, limit=10)
        done = next(row for row in events if row["event_type"] == "action_done")
        self.assertEqual("整理书签", done["detail"]["desc"])

    async def test_recent_start_keeps_execution_name_after_config_rename(self):
        await self.start_and_rename()
        prompt = await self.engine.preview_autonomous_prompt(fixture.SESSION)
        self.assertTrue("- 开始：整理书签" in prompt)
        self.assertFalse("- 开始：浇花" in prompt)

    async def test_recent_instant_activity_keeps_execution_name_after_config_rename(self):
        action_id = self.configure_action(name="整理书签", llm_level="template", template="")
        await self.execute(action_id)
        self.configure_action(name="浇花")
        prompt = await self.engine.preview_autonomous_prompt(fixture.SESSION)
        self.assertTrue("- 做了：整理书签" in prompt)
        self.assertFalse("- 做了：浇花" in prompt)

    async def test_legacy_action_without_name_snapshot_keeps_existing_fallback(self):
        await self.start_and_rename()
        async with self.engine.session_state(fixture.SESSION) as state:
            action = dict(state.current_action)
            action.pop("action_name", None)
            action["desc"] = "旧存档中不能证明动作名称的私人内容"
            await self.engine._finish_action(state, self.engine.node(state.node_id),
                fixture.TickOutcome(fixture.SESSION), action)
        rows = await self.memories()
        self.assertEqual(["在这里浇花"], [row["content"] for row in rows])
        self.assertEqual("- 做了：浇花", self.engine.prompts._event_line(
            {"kind": "action", "detail": {"type": "quiet_posture"}}))

    async def test_movement_remembers_action_name_without_ongoing_description(self):
        label = self.engine.world.action_map()["walk_to"].name
        await self.execute("walk_to", target_node="window", intent="私人的出行动机")
        async with self.engine.session_state(fixture.SESSION) as state:
            action = dict(state.current_action)
            self.assertTrue(action["desc"].startswith("正在走去"))
            await self.engine._finish_action(state, self.engine.node(state.node_id),
                fixture.TickOutcome(fixture.SESSION), action)
        rows = await self.memories()
        self.assertEqual([f"在这里{label}"], [row["content"] for row in rows])
        self.assertEqual("window", rows[0]["node_id"])
        self.assertNotIn("私人", json.dumps(rows, ensure_ascii=False))

    async def test_personal_memory_uses_label_and_existing_persona_scope(self):
        action_id = self.configure_action(name="整理书签", llm_level="single")
        await self.execute(action_id, content="聊天私密台词", params={"secret": "秘密参数"})
        rows = await self.memories()
        self.assertEqual(1, len(rows))
        self.assertEqual("在这里整理书签", rows[0]["content"])
        self.assertEqual("study", rows[0]["node_id"])
        self.assertNotIn("私密", json.dumps(rows, ensure_ascii=False))
        self.assertNotIn("秘密参数", json.dumps(rows, ensure_ascii=False))
        original_persona = json.loads(rows[0]["context"])["persona_key"]
        self.persona.text = "另一个生活角色"
        state = await self.engine.load_state(fixture.SESSION, cold_start=False)
        request = self.engine.context_request(state, fixture.SESSION, text="整理书签")
        await self.engine.prepare_context_memory(request)
        self.assertNotEqual(original_persona, request.life_persona)
        self.assertEqual([], self.engine.context_memories(state, request))

    async def test_dialogue_and_person_directed_actions_do_not_become_personal_scenes(self):
        for index, fields in enumerate(({"target_type": "user"}, {"target": "1002"},
                                       {"interject": True})):
            with self.subTest(fields=fields):
                definition = {key: value for key, value in fields.items() if key == "target_type"}
                action = {key: value for key, value in fields.items() if key != "target_type"}
                action_id = self.configure_action(action_id=f"private_{index}", name="私人纸条",
                                                 llm_level="template", template="", **definition)
                await self.execute(action_id, content="不应成为公共生活", **action)
        await self.execute("say", content="正在聊天")
        await self.execute("think", content="只在心里想")
        rows = await self.memories()
        self.assertFalse(any(row["type"] == "scene" for row in rows), rows)
        self.assertEqual(1, sum(row["type"] == "inner" for row in rows))
