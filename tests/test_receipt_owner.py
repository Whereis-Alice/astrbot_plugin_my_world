"""Accepted speech remains in the life that actually spoke across regrouping."""
from __future__ import annotations

import asyncio
import importlib
import unittest
from hashlib import sha256

from tests import test_queued_home_delivery as fixture


class ReceiptOwnerTests(unittest.IsolatedAsyncioTestCase):
    authorize = fixture.QueuedHomeDeliveryTests.authorize
    draft = fixture.QueuedHomeDeliveryTests.draft

    async def asyncSetUp(self):
        await fixture.QueuedHomeDeliveryTests.asyncSetUp(self)
        self.receipts = importlib.import_module(self.module.__name__ + ".core.engine.speech_receipts")
        self.decisions = importlib.import_module(self.module.__name__ + ".core.speech_decision")
        self.runtime = importlib.import_module(self.module.__name__ + ".core.speech_runtime")
        self.other = "test:GroupMessage:200"
        self.plugin.store.add_session(self.other)
        self.authorize([self.session, self.other])
        async with self.engine.session_state(self.other) as state:
            state.note_chat(user_id="__self__", name="另一个角色", text="另一段已经存在的生活。",
                now=self.engine._now(), is_self=True, origin=self.other, event_id="other-marker")
        self.new_checkpoint = None

    async def regroup(self, *, split=False):
        sessions = self.plugin.store.raw_sessions()
        sessions["groups"] = [] if split else [{"id": "moved", "name": "另一个角色的生活",
            "sessions": [self.session, self.other], "main_session": self.other}]
        self.plugin.store.save_sessions(sessions)
        self.engine.reload_config()
        self.new_checkpoint = await self.engine.db.call("get_state", self.session if split else self.other)

    async def state_payload(self, key):
        return await self.engine.db.call("get_state", key)

    async def own_lines(self, key):
        state = await self.state_payload(key)
        return [row["text"] for row in (state or {}).get("recent_chat", []) if row.get("is_self")]

    async def decided(self, *, kind="life_share", images=False, image_only=False):
        body = "这是原来那位角色已经说出口的话。"
        async with self.engine.session_state(self.session) as state:
            outcome = self.models.TickOutcome(self.session, speech_kind=kind)
            if kind == "outreach":
                outcome.contact_people = {"42"}
            await self.engine.remember_speech_decision(state, self.session,
                self.decisions.SpeechDecision(inner="给这份真实生活留下记录。", say=body, intent=kind),
                trigger="expression", outcome=outcome, drafts=[] if image_only else [body],
                raw=self.runtime.SpeechDraft("", images=["https://example.com/original.png"] if images else []))
            if not image_only:
                outcome.add_speech(body)
        return outcome, body

    async def assert_original_receipt(self, outcome, body, *, original=None, image_only=False):
        original = original or self.session
        new = self.other if original == self.session else self.session
        self.assertEqual(self.plugin.context.send_message.await_count, 1)
        state = await self.state_payload(original)
        own = [row for row in state["recent_chat"] if row.get("is_self")]
        if image_only:
            self.assertEqual([row["images"] for row in own if row.get("has_images")],
                             [["https://example.com/original.png"]])
        else:
            self.assertIn(body, [row["text"] for row in own])
            self.assertEqual([row["text"] for row in own].count(body), 1)
        self.assertEqual(await self.state_payload(new), self.new_checkpoint)
        self.assertTrue(all(record["owner"].state_key == original for record in outcome.accepted_receipts.values()))
        events = self.engine.db.raw.query_events(session_id=original, event_type="bot_message")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["persona_id"], sha256(self.role.encode()).hexdigest()[:24])
        self.assertEqual(self.engine.db.raw.query_events(session_id=new, event_type="bot_message"), [])
        self.assertEqual(self.engine.db.raw.query_events(session_id=new, event_type="speech_result"), [])
        self.assertEqual(outcome.speech_decisions[0]["status"], "sent")
        thoughts = state["thoughts"]
        trace = next(row["detail"]["speech"] for row in thoughts
                     if (row.get("detail") or {}).get("speech", {}).get("id") == outcome.speech_decisions[0]["id"])
        self.assertEqual(trace["status"], "sent")

    async def test_real_text_accepted_during_regrouping_stays_in_original_life_and_logs(self):
        outcome, body = await self.decided()
        async def send(*_):
            await self.regroup()
            self.role = "新的角色"
            return True
        original_role = self.role
        self.plugin.context.send_message.side_effect = send
        await self.engine._deliver(outcome)
        self.role = original_role
        await self.assert_original_receipt(outcome, body)
        self.assertTrue(self.engine.db.raw.kv_get("outreach:" + self.session, {}).get("sessions"))
        self.assertEqual(self.engine.db.raw.kv_get("outreach:" + self.other, {}), {})

    async def test_real_image_accepted_during_regrouping_stays_in_original_life(self):
        outcome, body = await self.decided(images=True, image_only=True)
        async def send(*_):
            await self.regroup()
            return True
        self.plugin.context.send_message.side_effect = send
        await self.engine._deliver(outcome)
        await self.assert_original_receipt(outcome, body, image_only=True)

    async def test_native_reply_generated_in_original_lease_keeps_its_owner(self):
        outcome, body = await self.decided(kind="reply")
        async def send(*_):
            await self.regroup()
            return True
        self.plugin.context.send_message.side_effect = send
        await self.engine._deliver(outcome)
        await self.assert_original_receipt(outcome, body)
        self.engine.persona.get_persona_text_for_send.assert_not_awaited()

    async def test_shared_life_split_does_not_transfer_accepted_speech_to_new_independent_life(self):
        await self.regroup()
        outcome, body = await self.decided(kind="reply")
        async def send(*_):
            await self.regroup(split=True)
            return True
        self.plugin.context.send_message.side_effect = send
        await self.engine._deliver(outcome)
        await self.assert_original_receipt(outcome, body, original=self.other)

    async def test_receipt_waiting_for_original_lock_cannot_load_the_new_group(self):
        outcome, body = await self.decided()
        sent, release = asyncio.Event(), asyncio.Event()
        async def block_original():
            async with self.engine.lock(self.session):
                sent.set()
                await release.wait()
        blocker = None
        async def send(*_):
            nonlocal blocker
            blocker = asyncio.create_task(block_original())
            await sent.wait()
            return True
        self.plugin.context.send_message.side_effect = send
        task = asyncio.create_task(self.engine._deliver(outcome))
        try:
            await asyncio.wait_for(sent.wait(), 2)
            await self.regroup()
        finally:
            release.set()
        await asyncio.wait_for(task, 2)
        await blocker
        await self.assert_original_receipt(outcome, body)

    async def test_transient_persistence_failure_retries_the_original_owner_without_resending(self):
        outcome, body = await self.decided()
        save = self.engine.save_state
        failed = False
        async def fail_once(state):
            nonlocal failed
            if not failed and state.session_id == self.session:
                failed = True
                raise OSError("temporary storage error")
            await save(state)
        self.engine.save_state = fail_once
        async def send(*_):
            await self.regroup()
            return True
        self.plugin.context.send_message.side_effect = send
        await self.engine._deliver(outcome)
        self.assertTrue(failed)
        await self.assert_original_receipt(outcome, body)

    async def test_cancelled_persistence_after_real_acceptance_preserves_original_receipt(self):
        outcome, body = await self.decided()
        save = self.engine.save_state
        cancelled = False
        async def cancel_once(state):
            nonlocal cancelled
            if not cancelled and state.session_id == self.session:
                cancelled = True
                raise asyncio.CancelledError()
            await save(state)
        self.engine.save_state = cancel_once
        async def send(*_):
            await self.regroup()
            return True
        self.plugin.context.send_message.side_effect = send
        with self.assertRaises(asyncio.CancelledError):
            await self.engine._deliver(outcome)
        await self.assert_original_receipt(outcome, body)
        self.assertFalse(any("平台没有接收" in note for note in outcome.notes))

    async def test_cancelled_image_persistence_preserves_original_fact_and_final_message_log(self):
        outcome, body = await self.decided(images=True, image_only=True)
        save = self.engine.save_state
        cancelled = False
        async def cancel_once(state):
            nonlocal cancelled
            if not cancelled and state.session_id == self.session:
                cancelled = True
                raise asyncio.CancelledError()
            await save(state)
        self.engine.save_state = cancel_once
        async def send(*_):
            await self.regroup()
            return True
        self.plugin.context.send_message.side_effect = send
        with self.assertRaises(asyncio.CancelledError):
            await self.engine._deliver(outcome)
        await self.assert_original_receipt(outcome, body, image_only=True)

    async def test_instant_send_inside_old_lease_keeps_receipt_and_final_logs_after_regrouping(self):
        outcome, body = await self.decided()
        self.engine.say_sink = lambda sid, text: self.plugin.messenger.send_text(sid, [text])
        async def send(*_):
            await self.regroup()
            return True
        self.plugin.context.send_message.side_effect = send
        async with self.engine.session_state(self.session):
            await self.engine._flush_say(outcome)
        await self.engine._deliver(outcome)
        await self.assert_original_receipt(outcome, body)
        self.assertEqual(outcome.live_messages, [body])

    async def test_outreach_quota_and_cadence_stay_with_original_reservation(self):
        outcome, body = await self.decided(kind="outreach")
        async def send(*_):
            await self.regroup()
            return True
        self.plugin.context.send_message.side_effect = send
        await self.engine._deliver(outcome)
        await self.assert_original_receipt(outcome, body)
        old = self.engine.db.raw.kv_get("proactive_contacts:" + self.session, {})
        self.assertEqual(len(old.get("contacts", {})), 1)
        self.assertEqual(self.engine.db.raw.kv_get("proactive_contacts:" + self.other, {}), {})
        cadence = self.engine.db.raw.kv_get("outreach:" + self.session, {})
        self.assertTrue(cadence.get("sessions", {}).get(self.session))
        self.assertEqual(self.engine.db.raw.kv_get("outreach:" + self.other, {}), {})
        self.assertEqual(self.engine.profiles.proactive_quota_left(self.session, "42"), 1)
        self.assertEqual(self.engine.profiles.proactive_quota_left(self.other, "42"), 2)

    async def test_legacy_reply_binds_before_platform_wait_and_leaves_unknown_persona_blank(self):
        text = "已经接受的旧调用回复。"
        outcome = self.models.TickOutcome(self.session, speech_kind="reply", messages=[text])
        self.engine.persona.get_persona_text.reset_mock()
        async def send(*_):
            await self.regroup()
            self.role = "另一位新角色"
            return True
        self.plugin.context.send_message.side_effect = send
        await self.engine._deliver(outcome)
        self.assertEqual(await self.own_lines(self.session), [text])
        self.assertEqual(await self.state_payload(self.other), self.new_checkpoint)
        events = self.engine.db.raw.query_events(session_id=self.session, event_type="bot_message")
        self.assertEqual(events[0]["persona_id"], "")
        self.engine.persona.get_persona_text.assert_not_awaited()
        self.engine.persona.get_persona_text_for_send.assert_not_awaited()

    async def test_routed_instant_view_keeps_original_owner_and_actual_message_origin(self):
        await self.regroup()
        # Both places share the original life; the queued speech still originates
        # in the actual destination, while its life owner survives a later split.
        async with self.engine.session_state(self.session):
            outcome = self.models.TickOutcome(self.session, speech_kind="reply", speech_home=self.other)
            outcome.add_speech("给实际落点的即时回复。")
        self.engine.say_sink = lambda sid, text: self.plugin.messenger.send_text(sid, [text])
        async def send(*_):
            await self.regroup(split=True)
            return True
        self.plugin.context.send_message.side_effect = send
        await self.engine._flush_say(outcome)
        await self.engine._deliver(outcome)
        self.assertEqual(self.plugin.context.send_message.await_count, 1)
        self.assertEqual(self.plugin.context.send_message.await_args.args[0], self.other)
        own = [row for row in (await self.state_payload(self.other))["recent_chat"]
               if row.get("text") == "给实际落点的即时回复。"]
        self.assertEqual(len(own), 1)
        self.assertEqual(own[0]["origin"], self.other)
        self.assertEqual(await self.state_payload(self.session), self.new_checkpoint)
        self.assertTrue(all(record["owner"].state_key == self.other for record in outcome.accepted_receipts.values()))

    async def test_legacy_reply_cannot_borrow_unrelated_lease_when_delivering(self):
        text = "A的旧调用回复。"
        outcome = self.models.TickOutcome(self.session, speech_kind="reply", messages=[text])
        async with self.engine.session_state(self.other):
            await self.engine._deliver(outcome)
        self.assertEqual(outcome.receipt_owner.origin, self.session)
        self.assertEqual(outcome.receipt_owner.state_key, self.session)
        self.assertIsNone(outcome.receipt_owner.persona_id)
        self.assertEqual(await self.own_lines(self.session), [text])
        self.assertEqual(await self.own_lines(self.other), ["另一段已经存在的生活。"])

    async def test_constructor_cannot_borrow_unrelated_lease_owner_or_home_proof(self):
        text = "属于声明源A的家中台词。"
        async with self.engine.session_state(self.other):
            outcome = self.models.TickOutcome(self.session, speech_kind="life_share", messages=[text])
            self.assertIsNone(outcome.receipt_owner)
            self.assertEqual(outcome.speech_proofs[""], [None])
            await self.engine._deliver(outcome)
        self.assertEqual(outcome.receipt_owner.origin, self.session)
        self.assertIsNone(outcome.receipt_owner.persona_id)
        self.assertEqual(await self.own_lines(self.session), [text])
        self.assertEqual(await self.own_lines(self.other), ["另一段已经存在的生活。"])
        self.engine.persona.get_persona_text_for_send.assert_not_awaited()

    async def test_add_speech_and_images_cannot_borrow_unrelated_lease_metadata(self):
        outcome = self.models.TickOutcome(self.session, speech_kind="life_share")
        async with self.engine.session_state(self.other):
            outcome.add_speech("源A追加的台词。")
            outcome.add_images(["https://example.com/source-a.png"])
            self.assertIsNone(outcome.receipt_owner)
            self.assertEqual(outcome.speech_proofs[""], [None])
            self.assertEqual(outcome.image_proofs[""], [None])
            await self.engine._deliver(outcome)
        self.assertEqual(self.plugin.context.send_message.await_count, 2)
        self.assertEqual(outcome.receipt_owner.state_key, self.session)
        self.assertIsNone(outcome.receipt_owner.persona_id)
        self.assertIn("源A追加的台词。", await self.own_lines(self.session))
        self.assertEqual(await self.own_lines(self.other), ["另一段已经存在的生活。"])

    async def test_existing_owner_cannot_refresh_from_an_unrelated_lease(self):
        outcome, body = await self.decided()
        original = outcome.receipt_owner
        async with self.engine.session_state(self.other):
            await self.engine._deliver(outcome)
        self.assertIs(outcome.receipt_owner, original)
        self.assertEqual(original.state_key, self.session)
        self.assertIn(body, await self.own_lines(self.session))
        self.assertEqual(await self.own_lines(self.other), ["另一段已经存在的生活。"])

    async def test_shared_noncanonical_alias_does_not_borrow_another_alias_persona(self):
        await self.regroup()
        async with self.engine.session_state(self.other):
            outcome = self.models.TickOutcome(self.session, speech_kind="reply", messages=["真实alias A的回复。"])
            self.assertIsNone(outcome.receipt_owner)
            await self.engine._deliver(outcome)
        self.assertEqual(outcome.receipt_owner.origin, self.session)
        self.assertEqual(outcome.receipt_owner.state_key, self.other)
        self.assertIsNone(outcome.receipt_owner.persona_id)
        events = self.engine.db.raw.query_events(session_id=self.other, event_type="bot_message")
        self.assertEqual(events[0]["persona_id"], "")

    async def test_canonical_shared_home_draft_after_return_keeps_real_lease_proof(self):
        await self.regroup()
        self.presence["epoch"] = 1
        async with self.engine.session_state(self.session) as state:
            self.assertEqual(state.session_id, self.other)
            outcome = self.models.TickOutcome(state.session_id, speech_kind="life_share")
            outcome.add_speech("归家后刚写的canonical台词。")
            outcome.add_images(["https://example.com/returned-home.png"])
            self.assertEqual(outcome.receipt_owner.origin, self.session)
            self.assertEqual(outcome.receipt_owner.state_key, self.other)
            self.assertEqual(outcome.speech_proofs[""][0].epoch, 1)
            self.assertIs(outcome.speech_proofs[""][0], outcome.image_proofs[""][0])
            await self.engine._deliver(outcome)
        self.assertEqual(self.plugin.context.send_message.await_count, 2)
        self.assertIn("归家后刚写的canonical台词。", await self.own_lines(self.other))
        self.assertEqual(outcome.receipt_owner.persona_id, sha256(self.role.encode()).hexdigest()[:24])
