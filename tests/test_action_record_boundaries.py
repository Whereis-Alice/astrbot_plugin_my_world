"""Completed private life and inner actions retain their own record boundaries."""
import json
import unittest
from unittest.mock import AsyncMock

from core.engine import TickOutcome
from core.json_actions import PlannedAction
from tests import test_engine as fixture


class ActionRecordBoundaryTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    def configure_action(self, *, action_id="quiet_posture", **fields):
        raw = self.store.raw_world()
        definition = next((row for row in raw["actions"] if row["id"] == action_id), None)
        if definition is None:
            definition = {"id": action_id, "name": "调整靠椅姿势", "category": "instant",
                          "scope": "global", "target_type": "none", "llm_level": "single"}
            raw["actions"].append(definition)
        definition.update(fields)
        self.store.save_world(raw)
        self.engine.reload_config()
        return action_id

    async def execute(self, action_id, **fields):
        outcome = TickOutcome(fixture.SESSION, speech_kind="life_share")
        async with self.engine.session_state(fixture.SESSION) as state:
            await self.engine._execute_actions(state, self.engine.node(state.node_id), outcome=outcome,
                actions=[PlannedAction(type=action_id, **fields)], depth=0, autonomous=True)
        await self.engine._deliver(outcome)
        return outcome

    async def diary_sources(self):
        day = self.engine.local_now().date().isoformat()
        start, _ = self.engine._journal_day_bounds(day)
        self.db.raw._execute("UPDATE event_log SET created_at=?", (start.timestamp() + 60,))
        return await self.engine._diary_sources(fixture.SESSION, day)

    async def test_single_personal_action_keeps_diary_evidence_when_model_listens(self):
        action_id = self.configure_action(visible=True)
        self.llm.replies = [json.dumps({"intent": "listen", "reasoning": {
            "inner": "椅背刚好能撑住，安静坐一会儿。", "intent": "现在不想打扰别人"}, "actions": []})]
        await self.execute(action_id)
        self.assertEqual([], self.messenger.flat_messages)
        sources = await self.diary_sources()
        events = [row for row in sources["events"] if row["type"] == "action"]
        self.assertEqual(1, len(events))
        self.assertIn("调整靠椅姿势", events[0]["detail"])
        self.assertIn("completed", events[0]["detail"])
        self.assertNotIn("椅背刚好", json.dumps(sources, ensure_ascii=False))
        self.clock.advance(86400)
        self.engine.world.diary.automatic = True
        self.engine._ask_journal = AsyncMock(return_value=json.dumps({
            "title": "安静坐了一会儿", "content": "调整了椅背，坐着休息了一会儿。"}))
        saved = await self.engine.ensure_diary(fixture.SESSION, sources["day"], automatic=True)
        self.assertIsNotNone(saved)
        self.assertEqual(1, self.engine._ask_journal.await_count)

    async def test_single_personal_action_records_label_without_dialogue_or_parameters(self):
        action_id = self.configure_action(visible=True)
        await self.execute(action_id, content="聊天里的私密台词", params={"secret": "秘密参数"})
        self.assertEqual(["聊天里的私密台词"], self.messenger.flat_messages)
        encoded = json.dumps((await self.diary_sources())["events"], ensure_ascii=False)
        self.assertIn("调整靠椅姿势", encoded)
        self.assertNotIn("私密", encoded)
        self.assertNotIn("秘密参数", encoded)

    async def test_single_person_directed_action_remains_outside_diary(self):
        action_id = self.configure_action(name="给某人递纸条", target_type="user", visible=True)
        await self.execute(action_id, content="给他的私密台词", target="1002")
        self.assertEqual([], (await self.diary_sources())["events"])

    async def test_visible_inner_action_still_records_the_private_thought(self):
        self.configure_action(action_id="think", visible=True)
        await self.execute("think", content="只想自己记住的念头")
        self.assertEqual([], self.messenger.flat_messages)
        thoughts = (await self.engine.thoughts_payload(fixture.SESSION))["thoughts"]
        self.assertTrue(any(row["content"] == "只想自己记住的念头" for row in thoughts))

    async def test_inner_action_template_cannot_publish_its_private_content(self):
        self.configure_action(action_id="think", visible=True, template="{content}")
        await self.execute("think", content="只想自己记住的念头")
        self.assertEqual([], self.messenger.flat_messages)
        self.assertEqual([], (await self.diary_sources())["events"])
        thoughts = (await self.engine.thoughts_payload(fixture.SESSION))["thoughts"]
        self.assertTrue(any(row["content"] == "只想自己记住的念头" for row in thoughts))
