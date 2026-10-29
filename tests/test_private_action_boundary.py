"""Private user actions cannot send to one person while accounting for another."""
import unittest
from unittest.mock import AsyncMock

from core.engine import TickOutcome
from core.json_actions import PlannedAction
from core.models import ActionDef, OnComplete
from core.planner import create_plan
from tests import test_participation as fixture


class PrivateActionBoundaryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.ParticipationTests.asyncSetUp

    def contacts(self):
        self.engine.resolve_send_to = lambda state, raw, **kwargs: raw if raw == fixture.PRIVATE else ""
        self.engine.profiles.touch(fixture.PRIVATE, "alice", "Alice")
        self.engine.profiles.touch(fixture.PRIVATE, "111", "Another person")

    async def run_poke(self, *, target="", send_to="", home=fixture.GROUP):
        self.contacts()
        outcome = TickOutcome(home)
        async with self.engine.session_state(home) as state:
            state.current_plan = None
            state.current_action = None
            state.note_chat(user_id="111", name="群里的另一个人", text="刚才群里的话",
                            now=self.clock.now(), origin=fixture.GROUP)
            await self.engine._execute_actions(state, self.engine.node(state.node_id), outcome,
                [PlannedAction(type="poke", target=target, send_to=send_to)], depth=0, autonomous=True)
        return outcome

    def assert_no_private_contact(self):
        self.assertFalse(self.messenger.pokes)
        self.assertEqual(self.engine.contact_status(fixture.PRIVATE)["left"], 6)
        self.assertEqual(self.engine.contact_status(fixture.PRIVATE, "alice")["person_left"], 2)
        self.assertEqual(self.engine.contact_status(fixture.PRIVATE, "111")["person_left"], 2)

    async def test_explicit_private_poke_rejects_a_different_person(self):
        outcome = await self.run_poke(target="111", send_to=fixture.PRIVATE)
        self.assert_no_private_contact()
        self.assertTrue(any("对象冲突" in note for note in outcome.notes))

    async def test_matching_private_poke_sends_and_counts_the_same_person(self):
        await self.run_poke(target="alice", send_to=fixture.PRIVATE)
        self.assertEqual(self.messenger.pokes, [(fixture.PRIVATE, "alice")])
        self.assertEqual(self.engine.contact_status(fixture.PRIVATE, "alice")["person_left"], 1)
        self.assertEqual(self.engine.contact_status(fixture.PRIVATE, "111")["person_left"], 2)

    async def test_explicit_private_poke_without_target_uses_the_recipient(self):
        await self.run_poke(send_to=fixture.PRIVATE)
        self.assertEqual(self.messenger.pokes, [(fixture.PRIVATE, "alice")])
        self.assertEqual(self.engine.contact_status(fixture.PRIVATE, "alice")["person_left"], 1)

    async def test_implicit_private_poke_without_target_does_not_borrow_group_sender(self):
        await self.run_poke(home=fixture.PRIVATE)
        self.assertEqual(self.messenger.pokes, [(fixture.PRIVATE, "alice")])
        self.assertEqual(self.engine.contact_status(fixture.PRIVATE, "alice")["person_left"], 1)

    async def run_private_step(self, target):
        self.contacts()
        outcome = TickOutcome(fixture.GROUP)
        async with self.engine.session_state(fixture.GROUP) as state:
            state.note_chat(user_id="111", name="群里的另一个人", text="刚才群里的话",
                            now=self.clock.now(), origin=fixture.GROUP)
            state.current_plan = create_plan(steps=[{"action": "poke", "target": target}],
                world_time=state.world_time, now=self.clock.now(), tick_seconds=self.engine.tick_seconds)
            state.current_plan["steps"][0]["session"] = fixture.PRIVATE
            await self.engine._tick_plan(state, self.engine.node(state.node_id), outcome, depth=0)
        return outcome

    async def test_persisted_implicit_private_step_rejects_a_conflicting_person(self):
        outcome = await self.run_private_step("111")
        self.assert_no_private_contact()
        self.assertTrue(any("对象冲突" in note for note in outcome.notes))

    async def test_persisted_implicit_private_step_without_target_uses_the_recipient(self):
        await self.run_private_step("")
        self.assertEqual(self.messenger.pokes, [(fixture.PRIVATE, "alice")])
        self.assertEqual(self.engine.contact_status(fixture.PRIVATE, "alice")["person_left"], 1)

    async def test_old_completion_cannot_tell_one_person_about_another_persons_action(self):
        self.contacts()
        self.engine.world.actions.append(ActionDef(id="read_to_person", name="一起读书", category="continuous",
            target_type="user", intimacy=0, on_complete=OnComplete(trigger="llm_followup", effects={"energy": "+0.1"})))
        self.engine._llm_followup = AsyncMock(return_value=True)
        outcome = TickOutcome(fixture.GROUP)
        async with self.engine.session_state(fixture.GROUP) as state:
            state.energy = .5
            state.current_action = {"type": "read_to_person", "target": "111", "session": fixture.PRIVATE,
                "speech_kind": "outreach", "contact_people": ["111"],
                "duration_ticks": 1, "elapsed_ticks": 1}
            await self.engine._finish_action(state, self.engine.node(state.node_id), outcome, state.current_action)
            self.assertIsNone(state.current_action)
            self.assertGreater(state.energy, .5)
        self.engine._llm_followup.assert_not_awaited()
        self.assertFalse(outcome.messages or outcome.routed)
        self.assertTrue(any("对象冲突" in note for note in outcome.notes))
        self.assert_no_private_contact()

    async def test_any_user_action_template_rejects_a_conflicting_private_recipient(self):
        self.contacts()
        self.engine.world.actions.append(ActionDef(id="greet_person", name="招呼一下", target_type="user",
            intimacy=0, llm_level="template", template="刚才和你的约定还记得吗？"))
        self.engine._instant_output = AsyncMock(return_value=["刚才和你的约定还记得吗？"])
        outcome = TickOutcome(fixture.GROUP)
        async with self.engine.session_state(fixture.GROUP) as state:
            await self.engine._execute_actions(state, self.engine.node(state.node_id), outcome,
                [PlannedAction(type="greet_person", target="111", send_to=fixture.PRIVATE)],
                depth=0, autonomous=True)
        await self.engine._deliver(outcome)
        self.engine._instant_output.assert_not_awaited()
        self.assertFalse(self.messenger.sent)
        self.assert_no_private_contact()

    async def test_private_poke_without_target_cannot_bypass_the_recipients_quota(self):
        self.contacts()
        for _ in range(2):
            self.engine.profiles.note_proactive(fixture.PRIVATE, "alice", now=self.clock.now())
        await self.run_poke(send_to=fixture.PRIVATE)
        self.assertFalse(self.messenger.pokes)
        self.assertEqual(self.engine.contact_status(fixture.PRIVATE)["left"], 6)
        self.assertEqual(self.engine.contact_status(fixture.PRIVATE, "alice")["person_left"], 0)
        self.assertEqual(self.engine.contact_status(fixture.PRIVATE, "111")["person_left"], 2)

    async def test_completion_error_restores_previous_object_and_room(self):
        self.engine.world.actions.append(ActionDef(id="read_with_user", name="一起读书", category="continuous",
            target_type="user", intimacy=0, on_complete=OnComplete(trigger="llm_followup")))
        self.engine._llm_followup = AsyncMock(side_effect=RuntimeError("synthetic followup failure"))
        outcome = TickOutcome(fixture.GROUP, speech_home=fixture.GROUP, contact_people={"original"})
        async with self.engine.session_state(fixture.GROUP) as state:
            state.current_plan = None
            state.current_action = {"type": "read_with_user", "target": "alice", "session": fixture.PRIVATE,
                "speech_kind": "outreach", "contact_people": ["alice"],
                "duration_ticks": 1, "elapsed_ticks": 1}
            with self.assertRaises(RuntimeError):
                await self.engine._finish_action(state, self.engine.node(state.node_id), outcome, state.current_action)
        self.assertEqual(outcome.contact_people, {"original"})
        self.assertEqual(outcome.home(), fixture.GROUP)
        self.assertEqual(outcome.speech_kind, "")

    async def test_start_error_restores_previous_object_and_room(self):
        self.contacts()
        definition = ActionDef(id="hello_user", name="招呼一下", target_type="user", intimacy=0,
                              llm_level="single")
        self.engine.world.actions.append(definition)
        self.engine._instant_output = AsyncMock(side_effect=RuntimeError("synthetic output failure"))
        outcome = TickOutcome(fixture.GROUP, speech_home=fixture.GROUP, contact_people={"original"})
        async with self.engine.session_state(fixture.GROUP) as state:
            state.current_plan = None
            state.current_action = None
            with self.assertRaises(RuntimeError):
                await self.engine._start_action(state, self.engine.node(state.node_id), outcome, definition,
                    PlannedAction(type="hello_user", target="alice", send_to=fixture.PRIVATE), 0, True)
        self.assertEqual(outcome.contact_people, {"original"})
        self.assertEqual(outcome.home(), fixture.GROUP)
        self.assertEqual(outcome.speech_kind, "")
