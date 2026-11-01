"""Real transport boundaries cannot revive home speech from an older life."""
from __future__ import annotations

import asyncio
import importlib
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests import test_group_delivery as fixture
from tests import test_proactive_delivery_identity as hook_fixture


class QueuedHomeDeliveryTests(unittest.IsolatedAsyncioTestCase):
    hook_environment = hook_fixture.ProactiveDeliveryIdentityTests.hook_environment
    async def asyncSetUp(self):
        await fixture.GroupDeliveryTests.asyncSetUp(self)
        self.engine = self.plugin.engine
        self.models = importlib.import_module(self.module.__name__ + ".core.engine.models")
        self.home = importlib.import_module(self.module.__name__ + ".core.engine.queued_home")
        self.presence = {"epoch": 0, "active": None}
        self.engine.visits = SimpleNamespace(
            presence_epoch=lambda scope: self.presence["epoch"] if scope == self.engine.state_key(self.session) else 0,
            active=lambda scope: self.presence["active"] if scope == self.engine.state_key(self.session) else None)
        self.role = "正在家里的角色"
        self.engine.persona = SimpleNamespace(
            get_persona_text=AsyncMock(side_effect=lambda _: self.role),
            get_persona_text_for_send=AsyncMock(side_effect=lambda _: self.role))
        self.authorize([self.session])

    def authorize(self, targets):
        world = self.plugin.store.raw_world()
        world["proactive_session_whitelist"] = targets
        world["life_share_session_whitelist"] = targets
        self.plugin.store.save_world(world)
        self.engine.reload_config()

    async def draft(self, *, kind="life_share", target="", images=False, constructor=False):
        async with self.engine.session_state(self.session):
            if constructor:
                return self.models.TickOutcome(self.session, speech_kind=kind, messages=["家里新写的一句。"])
            outcome = self.models.TickOutcome(self.session, speech_kind=kind)
            if kind == "outreach":
                outcome.contact_people = {"42"}
            outcome.add_speech("我刚刚把书打开了。", target)
            outcome.add_speech("我现在还在自己的房间里读书。", target)
            if images:
                outcome.add_images(["https://example.com/home.png"], target)
            return outcome

    def arrive(self, *, returned=False):
        self.presence.update(epoch=self.presence["epoch"] + 1,
                             active=None if returned else {"status": "active"})

    async def remembered(self):
        state = await self.engine.load_state(self.session, cold_start=False)
        return [row["text"] for row in state.recent_chat if row.get("is_self")]

    async def test_arrival_before_delivery_uses_no_persona_read_or_platform_call(self):
        outcome = await self.draft(images=True)
        self.arrive()
        await self.engine._deliver(outcome)
        self.plugin.context.send_message.assert_not_awaited()
        self.engine.persona.get_persona_text_for_send.assert_not_awaited()
        self.assertTrue(any("串门经历已变化" in note for note in outcome.notes))

    async def test_short_return_does_not_revive_old_home_speech(self):
        outcome = await self.draft()
        self.arrive(returned=True)
        await self.engine._deliver(outcome)
        self.plugin.context.send_message.assert_not_awaited()

    async def test_partial_real_send_keeps_receipt_and_stops_stale_tail_and_images(self):
        outcome = await self.draft(images=True)
        async def send(*_):
            self.arrive()
            return True
        self.plugin.context.send_message.side_effect = send
        await self.engine._deliver(outcome)
        self.assertEqual(self.plugin.context.send_message.await_count, 1)
        self.assertEqual(await self.remembered(), ["我刚刚把书打开了。"])
        self.assertEqual(self.engine.persona.get_persona_text_for_send.await_count, 1)
        self.assertFalse(self.plugin.messenger.blocked(self.session))

    async def test_outreach_stops_after_short_return_with_accepted_mention_intact(self):
        outcome = await self.draft(kind="outreach")
        async def send(*_):
            self.arrive(returned=True)
            return True
        self.plugin.context.send_message.side_effect = send
        await self.engine._deliver(outcome)
        self.assertEqual(self.plugin.context.send_message.await_count, 1)
        first_chain = self.plugin.context.send_message.await_args.args[1].chain
        self.assertTrue(any((isinstance(part, fixture.At) and str(part.qq) == "42")
                            or (isinstance(part, fixture.Plain) and "@阿岚" in part.text)
                            for part in first_chain))
        self.assertEqual(await self.remembered(), ["我刚刚把书打开了。"])

    async def test_new_constructor_draft_after_return_is_valid(self):
        self.arrive(returned=True)
        outcome = await self.draft(constructor=True)
        await self.engine._deliver(outcome)
        self.assertEqual(self.plugin.context.send_message.await_count, 1)
        self.assertEqual(await self.remembered(), ["家里新写的一句。"])

    async def test_legacy_buffer_after_return_is_conservative_but_reply_remains_valid(self):
        self.arrive(returned=True)
        await self.engine._deliver(self.models.TickOutcome(self.session, speech_kind="life_share", messages=["没有生成证明。"] ))
        self.plugin.context.send_message.assert_not_awaited()
        self.presence["active"] = {"status": "active"}
        await self.engine._deliver(self.models.TickOutcome(self.session, speech_kind="reply", messages=["我在朋友那里。"] ))
        self.assertEqual(self.plugin.context.send_message.await_count, 1)
        self.engine.persona.get_persona_text_for_send.assert_not_awaited()

    async def test_arrival_during_member_lookup_blocks_before_persona_read(self):
        outcome = await self.draft(kind="outreach")
        async def member(*_, **__):
            self.arrive()
            return {"user_id": 42, "nickname": "阿岚"}
        self.bot.call_action.side_effect = member
        await self.engine._deliver(outcome)
        self.bot.call_action.assert_awaited_once()
        self.engine.persona.get_persona_text_for_send.assert_not_awaited()
        self.plugin.context.send_message.assert_not_awaited()

    async def test_persona_change_during_member_lookup_cancels_old_speech(self):
        outcome = await self.draft(kind="outreach")
        async def member(*_, **__):
            self.role = "另一个角色"
            return {"user_id": 42, "nickname": "阿岚"}
        self.bot.call_action.side_effect = member
        await self.engine._deliver(outcome)
        self.plugin.context.send_message.assert_not_awaited()
        self.assertEqual(self.engine.persona.get_persona_text_for_send.await_count, 1)
        self.assertTrue(any("角色人设已变化" in note for note in outcome.notes))

    async def test_persona_change_during_real_typing_wait_keeps_first_sentence(self):
        outcome = await self.draft()
        waiting, release = asyncio.Event(), asyncio.Event()
        self.plugin.typing_delay_for = lambda _: .001
        original_sleep = asyncio.sleep
        async def sleep(delay):
            if delay == .001:
                waiting.set()
                await release.wait()
            else:
                await original_sleep(delay)
        with patch.object(self.messenger.asyncio, "sleep", side_effect=sleep):
            task = asyncio.create_task(self.engine._deliver(outcome))
            try:
                await asyncio.wait_for(waiting.wait(), 2)
                self.role = "新角色"
            finally:
                release.set()
            await asyncio.wait_for(task, 2)
        self.assertEqual(self.plugin.context.send_message.await_count, 1)
        self.assertEqual(await self.remembered(), ["我刚刚把书打开了。"])

    async def test_hook_wait_arrival_is_checked_before_real_send(self):
        outcome = await self.draft()
        @asynccontextmanager
        async def prepare(*_, **__):
            self.arrive()
            yield
        self.plugin.messenger.proactive_delivery = prepare
        await self.engine._deliver(outcome)
        self.plugin.context.send_message.assert_not_awaited()
        self.engine.persona.get_persona_text_for_send.assert_not_awaited()

    async def test_hook_wait_persona_change_is_checked_before_real_send(self):
        outcome = await self.draft()
        @asynccontextmanager
        async def prepare(*_, **__):
            self.role = "变更后的角色"
            yield
        self.plugin.messenger.proactive_delivery = prepare
        await self.engine._deliver(outcome)
        self.plugin.context.send_message.assert_not_awaited()

    async def test_persona_read_timeout_never_marks_transport_failed_or_retries_draft(self):
        outcome = await self.draft()
        async def unavailable(_):
            await asyncio.Event().wait()
        self.engine.persona.get_persona_text_for_send.side_effect = unavailable
        with patch.object(self.home, "PERSONA_CHECK_SECONDS", .01):
            await self.engine._deliver(outcome)
        self.plugin.context.send_message.assert_not_awaited()
        self.assertFalse(self.plugin.messenger.blocked(self.session))
        self.assertTrue(any("TimeoutError" in note for note in outcome.notes))
        self.assertFalse(any("平台没有接收" in note for note in outcome.notes))
        self.engine.persona.get_persona_text_for_send.side_effect = lambda _: self.role
        await self.engine._deliver(outcome)
        self.plugin.context.send_message.assert_not_awaited()
        self.assertEqual(self.engine.persona.get_persona_text_for_send.await_count, 1)

    async def test_persona_read_failure_after_acceptance_keeps_first_receipt(self):
        outcome = await self.draft()
        self.engine.persona.get_persona_text_for_send.side_effect = [self.role, RuntimeError("unavailable")]
        await self.engine._deliver(outcome)
        self.assertEqual(self.plugin.context.send_message.await_count, 1)
        self.assertEqual(await self.remembered(), ["我刚刚把书打开了。"])
        self.assertFalse(self.plugin.messenger.blocked(self.session))

    async def test_arrival_during_persona_read_rechecks_epoch_after_await(self):
        outcome = await self.draft()
        async def read(_):
            self.arrive(returned=True)
            return self.role
        self.engine.persona.get_persona_text_for_send.side_effect = read
        await self.engine._deliver(outcome)
        self.plugin.context.send_message.assert_not_awaited()

    async def test_cancellation_during_second_persona_read_keeps_real_receipt(self):
        outcome = await self.draft()
        async def read(_):
            if self.engine.persona.get_persona_text_for_send.await_count == 2:
                raise asyncio.CancelledError()
            return self.role
        self.engine.persona.get_persona_text_for_send.side_effect = read
        with self.assertRaises(asyncio.CancelledError):
            await self.engine._deliver(outcome)
        self.assertEqual(self.plugin.context.send_message.await_count, 1)
        self.assertEqual(await self.remembered(), ["我刚刚把书打开了。"])

    async def test_regrouping_rejects_original_life_without_persona_lookup(self):
        outcome = await self.draft()
        other = "test:GroupMessage:200"
        self.plugin.store.add_session(other)
        sessions = self.plugin.store.raw_sessions()
        sessions["groups"] = [{"id": "new", "name": "另一种生活", "sessions": [self.session, other], "main_session": other}]
        self.plugin.store.save_sessions(sessions)
        self.engine.reload_config()
        await self.engine._deliver(outcome)
        self.plugin.context.send_message.assert_not_awaited()
        self.assertTrue(any("生活归属已变化" in note for note in outcome.notes))

    async def test_explicit_route_reads_original_persona_instead_of_destination_persona(self):
        other = "test:GroupMessage:200"
        self.plugin.store.add_session(other)
        self.authorize([self.session, other])
        outcome = await self.draft(kind="schedule", target=other)
        self.engine.persona.get_persona_text_for_send.side_effect = lambda sid: self.role if sid == self.session else "别处的角色"
        await self.engine._deliver(outcome)
        self.assertEqual(self.plugin.context.send_message.await_count, 2)
        self.assertTrue(all(call.args[0] == self.session for call in self.engine.persona.get_persona_text_for_send.await_args_list))
        self.assertTrue(all(call.args[0] == other for call in self.plugin.context.send_message.await_args_list))

    async def test_broadcast_does_not_rebind_proof_when_source_arrives_between_targets(self):
        other = "test:GroupMessage:200"
        self.plugin.store.add_session(other)
        self.authorize([self.session, other])
        outcome = await self.draft(kind="schedule", constructor=True)
        outcome.broadcast_targets = [other]
        async def send(*_):
            self.arrive(returned=True)
            return True
        self.plugin.context.send_message.side_effect = send
        await self.engine._deliver(outcome)
        self.assertEqual(self.plugin.context.send_message.await_count, 1)

    async def test_instant_flush_keeps_proof_after_failure_and_never_revives_on_return(self):
        outcome = await self.draft()
        self.engine.say_sink = AsyncMock(return_value=False)
        await self.engine._flush_say(outcome)
        self.assertEqual(len(outcome.messages), 2)
        self.arrive(returned=True)
        await self.engine._deliver(outcome)
        self.plugin.context.send_message.assert_not_awaited()

    async def test_real_instant_flush_rechecks_persona_and_remembers_only_accepted_line(self):
        outcome = await self.draft()
        self.engine.say_sink = lambda sid, text: self.plugin.messenger.send_text(sid, [text])
        async def send(*_):
            self.role = "变更后的角色"
            return True
        self.plugin.context.send_message.side_effect = send
        await self.engine._flush_say(outcome)
        await self.engine._deliver(outcome)
        self.assertEqual(self.plugin.context.send_message.await_count, 1)
        self.assertEqual(await self.remembered(), ["我刚刚把书打开了。"])

    async def test_strict_astrbot_persona_failure_does_not_become_valid_empty_persona(self):
        persona = importlib.import_module(self.module.__name__ + ".adapters.persona").AstrBotPersona(self.plugin)
        self.plugin.context.persona_manager = SimpleNamespace(get_default_persona_v3=AsyncMock(return_value=SimpleNamespace(prompt="")))
        self.engine.persona = persona
        outcome = await self.draft(constructor=True)
        self.plugin.context.persona_manager.get_default_persona_v3.side_effect = RuntimeError("offline")
        await self.engine._deliver(outcome)
        self.plugin.context.send_message.assert_not_awaited()
        self.assertTrue(any("RuntimeError" in note for note in outcome.notes))

    async def test_valid_empty_astrbot_persona_remains_compatible(self):
        persona = importlib.import_module(self.module.__name__ + ".adapters.persona").AstrBotPersona(self.plugin)
        self.plugin.context.persona_manager = SimpleNamespace(get_default_persona_v3=AsyncMock(return_value=SimpleNamespace(prompt="")))
        self.engine.persona = persona
        outcome = await self.draft(constructor=True)
        await self.engine._deliver(outcome)
        self.assertEqual(self.plugin.context.send_message.await_count, 1)

    async def test_real_response_hook_arrival_cancels_both_generated_lines(self):
        outcome = await self.draft()
        async def hook(_plugin, _event, name, *_args):
            if name == "OnDecoratingResultEvent" and not self.presence["epoch"]:
                self.arrive(returned=True)
            return False
        with self.hook_environment(hook):
            await self.engine._deliver(outcome)
        self.plugin.context.send_message.assert_not_awaited()
        self.engine.persona.get_persona_text_for_send.assert_not_awaited()

    async def test_real_hook_suppression_keeps_second_lines_original_proof(self):
        outcome = await self.draft()
        proof = outcome.speech_proofs[""][1]
        calls = 0
        async def hook(_plugin, _event, name, *_args):
            nonlocal calls
            if name == "OnLLMResponseEvent":
                calls += 1
                return calls == 1
            return False
        with self.hook_environment(hook):
            async with self.delivery.delivery(self.plugin, self.session, outcome):
                self.assertEqual(outcome.messages, ["我现在还在自己的房间里读书。"])
                self.assertIs(outcome.speech_proofs[""][0], proof)
                self.arrive(returned=True)
                await self.engine._deliver(outcome)
        self.plugin.context.send_message.assert_not_awaited()

    async def test_shared_life_route_keeps_generation_persona_and_original_config(self):
        other = "test:GroupMessage:200"
        self.plugin.store.add_session(other)
        sessions = self.plugin.store.raw_sessions()
        sessions["groups"] = [{"id": "one", "name": "共享生活", "sessions": [self.session, other], "main_session": self.session}]
        self.plugin.store.save_sessions(sessions)
        self.authorize([self.session, other])
        outcome = await self.draft(target=other)
        self.engine.persona.get_persona_text_for_send.side_effect = lambda sid: self.role if sid == self.session else "落点的另一个默认人设"
        await self.engine._deliver(outcome)
        self.assertEqual(self.plugin.context.send_message.await_count, 2)
        self.assertTrue(all(call.args[0] == self.session for call in self.engine.persona.get_persona_text_for_send.await_args_list))

    async def test_post_hook_life_filter_cannot_borrow_identical_other_lines_mention(self):
        other = "test:GroupMessage:200"
        self.plugin.store.add_session(other)
        self.authorize([self.session, other])
        outcome = self.models.TickOutcome(self.session, speech_kind="outreach")
        text = "最近过得怎么样？"
        for origin, uid in ((self.session, "111"), (other, "222")):
            async with self.engine.session_state(origin):
                generated = self.models.TickOutcome(origin, speech_kind="outreach")
                generated.contact_people = {uid}
                generated.speech_decisions.append({"id": uid, "target": self.session, "drafts": [text],
                    "status": "prepared", "at": self.engine._now(), "intent": "outreach", "origin": "outreach", "sent": []})
                generated.add_speech(text, self.session)
            # Build only the policy-filter/prepared-chain test view from two
            # explicitly proven candidates. Production keeps one life per turn.
            key = "" if origin == self.session else self.session
            outcome.messages.extend(generated.messages if not key else generated.routed[key])
            outcome.speech_decisions.extend(generated.speech_decisions)
            outcome.receipt_owner = outcome.receipt_owner or generated.receipt_owner
            for name in ("speech_kinds", "speech_ids", "speech_people", "speech_origins", "speech_proofs"):
                getattr(outcome, name).setdefault("", []).extend(getattr(generated, name)[key])
        self.engine.profiles.touch(self.session, "111", "第一位")
        self.engine.profiles.touch(self.session, "222", "第二位")
        async def hook(_plugin, event, name, *_args):
            if name == "OnDecoratingResultEvent":
                event.get_result().chain.insert(0, fixture.At(event.get_sender_id()))
                if event.get_sender_id() == "222":
                    self.arrive(returned=True)
            return False
        with self.hook_environment(hook):
            await self.engine._deliver(outcome)
        self.plugin.context.send_message.assert_awaited_once()
        chain = self.plugin.context.send_message.await_args.args[1].chain
        body = "".join(part.text for part in chain if isinstance(part, fixture.Plain))
        self.assertIn("@第二位", body)
        self.assertNotIn("第一位", body)
        self.assertEqual(outcome.delivered_decisions.get("222"), [text])
        self.assertFalse(outcome.delivered_decisions.get("111"))

    async def test_stale_home_tail_does_not_cancel_current_truthful_reply_in_same_batch(self):
        outcome = await self.draft()
        outcome.add_speech("我在朋友那里，稍等一下。", kind="reply")
        async def send(*_):
            if self.plugin.context.send_message.await_count == 1:
                self.arrive()
            return True
        self.plugin.context.send_message.side_effect = send
        await self.engine._deliver(outcome)
        self.assertEqual(self.plugin.context.send_message.await_count, 2)
        self.assertEqual(await self.remembered(), ["我刚刚把书打开了。", "我在朋友那里，稍等一下。"])

    async def duplicate_draft(self, *, echo=False):
        text = "这是相同的正文。"
        async with self.engine.session_state(self.session):
            outcome = self.models.TickOutcome(self.session, speech_kind="life_share")
            for kind, identity in (("life_share", "cancelled-home"), ("reply", "accepted-reply")):
                outcome.speech_decisions.append({"id": identity, "target": self.session, "drafts": [text],
                    "status": "prepared", "at": self.engine._now(), "intent": kind, "origin": kind, "sent": []})
                outcome.add_speech(text, kind=kind)
            if echo:
                outcome.debug_messages = [text]
                outcome.debug_positions = [0]
        return outcome, text

    async def test_exact_reply_receipt_cannot_reassign_identical_cancelled_home_draft(self):
        outcome, text = await self.duplicate_draft()
        self.engine.persona.get_persona_text_for_send.side_effect = lambda _: "更换后的角色"
        await self.engine._deliver(outcome)
        self.plugin.context.send_message.assert_awaited_once()
        self.assertEqual(await self.remembered(), [text])
        self.assertEqual(outcome.delivered_messages, {self.session: [text]})
        self.assertEqual(len(outcome.accepted_receipts), 1)
        self.assertFalse(outcome.delivered_decisions.get("cancelled-home"))
        self.assertEqual(outcome.delivered_decisions.get("accepted-reply"), [text])

    async def test_exact_echo_and_reply_receipts_cannot_revive_identical_cancelled_home_draft(self):
        outcome, text = await self.duplicate_draft(echo=True)
        self.engine.persona.get_persona_text_for_send.side_effect = lambda _: "更换后的角色"
        await self.engine._deliver(outcome)
        self.assertEqual(self.plugin.context.send_message.await_count, 2)
        self.assertEqual(await self.remembered(), [text])
        self.assertEqual(outcome.delivered_messages, {self.session: [text]})
        self.assertEqual(len(outcome.accepted_receipts), 1)
        self.assertFalse(outcome.delivered_decisions.get("cancelled-home"))
        self.assertEqual(outcome.delivered_decisions.get("accepted-reply"), [text])

    async def test_custom_text_only_receipt_cannot_revive_guard_rejected_occurrence(self):
        outcome, text = await self.duplicate_draft()
        accepted = []
        async def send(target, lines):
            self.arrive(returned=True)
            for position, line in enumerate(lines):
                if self.policy.segment_allowed(target, position):
                    accepted.append(line)
            return bool(accepted)
        self.plugin.messenger.send_text = send
        self.plugin.messenger.take_sent_texts = lambda _: accepted
        await self.engine._deliver(outcome)
        self.assertEqual(accepted, [text])
        self.assertEqual(await self.remembered(), [text])
        self.assertEqual(len(outcome.accepted_receipts), 1)
        self.assertFalse(outcome.delivered_decisions.get("cancelled-home"))
        self.assertEqual(outcome.delivered_decisions.get("accepted-reply"), [text])

    async def test_chat_media_policy_reload_preserves_native_home_text_and_image_together(self):
        outcome = await self.draft(images=True)
        raw = self.plugin.store.raw_world()
        raw.setdefault("decider", {})["media_only_participation"] = "directed"
        self.plugin.store.save_world(raw)
        self.engine.reload_config()
        await self.engine._deliver(outcome)
        self.assertEqual(self.plugin.context.send_message.await_count, 3)
        self.assertEqual(outcome.delivered_messages[self.session],
                         ["我刚刚把书打开了。", "我现在还在自己的房间里读书。"])
        state = await self.engine.load_state(self.session, cold_start=False)
        images = [row for row in state.recent_chat if row.get("is_self") and row.get("has_images")]
        self.assertEqual(images[0]["images"], ["https://example.com/home.png"])
        self.assertEqual(self.engine.persona.get_persona_text_for_send.await_count, 3)

    async def test_changed_home_world_still_cancels_both_text_and_image(self):
        outcome = await self.draft(images=True)
        raw = self.plugin.store.raw_world()
        raw["name"] = "已经替换的世界"
        self.plugin.store.save_world(raw)
        self.engine.reload_config()
        await self.engine._deliver(outcome)
        self.plugin.context.send_message.assert_not_awaited()
        self.engine.persona.get_persona_text_for_send.assert_not_awaited()
        self.assertTrue(any("世界或会话设置已变化" in note for note in outcome.notes))

    async def test_scoped_chat_media_policy_reload_preserves_native_home_text_and_image_together(self):
        raw = self.plugin.store.raw_world()
        raw.setdefault("decider", {})["media_only_participation"] = "normal"
        self.plugin.store.save_scope(self.session, raw, self.plugin.store.raw_schedules(), ["settings"], reason="独立设置回归")
        self.engine.reload_config()
        outcome = await self.draft(images=True)
        raw["decider"]["media_only_participation"] = "directed"
        self.plugin.store.save_scope_block(self.session, "settings", raw, reason="媒体接话政策回归")
        self.engine.reload_config()
        await self.engine._deliver(outcome)
        self.assertEqual(self.plugin.context.send_message.await_count, 3)
        self.assertEqual(outcome.delivered_messages[self.session],
                         ["我刚刚把书打开了。", "我现在还在自己的房间里读书。"])
        state = await self.engine.load_state(self.session, cold_start=False)
        images = [row for row in state.recent_chat if row.get("is_self") and row.get("has_images")]
        self.assertEqual(images[0]["images"], ["https://example.com/home.png"])
