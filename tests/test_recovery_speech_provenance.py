"""Automatic recovery keeps its real clock without granting completion narration."""
import unittest

from core.engine import TickOutcome
from core.json_actions import PlannedAction
from core.models import ActionDef
from core.planner import create_plan
from core.recovery import system_recovery_action
from core.speech_decision import SpeechDecision
from core.speech_policy import COMPLETION_SOURCE, segment_allowed
from tests import test_recovery as fixture
from tests.test_engine import SESSION


class RecoverySpeechProvenanceTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.RecoveryTests.asyncSetUp
    set_state = fixture.RecoveryTests.set_state
    get_state = fixture.RecoveryTests.get_state

    def outcome(self, kind=""):
        return TickOutcome(SESSION, speech_kind=kind)

    async def pending_completion(self, text="靠背凉凉的。", *, kind="life_share", source="recovery"):
        outcome = self.outcome(kind)
        token = COMPLETION_SOURCE.set((outcome, {"source": source, "action_id": "charge",
            "plan_id": "completed-recovery"}) if source is not None else None)
        try:
            async with self.engine.session_state(SESSION) as state:
                await self.engine.remember_speech_decision(state, SESSION,
                    SpeechDecision(inner="先记下刚才的感觉。", say=text, intent="life_share"),
                    trigger="completion", outcome=outcome, drafts=[text])
        finally:
            COMPLETION_SOURCE.reset(token)
        outcome.add_speech(text)
        return outcome

    async def begin_recovery(self):
        async with self.engine.session_state(SESSION) as state:
            await self.engine.tick_activity_recovery(state, self.outcome())

    async def test_system_plan_keeps_travel_and_recovery_quiet(self):
        self.action.allowed_nodes = ["bedroom"]
        plan = self.engine.decider.recovery_plan(await self.get_state())
        self.assertGreater(len(plan["steps"]), 1)
        self.assertTrue(all(step["kind"] == "quiet" for step in plan["steps"]))

    async def test_system_recovery_does_not_broadcast_templates_or_completion(self):
        self.action.duration_mode = "fixed"
        self.action.duration = 60
        self.action.visible = True
        self.action.template = "接上了恢复装置。"
        self.action.on_complete.trigger = "llm_followup"
        outcome = self.outcome()
        async with self.engine.session_state(SESSION) as state:
            await self.engine.tick_activity_recovery(state, outcome)
            self.assertEqual(state.current_action["speech_kind"], "quiet")
            self.assertEqual(state.current_action["plan_source"], "recovery")
            await self.engine._tick_continuous(state, self.engine.node("study"), outcome)
            self.assertAlmostEqual(state.energy, .21)
            self.assertIsNone(state.current_action)
        await self.engine._deliver(outcome)
        self.assertFalse(self.messenger.sent)
        self.assertFalse(self.llm.calls)

    async def test_legacy_pending_recovery_plan_cannot_inherit_life_share(self):
        self.action.template = "旧配置的恢复文案。"
        self.action.visible = True
        outcome = self.outcome("life_share")
        async with self.engine.session_state(SESSION) as state:
            state.current_plan = create_plan(steps=[{"action": "charge", "duration": 60,
                "kind": "life_share"}], world_time=state.world_time, now=self.clock.now(), source="recovery")
            await self.engine._tick_plan(state, self.engine.node("study"), outcome, depth=0)
            self.assertEqual(state.current_action["speech_kind"], "quiet")
        await self.engine._deliver(outcome)
        self.assertFalse(self.messenger.sent)

    async def test_legacy_running_recovery_finishes_without_replaying_narration(self):
        self.action.llm_level = "single"
        self.action.on_complete.trigger = "llm_followup"
        outcome = self.outcome()
        async with self.engine.session_state(SESSION) as state:
            await self.engine.tick_activity_recovery(state, outcome)
            # Saved before the new provenance field, with an inherited source.
            state.current_action.pop("plan_source")
            state.current_action["speech_kind"] = "life_share"
            state.current_action["silent_followup"] = False
            state.current_action["duration_ticks"] = 1
            await self.engine._tick_continuous(state, self.engine.node("study"), outcome)
            self.assertAlmostEqual(state.energy, .21)
            self.assertIsNone(state.current_action)
        await self.engine._deliver(outcome)
        self.assertFalse(self.messenger.sent)
        self.assertFalse(self.llm.calls)

    async def test_legacy_completion_draft_is_retired_when_recovery_continues(self):
        outcome = await self.pending_completion()
        await self.begin_recovery()
        await self.engine._deliver(outcome)
        self.assertFalse(self.messenger.sent)
        self.assertEqual(outcome.speech_decisions[0]["status"], "not_sent")
        self.assertTrue(outcome.speech_decisions[0]["recovery_cancelled"])
        state = await self.get_state()
        self.assertEqual(state.current_action["type"], "charge")
        self.assertEqual(state.current_action["elapsed_ticks"], 0)

    async def test_recovery_start_during_typing_retires_old_completion(self):
        outcome = await self.pending_completion()
        # Initially free, so the earlier filtering pass accepts the draft.
        await self.set_state(energy=.9)

        async def delayed_send(target, messages):
            await self.set_state(energy=.2)
            await self.begin_recovery()
            if not segment_allowed(target, 0):
                return False
            self.messenger.sent.append((target, list(messages)))
            return True

        self.messenger.send_text = delayed_send
        await self.engine._deliver(outcome)
        self.assertFalse(self.messenger.sent)
        self.assertTrue(outcome.speech_decisions[0]["recovery_cancelled"])
        self.assertFalse(any("平台没有接收" in note for note in outcome.notes))

    async def test_direct_reply_during_recovery_keeps_progress_and_can_send(self):
        await self.begin_recovery()
        outcome = self.outcome()
        async with self.engine.session_state(SESSION) as state:
            await self.engine._execute_actions(state, self.engine.node("study"), outcome,
                [PlannedAction(type="say", messages=["嗯，我听见了。"])], depth=0, autonomous=False)
            self.assertEqual(state.current_action["type"], "charge")
            self.assertEqual(state.current_action["elapsed_ticks"], 0)
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.flat_messages, ["嗯，我听见了。"])

    async def test_reply_completion_receipt_is_not_silenced_by_recovery(self):
        outcome = await self.pending_completion("你问的事已经弄好了。", kind="reply")
        await self.begin_recovery()
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.flat_messages, ["你问的事已经弄好了。"])

    async def test_positive_energy_hobby_keeps_its_ordinary_completion_share(self):
        hobby = ActionDef(id="drawing", name="画画", category="continuous", recovery_kind="none",
            llm_level="template", scope="node", allowed_nodes=["study"], duration_mode="fixed", duration=60,
            on_complete={"trigger": "llm_followup", "effects_per_minute": {"energy": "+0.1"}})
        self.engine.world.actions.append(hobby)
        self.llm.replies = ['{"inner":"总算把颜色调好了。","intent":"life_share",'
            '"actions":[{"type":"say","messages":["那片蓝色终于调出来了。"]}]}']
        await self.set_state(energy=.5)
        outcome = self.outcome("life_share")
        async with self.engine.session_state(SESSION) as state:
            await self.engine._start_action(state, self.engine.node("study"), outcome, hobby,
                PlannedAction(type=hobby.id, duration=60), 0, True)
            await self.engine._tick_continuous(state, self.engine.node("study"), outcome)
            self.assertAlmostEqual(state.energy, .6)
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.flat_messages, ["那片蓝色终于调出来了。"])

    async def test_non_completion_share_during_recovery_keeps_its_own_receipt(self):
        outcome = self.outcome("life_share")
        async with self.engine.session_state(SESSION) as state:
            await self.engine.remember_speech_decision(state, SESSION,
                SpeechDecision(inner="想起了上午的画。", say="上午的颜色已经调好了。", intent="life_share"),
                trigger="expression", outcome=outcome, drafts=["上午的颜色已经调好了。"])
        outcome.add_speech("上午的颜色已经调好了。")
        await self.begin_recovery()
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.flat_messages, ["上午的颜色已经调好了。"])

    async def test_normal_completion_can_share_after_automatic_recovery_begins(self):
        hobby = ActionDef(id="drawing", name="画画", category="continuous", recovery_kind="none",
            llm_level="template", scope="node", allowed_nodes=["study"], duration_mode="fixed", duration=60,
            on_complete={"trigger": "llm_followup"})
        self.engine.world.actions.append(hobby)
        self.llm.replies = ['{"inner":"总算把颜色调好了。","intent":"life_share",'
            '"actions":[{"type":"say","messages":["那片蓝色终于调出来了。"]}]}']
        outcome = self.outcome("life_share")
        async with self.engine.session_state(SESSION) as state:
            await self.engine._start_action(state, self.engine.node("study"), outcome, hobby,
                PlannedAction(type="drawing", duration=60), 0, True)
            await self.engine._tick_continuous(state, self.engine.node("study"), outcome)
        self.assertEqual(outcome.speech_decisions[0]["completion_source"]["action_id"], "drawing")
        await self.begin_recovery()
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.flat_messages, ["那片蓝色终于调出来了。"])
        self.assertNotIn("recovery_cancelled", outcome.speech_decisions[0])
        self.assertEqual(outcome.speech_decisions[0]["status"], "sent")

    async def test_unknown_legacy_completion_is_not_guessed_to_be_recovery(self):
        outcome = await self.pending_completion("刚发现了新的颜色。", source=None)
        await self.begin_recovery()
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.flat_messages, ["刚发现了新的颜色。"])
        self.assertNotIn("completion_source", outcome.speech_decisions[0])

    async def test_autonomously_chosen_charge_keeps_normal_completion_expression(self):
        self.action.duration_mode = "fixed"
        self.action.duration = 60
        self.action.on_complete.trigger = "llm_followup"
        self.llm.replies = ['{"inner":"刚才的安静让人松了口气。","intent":"life_share",'
            '"actions":[{"type":"say","messages":["刚才听见窗外有鸟叫。"]}]}']
        await self.set_state(energy=.5)
        outcome = self.outcome("life_share")
        async with self.engine.session_state(SESSION) as state:
            await self.engine._start_action(state, self.engine.node("study"), outcome, self.action,
                PlannedAction(type="charge", duration=60), 0, True)
            await self.engine.tick_activity_recovery(state, outcome)
            self.assertEqual(state.current_action["speech_kind"], "life_share")
            await self.engine._tick_continuous(state, self.engine.node("study"), outcome)
            self.assertAlmostEqual(state.energy, .51)
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.flat_messages, ["刚才听见窗外有鸟叫。"])
        self.assertEqual(outcome.speech_decisions[0]["completion_source"]["source"], "activity")
        self.assertEqual(outcome.speech_decisions[0]["completion_source"]["action_id"], "charge")
        self.assertIsNone(COMPLETION_SOURCE.get())

    async def test_stale_recovery_state_cannot_cancel_an_ordinary_completion(self):
        for case in ("expired", "finished_steps", "finished_action"):
            with self.subTest(case=case):
                outcome = await self.pending_completion("颜色调好了。")
                async with self.engine.session_state(SESSION) as state:
                    state.current_plan = create_plan(steps=[{"action": "charge", "duration": 60}],
                        world_time=state.world_time, now=self.clock.now(), source="recovery")
                    if case == "expired":
                        state.current_plan["expires_at"] = self.clock.now() - 1
                    elif case == "finished_steps":
                        state.current_plan["steps"][0]["status"] = "done"
                    else:
                        state.current_plan = None
                        state.current_action = {"type": "charge", "plan_source": "recovery",
                            "duration_ticks": 1, "elapsed_ticks": 1}
                self.engine._filter_proactive_speech(outcome)
                self.assertEqual(outcome.messages, ["颜色调好了。"])

    def test_unrelated_plan_does_not_relabel_a_hobby_as_system_recovery(self):
        recovery = {"source": "recovery", "id": "new-plan"}
        hobby = {"type": "drawing", "from_plan": True, "plan_id": "other-plan"}
        self.assertFalse(system_recovery_action(hobby, recovery))
        self.assertTrue(system_recovery_action({"plan_source": "recovery"}))


if __name__ == "__main__":
    unittest.main()
