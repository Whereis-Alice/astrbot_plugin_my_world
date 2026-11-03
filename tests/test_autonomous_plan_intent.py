"""A social planning trigger cannot broadcast or revive an answered message."""
import json
import unittest

from core.engine import MessageContext, TickOutcome
from core.models import ActionDef
from core.speech_policy import contact_targets, reply_target
from tests import test_participation as fixture


class AutonomousPlanIntentTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.ParticipationTests.asyncSetUp
    chat = fixture.ParticipationTests.chat

    async def prepare(self, *, kind="outreach", intent=None, contacts=None, reply="", text="你先关屏吧"):
        self.engine.world.actions.append(ActionDef(
            id="quiet_sit", name="安静坐着", category="continuous", duration=60))
        payload = {"inner": "先把今晚的事情放下。", "reason": "回应之前催睡的话",
            "plan": [{"action": "say", "messages": [text]},
                     {"action": "quiet_sit", "duration": 60}]}
        if intent is not None:
            payload["intent"] = intent
        if contacts is not None:
            payload["contact_people"] = contacts
        if reply:
            payload["reply_to"] = reply
        self.llm.replies = [json.dumps(payload, ensure_ascii=False)]
        outcome = TickOutcome(fixture.GROUP, speech_kind=kind)
        async with self.engine.session_state(fixture.GROUP) as state:
            plan = await self.engine._ask_llm_for_plan(state, self.engine.node(state.node_id),
                outcome, force=True)
            self.assertIsNotNone(plan)
            self.engine._freeze_plan_target(state, outcome, plan, fallback=fixture.GROUP)
            await self.engine._apply_plan(state, self.engine.node(state.node_id), outcome, plan)
        return plan, outcome

    async def run_plan(self):
        outcome = TickOutcome(fixture.GROUP)
        async with self.engine.session_state(fixture.GROUP) as state:
            await self.engine._tick_plan(state, self.engine.node(state.node_id), outcome, depth=0)
        await self.engine._deliver(outcome)
        return outcome

    async def person(self, uid="alice", name="Alice"):
        self.engine.profiles.touch(fixture.GROUP, uid, name)
        self.engine.profiles.note_bond(fixture.GROUP, uid, type="朋友", asserted_by="测试熟人")
        self.engine.profiles.adjust_affinity(fixture.GROUP, uid, 40, reason="测试熟人", now=self.clock.now())
        async with self.engine.session_state(fixture.GROUP) as state:
            state.user_presence[uid] = {"name": name, "last_seen": self.clock.now()}

    async def test_unannotated_desire_plan_keeps_life_without_broadcast(self):
        await self.person()
        plan, _ = await self.prepare()
        self.assertEqual([(s["action"], s["kind"]) for s in plan["steps"]], [("quiet_sit", "quiet")])
        await self.run_plan()
        self.assertFalse(self.messenger.sent)
        state = await self.engine.load_state(fixture.GROUP, cold_start=False)
        self.assertEqual(state.current_action["type"], "quiet_sit")
        self.assertEqual(self.engine.contact_status(fixture.GROUP)["left"], 6)
        self.assertIn("开口必须明确选择", self.llm.calls[0]["prompt"])

    async def test_unannotated_normal_plan_cannot_borrow_life_share(self):
        plan, _ = await self.prepare(kind="life_share")
        self.assertEqual(plan["speech_choice"]["intent"], "listen")
        await self.run_plan()
        self.assertFalse(self.messenger.sent)

    async def test_screenshot_payload_does_not_replay_hours_old_sleep_prompt(self):
        await self.person()
        await self.chat("你俩赶紧把眼睛闭上拔网线睡觉", uid="alice", message_id="sleep-source", age=10800)
        self.engine.world.actions.append(ActionDef(id="bedroom_charge_on_seat", name="坐上充电座椅充电",
            category="continuous", duration=1500))
        self.engine.desire_push_ready = lambda *args: True
        self.llm.replies = [json.dumps({"plan": [
            {"action": "say", "messages": ["椅子那头我按灭了 线还得留着过夜", "你先关 我这边不用盯着"]},
            {"action": "bedroom_charge_on_seat", "duration": 1500}],
            "inner": "灯灭了 眼睛也闭了 就让电流慢慢走一会儿",
            "reason": "被点名催睡，回一句解释椅子不是屏幕，然后自己安静充一会儿电，不再多话。"}, ensure_ascii=False)]
        outcome = TickOutcome(fixture.GROUP)
        async with self.engine.session_state(fixture.GROUP) as state:
            state.desire = 1.0
            self.assertTrue(await self.engine._maybe_desire_push(
                state, self.engine.node(state.node_id), outcome, fixture.GROUP))
            self.assertEqual([(s["action"], s["kind"]) for s in state.current_plan["steps"]],
                [("bedroom_charge_on_seat", "quiet")])
        await self.run_plan()
        state = await self.engine.load_state(fixture.GROUP, cold_start=False)
        self.assertEqual(state.current_action["type"], "bedroom_charge_on_seat")
        self.assertFalse(self.messenger.sent)
        self.assertEqual(len(self.llm.calls), 1)

    async def test_explicit_outreach_without_person_keeps_quiet_activity(self):
        await self.person()
        plan, outcome = await self.prepare(intent="outreach")
        self.assertFalse(plan["contact_people"])
        self.assertTrue(any("具体对象" in note for note in outcome.notes))
        await self.run_plan()
        self.assertFalse(self.messenger.sent)

    async def test_real_contact_persists_recipient_and_unanswered_backoff(self):
        await self.person()
        plan, _ = await self.prepare(intent="outreach", contacts=["alice"], text="Alice，那本书我找到了")
        self.assertEqual(plan["contact_people"], ["alice"])
        wire = []

        async def send(sid, lines):
            wire.append((list(lines), contact_targets(sid)))
            return True

        self.messenger.send_text = send
        await self.run_plan()
        self.assertEqual(wire, [(["Alice，那本书我找到了"], ("alice",))])
        self.clock.advance(1800)
        self.assertIn("还没有回应", self.engine.contact_status(fixture.GROUP, "alice")["reason"])
        ledger = self.db.raw.kv_get("outreach:" + self.engine.state_key(fixture.GROUP), {})
        self.assertEqual(next(iter(ledger["people"].values()))["user_id"], "alice")
        async with self.engine.session_state(fixture.GROUP) as state:
            state.current_action = None
            state.current_plan = None
        retry, _ = await self.prepare(intent="outreach", contacts=["alice"], text="Alice，刚才那本书")
        self.assertEqual(retry["speech_choice"]["intent"], "listen")
        await self.run_plan()
        self.assertEqual(len(wire), 1)
        await self.engine.note_presence(MessageContext(fixture.GROUP, user_id="alice", text="看到了，谢谢",
            is_mentioned=True))
        self.assertFalse(self.engine.contact_status(fixture.GROUP, "alice")["reason"])

    async def test_quoted_current_message_owns_target_instead_of_contact_candidate(self):
        await self.person("111", "Alice")
        await self.chat("这个书签很好看", uid="222", message_id="bob-source")
        plan, _ = await self.prepare(intent="continue_chat", reply="c1", text="边上那道金色很合适")
        self.assertFalse(plan["contact_people"])
        self.assertEqual(plan["speech_scene"]["reply_to"], "bob-source")
        wire = []

        async def send(sid, lines):
            wire.append((contact_targets(sid), reply_target(sid)))
            return True

        self.messenger.send_text = send
        await self.run_plan()
        self.assertEqual(wire, [((), "bob-source")])

    async def test_answered_sleep_prompt_cannot_become_new_continuation(self):
        row = await self.chat("闭眼拔网线睡觉", uid="bob", message_id="old-sleep")
        async with self.engine.session_state(fixture.GROUP) as state:
            self.engine._set_watermark(state, fixture.GROUP, until=row["at"], seq=row["seq"])
        plan, _ = await self.prepare(intent="continue_chat", reply="c1")
        self.assertEqual(plan["speech_choice"]["intent"], "listen")
        self.assertNotIn("speech_scene", plan)
        await self.run_plan()
        self.assertFalse(self.messenger.sent)

    async def test_old_sleep_message_outside_live_window_is_not_a_reply_capability(self):
        await self.chat("闭眼拔网线睡觉", uid="bob", message_id="old-sleep", age=3600)
        plan, _ = await self.prepare(intent="continue_chat", reply="c1")
        self.assertEqual(plan["speech_choice"]["intent"], "listen")
        await self.run_plan()
        self.assertFalse(self.messenger.sent)

    async def test_explicit_life_fact_remains_shareable(self):
        plan, _ = await self.prepare(intent="life_share", text="书签刚夹好")
        self.assertEqual(plan["steps"][0]["kind"], "life_share")
        await self.run_plan()
        self.assertEqual(self.messenger.sent, [(fixture.GROUP, ["书签刚夹好"])])

    async def test_empty_group_contact_cannot_send_even_when_reserved(self):
        outcome = TickOutcome(fixture.GROUP, speech_kind="outreach")
        self.assertFalse(self.engine.contact_speech_allowed(outcome, fixture.GROUP, "outreach", reserved=True))
        receipt, error = self.engine._reserve_outreach(outcome, fixture.GROUP, ["outreach"])
        self.assertIsNone(receipt)
        self.assertIn("没有明确对象", error)

    async def test_user_reply_and_private_contact_keep_existing_permission(self):
        outcome = TickOutcome(fixture.GROUP)
        self.assertTrue(self.engine.contact_speech_allowed(outcome, fixture.GROUP, "reply", reserved=True))
        private = TickOutcome(fixture.PRIVATE)
        self.assertTrue(self.engine.contact_speech_allowed(private, fixture.PRIVATE, "outreach", reserved=True))

    async def test_blank_recipient_is_empty_contact(self):
        outcome = TickOutcome(fixture.GROUP, contact_people={"", " "})
        self.assertFalse(self.engine.contact_speech_allowed(outcome, fixture.GROUP, "outreach", reserved=True))

    async def test_empty_old_contact_draft_neither_sends_nor_consumes_contact(self):
        outcome = TickOutcome(fixture.GROUP)
        outcome.add_speech("你先关屏吧", kind="outreach")
        await self.engine._deliver(outcome)
        self.assertFalse(self.messenger.sent)
        self.assertEqual(self.engine.contact_status(fixture.GROUP)["left"], 6)

    async def test_requested_scheduled_and_event_plans_keep_original_protocol(self):
        for kind in ("reply", "schedule", "event"):
            with self.subTest(kind=kind):
                self.messenger.sent.clear()
                async with self.engine.session_state(fixture.GROUP) as state:
                    state.current_action = None
                    state.current_plan = None
                plan, _ = await self.prepare(kind=kind)
                self.assertNotIn("speech_choice", plan)
                self.assertEqual(plan["steps"][0]["kind"], kind)
                self.assertNotIn("开口必须明确选择", self.llm.calls[-1]["prompt"])
