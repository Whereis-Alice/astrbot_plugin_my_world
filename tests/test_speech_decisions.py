"""Private thoughts, source permissions and platform receipts use one identity."""
import json
import asyncio
import unittest
from unittest.mock import AsyncMock

from core.engine import TickOutcome
from core.speech_decision import SpeechDecision, parse_decision
from tests import test_participation as participation

GROUP, OTHER = participation.GROUP, participation.OTHER


class SpeechDecisionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = participation.ParticipationTests.asyncSetUp
    chat = participation.ParticipationTests.chat

    async def test_format_repair_keeps_tool_material_without_replaying_tools(self):
        from core.speech_runtime import SpeechDraft, SPEECH_REQUEST
        await self.chat(message_id='material-source')
        requests = []

        async def generate(*args, **kwargs):
            requests.append(SPEECH_REQUEST.get()['allow_tools'])
            if len(requests) == 1:
                return SpeechDraft('{"say":"看到了。"}', images=['/synthetic/image.png'],
                                   tools=[{'name': 'lookup', 'status': 'completed'}])
            return '{"inner":"终于看清了。","say":"看到了。"}'

        self.engine._ask_llm = generate
        outcome = TickOutcome(GROUP)
        async with self.engine.session_state(GROUP) as state:
            await self.engine.participation_text(state, GROUP, outcome=outcome)
            self.assertEqual(requests, [True, False])
            self.assertEqual(state.thoughts[-1]['detail']['speech']['tools'][0]['name'], 'lookup')
            self.assertIn('/synthetic/image.png', outcome.images)

    async def test_listening_does_not_release_a_tools_buffered_image(self):
        from core.speech_runtime import SpeechDraft
        await self.chat()
        self.engine._ask_llm = AsyncMock(return_value=SpeechDraft(
            '{"inner":"先记在心里。","intent":"listen","say":""}', images=['/synthetic/image.png']))
        outcome = TickOutcome(GROUP)
        async with self.engine.session_state(GROUP) as state:
            await self.engine.participation_text(state, GROUP, outcome=outcome)
            self.assertTrue(state.thoughts)
        self.assertFalse(outcome.routed_images)
        self.assertFalse(outcome.images)

    async def test_join_and_silence_both_keep_one_private_thought(self):
        for say, status in [("风车很好看。", "sent"), ("", "listening")]:
            self.llm.replies = [json.dumps({"inner": "有点想看看山上的风。", "say": say})]
            await self.chat(message_id="one-" + status)
            outcome = await self.engine.maybe_participate(GROUP)
            thought = (await self.engine.thoughts_payload(GROUP))["thoughts"][0]
            self.assertEqual(thought["content"], "有点想看看山上的风。")
            self.assertEqual(thought["detail"]["speech"]["status"], status)
            self.assertEqual(thought["reply"], say)
            self.assertEqual(outcome.speech_decisions[0]["id"], thought["decision_id"])
            self.clock.advance(181)
        self.assertEqual(len(self.messenger.sent), 1)
        self.assertNotIn("有点想", self.messenger.sent[0][1][0])

    async def test_missing_thought_repairs_once_and_never_fabricates(self):
        self.llm.replies = ['{"say":"缺少想法"}', '{"say":"仍然缺少"}']
        await self.chat()
        await self.engine.maybe_participate(GROUP)
        self.assertEqual(len(self.llm.calls), 2)
        self.assertFalse(self.messenger.sent)
        self.assertFalse((await self.engine.thoughts_payload(GROUP))["thoughts"])

    async def test_repair_can_succeed_without_sending_the_first_draft(self):
        self.llm.replies = ['{"say":"第一稿"}', '{"inner":"有点期待。","say":"一起看看吧。"}']
        await self.chat()
        await self.engine.maybe_participate(GROUP)
        self.assertEqual(self.messenger.sent, [(GROUP, ["一起看看吧。"])])

    async def test_expired_or_failed_send_does_not_become_a_sent_memory(self):
        self.messenger.send_text = AsyncMock(return_value=False)
        await self.chat()
        await self.engine.maybe_participate(GROUP)
        thought = (await self.engine.thoughts_payload(GROUP))["thoughts"][0]
        self.assertEqual(thought["detail"]["speech"]["status"], "not_sent")
        self.assertFalse(thought["reply"])

    async def test_partial_receipt_and_same_text_have_exact_ownership(self):
        outcome = TickOutcome(GROUP, speech_kind="life_share")
        async with self.engine.session_state(GROUP) as state:
            for drafts in [["第一句", "第二句"], ["第一句"]]:
                await self.engine.remember_speech_decision(state, GROUP,
                    SpeechDecision(inner="风吹得人很清醒。", say="\n".join(drafts), intent="life_share"),
                    trigger="completion", outcome=outcome, drafts=drafts)
        outcome.delivered_messages = {GROUP: ["第一句"]}
        outcome.delivered_decisions = {outcome.speech_decisions[0]["id"]: ["第一句"]}
        await self.engine.finish_speech_decisions(outcome)
        self.assertEqual([d["status"] for d in outcome.speech_decisions], ["partial", "not_sent"])
        self.assertEqual(outcome.speech_decisions[0]["sent"], ["第一句"])

    async def test_life_disabled_can_continue_real_chat_but_not_broadcast(self):
        self.engine.world.life_share_session_whitelist = []
        await self.chat("那张照片我想再看看", message_id="real-message")
        self.llm.replies = ['{"inner":"他还惦记着那张照片。","intent":"continue_chat","reply_to":"c1","actions":[{"type":"say","messages":["等下翻给你看。"]}]}']
        outcome = TickOutcome(GROUP, speech_kind="life_share")
        async with self.engine.session_state(GROUP) as state:
            await self.engine._llm_followup(state, self.engine.node(state.node_id), outcome, "刚整理好书签", "三枚书签归位了。")
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.sent, [(GROUP, ["等下翻给你看。"])])
        self.assertEqual(self.engine.contact_status(GROUP)["left"], 6)
        self.assertEqual(self.engine.life_share_status(GROUP)["left"], 6)
        self.assertEqual(outcome.speech_kinds[""], ["continuation"])

    async def test_cancelled_split_send_keeps_the_actual_receipt_and_thought(self):
        outcome = TickOutcome(GROUP, speech_kind="life_share")
        async with self.engine.session_state(GROUP) as state:
            await self.engine.remember_speech_decision(state, GROUP,
                SpeechDecision(inner="书签收好了。", say="第一句\n第二句", intent="life_share"),
                trigger="completion", outcome=outcome, drafts=["第一句", "第二句"])
        for line in ("第一句", "第二句"):
            outcome.add_speech(line)
        self.messenger.send_text = AsyncMock(side_effect=asyncio.CancelledError())
        self.messenger.take_sent_texts = lambda _: ["第一句"]
        with self.assertRaises(asyncio.CancelledError):
            await self.engine._deliver(outcome)
        thought = (await self.engine.thoughts_payload(GROUP))["thoughts"][0]
        self.assertEqual(thought["reply"], "第一句")
        self.assertEqual(thought["detail"]["speech"]["status"], "partial")
        self.assertEqual(self.engine.life_share_status(GROUP)["left"], 5)

    async def test_identical_drafts_do_not_transfer_a_receipt_to_a_blocked_decision(self):
        self.engine.world.life_share_daily_max = 0
        row = await self.chat(message_id="question")
        outcome = TickOutcome(GROUP)
        async with self.engine.session_state(GROUP) as state:
            for kind in ("life_share", "participation"):
                await self.engine.remember_speech_decision(state, GROUP,
                    SpeechDecision(inner="有一点想回应。", say="好呀。", intent=kind),
                    trigger="message", outcome=outcome)
                outcome.add_speech("好呀。", kind=kind)
        self.engine.claim_chat_scene(outcome, GROUP, [row])
        await self.engine._deliver(outcome)
        self.assertEqual([d["status"] for d in outcome.speech_decisions], ["not_sent", "sent"])

    async def test_continuation_cannot_quote_an_answered_or_other_session_message(self):
        await self.chat(message_id="old")
        await self.chat(sid=OTHER, message_id="elsewhere")
        async with self.engine.session_state(GROUP) as state:
            self.engine.mark_chat_replied(state, GROUP)
            allowed, _, targets = self.engine.expression_options(state, GROUP, "life_share")
        self.assertNotIn("continue_chat", allowed)
        self.assertFalse(targets)
        self.assertTrue(parse_decision('{"inner":"我想接话。","intent":"continue_chat","say":"好呀","reply_to":"elsewhere"}',
            targets, allowed=("continue_chat",)).error)

    async def test_life_and_outreach_ledgers_are_independent(self):
        self.engine.world.life_share_daily_max = 1
        self.engine.world.life_share_min_interval_seconds = 0
        await self.engine._deliver(TickOutcome(GROUP, messages=["生活分享"], speech_kind="life_share"))
        self.assertEqual(self.engine.life_share_status(GROUP)["left"], 0)
        self.assertEqual(self.engine.contact_status(GROUP)["left"], 6)
        self.clock.advance(100)
        await self.engine._deliver(TickOutcome(GROUP, messages=["想找你聊聊照片"], speech_kind="outreach"))
        self.assertEqual(self.engine.contact_status(GROUP)["left"], 5)
        self.assertEqual(self.engine.life_share_status(GROUP)["left"], 0)

    async def test_autonomous_poke_is_a_contact_not_a_life_share(self):
        from core.json_actions import PlannedAction
        self.engine.world.life_share_session_whitelist = []
        self.messenger.poke = AsyncMock(return_value=True)
        outcome = TickOutcome(GROUP, speech_kind="life_share")
        async with self.engine.session_state(GROUP) as state:
            await self.engine._run_poke(state, self.engine.node(state.node_id), outcome,
                self.engine.world.action_map()["poke"], PlannedAction(type="poke", target="alice"))
        self.messenger.poke.assert_awaited_once()
        self.assertEqual(self.engine.contact_status(GROUP)["left"], 5)
        self.assertEqual(self.engine.life_share_status(GROUP)["left"], 6)
        self.assertEqual(self.engine.contact_status(GROUP, "alice")["person_left"], 1)

    async def test_life_reservation_rolls_back_and_counts_cross_midnight_once(self):
        outcome = TickOutcome(GROUP, speech_kind="life_share")
        receipt, error = self.engine._reserve_contact(outcome, GROUP, ["life_share"])
        self.assertFalse(error)
        self.assertTrue(self.engine._reserve_contact(TickOutcome(GROUP), GROUP, ["life_share"])[1])
        self.engine._finish_contact(receipt, False)
        self.assertEqual(self.engine.life_share_status(GROUP)["left"], 6)
        receipt, _ = self.engine._reserve_contact(outcome, GROUP, ["life_share"])
        self.clock.advance(86400)
        self.engine._finish_contact(receipt, {"life_share"})
        self.assertEqual(self.engine.life_share_status(GROUP)["left"], 5)
        self.assertFalse(self.engine._contact_pending)

    async def test_blocked_life_share_does_not_swallow_simultaneous_chat(self):
        self.engine.world.life_share_daily_max = 0
        row = await self.chat(message_id="question")
        outcome = TickOutcome(GROUP)
        outcome.add_speech("生活分享", kind="life_share")
        outcome.add_speech("回答眼前的话题", kind="participation")
        self.engine.claim_chat_scene(outcome, GROUP, [row])
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.sent, [(GROUP, ["回答眼前的话题"])])
