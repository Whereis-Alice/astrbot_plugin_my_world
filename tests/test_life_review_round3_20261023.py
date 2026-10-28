"""Appointments use their real work and retry time when admitting new life."""
import copy
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from core import life_matters as matters
from core.models import ActionDef, ChainStep, ScheduleDef
from core.events import CheckResult, EventPackage, default_decision
from tests import test_life_audit as audit
from tests import test_chat_browse as browse

S = audit.S


class AppointmentContinuityTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = audit.LifeAuditTests.asyncSetUp
    idle = audit.LifeAuditTests.idle

    async def appointment(self, *, chain=(), smart=False):
        await self.idle()
        self.clock.set_struct(datetime(2026, 10, 19, 21, 30))
        async with self.engine.session_state(S) as state:
            item = self.engine.add_life_matter(state, "接着读自己的长篇小说")
            item["steps"] = [{"id": "chapter", "text": "读下一章", "action": "read",
                              "node": "study", "minutes": 10, "status": "pending"}]
            identity = item["id"]
        schedule = ScheduleDef(id="project", time="21:35", matter_id=identity,
                               action_chain=[ChainStep(type=k) for k in chain], smart=smart)
        self.engine.schedules.schedules = [schedule]
        return schedule

    async def test_linked_appointment_runs_project_even_with_old_sleep_chain(self):
        schedule = await self.appointment(chain=["sleep"])
        self.engine.world.sleep.mode = "autonomous"
        self.assertEqual(self.engine._next_schedule(S)["id"], schedule.id)
        self.clock.set_struct(datetime(2026, 10, 19, 21, 35))
        self.engine._schedule_reset_pending = True
        await self.engine.run_schedules()
        state = await self.engine.load_state(S)
        self.assertEqual((state.current_action or {}).get("matter_id"), schedule.matter_id)
        self.assertEqual((state.current_action or {}).get("type"), "read")
        self.assertEqual(matters.find(state, schedule.matter_id)["status"], "working")

    async def test_linked_appointment_gate_does_not_treat_old_chain_as_real_work(self):
        schedule = await self.appointment(chain=["sleep", "walk_to"])
        self.engine.world.sleep.mode = "fixed"
        self.assertEqual(self.engine._schedule_gate_kind(schedule), "")

    async def test_linked_smart_appointment_does_not_plan_unused_tool_chain(self):
        self.engine.world.actions.append(ActionDef(id="old_command", llm_level="command",
                                                   trigger_command="old_lookup"))
        schedule = await self.appointment(chain=["old_command"], smart=True)
        model = self.engine._ask_llm = AsyncMock(return_value='{"intents":[]}')
        original = copy.deepcopy(schedule.action_chain)
        self.clock.set_struct(datetime(2026, 10, 19, 21, 35))
        self.engine._schedule_reset_pending = True
        await self.engine.run_schedules()
        state = await self.engine.load_state(S)
        self.assertEqual((state.current_action or {}).get("matter_id"), schedule.matter_id)
        model.assert_not_awaited()
        self.assertEqual(schedule.action_chain, original)
        self.assertEqual(self.messenger.sent, [])

    async def reading_schedule(self, *, once=False, date=""):
        await self.idle()
        self.clock.set_struct(datetime(2026, 10, 19, 21, 30))
        schedule = ScheduleDef(id="reading", time="21:30", once=once, date=date,
                               action_chain=[ChainStep(type="read")])
        self.engine.schedules.schedules = [schedule]
        self.engine.world.events.schedule_delay_minutes = 5
        return schedule

    async def test_delayed_schedule_reserves_its_retry_not_tomorrows_clock_slot(self):
        schedule = await self.reading_schedule()
        async with self.engine.session_state(S) as state:
            self.engine._delay_schedule(state, schedule, "2026-10-19", "21:30")
        self.clock.set_struct(datetime(2026, 10, 19, 21, 32))
        async with self.engine.session_state(S) as state:
            upcoming = self.engine._next_schedule(S, state=state)
            self.assertEqual(upcoming["in_minutes"], 3)
            self.assertEqual(upcoming["time"], "21:35")
            self.assertEqual(self.engine.life_choice(state)["kind"], "schedule")
        self.clock.set_struct(datetime(2026, 10, 19, 21, 35))
        await self.engine.run_schedules()
        state = await self.engine.load_state(S)
        self.assertEqual((state.current_action or {}).get("type"), "read")
        self.assertNotIn(schedule.id, state.schedule_delays)

    async def test_cancelled_today_does_not_reserve_free_time(self):
        schedule = await self.reading_schedule()
        self.clock.set_struct(datetime(2026, 10, 19, 21, 36))
        async with self.engine.session_state(S) as state:
            self.engine._cancel_schedule(state, schedule, "2026-10-19")
            upcoming = self.engine._next_schedule(S, state=state)
            self.assertGreater(upcoming["in_minutes"], 1400)
            self.assertEqual(self.engine.life_choice(state)["kind"], "idle")

    async def test_once_retry_survives_midnight_and_uses_its_actual_time(self):
        schedule = await self.reading_schedule(once=True, date="2026-10-19")
        schedule.time = "23:58"
        self.clock.set_struct(datetime(2026, 10, 19, 23, 58))
        async with self.engine.session_state(S) as state:
            self.engine._delay_schedule(state, schedule, "2026-10-19", "23:58")
        self.clock.set_struct(datetime(2026, 10, 20, 0, 1))
        async with self.engine.session_state(S) as state:
            upcoming = self.engine._next_schedule(S, state=state)
            self.assertIsNotNone(upcoming)
            self.assertEqual(upcoming["in_minutes"], 2)
            self.assertEqual(upcoming["time"], "00:03")
            self.assertEqual(self.engine.life_choice(state)["kind"], "schedule")

    async def test_cancelled_once_appointment_is_not_previewed(self):
        schedule = await self.reading_schedule(once=True, date="2026-10-19")
        self.clock.set_struct(datetime(2026, 10, 19, 21, 36))
        async with self.engine.session_state(S) as state:
            self.engine._cancel_schedule(state, schedule, "2026-10-19")
            self.assertIsNone(self.engine._next_schedule(S, state=state))


class ReadingIntentTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = browse.ChatBrowseTests.asyncSetUp
    configure = browse.ChatBrowseTests.configure
    note = browse.ChatBrowseTests.note
    prepare = browse.ChatBrowseTests.prepare
    complete = browse.ChatBrowseTests.complete

    async def test_reading_records_the_models_specific_intent_without_sending(self):
        action = await self.prepare()
        state = await self.complete(action, intent="先看看花的位置，再想想自己的窗边。")
        thought = next(t for t in state.thoughts if t.get("browse_id") == action["id"])
        self.assertEqual(thought["detail"]["intent"], "先看看花的位置，再想想自己的窗边。")
        self.assertEqual(self.messenger.sent, [])

    async def test_missing_intent_is_not_filled_with_a_fake_decision(self):
        action = await self.prepare()
        state = await self.complete(action)
        thought = next(t for t in state.thoughts if t.get("browse_id") == action["id"])
        self.assertFalse(thought["detail"].get("intent"))
        self.assertTrue(thought["content"])

    async def test_reply_choice_preserves_reading_feeling_and_records_real_motivation(self):
        action = await self.prepare(reply_enabled=True, message_id="flower")
        state = await self.complete(action, intent="想看看那盆花。", decision=(
            '{"intent":"join_chat","motivation":"想问问那盆花是不是晒得太久。",'
            '"say":"那盆花一直放在窗边吗？","reply_to":"m1"}'))
        thought = next(t for t in state.thoughts if t.get("browse_id") == action["id"])
        speech = thought["detail"]["speech"]
        self.assertEqual(speech.get("motivation"), "想问问那盆花是不是晒得太久。")
        self.assertEqual(speech["intent"], "join_chat")
        self.assertEqual(speech["status"], "sent")
        self.assertEqual(thought["detail"]["intent"], "想看看那盆花。")
        self.assertTrue(thought["browse_replied"])
        self.assertEqual(len([t for t in state.thoughts if t.get("browse_id") == action["id"]]), 1)

    async def test_old_messages_allow_a_real_model_listening_choice(self):
        action = await self.prepare(reply_enabled=True)
        self.clock.advance(901)
        state = await self.complete(action, decision=(
            '{"intent":"listen","motivation":"这件事已经聊完了，留着自己的感想。"}'))
        thought = next(t for t in state.thoughts if t.get("browse_id") == action["id"])
        self.assertEqual(thought["detail"]["speech"]["intent"], "listen")
        self.assertEqual(thought["detail"]["speech"]["motivation"], "这件事已经聊完了，留着自己的感想。")
        self.assertEqual(self.messenger.sent, [])
        self.assertEqual(len(self.llm.calls), 2)

    async def test_already_considered_scene_records_skip_without_a_new_model_choice(self):
        action = await self.prepare(reply_enabled=True)
        async with self.engine.session_state(S) as state:
            state.participation[S] = {"checked_seq": action["browse_rows"][-1]["seq"]}
        state = await self.complete(action)
        thought = next(t for t in state.thoughts if t.get("browse_id") == action["id"])
        self.assertEqual((thought["detail"].get("browse_reply") or {}).get("status"), "already_considered")
        self.assertEqual(len(self.llm.calls), 1)

    async def test_disabled_reply_is_recorded_without_claiming_she_chose_silence(self):
        action = await self.prepare(reply_enabled=False)
        state = await self.complete(action)
        thought = next(t for t in state.thoughts if t.get("browse_id") == action["id"])
        self.assertEqual(thought["detail"]["browse_reply"]["status"], "disabled")
        self.assertNotIn("speech", thought["detail"])
        self.assertEqual(len(self.llm.calls), 1)

    async def test_browse_reply_has_no_message_sampling_probability(self):
        raw = self.store.raw_world()
        raw.setdefault("decider", {})["natural_silence"] = False
        self.store.save_world(raw)
        action = await self.prepare(reply_enabled=True, message_id="flower")
        with patch.object(self.engine, "participation_sample", side_effect=AssertionError("reading reply must not use message observation scheduling")):
            state = await self.complete(action, decision='{"intent":"join_chat","say":"那盆花怎么样了？"}')
        thought = next(t for t in state.thoughts if t.get("browse_id") == action["id"])
        self.assertEqual(thought["detail"]["speech"]["status"], "sent")
        self.assertFalse(self.engine.world.decider.natural_silence)


class PersonaBoundaryTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = audit.LifeAuditTests.asyncSetUp
    idle = audit.LifeAuditTests.idle

    async def test_smart_tool_intents_keep_constraints_at_the_end_of_long_persona(self):
        await self.idle()
        self.persona.text = "原来的角色设定。" * 1000 + "核心边界：不喝茶，只能使用指定充电装置恢复。"
        self.engine.world.actions.append(ActionDef(id="lookup", llm_level="command", trigger_command="lookup"))
        schedule = ScheduleDef(id="news", action_chain=[ChainStep(type="lookup")], smart=True)
        model = self.engine._ask_llm = AsyncMock(return_value='{"intents":[]}')
        async with self.engine.session_state(S) as state:
            await self.engine._smart_chain(state, schedule)
        self.assertTrue(self.persona.text in model.call_args.args[1], "smart intent request lost the full persona")

    async def test_event_settlement_keeps_full_persona_before_recording_an_outcome(self):
        await self.idle()
        self.persona.text = "原来的角色设定。" * 1000 + "核心边界：不喝茶，只能使用指定充电装置恢复。"
        package = EventPackage(title="窗边的小花", hook="想给花添一点水")
        model = self.engine._ask_event = AsyncMock(return_value='{"outcome":"给花浇了水。"}')
        async with self.engine.session_state(S) as state:
            await self.engine._settle_event(state, self.engine.node(state.node_id), package,
                                          default_decision(package), check=CheckResult())
        self.assertTrue(self.persona.text in model.call_args.args[2], "settlement request lost the full persona")
