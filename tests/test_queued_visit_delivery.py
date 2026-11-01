"""Generated chat belongs to the life and visit history that observed its scene."""
from __future__ import annotations

import types
import unittest

from core.engine import TickOutcome
from core.speech_policy import accept_segment, segment_allowed
from tests import test_queued_chat_continuity as queued
from tests import test_browse_followup as browse

GROUP = queued.GROUP


class QueuedVisitDeliveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = queued.QueuedChatContinuityTests.asyncSetUp
    chat = queued.QueuedChatContinuityTests.chat
    pending_comment = queued.QueuedChatContinuityTests.pending_comment

    def visits(self):
        presence = {"epoch": 0, "active": None}
        self.engine.visits = types.SimpleNamespace(
            presence_epoch=lambda scope: presence["epoch"] if scope == GROUP else 0,
            active=lambda scope: presence["active"] if scope == GROUP else None)
        return presence

    async def test_arrival_before_delivery_cancels_prepared_comment(self):
        presence = self.visits()
        outcome, _ = await self.pending_comment()
        presence.update(epoch=1, active={"status": "active"})
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.sent, [])
        self.assertEqual(outcome.speech_decisions[0]["status"], "not_sent")
        self.assertTrue(any("串门经历已变化" in note for note in outcome.notes))

    async def test_return_before_delivery_does_not_resurrect_prepared_comment(self):
        presence = self.visits()
        outcome, _ = await self.pending_comment()
        presence["epoch"] = 1
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.sent, [])
        self.assertTrue(any("串门经历已变化" in note for note in outcome.notes))

    async def test_legacy_comment_reports_current_visit_without_an_arrival_cursor(self):
        presence = self.visits()
        outcome, _ = await self.pending_comment()
        outcome.chat_presence_epochs.clear()
        presence.update(epoch=1, active={"status": "active"})
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.sent, [])
        self.assertTrue(any("正在串门" in note for note in outcome.notes))

    async def test_new_comment_prepared_after_return_can_still_be_delivered(self):
        presence = self.visits()
        presence["epoch"] = 1
        outcome, _ = await self.pending_comment()
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.sent, [(GROUP, ["等下找给你。"])])

    async def test_regrouping_cancels_comment_owned_by_previous_life(self):
        outcome, _ = await self.pending_comment()
        sessions = self.engine.store.raw_sessions()
        sessions["groups"] = [{"id": "changed", "name": "新生活", "sessions": [GROUP, queued.participation.OTHER],
                               "main_session": queued.participation.OTHER}]
        self.engine.store.save_sessions(sessions)
        self.engine.reload_config()
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.sent, [])
        self.assertTrue(any("生活归属已变化" in note for note in outcome.notes))

    async def test_arrival_during_typing_keeps_first_receipt_and_cancels_remaining_speech(self):
        presence = self.visits()
        outcome, _ = await self.pending_comment()
        outcome.add_speech("还有另一张照片。")
        accepted = []

        async def delayed_send(target, texts):
            for index, text in enumerate(texts):
                if not segment_allowed(target, index):
                    break
                accepted.append(text)
                await accept_segment(target, text, index)
                presence.update(epoch=1, active={"status": "active"})
            return bool(accepted)

        self.messenger.send_text = delayed_send
        self.messenger.take_sent_texts = lambda target: list(accepted)
        await self.engine._deliver(outcome)
        self.assertEqual(accepted, ["等下找给你。"])
        state = await self.engine.load_state(GROUP, cold_start=False)
        sent = [row["text"] for row in state.recent_chat if row.get("is_self")]
        self.assertIn("等下找给你。", sent)
        self.assertNotIn("还有另一张照片。", sent)

    async def test_revoked_permission_already_cancels_prepared_comment(self):
        outcome, _ = await self.pending_comment()
        self.engine.world.proactive_session_whitelist = []
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.sent, [])

    async def test_native_reply_remains_available_while_visiting(self):
        presence = self.visits()
        presence.update(epoch=1, active={"status": "active"})
        await self.engine._deliver(TickOutcome(GROUP, speech_kind="reply", messages=["我在朋友那里，稍等一下。"]))
        self.assertEqual(self.messenger.sent, [(GROUP, ["我在朋友那里，稍等一下。"])])


class DeferredBrowseVisitTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = browse.BrowseFollowupTests.asyncSetUp
    configure = browse.BrowseFollowupTests.configure
    note = browse.BrowseFollowupTests.note
    prepare = browse.BrowseFollowupTests.prepare
    complete = browse.BrowseFollowupTests.complete
    add_group = browse.BrowseFollowupTests.add_group
    blocked_reading = browse.BrowseFollowupTests.blocked_reading
    stop_owner = browse.BrowseFollowupTests.stop_owner
    settled = browse.BrowseFollowupTests.settled

    async def test_return_before_deferred_reading_reply_cancels_original_opportunity(self):
        epochs = {}
        self.engine.visits = types.SimpleNamespace(active=lambda scope: None,
            presence_epoch=lambda scope: epochs.get(scope, 0))
        action, owner, release = await self.blocked_reading()
        epochs[self.engine.state_key(browse.S)] = 1
        release.set()
        await owner
        await self.settled()
        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual(self.messenger.sent, [])
        state = await self.engine.load_state(browse.S, cold_start=False)
        thought = next(row for row in state.thoughts if row.get("browse_id") == action["id"])
        self.assertEqual(thought["detail"]["browse_reply"]["status"], "cancelled")
