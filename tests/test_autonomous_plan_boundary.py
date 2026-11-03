"""Model-authored plans retain each utterance's actual person and source."""
import json
import unittest

from core.engine import TickOutcome
from core.models import ActionDef
from core.speech_policy import contact_targets, reply_target
from tests import test_participation as fixture
from tests import test_autonomous_plan_intent as intent_fixture


class AutonomousPlanBoundaryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.ParticipationTests.asyncSetUp
    chat = fixture.ParticipationTests.chat
    person = intent_fixture.AutonomousPlanIntentTests.person
    run_plan = intent_fixture.AutonomousPlanIntentTests.run_plan

    async def plan(self, payload, *, contact_user=""):
        self.engine.world.actions.append(ActionDef(id="quiet_sit", name="安静坐着",
            category="continuous", duration=60))
        self.llm.replies = [json.dumps(payload, ensure_ascii=False)]
        outcome = TickOutcome(fixture.GROUP, speech_kind="outreach")
        async with self.engine.session_state(fixture.GROUP) as state:
            plan = await self.engine._ask_llm_for_plan(state, self.engine.node(state.node_id),
                outcome, force=True, contact_user=contact_user)
            self.assertIsNotNone(plan)
            self.engine._freeze_plan_target(state, outcome, plan, fallback=fixture.GROUP)
            await self.engine._apply_plan(state, self.engine.node(state.node_id), outcome, plan)
        return plan

    def wire(self):
        captured = []

        async def send(sid, lines):
            captured.extend((sid, line, contact_targets(sid, i), reply_target(sid, i))
                for i, line in enumerate(lines))
            return True

        self.messenger.send_text = send
        return captured

    @staticmethod
    def payload(steps, **extra):
        return {"inner": "把各自的事情分开说清楚。", "intent": "outreach",
            "plan": [*steps, {"action": "quiet_sit", "duration": 60}], **extra}

    async def test_mixed_message_values_do_not_become_public_text(self):
        wire = self.wire()
        await self.plan(self.payload([{"action": "say", "messages": [4, {"internal": "field"}, "真正的一句话"]}],
            intent="life_share"))
        await self.run_plan()
        self.assertEqual([item[1] for item in wire], ["真正的一句话"])
        self.assertEqual(len(self.llm.calls), 1)

    async def test_invalid_message_list_does_not_request_a_replacement_utterance(self):
        wire = self.wire()
        await self.plan(self.payload([{"action": "say", "messages": [4, {"internal": "field"}]}],
            intent="life_share"))
        await self.run_plan()
        self.assertFalse(wire)
        self.assertEqual(len(self.llm.calls), 1)

    async def test_structured_action_content_cannot_become_public_narration(self):
        self.engine.world.actions.append(ActionDef(id="touch_book", name="摸摸书页", llm_level="single"))
        wire = self.wire()
        await self.plan(self.payload([{"action": "touch_book", "content": {"internal": "field"}}],
            intent="life_share"))
        await self.run_plan()
        self.assertFalse(wire)
        self.assertEqual(len(self.llm.calls), 1)

    async def test_malformed_contact_lists_keep_life_silent_without_crashing(self):
        await self.person("alice", "Alice")
        wire = self.wire()
        for contacts in ({"id": "alice"}, "alice", ["alice", {}], [True]):
            with self.subTest(contacts=contacts):
                async with self.engine.session_state(fixture.GROUP) as state:
                    state.current_action = None
                    state.current_plan = None
                await self.plan(self.payload([{"action": "say", "messages": ["这页给你"]}],
                    contact_people=contacts))
                await self.run_plan()
                self.assertFalse(wire)

    async def test_numeric_platform_ids_are_valid_contact_people(self):
        await self.person("12345", "朋友")
        wire = self.wire()
        await self.plan(self.payload([{"action": "say", "messages": ["这页给你"]}], contact_people=[12345]))
        await self.run_plan()
        self.assertEqual(wire[0][2], ("12345",))

    async def test_each_explicit_say_target_overrides_a_multi_person_default(self):
        await self.person("alice", "Alice")
        await self.person("bob", "Bob")
        wire = self.wire()
        await self.plan(self.payload([
            {"action": "say", "target": "alice", "messages": ["Alice，这页给你"]},
            {"action": "say", "target": "bob", "messages": ["Bob，另一页给你"]}],
            contact_people=["alice", "bob"]))
        await self.run_plan()
        self.assertEqual([(item[1], item[2]) for item in wire],
            [("Alice，这页给你", ("alice",)), ("Bob，另一页给你", ("bob",))])

    async def test_explicit_action_person_can_differ_from_the_plan_default(self):
        await self.person("alice", "Alice")
        await self.person("bob", "Bob")
        self.engine.world.actions.append(ActionDef(id="greet_person", name="打招呼", category="instant",
            target_type="user", llm_level="template", template="{user}，这本书给你。"))
        wire = self.wire()
        await self.plan(self.payload([
            {"action": "greet_person", "target": "bob"},
            {"action": "say", "messages": ["Alice，还有这张书签"]}], contact_people=["alice"]))
        await self.run_plan()
        self.assertEqual([(item[1], item[2]) for item in wire],
            [("Bob，这本书给你。", ("bob",)), ("Alice，还有这张书签", ("alice",))])

    async def test_separate_rooms_keep_each_steps_explicit_person(self):
        sessions = self.engine.store.raw_sessions()
        sessions["groups"] = [{"id": "same-life", "name": "同一生活", "sessions": [fixture.GROUP, fixture.OTHER],
            "main_session": fixture.GROUP}]
        self.engine.store.save_sessions(sessions)
        self.engine.reload_config()
        await self.person("alice", "Alice")
        await self.person("bob", "Bob")
        wire = self.wire()
        await self.plan(self.payload([
            {"action": "say", "target": "alice", "send_to": fixture.GROUP, "messages": ["Alice，这页给你"]},
            {"action": "say", "target": "bob", "send_to": fixture.OTHER, "messages": ["Bob，另一页给你"]}],
            contact_people=["alice", "bob"]))
        await self.run_plan()
        self.assertEqual([(item[0], item[2]) for item in wire],
            [(fixture.GROUP, ("alice",)), (fixture.OTHER, ("bob",))])

    async def test_private_destination_cannot_replace_an_explicit_person(self):
        await self.person("alice", "Alice")
        await self.person("bob", "Bob")
        wire = self.wire()
        await self.plan(self.payload([{"action": "say", "target": "bob", "send_to": fixture.PRIVATE,
            "messages": ["给 Bob 的私话"]}], contact_people=["alice", "bob"]))
        await self.run_plan()
        self.assertFalse(wire)

    async def test_malformed_intent_keeps_personal_activity_without_speech(self):
        wire = self.wire()
        for intent in ({"name": "life_share"}, ["life_share"], 10, True):
            with self.subTest(intent=intent):
                async with self.engine.session_state(fixture.GROUP) as state:
                    state.current_action = None
                    state.current_plan = None
                await self.plan(self.payload([{"action": "say", "messages": ["不能发送的文本"]}], intent=intent))
                await self.run_plan()
                self.assertFalse(wire)

    async def test_multiple_explicit_people_do_not_supply_an_unaddressed_line(self):
        await self.person("alice", "Alice")
        await self.person("bob", "Bob")
        wire = self.wire()
        await self.plan(self.payload([
            {"action": "say", "target": "alice", "messages": ["Alice，这页给你"]},
            {"action": "say", "target": "bob", "messages": ["Bob，另一页给你"]},
            {"action": "say", "messages": ["别的人也接着听吧"]}]))
        await self.run_plan()
        self.assertEqual([(item[1], item[2]) for item in wire],
            [("Alice，这页给你", ("alice",)), ("Bob，另一页给你", ("bob",))])

    async def test_private_personal_activity_cannot_supply_a_broadcast_recipient(self):
        await self.person("alice", "Alice")
        wire = self.wire()
        await self.plan(self.payload([
            {"action": "say", "messages": ["没有选择的联系对象"]},
            {"action": "quiet_sit", "send_to": fixture.PRIVATE, "duration": 60}]))
        await self.run_plan()
        self.assertFalse(wire)

    async def test_contact_candidate_cannot_be_replaced_by_a_say_target(self):
        await self.person("alice", "Alice")
        await self.person("bob", "Bob")
        wire = self.wire()
        await self.plan(self.payload([{"action": "say", "target": "bob", "messages": ["给 Bob 的话"]}]),
            contact_user="alice")
        await self.run_plan()
        self.assertFalse(wire)

    async def test_personal_completion_cannot_mention_its_old_contact(self):
        self.engine.world.actions.append(ActionDef(id="rest_on_seat", name="坐一会儿", category="continuous",
            duration=60, on_complete={"trigger": "llm_followup"}))
        self.llm.replies = [json.dumps({"inner": "坐了一会儿，肩膀轻松了。", "intent": "life_share",
            "actions": [{"type": "say", "messages": ["肩膀轻松些了"]}]}, ensure_ascii=False)]
        wire = self.wire()
        outcome = TickOutcome(fixture.GROUP)
        async with self.engine.session_state(fixture.GROUP) as state:
            action = {"type": "rest_on_seat", "session": fixture.GROUP, "speech_kind": "outreach",
                "contact_people": ["alice"], "elapsed_ticks": 1}
            state.current_action = action
            await self.engine._finish_action(state, self.engine.node(state.node_id), outcome, action)
        await self.engine._deliver(outcome)
        self.assertEqual([(item[1], item[2]) for item in wire], [("肩膀轻松些了", ())])
        self.assertEqual(self.engine.contact_status(fixture.GROUP)["left"], 6)

    async def test_candidate_plan_does_not_rebuild_reference_numbers_after_generation(self):
        await self.person("alice", "Alice")
        await self.chat("给你看看这个书签", uid="bob", message_id="original-source")
        payload = self.payload([{"action": "say", "messages": ["这个边框很合适"]}],
            intent="continue_chat", reply_to="c1")

        async def changed_scene(state, target, *args, **kwargs):
            state.recent_chat.clear()
            state.note_chat(user_id="new-person", name="后来的人", text="另一个话题",
                now=self.clock.now(), origin=target, message_id="new-source")
            return json.dumps(payload, ensure_ascii=False)

        self.engine._ask_speech_media = changed_scene
        plan = await self.plan(payload, contact_user="alice")
        self.assertEqual(plan["speech_scene"]["reply_to"], "original-source")
        self.assertEqual(plan["speech_scene"]["rows"][0]["user_id"], "bob")

    async def test_consumed_source_during_generation_never_sends_a_late_reply(self):
        await self.person("alice", "Alice")
        row = await self.chat("给你看看这个书签", uid="bob", message_id="original-source")
        payload = self.payload([{"action": "say", "messages": ["这个边框很合适"]}],
            intent="continue_chat", reply_to="c1")

        async def answered_scene(state, target, *args, **kwargs):
            self.engine._set_watermark(state, target, until=row["at"], seq=row["seq"])
            return json.dumps(payload, ensure_ascii=False)

        self.engine._ask_speech_media = answered_scene
        wire = self.wire()
        await self.plan(payload)
        await self.run_plan()
        self.assertFalse(wire)
        await self.run_plan()
        state = await self.engine.load_state(fixture.GROUP, cold_start=False)
        self.assertEqual(state.current_action["type"], "quiet_sit")
