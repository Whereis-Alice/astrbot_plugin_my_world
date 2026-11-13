"""Slow planning preserves natural updates and still rejects changed life content."""
from __future__ import annotations

import asyncio
import copy
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from tests import fake_astrbot
fake_astrbot.install()

from core.engine.life_inference import LIFE_WORK, LifeWorkDiscarded, comparable_life_states, released_life_lock  # noqa: E402
from core.models import ActionDef  # noqa: E402
from tests import test_engine as fixture  # noqa: E402

S = fixture.SESSION
PERSON = "2692047521"


class NaturalLifeInferenceTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp
    ctx = fixture.EngineTestCase.ctx

    async def prepare(self):
        self.engine.world.sleep.mode = "activity"
        self.engine.world.state_dynamics.energy_decay_per_min = 0
        self.engine.world.state_dynamics.daily_mood_enabled = False
        async with self.engine.session_state(S) as state:
            state.state, state.node_id, state.energy = "idle", "study", .9
            state.current_action = state.current_plan = None
            state.life_updated_at = self.clock.now()

    async def rejoin_after(self, update):
        token = LIFE_WORK.set(True)
        try:
            async with self.engine.session_state(S) as state:
                async with released_life_lock(self.engine):
                    async def writer():
                        self.clock.advance(60)
                        async with self.engine.session_state(S) as latest:
                            update(latest)
                    await asyncio.create_task(writer())
                return copy.deepcopy(state)
        finally:
            LIFE_WORK.reset(token)

    async def test_an_event_scan_without_an_event_does_not_discard_and_keeps_the_new_scan_clock(self):
        await self.prepare()
        previous = copy.deepcopy((await self.engine.load_state(S, cold_start=False)).recent_events)
        state = await self.rejoin_after(lambda latest: self.engine._event_roll_gap_hours(latest, self.clock.now()))
        self.assertEqual(state.event_last_roll_at, self.clock.now())
        self.assertEqual(state.recent_events, previous)

    async def test_same_heart_knot_naturally_decays_and_preserves_its_latest_value(self):
        await self.prepare()
        async with self.engine.session_state(S) as state:
            self.engine.note_heart_knot(state, "还在惦记那件小事")
            strength = state.heart_knots[0]["strength"]
        state = await self.rejoin_after(lambda latest: self.engine._decay_heart_knots(latest, now=self.clock.now()))
        self.assertEqual(state.heart_knots[0]["at"], self.clock.now())
        self.assertAlmostEqual(state.heart_knots[0]["strength"], strength - self.engine.world.state_dynamics.heart_knot_decay_per_hour / 60)

    async def start_idle(self):
        await self.prepare()
        for action in self.engine.world.actions:
            if action.idle:
                action.enabled = False
        self.engine.world.actions.append(ActionDef(id="natural_practice", name="静静练习素描",
            category="continuous", idle=True, life_role="interest", duration_mode="fixed", duration=600))
        self.engine.world.idle_life.duration_min_minutes = 10
        self.engine.world.idle_life.duration_max_minutes = 10
        async with self.engine.session_state(S) as state:
            self.engine._tick_idle_life(state)

    def settle_idle_beat(self, latest):
        previous = latest.world_time
        latest.world_time += 1
        self.engine._capture_idle_progress(latest, previous)
        self.engine._settle_idle_practice(latest)

    async def test_same_idle_practice_receipt_rejoins_once_without_duplicating_interest_minutes(self):
        await self.start_idle()
        state = await self.rejoin_after(self.settle_idle_beat)
        identifier = state.idle_activity["practice_id"]
        self.assertEqual(state.life_interests["natural_practice"]["minutes"], 1)
        self.assertEqual(state.idle_activity["practice_accounted_ticks"], 1)
        state = await self.rejoin_after(self.settle_idle_beat)
        self.assertEqual(state.idle_activity["practice_id"], identifier)
        self.assertEqual(state.life_interests["natural_practice"]["minutes"], 2)
        self.assertEqual(state.life_interests["natural_practice"]["sessions"], 1)

    async def prepare_miss(self, score=.2):
        await self.prepare()
        await self.engine.handle_incoming(self.ctx(text="早上好", user_id=PERSON, user_name="熟悉的人"))
        self.engine.profiles.note_bond(S, PERSON, type="主人", asserted_by="她的判断")
        self.engine.profiles.adjust_affinity(S, PERSON, 80, reason="已有关系")
        async with self.engine.session_state(S) as state:
            state.miss[PERSON] = score
            state.miss_ready_at[PERSON] = self.clock.now() - 1

    def miss_beat(self, latest):
        latest.world_time += 1
        self.engine._update_miss(latest)

    async def test_small_predictable_miss_growth_does_not_discard_but_keeps_latest_score(self):
        await self.prepare_miss()
        state = await self.rejoin_after(self.miss_beat)
        self.assertGreater(state.miss[PERSON], .2)
        latest = await self.engine.load_state(S, cold_start=False)
        self.assertEqual(state.miss, latest.miss)

    async def test_changed_heart_content_or_unexplained_strength_still_discards(self):
        await self.prepare()
        async with self.engine.session_state(S) as state:
            self.engine.note_heart_knot(state, "还在惦记那件小事")
        for key, value in (("text", "遇上了另一件真正的新事"), ("strength", .1)):
            with self.subTest(key=key), self.assertRaises(LifeWorkDiscarded):
                await self.rejoin_after(lambda latest: latest.heart_knots[0].update({key: value}))

    async def test_miss_reset_large_jump_and_new_contact_threshold_still_discard(self):
        for score, update in ((.2, lambda latest: latest.miss.update({PERSON: .8})),
                              (.2, lambda latest: latest.miss.update({PERSON: 0})),
                              (.599, self.miss_beat)):
            await self.prepare_miss(score)
            with self.subTest(score=score), self.assertRaises(LifeWorkDiscarded):
                await self.rejoin_after(update)

    async def test_changed_idle_identity_and_unrelated_interest_content_still_discard(self):
        await self.start_idle()
        with self.assertRaises(LifeWorkDiscarded):
            await self.rejoin_after(lambda latest: latest.idle_activity.update(action_name="这是另一个活动"))
        with self.assertRaises(LifeWorkDiscarded):
            await self.rejoin_after(lambda latest: latest.life_interests.update({"unrelated": {"label": "真正的新经历", "minutes": 12}}))

    async def test_zero_rate_heart_maintenance_does_not_discard_a_timestamp_only_change(self):
        await self.prepare()
        self.engine.world.state_dynamics.heart_knot_decay_per_hour = 0
        async with self.engine.session_state(S) as state:
            self.engine.note_heart_knot(state, "仍然是同一件心事")
        state = await self.rejoin_after(lambda latest: self.engine._decay_heart_knots(latest, now=self.clock.now()))
        self.assertEqual(state.heart_knots[0]["strength"], .7)
        self.assertEqual(state.heart_knots[0]["at"], self.clock.now())

    async def test_all_four_production_fields_can_advance_together_without_discarding_the_plan(self):
        await self.start_idle()
        await self.prepare_miss()
        async with self.engine.session_state(S) as state:
            self.engine.note_heart_knot(state, "惦记同一件事")

        def heartbeat(latest):
            self.settle_idle_beat(latest)
            self.engine._event_roll_gap_hours(latest, self.clock.now())
            self.engine._update_miss(latest)
            self.engine._decay_heart_knots(latest, now=self.clock.now())

        state = await self.rejoin_after(heartbeat)
        self.assertEqual(state.event_last_roll_at, self.clock.now())
        self.assertEqual(state.life_interests["natural_practice"]["minutes"], 1)
        self.assertGreater(state.miss[PERSON], .2)
        self.assertEqual(state.heart_knots[0]["at"], self.clock.now())

    async def test_idle_completion_and_existing_practice_identity_changes_still_discard(self):
        await self.start_idle()
        await self.rejoin_after(self.settle_idle_beat)
        with self.assertRaises(LifeWorkDiscarded):
            await self.rejoin_after(lambda latest: latest.idle_activity.update(practice_id="another-real-execution"))
        with self.assertRaises(LifeWorkDiscarded):
            await self.rejoin_after(lambda latest: setattr(latest, "world_time", latest.idle_activity["until_tick"]))

    async def test_same_interest_label_edits_cannot_pass_as_a_natural_practice_receipt(self):
        await self.start_idle()
        await self.rejoin_after(self.settle_idle_beat)

        def edit_and_advance(latest):
            self.settle_idle_beat(latest)
            latest.life_interests["natural_practice"]["label"] = "另一段人为改写的经历"

        with self.assertRaises(LifeWorkDiscarded):
            await self.rejoin_after(edit_and_advance)
        latest = await self.engine.load_state(S, cold_start=False)
        self.assertEqual(latest.life_interests["natural_practice"]["label"], "另一段人为改写的经历")

    async def test_canonical_own_topic_mirror_refresh_does_not_discard_but_actual_matter_changes_do(self):
        await self.prepare()
        async with self.engine.session_state(S) as state:
            item = self.engine.add_life_matter(state, "读完这一小章", persona_key=self.engine._matter_persona_key(state) or "")
            item["last_attempt_at"] = self.clock.now()
            self.assertNotEqual(state.own_topics, state.life_matters)
        state = await self.rejoin_after(lambda latest: None)
        self.assertEqual(state.own_topics[0]["last_attempt_at"], self.clock.now() - 60)
        with self.assertRaises(LifeWorkDiscarded):
            await self.rejoin_after(lambda latest: latest.life_matters[0].update(text="换成另一件真正的打算"))

    async def test_multiple_jittered_practice_receipts_across_periods_rejoin_without_backdating(self):
        self.clock._now = datetime(2026, 11, 13, 17, 58, 30, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        self.engine.tick_seconds = 61.7
        await self.start_idle()

        def several_beats(latest):
            for delay in (75, 105, 62):
                self.clock.advance(delay)
                self.settle_idle_beat(latest)

        state = await self.rejoin_after(several_beats)
        interest = state.life_interests["natural_practice"]
        self.assertAlmostEqual(interest["minutes"], 3 * 61.7 / 60)
        self.assertEqual({episode["period"] for episode in interest["episodes"]}, {"白天", "晚间"})
        self.assertEqual(interest["last_at"], self.clock.now())
        self.assertEqual(state.idle_activity["practice_accounted_ticks"], 3)

    async def test_cross_midnight_discards_the_old_day_but_preserves_actual_practice_segments(self):
        self.clock._now = datetime(2026, 11, 13, 23, 58, 30, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        await self.start_idle()

        def next_day_beat(latest):
            self.clock.advance(75)
            self.settle_idle_beat(latest)

        with self.assertRaises(LifeWorkDiscarded):
            await self.rejoin_after(next_day_beat)
        latest = await self.engine.load_state(S, cold_start=False)
        interest = latest.life_interests["natural_practice"]
        self.assertEqual({row["day"] for row in interest["episodes"]}, {"2026-11-13", "2026-11-14"})
        self.assertEqual(interest["minutes"], 1)
        self.assertEqual(interest["last_at"], self.clock.now())

    async def test_practice_prediction_never_mutates_either_snapshot(self):
        await self.start_idle()
        before = (await self.engine.load_state(S, cold_start=False)).to_payload()
        self.clock.advance(60)
        async with self.engine.session_state(S) as latest:
            self.settle_idle_beat(latest)
        after = (await self.engine.load_state(S, cold_start=False)).to_payload()
        snapshots = copy.deepcopy((before, after))
        async with self.engine.session_state(S):
            first, last, retained = comparable_life_states(self.engine, before, after)
        self.assertEqual(first, last)
        self.assertIn("life_interests", retained)
        self.assertEqual((before, after), snapshots)

    async def test_invalid_heart_timestamp_discards_with_diagnostics_and_keeps_latest(self):
        await self.prepare()
        async with self.engine.session_state(S) as state:
            self.engine.note_heart_knot(state, "同一件心事")
        malformed = {"bad": 1}
        with patch.object(self.engine, "decision_trace") as trace, self.assertRaises(LifeWorkDiscarded):
            await self.rejoin_after(lambda latest: latest.heart_knots[0].update(at=malformed))
        self.assertIn("heart_knots", trace.call_args.kwargs["changed_fields"])
        latest = await self.engine.load_state(S, cold_start=False)
        self.assertEqual(latest.heart_knots[0]["at"], malformed)

    async def test_invalid_previous_miss_timestamp_discards_with_diagnostics_and_keeps_latest(self):
        await self.prepare_miss()
        async with self.engine.session_state(S) as state:
            state.miss_ready_at[PERSON] = {"bad": 1}

        def update(latest):
            latest.world_time += 1
            latest.miss[PERSON] = .2001

        with patch.object(self.engine, "decision_trace") as trace, self.assertRaises(LifeWorkDiscarded):
            await self.rejoin_after(update)
        self.assertIn("miss", trace.call_args.kwargs["changed_fields"])
        latest = await self.engine.load_state(S, cold_start=False)
        self.assertEqual(latest.miss[PERSON], .2001)
        self.assertEqual(latest.miss_ready_at[PERSON], {"bad": 1})

    async def test_saturated_tie_that_changes_actual_contact_target_discards(self):
        await self.prepare_miss()
        for person in ("200", "100"):
            await self.engine.handle_incoming(self.ctx(text="早上好", user_id=person, user_name=person))
            self.engine.profiles.note_bond(S, person, type="主人", asserted_by="她的判断")
            self.engine.profiles.adjust_affinity(S, person, 80, reason="已有关系")
        self.engine.world.profile.miss_growth_per_min = .01
        self.engine.world.profile.miss_min_level = 0
        with patch.object(self.engine, "contact_status", return_value={"reason": ""}):
            async with self.engine.session_state(S) as state:
                state.miss = {"200": .999, "100": 1.}
                state.miss_ready_at = {person: self.clock.now() - 1 for person in state.miss}
                self.assertEqual(self.engine._top_miss_candidate(state)[0], "100")
            with self.assertRaises(LifeWorkDiscarded):
                await self.rejoin_after(self.miss_beat)
            latest = await self.engine.load_state(S, cold_start=False)
            self.assertEqual(latest.miss, {"200": 1., "100": 1.})
            self.assertEqual(self.engine._top_miss_candidate(latest)[0], "200")

    async def test_validator_does_not_convert_shutdown_cancellation_to_a_natural_update(self):
        await self.prepare()
        before = (await self.engine.load_state(S, cold_start=False)).to_payload()
        after = copy.deepcopy(before)
        after["heart_knots"] = [{"text": "新的心事", "strength": .7}]
        with patch("core.engine.life_inference._natural_heart_decay", side_effect=asyncio.CancelledError), self.assertRaises(asyncio.CancelledError):
            comparable_life_states(self.engine, before, after)

    async def test_invalid_idle_deadline_discards_and_restores_latest_before_returning(self):
        await self.start_idle()
        with patch.object(self.engine, "decision_trace") as trace, self.assertRaises(LifeWorkDiscarded):
            await self.rejoin_after(lambda latest: latest.idle_activity.update(until_tick={"bad": 1}))
        self.assertIn("idle_activity", trace.call_args.kwargs["changed_fields"])
        latest = await self.engine.load_state(S, cold_start=False)
        self.assertEqual(latest.idle_activity["until_tick"], {"bad": 1})

    async def test_invalid_decision_bands_remain_observable_changes(self):
        await self.prepare()
        before = (await self.engine.load_state(S, cold_start=False)).to_payload()
        for source, band in (("energy", "energy_band"), ("valence", "valence_band"), ("sleep_pressure", "sleep_band")):
            for malformed in ({"bad": 1}, float("nan"), float("inf")):
                with self.subTest(source=source, malformed=malformed):
                    after = copy.deepcopy(before)
                    after[source] = malformed
                    first, last, _ = comparable_life_states(self.engine, before, after)
                    self.assertNotEqual(first[band], last[band])

    async def test_old_practice_episode_edits_do_not_pass_as_new_receipts(self):
        await self.start_idle()
        await self.rejoin_after(self.settle_idle_beat)

        def rewrite(latest):
            self.settle_idle_beat(latest)
            latest.life_interests["natural_practice"]["episodes"][0]["label"] = "改写旧经历"

        with self.assertRaises(LifeWorkDiscarded):
            await self.rejoin_after(rewrite)

    async def test_legacy_topics_remain_part_of_the_decision(self):
        await self.prepare()
        before = (await self.engine.load_state(S, cold_start=False)).to_payload()
        before.update(life_migrated=False, own_topics=[{"text": "旧版自己想做的事"}])
        after = copy.deepcopy(before)
        after["own_topics"][0]["text"] = "另一个打算"
        first, last, retained = comparable_life_states(self.engine, before, after)
        self.assertNotEqual(first["own_topics"], last["own_topics"])
        self.assertNotIn("own_topics", retained)

    async def test_legacy_unowned_interest_history_migrates_without_discarding_new_practice(self):
        await self.start_idle()
        first = await self.rejoin_after(self.settle_idle_beat)
        historical = copy.deepcopy(first.life_interests["natural_practice"])
        historical.pop("personas")
        async with self.engine.session_state(S) as state:
            state.life_interests["natural_practice"] = copy.deepcopy(historical)
            for name in ("practice_id", "practice_accounted_ticks"):
                state.idle_activity.pop(name, None)
            state.idle_activity["start_tick"] = state.world_time
            state.idle_activity["elapsed_ticks"] = 0
        state = await self.rejoin_after(self.settle_idle_beat)
        interest = state.life_interests["natural_practice"]
        self.assertEqual(interest["personas"][""], historical)
        self.assertEqual(interest["minutes"], 2)
        self.assertEqual(interest["sessions"], 2)
        self.assertEqual(len(interest["episodes"]), 2)


if __name__ == "__main__":
    unittest.main()
