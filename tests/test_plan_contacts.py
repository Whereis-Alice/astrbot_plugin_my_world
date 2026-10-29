"""Autonomous plans contact only the people in the action that really ran."""
import json
import unittest
from unittest.mock import AsyncMock

from core.engine import TickOutcome
from core.json_actions import PlannedAction
from core.models import ActionDef, OnComplete
from core.planner import create_plan
from core.speech_policy import contact_targets
from tests import test_participation as fixture
from tests.stub_ports import StubTools


class PlanContactTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.ParticipationTests.asyncSetUp

    async def prepare_desire_plan(self, steps, **extra):
        self.engine.world.actions.append(ActionDef(
            id="quiet_sit", name="安静坐着", category="continuous", duration=60))
        self.engine.world.actions.append(ActionDef(
            id="greet_person", name="招呼一下", category="instant", target_type="user",
            llm_level="template", intimacy=0, template="{user}，这本书我看完了。"))
        people = [{"user_id": uid, "name": name, "affinity": 60, "level": "朋友",
                   "deny": [], "quota_left": 2, "places": fixture.GROUP}
                  for uid, name in (("111", "Alice"), ("222", "Bob"))]
        self.engine._cuddle_people = lambda *args: people
        self.engine.desire_push_ready = lambda *args: True
        for person in people:
            self.engine.profiles.touch(fixture.GROUP, person["user_id"], person["name"])
        self.llm.replies = [json.dumps({"inner": "先招呼她，之后再找他。",
                                       "reason": "按各自的事情联系", "plan": steps, **extra}, ensure_ascii=False)]
        self.wire = []

        async def send(sid, lines):
            self.wire.append((sid, list(lines), [contact_targets(sid, i) for i in range(len(lines))]))
            self.messenger.sent.append((sid, list(lines)))
            return True

        self.messenger.send_text = send
        outcome = TickOutcome(fixture.GROUP)
        async with self.engine.session_state(fixture.GROUP) as state:
            for person in people:
                state.user_presence[person["user_id"]] = {"name": person["name"], "last_seen": self.clock.now()}
            self.assertTrue(await self.engine._maybe_desire_push(
                state, self.engine.node(state.node_id), outcome, fixture.GROUP))
        return outcome

    async def test_first_poke_does_not_charge_a_future_plan_recipient(self):
        await self.prepare_desire_plan([
            {"action": "poke", "target": "111"},
            {"action": "quiet_sit", "duration": 60},
            {"action": "poke", "target": "222"},
        ])
        fresh = TickOutcome(fixture.GROUP)
        async with self.engine.session_state(fixture.GROUP) as state:
            await self.engine._tick_plan(state, self.engine.node(state.node_id), fresh, depth=0)
        self.assertEqual(self.messenger.pokes, [(fixture.GROUP, "111")])
        self.assertEqual(self.engine.contact_status(fixture.GROUP, "111")["person_left"], 1)
        self.assertEqual(self.engine.contact_status(fixture.GROUP, "222")["person_left"], 2)

    async def test_first_public_action_does_not_mention_a_future_plan_recipient(self):
        await self.prepare_desire_plan([
            {"action": "greet_person", "target": "111"},
            {"action": "quiet_sit", "duration": 60},
            {"action": "greet_person", "target": "222"},
        ])
        fresh = TickOutcome(fixture.GROUP)
        async with self.engine.session_state(fixture.GROUP) as state:
            await self.engine._tick_plan(state, self.engine.node(state.node_id), fresh, depth=0)
        await self.engine._deliver(fresh)
        self.assertEqual(self.wire, [(fixture.GROUP, ["Alice，这本书我看完了。"], [("111",)])])
        self.assertEqual(self.engine.contact_status(fixture.GROUP, "222")["person_left"], 2)

    async def run_saved_plan(self):
        fresh = TickOutcome(fixture.GROUP)
        async with self.engine.session_state(fixture.GROUP) as state:
            await self.engine._tick_plan(state, self.engine.node(state.node_id), fresh, depth=0)
        return fresh

    async def test_two_people_in_one_tick_keep_each_lines_own_addressee(self):
        await self.prepare_desire_plan([
            {"action": "greet_person", "target": "111"},
            {"action": "greet_person", "target": "222"},
            {"action": "greet_person", "target": "111"},
        ])
        await self.engine._deliver(await self.run_saved_plan())
        self.assertEqual(self.wire[0][2], [("111",), ("222",), ()])
        self.assertEqual(self.engine.contact_status(fixture.GROUP)["left"], 5)
        for uid in ("111", "222"):
            self.assertEqual(self.engine.contact_status(fixture.GROUP, uid)["person_left"], 1)
        self.assertEqual(len(self.llm.calls), 1)

    async def test_partial_acceptance_does_not_charge_the_unsent_person(self):
        await self.prepare_desire_plan([
            {"action": "greet_person", "target": "111"},
            {"action": "greet_person", "target": "222"},
        ])
        self.messenger.take_sent_texts = lambda sid: ["Alice，这本书我看完了。"]
        await self.engine._deliver(await self.run_saved_plan())
        self.assertEqual(self.engine.contact_status(fixture.GROUP, "111")["person_left"], 1)
        self.assertEqual(self.engine.contact_status(fixture.GROUP, "222")["person_left"], 2)

    async def test_exhausted_future_recipient_does_not_block_the_current_person(self):
        await self.prepare_desire_plan([
            {"action": "greet_person", "target": "111"},
            {"action": "greet_person", "target": "222"},
        ])
        for _ in range(2):
            self.engine.profiles.note_proactive(fixture.GROUP, "222", now=self.clock.now())
        await self.engine._deliver(await self.run_saved_plan())
        self.assertEqual(self.wire, [(fixture.GROUP, ["Alice，这本书我看完了。"], [("111",)])])
        self.assertEqual(self.engine.contact_status(fixture.GROUP, "111")["person_left"], 1)

    async def test_private_receipt_counts_only_its_actual_recipient(self):
        self.engine.profiles.touch(fixture.PRIVATE, "alice", "Alice")
        self.engine.profiles.touch(fixture.PRIVATE, "222", "Bob")
        outcome = TickOutcome(fixture.PRIVATE)
        outcome.contact_people = {"222"}
        outcome.add_speech("这本书我看完了。", kind="outreach")
        await self.engine._deliver(outcome)
        self.assertEqual(self.engine.contact_status(fixture.PRIVATE, "alice")["person_left"], 1)
        self.assertEqual(self.engine.contact_status(fixture.PRIVATE, "222")["person_left"], 2)

    async def test_each_room_gets_only_its_own_planned_recipient(self):
        self.engine.resolve_send_to = lambda state, raw, **kwargs: raw if raw in {fixture.GROUP, fixture.OTHER} else ""
        for uid, name in (("111", "Alice"), ("222", "Bob")):
            self.engine.profiles.touch(fixture.OTHER, uid, name)
        await self.prepare_desire_plan([
            {"action": "greet_person", "target": "111", "send_to": fixture.GROUP},
            {"action": "greet_person", "target": "222", "send_to": fixture.OTHER},
        ])
        await self.engine._deliver(await self.run_saved_plan())
        self.assertEqual(self.wire, [
            (fixture.GROUP, ["Alice，这本书我看完了。"], [("111",)]),
            (fixture.OTHER, ["Bob，这本书我看完了。"], [("222",)]),
        ])
        self.assertEqual(self.engine.contact_status(fixture.GROUP, "222")["person_left"], 2)
        self.assertEqual(self.engine.contact_status(fixture.OTHER, "111")["person_left"], 2)

    async def test_explicit_action_recipient_overrides_a_plan_default(self):
        await self.prepare_desire_plan([
            {"action": "greet_person", "target": "222"},
            {"action": "say", "messages": ["前几天的约定还记得吗？"]},
        ])
        async with self.engine.session_state(fixture.GROUP) as state:
            state.current_plan["contact_people"] = ["111"]
        await self.engine._deliver(await self.run_saved_plan())
        self.assertEqual(self.wire[0][2], [("222",), ("111",)])

    async def test_instant_send_keeps_its_saved_person_before_a_slow_activity(self):
        await self.prepare_desire_plan([
            {"action": "greet_person", "target": "111"},
            {"action": "quiet_sit", "duration": 60},
            {"action": "greet_person", "target": "222"},
        ])
        async def say(sid, line):
            self.wire.append((sid, [line], [contact_targets(sid)]))
            return True
        self.engine.say_sink = say
        fresh = await self.run_saved_plan()
        await self.engine._deliver(fresh)
        self.assertEqual(self.wire, [(fixture.GROUP, ["Alice，这本书我看完了。"], [("111",)])])
        self.assertEqual(fresh.live_people, [("111",)])
        self.assertEqual(self.engine.contact_status(fixture.GROUP, "222")["person_left"], 2)

    async def test_send_time_person_quota_change_does_not_block_another_person(self):
        await self.prepare_desire_plan([
            {"action": "greet_person", "target": "111"},
            {"action": "greet_person", "target": "222"},
        ])
        fresh = await self.run_saved_plan()
        for _ in range(2):
            self.engine.profiles.note_proactive(fixture.GROUP, "222", now=self.clock.now())
        await self.engine._deliver(fresh)
        self.assertEqual(self.wire, [(fixture.GROUP, ["Alice，这本书我看完了。"], [("111",)])])

    async def test_normal_decision_delivers_a_step_with_only_a_separate_room(self):
        self.engine.resolve_send_to = lambda state, raw, **kwargs: raw if raw == fixture.OTHER else ""
        await self.prepare_desire_plan([
            {"action": "greet_person", "target": "111", "send_to": fixture.OTHER},
        ])
        async with self.engine.session_state(fixture.GROUP) as state:
            state.current_plan = None
        self.engine._maybe_desire_push = lambda *args: self.no_push()
        self.engine._maybe_miss_push = lambda *args: self.no_push()
        self.engine.life_choice = lambda *args: {"kind": "idle", "reason": "正常空闲"}
        self.engine.decider.rule_plan = lambda *args, **kwargs: None
        self.engine.decider.should_ask_llm = lambda *args: True
        self.engine._llm_plan_allowed = lambda *args: True
        self.llm.replies = [json.dumps({"plan": [
            {"action": "greet_person", "target": "111", "send_to": fixture.OTHER}]}, ensure_ascii=False)]
        outcome = await self.engine.maybe_decide(fixture.GROUP, force=True)
        self.assertEqual(outcome.routed, {fixture.OTHER: ["Alice，这本书我看完了。"]})
        self.assertEqual(self.wire, [(fixture.OTHER, ["Alice，这本书我看完了。"], [("111",)])])

    async def test_normal_decision_delivers_image_only_to_the_steps_room(self):
        self.engine.resolve_send_to = lambda state, raw, **kwargs: raw if raw == fixture.OTHER else ""
        await self.prepare_desire_plan([{"action": "quiet_sit", "duration": 60}])
        self.engine.tools = StubTools(tools={"image_tool": "生成图片"},
                                     results={"image_tool": "已生成"},
                                     images={"image_tool": ["https://images.example.test/book.png"]})
        self.engine.world.actions.append(ActionDef(id="draw_book", name="画书", llm_level="tool",
                                                  tool_names=["image_tool"], visible=False))
        self.engine.world.tool_result_reply = False
        async with self.engine.session_state(fixture.GROUP) as state:
            state.current_plan = None
        self.engine._maybe_desire_push = lambda *args: self.no_push()
        self.engine._maybe_miss_push = lambda *args: self.no_push()
        self.engine.life_choice = lambda *args: {"kind": "idle", "reason": "正常空闲"}
        self.engine.decider.rule_plan = lambda *args, **kwargs: None
        self.engine.decider.should_ask_llm = lambda *args: True
        self.engine._llm_plan_allowed = lambda *args: True
        self.llm.replies = [json.dumps({"plan": [{"action": "draw_book", "send_to": fixture.OTHER}]})]
        outcome = await self.engine.maybe_decide(fixture.GROUP, force=True)
        self.assertEqual(outcome.routed_images, {fixture.OTHER: ["https://images.example.test/book.png"]})
        self.assertEqual(self.messenger.sent_images, [(fixture.OTHER, ["https://images.example.test/book.png"])])
        self.assertFalse(self.wire)
        self.assertEqual(self.engine.tools.calls, [("image_tool", {})])

    async def test_quiet_decision_with_no_public_content_needs_no_delivery(self):
        await self.prepare_desire_plan([{"action": "quiet_sit", "duration": 60}])
        async with self.engine.session_state(fixture.GROUP) as state:
            state.current_plan = None
        self.engine.proactive_chat_allowed = lambda *args: False
        self.engine.life_choice = lambda *args: {"kind": "idle", "reason": "正常空闲"}
        self.engine.decider.should_ask_llm = lambda *args: False
        self.engine.decider.rule_plan = lambda state, **kwargs: create_plan(
            steps=[{"action": "quiet_sit", "duration": 60}], world_time=state.world_time,
            now=self.clock.now(), tick_seconds=self.engine.tick_seconds)
        self.engine._deliver = AsyncMock()
        outcome = await self.engine.maybe_decide(fixture.GROUP, force=True)
        self.engine._deliver.assert_not_awaited()
        self.assertFalse(outcome.messages or outcome.routed or outcome.images or outcome.routed_images)
        state = await self.engine.load_state(fixture.GROUP, cold_start=False)
        self.assertEqual(state.current_action["type"], "quiet_sit")
        self.assertEqual(state.current_action["speech_kind"], "quiet")

    async def no_push(self):
        return False

    async def test_unresolved_private_plan_never_falls_back_to_public(self):
        await self.prepare_desire_plan([
            {"action": "greet_person", "target": "111"},
        ], send_to="aiocqhttp:PrivateMessage:missing")
        await self.engine._deliver(await self.run_saved_plan())
        self.assertFalse(self.wire)

    async def test_unresolved_private_step_cannot_poke_someone_in_the_group(self):
        await self.prepare_desire_plan([
            {"action": "poke", "target": "111", "send_to": "aiocqhttp:PrivateMessage:missing"},
        ])
        await self.run_saved_plan()
        self.assertFalse(self.messenger.pokes)

    async def test_unresolved_private_plan_keeps_an_independent_activity_quiet(self):
        await self.prepare_desire_plan([
            {"action": "quiet_sit", "duration": 60},
            {"action": "greet_person", "target": "111"},
        ], send_to="aiocqhttp:PrivateMessage:missing")
        fresh = await self.run_saved_plan()
        await self.engine._deliver(fresh)
        state = await self.engine.load_state(fixture.GROUP, cold_start=False)
        self.assertEqual(state.current_action["type"], "quiet_sit")
        self.assertEqual(state.current_action["speech_kind"], "quiet")
        self.assertFalse(self.wire)

    def image_outcome(self):
        for uid in ("111", "222"):
            self.engine.profiles.touch(fixture.GROUP, uid, uid)
        return TickOutcome(fixture.GROUP)

    async def test_partial_images_count_only_the_accepted_recipient(self):
        outcome = self.image_outcome()
        outcome.contact_people = {"111"}
        outcome.add_images(["first.png"], kind="outreach")
        outcome.contact_people = {"222"}
        outcome.add_images(["second.png"], kind="outreach")
        outcome.contact_people = {"unrelated"}
        self.messenger.take_sent_images = lambda sid: ["first.png"]
        await self.engine._deliver(outcome)
        self.assertEqual(self.engine.contact_status(fixture.GROUP, "111")["person_left"], 1)
        self.assertEqual(self.engine.contact_status(fixture.GROUP, "222")["person_left"], 2)

    async def test_mixed_life_image_and_outreach_text_keep_ledgers_independent(self):
        outcome = self.image_outcome()
        outcome.contact_people = {"111"}
        outcome.add_images(["first.png"], kind="life_share")
        outcome.contact_people = {"222"}
        outcome.add_speech("给你看刚读到的事", kind="outreach")
        await self.engine._deliver(outcome)
        self.assertEqual(self.engine.contact_status(fixture.GROUP, "111")["person_left"], 2)
        self.assertEqual(self.engine.contact_status(fixture.GROUP, "222")["person_left"], 1)
        self.assertEqual(self.engine.life_share_status(fixture.GROUP)["left"], 5)

    async def test_exhausted_image_recipient_does_not_block_another_persons_text(self):
        outcome = self.image_outcome()
        for _ in range(2):
            self.engine.profiles.note_proactive(fixture.GROUP, "222", now=self.clock.now())
        outcome.contact_people = {"111"}
        outcome.add_speech("给你看刚读到的事", kind="outreach")
        outcome.contact_people = {"222"}
        outcome.add_images(["second.png"], kind="outreach")
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.sent, [(fixture.GROUP, ["给你看刚读到的事"])])
        self.assertEqual(self.messenger.sent_images, [])
        self.assertEqual(self.engine.contact_status(fixture.GROUP, "111")["person_left"], 1)

    async def test_explicit_action_room_checks_that_rooms_person_quota(self):
        await self.prepare_desire_plan([{"action": "quiet_sit", "duration": 60}])
        self.engine.profiles.touch(fixture.OTHER, "111", "Alice")
        self.engine.resolve_send_to = lambda state, raw, **kwargs: raw if raw == fixture.OTHER else ""
        for _ in range(2):
            self.engine.profiles.note_proactive(fixture.GROUP, "111", now=self.clock.now())
        outcome = TickOutcome(fixture.GROUP)
        async with self.engine.session_state(fixture.GROUP) as state:
            state.current_plan = None
            await self.engine._execute_actions(state, self.engine.node(state.node_id), outcome,
                [PlannedAction(type="greet_person", target="111", send_to=fixture.OTHER)],
                depth=0, autonomous=True)
        await self.engine._deliver(outcome)
        self.assertEqual(self.wire, [(fixture.OTHER, ["Alice，这本书我看完了。"], [("111",)])])
        self.assertEqual(self.engine.contact_status(fixture.OTHER, "111")["person_left"], 1)

    async def test_old_user_action_completion_uses_its_own_recipient(self):
        await self.prepare_desire_plan([{"action": "quiet_sit", "duration": 60}])
        self.engine.world.actions.append(ActionDef(id="read_to_person", name="一起读书", category="continuous",
            target_type="user", intimacy=0, on_complete=OnComplete(trigger="llm_followup")))
        async def followup(_state, _node, outcome, *args, **kwargs):
            outcome.add_speech("刚才那一段读完了。")
            return True
        self.engine._llm_followup = AsyncMock(side_effect=followup)
        outcome = TickOutcome(fixture.GROUP)
        async with self.engine.session_state(fixture.GROUP) as state:
            state.current_plan = None
            state.current_action = {"type": "read_to_person", "target": "111", "session": fixture.GROUP,
                "speech_kind": "outreach", "contact_people": ["111", "222"],
                "duration_ticks": 1, "elapsed_ticks": 1}
            await self.engine._finish_action(state, self.engine.node(state.node_id), outcome, state.current_action)
        await self.engine._deliver(outcome)
        self.assertEqual(self.wire, [(fixture.GROUP, ["刚才那一段读完了。"], [("111",)])])
        self.assertEqual(self.engine.contact_status(fixture.GROUP, "222")["person_left"], 2)

    async def test_poke_action_uses_its_explicit_room_and_that_rooms_quota(self):
        await self.prepare_desire_plan([{"action": "quiet_sit", "duration": 60}])
        self.engine.profiles.touch(fixture.OTHER, "111", "Alice")
        self.engine.resolve_send_to = lambda state, raw, **kwargs: raw if raw == fixture.OTHER else ""
        for _ in range(2):
            self.engine.profiles.note_proactive(fixture.GROUP, "111", now=self.clock.now())
        outcome = TickOutcome(fixture.GROUP)
        async with self.engine.session_state(fixture.GROUP) as state:
            state.current_plan = None
            await self.engine._execute_actions(state, self.engine.node(state.node_id), outcome,
                [PlannedAction(type="poke", target="111", send_to=fixture.OTHER)],
                depth=0, autonomous=True)
        self.assertEqual(self.messenger.pokes, [(fixture.OTHER, "111")])
        self.assertEqual(self.engine.contact_status(fixture.OTHER, "111")["person_left"], 1)

    async def test_slow_step_in_another_room_does_not_redirect_the_earlier_line(self):
        self.engine.resolve_send_to = lambda state, raw, **kwargs: raw if raw in {fixture.GROUP, fixture.OTHER} else ""
        await self.prepare_desire_plan([
            {"action": "greet_person", "target": "111", "send_to": fixture.GROUP},
            {"action": "quiet_sit", "duration": 60, "send_to": fixture.OTHER},
        ])
        async def say(sid, line):
            self.wire.append((sid, [line], [contact_targets(sid)]))
            return True
        self.engine.say_sink = say
        fresh = await self.run_saved_plan()
        await self.engine._deliver(fresh)
        self.assertEqual(self.wire, [(fixture.GROUP, ["Alice，这本书我看完了。"], [("111",)])])
        self.assertEqual(fresh.live_origins, [fixture.GROUP])
