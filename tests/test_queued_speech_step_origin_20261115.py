"""Mixed queues retain the actual step's reply, chat and schedule permissions."""
import unittest
from unittest.mock import Mock, patch

from core.engine import TickOutcome
from core.json_actions import PlannedAction
from core.models import ActionDef
from core.planner import create_plan
from tests import test_participation as participation
from tests import test_recovery as recovery
from tests.test_engine import SESSION


class QueuedSpeechStepOriginTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = participation.ParticipationTests.asyncSetUp
    chat = participation.ParticipationTests.chat

    async def queued_step(self, kind, *, parent="schedule", action=None, scene=None):
        outcome = TickOutcome(participation.GROUP)
        action = action or PlannedAction(type="say", messages=["这句保留原来的来路。"])
        if scene:
            action.raw["speech_scene"] = scene
        async with self.engine.session_state(participation.GROUP) as state:
            state.current_action = None
            state.current_plan = create_plan(
                steps=[{"action": "think", "content": "安静继续。", "kind": "quiet"}],
                world_time=state.world_time, now=self.clock.now(), source=parent)
            self.engine._queue_steps(state, outcome, [action], [], kind=kind, note="")
            await self.engine._tick_plan(state, self.engine.node(state.node_id), outcome, depth=0)
        await self.engine._deliver(outcome)
        return outcome

    async def test_reply_inserted_into_schedule_sends_with_proactive_permission_denied(self):
        with patch.object(self.engine, "speech_allowed", side_effect=lambda sid, kind="": kind == "reply"):
            await self.queued_step("reply")
        self.assertEqual(self.messenger.flat_messages, ["这句保留原来的来路。"])
        state = await self.engine.load_state(participation.GROUP, cold_start=False)
        self.assertEqual(state.recent_chat[-1]["speech_kind"], "reply")

    async def test_schedule_parent_cannot_skip_stored_continuation_cadence(self):
        row = await self.chat(message_id="current-source")
        self.engine.participation_sample = Mock(return_value=False)
        outcome = await self.queued_step("continuation", scene={"target": participation.GROUP,
            "rows": [row], "window": 900, "reply_to": "current-source"})
        self.assertEqual(self.engine.participation_sample.call_count, 1)
        self.assertFalse(self.messenger.sent)
        self.assertEqual(outcome.speech_decisions[0]["status"], "not_sent")

    async def test_admitted_continuation_is_recorded_as_chat_in_schedule_queue(self):
        row = await self.chat(message_id="current-source")
        self.engine.participation_sample = Mock(return_value=True)
        await self.queued_step("continuation", scene={"target": participation.GROUP,
            "rows": [row], "window": 900, "reply_to": "current-source"})
        self.assertEqual(self.engine.participation_sample.call_count, 1)
        self.assertEqual(self.messenger.flat_messages, ["这句保留原来的来路。"])
        state = await self.engine.load_state(participation.GROUP, cold_start=False)
        self.assertEqual(state.recent_chat[-1]["speech_kind"], "continuation")

    async def test_explicit_quiet_step_in_schedule_queue_remains_quiet(self):
        await self.queued_step("quiet")
        self.assertFalse(self.messenger.sent)
        self.assertFalse(self.llm.calls)

    async def test_outreach_in_schedule_queue_keeps_its_person_quota(self):
        definition = ActionDef(id="contact_probe", category="instant", target_type="user",
            llm_level="template", template="想和你说句话。")
        self.engine.world.actions.append(definition)
        self.engine.contact_status = Mock(return_value={"reason": "今天的联系额度已用完", "left": 0})
        with patch.object(self.engine, "_start_action", wraps=self.engine._start_action) as start:
            await self.queued_step("outreach", action=PlannedAction(type=definition.id, target="alice"))
        self.assertFalse(any(call.args[3].id == definition.id for call in start.call_args_list))
        self.assertFalse(self.messenger.sent)

    async def test_schedule_step_keeps_schedule_permissions_in_non_schedule_queue(self):
        definition = ActionDef(id="appointment_probe", category="instant", target_type="user",
            llm_level="template", template="约好的这件事到时间了。")
        self.engine.world.actions.append(definition)
        self.engine.contact_status = Mock(return_value={"reason": "今天的联系额度已用完", "left": 0})
        await self.queued_step("schedule", parent="pending",
            action=PlannedAction(type=definition.id, target="alice"))
        self.assertEqual(self.messenger.flat_messages, ["约好的这件事到时间了。"])
        state = await self.engine.load_state(participation.GROUP, cold_start=False)
        self.assertEqual(state.recent_chat[-1]["speech_kind"], "schedule")


class RecoveryQueuedReplyOriginTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = recovery.RecoveryTests.asyncSetUp
    set_state = recovery.RecoveryTests.set_state
    get_state = recovery.RecoveryTests.get_state

    async def test_reply_queued_after_system_recovery_keeps_its_explicit_origin(self):
        outcome = TickOutcome(SESSION)
        async with self.engine.session_state(SESSION) as state:
            await self.engine.tick_activity_recovery(state, outcome)
            self.assertEqual(state.current_plan["source"], "recovery")
            self.engine._queue_steps(state, outcome,
                [PlannedAction(type="say", messages=["刚才问的那件事，我记住了。"])], [],
                kind="reply", note="")
            self.assertEqual(state.current_action["speech_kind"], "quiet")
            self.assertEqual(state.current_plan["steps"][-1]["kind"], "reply")
            state.current_action["duration_ticks"] = 1
            await self.engine._tick_continuous(state, self.engine.node("study"), outcome)
            with patch.object(self.engine, "speech_allowed", side_effect=lambda sid, kind="": kind == "reply"):
                await self.engine._tick_plan(state, self.engine.node("study"), outcome, depth=0)
                await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.flat_messages, ["刚才问的那件事，我记住了。"])
        self.assertFalse(self.llm.calls)
        state = await self.get_state()
        self.assertEqual(state.recent_chat[-1]["speech_kind"], "reply")


if __name__ == "__main__":
    unittest.main()
