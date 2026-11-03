"""A personal instant activity cannot spend its preceding contact's speech permission."""
import json
import unittest
from unittest.mock import AsyncMock

from core.engine import TickOutcome
from core.json_actions import PlannedAction
from core.models import ActionDef
from core.planner import create_plan
from tests import test_participation as participation

GROUP = participation.GROUP


class InstantActivityProvenanceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = participation.ParticipationTests.asyncSetUp
    chat = participation.ParticipationTests.chat

    def activity(self):
        definition = ActionDef(id="read_plant_guide", name="翻植物图鉴", category="instant",
            target_type="none", llm_level="single", visible=True,
            description="翻开图鉴，找一种想在阳台养的小植物。",
            on_complete={"effects": {"energy": "+0.05"}})
        self.engine.world.actions.append(definition)
        self.engine._ask_llm = AsyncMock(return_value=json.dumps({
            "intent": "life_share", "reasoning": {"inner": "这盆植物适合阳台。"},
            "actions": [{"type": "say", "messages": ["图鉴里这种小植物很适合阳台。"]}]},
            ensure_ascii=False))
        return definition

    async def run_activity(self, kind="outreach"):
        outcome = TickOutcome(GROUP, speech_kind=kind, contact_people={"alice"})
        async with self.engine.session_state(GROUP) as state:
            state.energy = .6
            await self.engine._execute_actions(state, self.engine.node(state.node_id), outcome=outcome,
                actions=[PlannedAction(type="read_plant_guide")], depth=0, autonomous=True)
            self.assertAlmostEqual(state.energy, .65)
        await self.engine._deliver(outcome)
        return outcome

    async def test_contact_then_personal_instant_does_not_generate_another_contact(self):
        self.activity()
        self.engine.world.life_share_session_whitelist = []
        await self.chat("［图片］", raw_text="", has_images=True, message_id="cat-picture")
        outcome = TickOutcome(GROUP, speech_kind="outreach")
        async with self.engine.session_state(GROUP) as state:
            state.energy = .6
            plan = create_plan(steps=[
                {"action": "say", "messages": ["剑拿得比猫还高", "站得还挺认真"]},
                {"action": "read_plant_guide"}], world_time=state.world_time, now=self.clock.now(),
                tick_seconds=self.engine.tick_seconds, source="llm")
            plan["contact_people"] = ["alice"]
            await self.engine._apply_plan(state, self.engine.node(state.node_id), outcome, plan)
            await self.engine._tick_plan(state, self.engine.node(state.node_id), outcome, depth=0)
            self.assertAlmostEqual(state.energy, .65)
        await self.engine._deliver(outcome)
        self.engine._ask_llm.assert_not_awaited()
        self.assertEqual(self.messenger.sent, [(GROUP, ["剑拿得比猫还高", "站得还挺认真"])])
        self.assertEqual(self.engine.contact_status(GROUP)["left"], 5)
        events = self.db.raw._query("SELECT detail FROM event_log WHERE session_id=? AND event_type='action'",
                                    (GROUP,))
        activity = next(detail for row in events if (detail := json.loads(row["detail"])).get("type")
                        == "read_plant_guide")
        self.assertEqual(activity["messages"], [])
        self.assertTrue(activity["diary_life"])

    async def test_saved_instant_contact_step_uses_current_life_share_permission(self):
        self.activity()
        self.engine.world.life_share_session_whitelist = []
        outcome = TickOutcome(GROUP, speech_kind="outreach")
        async with self.engine.session_state(GROUP) as state:
            state.current_plan = create_plan(steps=[{"action": "read_plant_guide", "kind": "outreach"}],
                world_time=state.world_time, now=self.clock.now(), tick_seconds=self.engine.tick_seconds,
                source="llm")
            await self.engine._tick_plan(state, self.engine.node(state.node_id), outcome, depth=0)
        await self.engine._deliver(outcome)
        self.engine._ask_llm.assert_not_awaited()
        self.assertEqual(self.messenger.sent, [])

    async def test_unplanned_personal_instant_does_not_borrow_contact_permission(self):
        self.activity()
        self.engine.world.life_share_session_whitelist = []
        await self.run_activity()
        self.engine._ask_llm.assert_not_awaited()
        self.assertEqual(self.messenger.sent, [])

    async def test_authorized_instant_shares_the_activity_without_contact_invitation(self):
        self.activity()
        outcome = await self.run_activity()
        self.engine._ask_llm.assert_awaited_once()
        prompt = self.engine._ask_llm.call_args.args[2]
        self.assertIn("翻植物图鉴", prompt)
        self.assertNotIn("这次明确想找的人", prompt)
        self.assertNotIn("outreach 有具体缘由地找人", prompt)
        self.assertEqual(self.messenger.sent, [(GROUP, ["图鉴里这种小植物很适合阳台。"])])
        self.assertEqual(outcome.speech_kinds[""], ["life_share"])
        self.assertEqual(self.engine.contact_status(GROUP)["left"], 6)
        self.assertEqual(self.engine.life_share_status(GROUP)["left"], 5)

    async def test_native_activity_keeps_its_reply_permission_without_life_sharing(self):
        self.activity()
        self.engine.world.life_share_session_whitelist = []
        outcome = await self.run_activity("reply")
        self.engine._ask_llm.assert_awaited_once()
        self.assertEqual(len(self.messenger.sent), 1)
        self.assertEqual(outcome.speech_kinds[""], ["reply"])

    async def test_schedule_activity_keeps_its_authorized_schedule_permission(self):
        self.activity()
        outcome = await self.run_activity("schedule")
        self.engine._ask_llm.assert_awaited_once()
        self.assertEqual(len(self.messenger.sent), 1)
        self.assertEqual(outcome.speech_kinds[""], ["schedule"])

    async def test_person_directed_instant_retains_explicit_contact_permission(self):
        definition = self.activity()
        definition.target_type = "user"
        self.engine.world.life_share_session_whitelist = []
        self.engine._ask_llm.return_value = json.dumps({
            "intent": "outreach", "reasoning": {"inner": "把这页递给她看。"},
            "actions": [{"type": "say", "messages": ["这一页给你看。"]}]}, ensure_ascii=False)
        outcome = TickOutcome(GROUP, speech_kind="outreach")
        async with self.engine.session_state(GROUP) as state:
            await self.engine._execute_actions(state, self.engine.node(state.node_id), outcome=outcome,
                actions=[PlannedAction(type=definition.id, target="alice")], depth=0, autonomous=True)
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.sent, [(GROUP, ["这一页给你看。"])])
        self.assertEqual(self.engine.contact_status(GROUP)["left"], 5)

