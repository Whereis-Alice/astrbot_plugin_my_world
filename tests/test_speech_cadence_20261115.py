"""Actual room speech keeps its cadence after chat history is trimmed."""
import unittest
from unittest.mock import AsyncMock

from tests import fake_astrbot
fake_astrbot.install()

from core.engine import TickOutcome  # noqa: E402
from core.engine.expression_attention import _attention_proof, _proof_current  # noqa: E402
from core.engine.participation import ParticipationMixin  # noqa: E402
from core.engine.speech_receipts import remember_accepted, remember_accepted_images  # noqa: E402
from core.participation_opportunity import visible_turns  # noqa: E402
from core.speech_cadence import note_speech_turn  # noqa: E402
from tests import test_participation as fixture  # noqa: E402

GROUP, OTHER = fixture.GROUP, fixture.OTHER


class SpeechTurnTests(unittest.TestCase):
    def test_segments_and_retained_rows_merge_with_one_receipt_identity(self):
        receipts = note_speech_turn([], "batch", 1000)
        receipts = note_speech_turn(receipts, "batch", 1001)
        rows = [{"is_self": True, "speech_turn": "batch", "at": 1000, "text": "first"},
                {"is_self": True, "speech_turn": "batch", "at": 1001, "text": "second"}]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["seq"], 1)
        self.assertEqual(len(visible_turns(rows, 1010, receipts=receipts)), 1)
        self.assertEqual(len(visible_turns([], 1010, receipts=receipts)), 1)
        self.assertFalse(visible_turns([], 1602, window=600, receipts=receipts))

    def test_older_persistence_does_not_remove_a_newer_actual_receipt(self):
        receipts = note_speech_turn([], "later", 1001)
        receipts = note_speech_turn(receipts, "earlier", 1000)
        self.assertEqual({turn["token"] for turn in receipts}, {"later", "earlier"})
        self.assertEqual(max(turn["seq"] for turn in receipts), 2)
        receipts = note_speech_turn(receipts, "next", 4602)
        self.assertEqual([turn["token"] for turn in receipts], ["next"])
        self.assertEqual(receipts[0]["seq"], 3)


class SpeechCadenceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.ParticipationTests.asyncSetUp
    chat = fixture.ParticipationTests.chat

    async def trim(self):
        async with self.engine.session_state(GROUP) as state:
            for index in range(300):
                state.note_chat(user_id="human", name="human", text=f"新的游戏细节{index}",
                    now=self.clock.now(), origin=GROUP, message_id=f"burst-{index}", keep=300)
            self.assertEqual(len(state.recent_chat), 300)
            self.assertFalse(any(row.get("is_self") for row in state.recent_chat))

    async def choice(self, *, explicit=False):
        async with self.engine.session_state(GROUP) as state:
            new = {"text": "接着说一个新问题", "user_id": "human"}
            if explicit:
                new["addressing"] = "me"
            ParticipationMixin.participation_sample(self.engine, state, GROUP, [new], "")
            return state.participation[GROUP]["opportunity"]

    async def proof(self, outcome):
        async with self.engine.session_state(GROUP) as state:
            state.participation.setdefault(GROUP, {})["request_id"] = outcome.speech_receipt_batch
            return await _attention_proof(self.engine, state, GROUP, outcome, generation="generated")

    async def current(self, proof, *, allow_turn=""):
        async with self.engine.session_state(GROUP) as state:
            return await _proof_current(self.engine, state, GROUP, proof, allow_turn=allow_turn)

    async def test_native_receipt_keeps_space_and_turn_count_after_trim_and_reload(self):
        self.engine.note_visible_speech(GROUP, speech_turn="native")
        await self.engine.record_self_reply(GROUP, "早餐做好了", speech_turn="native")
        before = await self.choice()
        self.clock.advance(10)
        await self.trim()
        after = await self.choice()
        self.assertFalse(after["ready"])
        self.assertEqual(after["own_turns"], 1)
        self.assertEqual(after["due_at"], before["due_at"])
        self.engine.reload_config()
        self.assertEqual(await self.choice(), after)

    async def test_trim_does_not_reopen_fast_continuation_after_two_actual_turns(self):
        for index in range(2):
            outcome = TickOutcome(GROUP, speech_kind="continuation")
            await remember_accepted(self.engine, outcome, GROUP, "早餐做好了", f"part-{index}")
            self.clock.advance(1)
        self.clock.advance(28)
        before = await self.choice(explicit=True)
        await self.trim()
        after = await self.choice(explicit=True)
        self.assertEqual(before["own_turns"], 2)
        self.assertEqual(after["own_turns"], 2)
        self.assertFalse(after["continuation_priority"])
        self.assertFalse(after["ready"])
        self.assertEqual(after["due_at"], before["due_at"])
        self.clock.advance(600)
        later = await self.choice(explicit=True)
        self.assertTrue(later["continuation_priority"])
        self.assertTrue(later["ready"])

    async def test_accepted_segments_images_and_repeated_receipt_are_one_turn(self):
        outcome = TickOutcome(GROUP, speech_kind="life_share")
        await remember_accepted(self.engine, outcome, GROUP, "第一段", "one")
        self.clock.advance(1)
        await remember_accepted(self.engine, outcome, GROUP, "第二段", "two")
        await remember_accepted(self.engine, outcome, GROUP, "第二段", "two")
        self.clock.advance(1)
        await remember_accepted_images(self.engine, outcome, GROUP,
            ["https://example.com/photo.png"], "photo", [""], ["life_share"])
        await self.trim()
        choice = await self.choice()
        self.assertEqual(choice["own_turns"], 1)
        self.assertFalse(choice["ready"])
        ledger = self.db.raw.kv_get("outreach:" + self.engine.state_key(GROUP), {})
        self.assertEqual(len(ledger["speech_turns"][GROUP]), 1)
        self.assertEqual(ledger["speech_turns"][GROUP][0]["token"], outcome.speech_receipt_batch)

    async def test_prepared_and_failed_text_image_delivery_records_no_actual_turn(self):
        outcome = TickOutcome(GROUP, messages=["未发送草稿"], speech_kind="life_share")
        outcome.add_images(["https://example.com/draft.png"])
        self.assertEqual((await self.choice())["own_turns"], 0)
        self.messenger.send_text = AsyncMock(return_value=False)
        self.messenger.send_images = AsyncMock(return_value=False)
        await self.engine._deliver(outcome)
        ledger = self.db.raw.kv_get("outreach:" + self.engine.state_key(GROUP), {})
        self.assertFalse(ledger.get("speech_turns"))
        self.assertFalse(ledger.get("sessions"))

    async def test_image_only_accepted_receipt_keeps_space_after_trim(self):
        outcome = TickOutcome(GROUP, speech_kind="life_share")
        outcome.add_images(["https://example.com/accepted.png"])
        await self.engine._deliver(outcome)
        self.assertEqual(len(self.messenger.sent_images), 1)
        self.clock.advance(10)
        await self.trim()
        self.assertEqual((await self.choice())["own_turns"], 1)
        self.assertFalse((await self.choice())["ready"])

    async def test_legacy_actual_timestamp_survives_trim_without_inventing_a_turn(self):
        self.engine.note_visible_speech(GROUP)
        await self.trim()
        self.clock.advance(10)
        choice = await self.choice()
        self.assertFalse(choice["ready"])
        self.assertEqual(choice["own_turns"], 0)

    async def test_native_new_turn_at_same_time_invalidates_trimmed_plan_proof(self):
        outcome = TickOutcome(GROUP, speech_kind="continuation")
        proof = await self.proof(outcome)
        await self.engine.record_self_reply(GROUP, "另一个真实回应", speech_turn="new-native")
        await self.trim()
        self.assertFalse(await self.current(proof))

    async def test_same_batch_tail_stays_valid_after_trim_but_other_batch_invalidates_it(self):
        outcome = TickOutcome(GROUP, speech_kind="continuation")
        proof = await self.proof(outcome)
        await remember_accepted(self.engine, outcome, GROUP, "已经接收的第一段", "one")
        self.clock.advance(1)
        self.engine.note_visible_speech(GROUP, speech_turn=outcome.speech_receipt_batch)
        await self.trim()
        self.assertTrue(await self.current(proof, allow_turn=outcome.speech_receipt_batch))
        await self.engine.record_self_reply(GROUP, "别的真实回复", speech_turn="another-native")
        await self.trim()
        self.assertFalse(await self.current(proof, allow_turn=outcome.speech_receipt_batch))

    async def test_legacy_proof_does_not_treat_older_new_format_receipts_as_new_speech(self):
        self.engine.note_visible_speech(GROUP, speech_turn="older")
        self.clock.advance(1)
        outcome = TickOutcome(GROUP, speech_kind="continuation")
        proof = await self.proof(outcome)
        proof.pop("speech_seq")
        await self.trim()
        self.assertTrue(await self.current(proof))

    async def test_receipts_in_another_session_do_not_change_this_room_cadence(self):
        await self.engine.record_self_reply(OTHER, "另一间房的回复", speech_turn="other")
        choice = await self.choice()
        self.assertTrue(choice["ready"])
        self.assertEqual(choice["own_turns"], 0)


if __name__ == "__main__":
    unittest.main()
