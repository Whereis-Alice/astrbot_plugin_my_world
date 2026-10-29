"""A miss-triggered plan chooses its addressee independently of the feeling."""
import json
import unittest
from unittest.mock import AsyncMock

from core.engine import TickOutcome
from core.models import ActionDef
from core.speech_policy import contact_targets, reply_target
from tests import test_participation as fixture


class MissAddressingTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.ParticipationTests.asyncSetUp
    chat = fixture.ParticipationTests.chat

    async def prepare(self, payload):
        async with self.engine.session_state(fixture.GROUP) as state:
            state.recent_chat = []
        await self.chat("TV D", uid="222", message_id="tv-d")
        self.engine.profiles.touch(fixture.GROUP, "111", "特别想念的人")
        self.engine._top_miss_candidate = lambda *args, **kwargs: ("111", .9)
        if "quiet_sit" not in self.engine.world.action_map():
            self.engine.world.actions.append(ActionDef(id="quiet_sit", name="安静坐着", category="continuous", duration=60))
        self.llm.replies = [json.dumps(payload, ensure_ascii=False)]
        self.wire = []

        async def send(sid, lines):
            self.wire.append((sid, list(lines), contact_targets(sid), reply_target(sid)))
            self.messenger.sent.append((sid, list(lines)))
            return True

        self.messenger.send_text = send
        outcome = TickOutcome(fixture.GROUP)
        async with self.engine.session_state(fixture.GROUP) as state:
            result = await self.engine._maybe_miss_push(state, self.engine.node(state.node_id), outcome, fixture.GROUP)
        return result, outcome

    def payload(self, intent="continue_chat", reply="c1", **extra):
        return {"inner": "本来有点想他，看到这句忍不住想吐槽。", "intent": intent,
                "reply_to": reply, "reason": "想接 TV D 这句", "plan": [
                    {"action": "say", "messages": ["TV版天天网购魔兽能不坏吗"]}], **extra}

    async def test_real_miss_path_replies_to_tv_source_without_pinging_missed_person(self):
        result, outcome = await self.prepare(self.payload())
        self.assertTrue(result)
        await self.engine._deliver(outcome)
        self.assertEqual(self.wire, [(fixture.GROUP, ["TV版天天网购魔兽能不坏吗"], (), "tv-d")])
        self.assertEqual(outcome.contact_people, set())
        self.assertEqual(outcome.speech_decisions[0]["intent"], "continue_chat")
        self.assertEqual(outcome.speech_decisions[0]["reply_to"], "tv-d")
        self.assertEqual(self.engine.contact_status(fixture.GROUP)["left"], 6)
        self.assertEqual(self.engine.contact_status(fixture.GROUP, "111")["person_left"], 2)
        prompt = self.llm.calls[0]["prompt"]
        self.assertIn('"who": "222"', prompt)
        self.assertIn('"user_id": "222"', prompt)
        self.assertIn("想念候选不是群聊接话的对象", prompt)

    async def test_explicit_personal_outreach_still_mentions_and_counts_person(self):
        payload = self.payload("outreach", "")
        payload["plan"][0]["messages"] = ["想起你前几天说的那本书，读完了吗？"]
        _, outcome = await self.prepare(payload)
        await self.engine._deliver(outcome)
        self.assertEqual(self.wire[0][2:], (("111",), ""))
        self.assertEqual(self.engine.contact_status(fixture.GROUP)["left"], 5)
        self.assertEqual(self.engine.contact_status(fixture.GROUP, "111")["person_left"], 1)

    async def test_listen_or_legacy_plan_does_not_inherit_a_contact_mention(self):
        for intent in ("listen", None):
            with self.subTest(intent=intent):
                payload = self.payload(intent, "")
                payload["plan"].append({"action": "quiet_sit", "duration": 60})
                _, outcome = await self.prepare(payload)
                await self.engine._deliver(outcome)
                self.assertEqual(self.wire, [])
                self.assertFalse(outcome.contact_people)
                state = await self.engine.load_state(fixture.GROUP, cold_start=False)
                self.assertEqual(state.current_action["type"], "quiet_sit")
                self.assertEqual(state.current_action["speech_kind"], "quiet")
                async with self.engine.session_state(fixture.GROUP) as state:
                    state.current_action = None
                    state.current_plan = None

    async def test_invalid_or_missing_source_cannot_become_personal_outreach(self):
        for ref in ("", "tv-d", "elsewhere", "c99"):
            with self.subTest(ref=ref):
                _, outcome = await self.prepare(self.payload(reply=ref))
                await self.engine._deliver(outcome)
                self.assertFalse(self.wire)
                self.assertFalse(outcome.contact_people)

    async def test_private_outreach_uses_its_actual_recipient(self):
        private = "aiocqhttp:FriendMessage:111"
        self.store = self.engine.store
        self.store.add_session(private, session_type="private", platform="aiocqhttp")
        self.engine.reload_config()
        self.engine.world.proactive_session_whitelist.append(private)
        # A standalone world cannot invent a destination outside its life group.
        self.engine.resolve_send_to = lambda state, raw, **kwargs: private if raw == private else fixture.GROUP
        _, outcome = await self.prepare(self.payload("outreach", "", send_to=private))
        await self.engine._deliver(outcome)
        self.assertEqual(self.wire[0][0], private)
        self.assertEqual(self.engine.contact_status(private, "111")["person_left"], 1)

    async def test_chat_choice_cannot_export_a_source_to_another_room(self):
        payload = self.payload(send_to=fixture.OTHER)
        payload["plan"][0]["send_to"] = fixture.OTHER
        _, outcome = await self.prepare(payload)
        await self.engine._deliver(outcome)
        self.assertEqual(self.wire[0][0], fixture.GROUP)
        self.assertEqual(self.wire[0][3], "tv-d")

    async def test_chat_source_expiry_and_real_reply_cancel_pending_public_text(self):
        _, outcome = await self.prepare(self.payload())
        self.clock.advance(901)
        await self.engine._deliver(outcome)
        self.assertFalse(self.wire)
        self.assertEqual(outcome.speech_decisions[0]["status"], "not_sent")
        self.clock.advance(1)
        _, outcome = await self.prepare(self.payload())
        async with self.engine.session_state(fixture.GROUP) as state:
            self.engine.mark_chat_replied(state, fixture.GROUP)
        await self.engine._deliver(outcome)
        self.assertFalse(self.wire)

    async def test_reloaded_queued_chat_keeps_selected_source_and_no_contact(self):
        payload = self.payload()
        payload["plan"].insert(0, {"action": "quiet_sit", "duration": 60})
        _, first = await self.prepare(payload)
        self.assertFalse(first.messages)
        async with self.engine.session_state(fixture.GROUP) as state:
            # Simulate unloading after an independent activity and loading the
            # persisted plan into a new tick with no in-memory outcome fields.
            state.current_action = None
            state.current_plan["current_step"] = 1
        fresh = TickOutcome(fixture.GROUP)
        async with self.engine.session_state(fixture.GROUP) as state:
            await self.engine._tick_plan(state, self.engine.node(state.node_id), fresh, depth=0)
        await self.engine._deliver(fresh)
        self.assertEqual(self.wire[0][2:], ((), "tv-d"))
        self.assertFalse(fresh.contact_people)

    async def test_saved_source_evicted_from_archive_still_owns_sender(self):
        payload = self.payload()
        payload["plan"].insert(0, {"action": "quiet_sit", "duration": 60})
        await self.prepare(payload)
        async with self.engine.session_state(fixture.GROUP) as state:
            state.current_action = None
            state.current_plan["current_step"] = 1
            state.recent_chat = []
        await self.chat("后来另一个人的消息", uid="333", message_id="newer")
        fresh = TickOutcome(fixture.GROUP)
        async with self.engine.session_state(fixture.GROUP) as state:
            await self.engine._tick_plan(state, self.engine.node(state.node_id), fresh, depth=0)
        source = next(row for row in fresh.chat_media_scenes[fixture.GROUP]["rows"] if row["message_id"] == "tv-d")
        self.assertEqual(source["user_id"], "222")
        await self.engine._deliver(fresh)
        self.assertEqual(self.wire[0][2:], ((), "tv-d"))

    async def test_outreach_to_another_private_person_is_not_sent(self):
        private = "aiocqhttp:FriendMessage:other-person"
        self.engine.resolve_send_to = lambda state, raw, **kwargs: private if raw == private else fixture.GROUP
        payload = self.payload("outreach", "", send_to=private)
        _, outcome = await self.prepare(payload)
        await self.engine._deliver(outcome)
        self.assertFalse(self.wire)
        self.assertFalse(outcome.contact_people)

    async def test_conflicting_outreach_and_group_reference_does_not_choose_a_person(self):
        _, outcome = await self.prepare(self.payload("outreach", "c1"))
        await self.engine._deliver(outcome)
        self.assertFalse(self.wire)
        self.assertFalse(outcome.contact_people)
        self.assertTrue(any("目标冲突" in note for note in outcome.notes))

    async def test_listen_does_not_execute_tool_or_user_targeted_actions(self):
        payload = self.payload("listen", "")
        payload["plan"] = [{"action": "poke", "target": "111"},
                           {"action": "search_web", "queries": ["TV D"]}]
        self.messenger.poke = AsyncMock(return_value=True)
        _, outcome = await self.prepare(payload)
        await self.engine._deliver(outcome)
        self.messenger.poke.assert_not_awaited()
        self.assertEqual(len(self.llm.calls), 1)
        self.assertFalse(self.wire)
