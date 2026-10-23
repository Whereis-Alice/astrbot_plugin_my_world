"""Actual recall batches and native FriendMessage outreach cadence."""
import unittest
from unittest.mock import AsyncMock

from core.engine import TickOutcome
from tests import test_engine as fixture
from tests import test_config_scope as scopes


class RecallBatchReviewTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    def memories(self, count=2):
        return [self.engine.memory.remember(session_id=fixture.SESSION, persona_id="", node_id="study",
            content=f"生活里值得再想一想的不同经历 {index}", memory_type="scene", tier="gist",
            next_review_at=1, weight=.6) for index in range(count)]

    async def test_one_remaining_review_does_not_pick_two_or_reschedule_the_extra(self):
        self.engine.world.profile.recall_daily_max = 1
        ids = self.memories()
        async with self.engine.session_state(fixture.SESSION) as state:
            picked = self.engine.take_due_reviews(state)
            self.assertEqual(len(picked), 1)
            self.assertEqual(len(state.review_visits), 1)
            remaining = set(ids) - {picked[0]["id"]}
            self.assertEqual(self.db.raw.memory_by_id(remaining.pop())["next_review_at"], 1)

    async def test_partially_used_daily_allowance_caps_the_current_batch(self):
        self.engine.world.profile.recall_daily_max = 5
        self.memories()
        async with self.engine.session_state(fixture.SESSION) as state:
            day = self.engine._today_key(self.clock.now())
            state.review_visits = [{"day": day, "at": self.clock.now(), "memory_id": -index - 1} for index in range(4)]
            self.assertEqual(len(self.engine.take_due_reviews(state)), 1)
            self.assertEqual(len(state.review_visits), 5)
            self.assertEqual(self.engine.take_due_reviews(state), [])

    async def test_two_available_reviews_keep_the_normal_batch_and_the_extra_due(self):
        self.engine.world.profile.recall_daily_max = 5
        ids = self.memories(3)
        async with self.engine.session_state(fixture.SESSION) as state:
            picked = self.engine.take_due_reviews(state)
            self.assertEqual(len(picked), 2)
            self.assertEqual(len(state.review_visits), 2)
        extra = set(ids) - {row["id"] for row in picked}
        self.assertEqual(self.db.raw.memory_by_id(extra.pop())["next_review_at"], 1)

    async def test_deferred_review_is_still_available_next_day_after_reload(self):
        self.engine.world.profile.recall_daily_max = 1
        ids = self.memories()
        async with self.engine.session_state(fixture.SESSION) as state:
            first = self.engine.take_due_reviews(state)
        self.clock.advance(86401)
        self.engine.reload_config()
        self.engine.world.profile.recall_daily_max = 1
        async with self.engine.session_state(fixture.SESSION) as state:
            second = self.engine.take_due_reviews(state)
            self.assertEqual(len(second), 1)
            self.assertEqual(second[0]["id"], (set(ids) - {first[0]["id"]}).pop())
            self.assertEqual(len([row for row in state.review_visits
                if row["day"] == self.engine._today_key(self.clock.now())]), 1)

    async def test_disabled_recall_does_not_consume_or_reschedule(self):
        ids = self.memories()
        for kind in ("zero", "manual", "characters", "profile"):
            with self.subTest(kind=kind):
                self.engine.world.profile.recall_daily_max = 5
                self.engine.world.profile.enabled = True
                self.engine.world.memory.automatic_recall = "auto"
                self.engine.world.memory.recall_max_chars = 1200
                if kind == "zero":
                    self.engine.world.profile.recall_daily_max = 0
                elif kind == "manual":
                    self.engine.world.memory.automatic_recall = "manual"
                elif kind == "characters":
                    self.engine.world.memory.recall_max_chars = 0
                else:
                    self.engine.world.profile.enabled = False
                async with self.engine.session_state(fixture.SESSION) as state:
                    self.assertEqual(self.engine.take_due_reviews(state), [])
                    self.assertEqual(state.review_visits, [])
                self.assertTrue(all(self.db.raw.memory_by_id(mid)["next_review_at"] == 1 for mid in ids))


class NativePrivateContactReviewTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = scopes.ConfigScopeTests.asyncSetUp

    def configure(self, target="test:FriendMessage:42"):
        self.target = target
        self.store.add_session(target, session_type="private")
        raw = self.store.raw_world()
        raw["proactive_session_whitelist"] = [target]
        raw["profile"]["proactive_daily_max"] = 10
        raw["profile"]["proactive_person_daily_max"] = 2
        raw.setdefault("decider", {})["min_speech_gap_seconds"] = 0
        raw["decider"]["person_retry_hours"] = 1
        self.store.save_world(raw)
        self.engine.reload_config()

    def outcome(self, text="想问问你今天怎么样"):
        outcome = TickOutcome(self.target, speech_kind="outreach")
        outcome.add_speech(text)
        return outcome

    async def test_native_private_send_records_its_recipient_and_person_allowance(self):
        self.configure()
        outcome = self.outcome()
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.flat_messages, [outcome.messages[0]])
        self.assertEqual(self.engine.profiles.proactive_quota_left(self.target, "42", now=self.clock.now()), 1)
        ledger = self.engine._outreach_ledger(self.target)
        self.assertIn(self.engine._contact_person_key(self.target, "42"), ledger["people"])

    async def test_native_private_person_quota_is_checked_before_actual_send(self):
        self.configure()
        for _ in range(2):
            self.engine.profiles.note_proactive(self.target, "42", now=self.clock.now())
        self.assertFalse(self.engine.contact_speech_allowed(self.outcome(), self.target, "outreach"))
        await self.engine._deliver(self.outcome())
        self.assertEqual(self.messenger.flat_messages, [])

    async def test_native_private_waits_for_response_after_session_gap_has_elapsed(self):
        self.configure()
        await self.engine._deliver(self.outcome("第一句话"))
        self.clock.advance(180)
        await self.engine._deliver(self.outcome("还没回应就再次主动找他"))
        self.assertEqual(self.messenger.flat_messages, ["第一句话"])

    async def test_both_private_origin_names_and_colon_ids_record_the_complete_person(self):
        for kind, uid in (("FriendMessage", "42"), ("PrivateMessage", "43"), ("FriendMessage", "user:44")):
            with self.subTest(kind=kind, uid=uid):
                self.configure(f"test:{kind}:{uid}")
                await self.engine._deliver(self.outcome())
                self.assertEqual(self.engine.profiles.proactive_quota_left(self.target, uid, now=self.clock.now()), 1)
                self.assertIn(self.engine._contact_person_key(self.target, uid), self.engine._outreach_ledger(self.target)["people"])

    async def test_failed_native_private_send_releases_allowance_and_waiting_record(self):
        self.configure()
        self.messenger.send_text = AsyncMock(return_value=False)
        await self.engine._deliver(self.outcome())
        self.assertEqual(self.engine.profiles.proactive_quota_left(self.target, "42", now=self.clock.now()), 2)
        self.assertEqual(self.engine._outreach_ledger(self.target)["people"], {})
        self.assertEqual(self.engine._contact_pending, {})
        self.messenger.send_text = AsyncMock(return_value=True)
        await self.engine._deliver(self.outcome())
        self.assertEqual(self.engine.profiles.proactive_quota_left(self.target, "42", now=self.clock.now()), 1)

    async def test_native_private_segments_share_one_contact_but_cannot_reuse_it_later(self):
        self.configure()
        outcome = self.outcome("第一段")
        await self.engine._deliver(outcome)
        outcome.messages = ["同次表达的第二段"]
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.flat_messages, ["第一段", "同次表达的第二段"])
        self.assertEqual(self.engine.profiles.proactive_quota_left(self.target, "42", now=self.clock.now()), 1)
        self.clock.advance(180)
        outcome.messages = ["过期后再沿用原联系"]
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.flat_messages, ["第一段", "同次表达的第二段"])

    async def test_real_private_response_clears_waiting_and_allows_next_contact(self):
        self.configure()
        await self.engine._deliver(self.outcome("先找他"))
        self.clock.advance(180)
        await self.engine.handle_incoming(fixture.MessageContext(session_id=self.target, user_id="42", text="我在",
            is_private=True, is_wake=True, is_mentioned=True))
        self.assertEqual(self.engine._outreach_ledger(self.target)["people"], {})
        await self.engine._deliver(self.outcome("收到回应后再联系"))
        self.assertEqual(self.messenger.flat_messages, ["先找他", "收到回应后再联系"])
        self.assertEqual(self.engine.profiles.proactive_quota_left(self.target, "42", now=self.clock.now()), 0)

    async def test_normal_reply_remains_available_at_exhausted_person_allowance(self):
        self.configure()
        for _ in range(2):
            self.engine.profiles.note_proactive(self.target, "42", now=self.clock.now())
        reply = TickOutcome(self.target, speech_kind="reply")
        reply.add_speech("正常回答你的问题")
        await self.engine._deliver(reply)
        self.assertEqual(self.messenger.flat_messages, ["正常回答你的问题"])
        self.assertEqual(self.engine.profiles.proactive_quota_left(self.target, "42", now=self.clock.now()), 0)

    async def test_group_destinations_do_not_infer_a_recipient_from_the_group_id(self):
        self.configure(scopes.A)
        await self.engine._deliver(self.outcome("对群里说一句"))
        self.assertEqual(self.engine.profiles.proactive_quota_left(scopes.A, "100", now=self.clock.now()), 2)
        self.assertEqual(self.engine._outreach_ledger(scopes.A)["people"], {})


if __name__ == "__main__":
    unittest.main()
