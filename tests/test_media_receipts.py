"""Actual images and pending image comments must obey delivery boundaries."""
import unittest
import asyncio
from unittest.mock import AsyncMock

from core.engine import TickOutcome
from core.speech_decision import SpeechDecision
from core.speech_runtime import SpeechDraft
from tests import test_participation as participation

GROUP = participation.GROUP


class MediaReceiptTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = participation.ParticipationTests.asyncSetUp
    chat = participation.ParticipationTests.chat

    async def image_draft(self, *, target=GROUP, images=None, drafts=None, kind="life_share"):
        images = images or ["https://example.com/photo.png"]
        outcome = TickOutcome(GROUP, speech_kind=kind)
        async with self.engine.session_state(GROUP) as state:
            await self.engine.remember_speech_decision(state, target,
                SpeechDecision(inner="把照片给他看吧。", say="拍好了。", intent="life_share"),
                trigger="completion", outcome=outcome, drafts=drafts or [],
                raw=SpeechDraft("", images=images))
        for line in drafts or []:
            outcome.add_speech(line, target)
        return outcome

    async def test_text_and_images_both_accepted_are_one_complete_decision(self):
        outcome = await self.image_draft(drafts=["拍好了。"])
        await self.engine._deliver(outcome)
        speech = (await self.engine.thoughts_payload(GROUP))["thoughts"][0]["detail"]["speech"]
        self.assertEqual(speech["status"], "sent")
        self.assertEqual(speech["sent"], ["拍好了。"])
        self.assertEqual(speech["sent_images"], ["https://example.com/photo.png"])
        self.assertEqual(self.engine.life_share_status(GROUP)["left"], 5)

    async def test_completion_chat_image_uses_chat_permission_when_life_share_is_disabled(self):
        self.engine.world.life_share_session_whitelist = []
        await self.chat("那张风车照片给我看看。", message_id="photo-question")
        self.engine._ask_llm = AsyncMock(return_value=SpeechDraft(
            '{"reasoning":{"inner":"他还惦记着照片。"},"intent":"continue_chat","reply_to":"c1",'
            '"actions":[{"type":"say","messages":["给你看看。"]}]}',
            images=["https://example.com/windmill.png"]))
        outcome = TickOutcome(GROUP, speech_kind="life_share")
        async with self.engine.session_state(GROUP) as state:
            await self.engine._llm_followup(state, self.engine.node(state.node_id), outcome,
                "刚整理好照片", "风车的照片归档了。")
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.sent, [(GROUP, ["给你看看。"])])
        self.assertEqual(self.messenger.sent_images, [(GROUP, ["https://example.com/windmill.png"])])
        self.assertEqual(outcome.speech_decisions[0]["status"], "sent")

    async def test_rejected_image_does_not_remove_accepted_text(self):
        outcome = await self.image_draft(drafts=["拍好了。"])
        self.messenger.send_images = AsyncMock(return_value=False)
        await self.engine._deliver(outcome)
        thought = (await self.engine.thoughts_payload(GROUP))["thoughts"][0]
        self.assertEqual(thought["detail"]["speech"]["status"], "partial")
        self.assertEqual(thought["reply"], "拍好了。")
        state = await self.engine.load_state(GROUP, cold_start=False)
        self.assertFalse(any(r.get("has_images") for r in state.recent_chat))

    async def test_filtered_image_receipts_only_remember_the_accepted_subset(self):
        refs = ["https://example.com/one.png", "https://example.com/two.png"]
        outcome = await self.image_draft(images=refs)
        self.messenger.take_sent_images = lambda _: refs[1:]
        await self.engine._deliver(outcome)
        speech = (await self.engine.thoughts_payload(GROUP))["thoughts"][0]["detail"]["speech"]
        self.assertEqual(speech["status"], "partial")
        self.assertEqual(speech["sent_images"], refs[1:])
        state = await self.engine.load_state(GROUP, cold_start=False)
        self.assertEqual(state.recent_chat[-1]["images"], refs[1:])

    async def test_cancelled_image_send_keeps_an_accepted_group(self):
        outcome = await self.image_draft()
        self.messenger.send_images = AsyncMock(side_effect=asyncio.CancelledError())
        self.messenger.take_sent_images = lambda _: ["https://example.com/photo.png"]
        with self.assertRaises(asyncio.CancelledError):
            await self.engine._deliver(outcome)
        thought = (await self.engine.thoughts_payload(GROUP))["thoughts"][0]
        self.assertEqual(thought["detail"]["speech"]["status"], "sent")
        self.assertFalse(thought["reply"])
        self.assertEqual(self.engine.life_share_status(GROUP)["left"], 5)

    async def test_transient_storage_error_does_not_lose_or_resend_an_accepted_image(self):
        outcome = await self.image_draft()
        save = self.engine.save_state
        failed = False

        async def fail_once(state):
            nonlocal failed
            if not failed:
                failed = True
                raise OSError("temporary storage error")
            await save(state)

        self.engine.save_state = fail_once
        await self.engine._deliver(outcome)
        self.assertEqual(len(self.messenger.sent_images), 1)
        self.assertEqual(self.engine.life_share_status(GROUP)["left"], 5)
        state = await self.engine.load_state(GROUP, cold_start=False)
        self.assertEqual(len([r for r in state.recent_chat if r.get("is_self")]), 1)
        self.assertEqual(outcome.speech_decisions[0]["status"], "sent")

    async def test_generated_payload_is_not_persisted_in_thoughts(self):
        payload = "base64://" + "a" * 20000
        outcome = await self.image_draft(images=[payload])
        await self.engine._deliver(outcome)
        state = await self.engine.load_state(GROUP, cold_start=False)
        self.assertNotIn(payload, str(state.thoughts))
        self.assertNotIn(payload, str(state.recent_chat))
        self.assertTrue(state.recent_chat[-1]["has_images"])
        self.assertEqual(state.recent_chat[-1]["image_count"], 1)
        speech = (await self.engine.thoughts_payload(GROUP))["thoughts"][0]["detail"]["speech"]
        self.assertEqual(speech["status"], "sent")
        self.assertTrue(speech["sent_images"][0].startswith("sha256:"))

    async def test_identical_image_does_not_transfer_to_another_decision(self):
        outcome = await self.image_draft()
        async with self.engine.session_state(GROUP) as state:
            await self.engine.remember_speech_decision(state, GROUP,
                SpeechDecision(inner="还是想给他看。", say="这一张。", intent="life_share"),
                trigger="completion", outcome=outcome, drafts=[],
                raw=SpeechDraft("", images=["https://example.com/photo.png"]))
        await self.engine._deliver(outcome)
        self.assertEqual([r["status"] for r in outcome.speech_decisions], ["sent", "not_sent"])
        self.assertEqual(len(self.messenger.sent_images), 1)

    async def test_tool_attachment_is_not_an_own_message_before_acceptance(self):
        outcome = TickOutcome(GROUP, speech_kind="life_share")
        self.engine._attach_images(outcome, ["https://example.com/tool.png"],
            session_id=GROUP, label="拍摄风车")
        state = await self.engine.load_state(GROUP, cold_start=False)
        self.assertFalse(any(r.get("is_self") for r in state.recent_chat))
        self.messenger.send_images = AsyncMock(return_value=False)
        await self.engine._deliver(outcome)
        state = await self.engine.load_state(GROUP, cold_start=False)
        self.assertFalse(any(r.get("is_self") for r in state.recent_chat))

    async def test_routed_image_only_records_the_actual_destination(self):
        sessions = self.engine.store.raw_sessions()
        sessions["groups"] = [{"id": "shared", "name": "同一个她", "sessions": [GROUP, participation.OTHER], "main_session": GROUP}]
        self.engine.store.save_sessions(sessions)
        self.engine.reload_config()
        outcome = await self.image_draft(target=participation.OTHER)
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.sent_images, [(participation.OTHER, ["https://example.com/photo.png"])])
        state = await self.engine.load_state(GROUP, cold_start=False)
        own = [r for r in state.recent_chat if r.get("is_self")]
        self.assertEqual([r["origin"] for r in own], [participation.OTHER])
        self.assertFalse((await self.engine.thoughts_payload(GROUP))["thoughts"])
        speech = (await self.engine.thoughts_payload(participation.OTHER))["thoughts"][0]["detail"]["speech"]
        self.assertEqual(speech["status"], "sent")

    async def test_accepted_image_without_text_is_partial_and_known(self):
        await self.chat(message_id="image-request")
        self.engine._ask_llm = AsyncMock(return_value=SpeechDraft(
            '{"inner":"把照片给他看吧。","intent":"join_chat","say":"拍到的风车给你看。"}',
            images=["https://example.com/windmill.png"]))
        self.messenger.send_text = AsyncMock(return_value=False)
        outcome = await self.engine.maybe_participate(GROUP)
        self.assertEqual(self.messenger.sent_images, [(GROUP, ["https://example.com/windmill.png"])])
        thought = (await self.engine.thoughts_payload(GROUP))["thoughts"][0]
        self.assertEqual(thought["detail"]["speech"]["status"], "partial")
        self.assertEqual(thought["detail"]["speech"]["sent_images"], ["https://example.com/windmill.png"])
        self.assertFalse(thought["reply"])
        state = await self.engine.load_state(GROUP, cold_start=False)
        self.assertEqual(state.participation[GROUP]["decision"], "joined")
        own = [r for r in state.recent_chat if r.get("is_self")]
        self.assertEqual(len(own), 1)
        self.assertEqual(own[0]["images"], ["https://example.com/windmill.png"])
        self.assertEqual(own[0]["speech_turn"], outcome.speech_receipt_batch)
        self.assertNotIn("拍到的风车给你看。", str(own))

    async def test_queued_image_comment_rechecks_policy_before_send(self):
        raw = self.engine.store.raw_world()
        raw.setdefault("decider", {})["media_only_participation"] = "normal"
        self.engine.store.save_world(raw)
        self.engine.reload_config()
        row = await self.chat("［图片］", raw_text="", has_images=True, message_id="pending-image")
        outcome = TickOutcome(GROUP, speech_kind="participation")
        async with self.engine.session_state(GROUP) as state:
            await self.engine.remember_speech_decision(state, GROUP,
                SpeechDecision(inner="这张图很有趣。", say="有点好笑。", intent="join_chat"),
                trigger="message", outcome=outcome)
        outcome.add_speech("有点好笑。")
        self.engine.claim_chat_scene(outcome, GROUP, [row])
        raw["decider"]["media_only_participation"] = "directed"
        self.engine.store.save_world(raw)
        self.engine.reload_config()
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.sent, [])
        self.assertEqual(outcome.speech_decisions[0]["status"], "not_sent")

    async def queued_comment(self, *, addressing="", kind="participation", drafts=None):
        raw = self.engine.store.raw_world()
        raw.setdefault("decider", {})["media_only_participation"] = "normal"
        self.engine.store.save_world(raw)
        self.engine.reload_config()
        row = await self.chat("［图片］", raw_text="", has_images=True, addressing=addressing,
                              message_id=f"queued-media-{self.clock.now()}")
        outcome = await self.image_draft(kind=kind, drafts=drafts or ["第一句。", "第二句。"])
        async with self.engine.session_state(GROUP) as state:
            self.engine.claim_chat_scene(outcome, GROUP, [row], state=state)
        raw["decider"]["media_only_participation"] = "directed"
        self.engine.store.save_world(raw)
        self.engine.reload_config()
        return outcome

    async def test_directed_image_still_allows_a_pending_comment(self):
        outcome = await self.queued_comment(addressing="me")
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.sent[0][1], ["第一句。", "第二句。"])
        self.assertEqual(outcome.speech_decisions[0]["status"], "sent")

    async def test_native_reply_and_life_share_do_not_acquire_the_chat_image_gate(self):
        for kind in ("reply", "life_share"):
            with self.subTest(kind=kind):
                outcome = await self.queued_comment(kind=kind)
                await self.engine._deliver(outcome)
                self.assertEqual(self.messenger.sent[-1][1], ["第一句。", "第二句。"])
                self.assertEqual(outcome.speech_decisions[0]["status"], "sent")
                self.clock.advance(301)

    async def test_expired_context_cannot_keep_a_bare_image_comment_alive(self):
        await self.chat("我的照片。", mine=True, message_id="own-photo")
        outcome = await self.queued_comment()
        raw = self.engine.store.raw_world()
        raw["decider"]["media_only_participation"] = "contextual"
        self.engine.store.save_world(raw)
        self.engine.reload_config()
        self.clock.advance(121)
        await self.engine._deliver(outcome)
        self.assertFalse(self.messenger.sent)
        self.assertFalse(self.messenger.sent_images)

    async def test_checked_old_text_cannot_authorize_new_bare_media(self):
        old = await self.chat("我想问你一个问题。", message_id="already-checked")
        image = await self.chat("［图片］", raw_text="", has_images=True, message_id="new-only-image")
        outcome = await self.image_draft(kind="participation", drafts=["想接这张图。"])
        async with self.engine.session_state(GROUP) as state:
            self.engine.claim_chat_scene(outcome, GROUP, [old, image], state=state, media_rows=[image])
        await self.engine._deliver(outcome)
        self.assertFalse(self.messenger.sent)

    async def test_a_started_turn_can_finish_but_not_bypass_media_policy_forever(self):
        from core.speech_policy import accept_segment, segment_allowed
        for delay, expected in ((0, ["第一句。", "第二句。"]), (121, ["第一句。"])):
            with self.subTest(delay=delay):
                outcome = await self.queued_comment(addressing="me")
                outcome.chat_media_scenes[GROUP]["new"][0].pop("addressing", None)
                raw = self.engine.store.raw_world()
                raw["decider"]["media_only_participation"] = "normal"
                self.engine.store.save_world(raw)
                self.engine.reload_config()
                accepted = []

                async def send(sid, lines):
                    for i, line in enumerate(lines):
                        if not segment_allowed(sid, i):
                            break
                        accepted.append(line)
                        await accept_segment(sid, line, i)
                        if i == 0:
                            raw["decider"]["media_only_participation"] = "directed"
                            self.engine.store.save_world(raw)
                            self.engine.reload_config()
                            self.clock.advance(delay)
                    return bool(accepted)

                self.messenger.send_text = send
                self.messenger.take_sent_texts = lambda _: list(accepted)
                await self.engine._deliver(outcome)
                self.assertEqual(accepted, expected)
                self.assertEqual(outcome.speech_decisions[0]["status"], "sent" if delay == 0 else "partial")
                self.clock.advance(301)
