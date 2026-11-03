"""Old plans and action completion cannot open another chat judgement outside attention cadence."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock

from core.background_budget import capture_background_admission, record_background_admission
from core.engine import TickOutcome
from core.engine.expression_attention import chat_expression_attention
from core.engine.participation import ParticipationMixin
from core.json_actions import PlannedAction
from core.models import ActionDef
from tests import test_participation as participation

GROUP, OTHER = participation.GROUP, participation.OTHER


class ExpressionAttentionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = participation.ParticipationTests.asyncSetUp
    chat = participation.ParticipationTests.chat

    def cadence(self):
        self.engine.world.decider.participation_level = "quiet"
        self.engine.participation_sample = ParticipationMixin.participation_sample.__get__(self.engine)

    async def generate(self, *args, **kwargs):
        record_background_admission(started=True)
        return json.dumps({"inner": "有一点想接这句话。", "intent": "continue_chat", "reply_to": "c1",
            "actions": [{"type": "say", "messages": ["我也想去看看。"]}]}, ensure_ascii=False)

    async def recent_scene(self):
        await self.chat("风车挺好看。", mine=True, age=10, message_id="own-speech")
        return await self.chat("想去山顶看看风车", age=5, message_id="windmill")

    async def completion(self, kind="life_share"):
        definition = ActionDef(id="sort_bookmarks", name="整理书签", category="continuous", duration=60,
            on_complete={"trigger": "llm_followup", "effects": {"energy": "+0.05"}})
        self.engine.world.actions.append(definition)
        outcome = TickOutcome(GROUP, speech_kind=kind)
        async with self.engine.session_state(GROUP) as state:
            state.energy = .6
            action = {"type": definition.id, "session": GROUP, "elapsed_ticks": 1, "speech_kind": kind}
            state.current_action = action
            await self.engine._finish_action(state, self.engine.node(state.node_id), outcome, action)
            self.assertIsNone(state.current_action)
            self.assertAlmostEqual(state.energy, .65)
        return outcome

    async def test_old_interjection_waits_after_actual_speech_without_generating(self):
        self.cadence()
        await self.recent_scene()
        self.engine.participation_text = AsyncMock(return_value=("", ""))
        outcome = TickOutcome(GROUP)
        async with self.engine.session_state(GROUP) as state:
            await self.engine._start_action(state, self.engine.node(state.node_id), outcome,
                self.engine.world.action_map()["say"], PlannedAction(type="say", interject=True),
                depth=0, autonomous=True)
        self.engine.participation_text.assert_not_awaited()
        self.assertFalse(outcome.messages)
        self.assertFalse(self.llm.calls)

    async def test_completion_borrowing_chat_permission_obeys_actual_speech_gap(self):
        self.cadence()
        self.engine.world.life_share_session_whitelist = []
        await self.recent_scene()
        self.engine._ask_llm = AsyncMock(side_effect=self.generate)
        for kind in ("life_share", "continuation"):
            with self.subTest(kind=kind):
                outcome = await self.completion(kind)
                self.assertFalse(outcome.messages)
        self.engine._ask_llm.assert_not_awaited()

    async def test_completion_charges_one_observation_after_gap_and_finishes_it(self):
        self.cadence()
        self.engine.world.life_share_session_whitelist = []
        await self.chat(message_id="windmill")
        self.engine._ask_llm = AsyncMock(side_effect=self.generate)
        outcome = await self.completion()
        self.engine._ask_llm.assert_awaited_once()
        self.assertEqual(outcome.messages, ["我也想去看看。"])
        state = await self.engine.load_state(GROUP, cold_start=False)
        self.assertEqual(state.participation[GROUP]["attempts"], [self.clock.now()])
        self.assertNotEqual(state.participation[GROUP]["decision"], "considering")

    async def test_native_reply_and_authorized_life_sharing_do_not_spend_chat_attention(self):
        self.cadence()
        await self.recent_scene()
        self.engine._ask_llm = AsyncMock(side_effect=self.generate)
        for kind in ("reply", "life_share"):
            with self.subTest(kind=kind):
                await self.completion(kind)
        self.assertEqual(self.engine._ask_llm.await_count, 2)
        state = await self.engine.load_state(GROUP, cold_start=False)
        self.assertFalse((state.participation.get(GROUP) or {}).get("attempts"))

    async def test_same_outcome_repair_and_accepted_tail_do_not_charge_twice(self):
        self.cadence()
        row = await self.chat(message_id="windmill")
        outcome = TickOutcome(GROUP)
        async with self.engine.session_state(GROUP) as state:
            for _ in range(2):
                async with chat_expression_attention(self.engine, state, GROUP, outcome, [row]) as allowed:
                    self.assertTrue(allowed)
                    record_background_admission(started=True)
            self.assertEqual(len(state.participation[GROUP]["attempts"]), 1)
            tail = TickOutcome(GROUP, accepted_speech_at={GROUP: self.clock.now()})
            async with chat_expression_attention(self.engine, state, GROUP, tail, [row]) as allowed:
                self.assertTrue(allowed)
            self.assertEqual(len(state.participation[GROUP]["attempts"]), 1)

    async def test_unstarted_deferral_rolls_back_only_its_own_observation(self):
        row = await self.chat(message_id="windmill")
        outcome = TickOutcome(GROUP)
        previous = {"checked_seq": 0, "evaluated_at": self.clock.now() - 500, "attempts": []}
        async with self.engine.session_state(GROUP) as state:
            state.participation[GROUP] = dict(previous)
            with capture_background_admission() as outer:
                record_background_admission(started=True)
                async with chat_expression_attention(self.engine, state, GROUP, outcome, [row]) as allowed:
                    self.assertTrue(allowed)
                    record_background_admission(deferred=True, reason="稍后再判断", next_at=self.clock.now() + 50)
                self.assertTrue(outer.started)
                self.assertTrue(outer.deferred)
                self.assertEqual(outer.reason, "稍后再判断")
            self.assertEqual(state.participation[GROUP]["checked_seq"], 0)
            self.assertEqual(state.participation[GROUP]["attempts"], [])
            self.assertEqual(state.participation[GROUP]["evaluated_at"], previous["evaluated_at"])
            self.assertEqual(state.participation[GROUP]["decision"], "deferred")

    async def test_unstarted_cancellation_does_not_overwrite_newer_owner(self):
        row = await self.chat(message_id="windmill")
        outcome = TickOutcome(GROUP)
        newer = {"request_id": "newer", "decision": "listening", "checked_seq": 999, "attempts": [1, 2]}
        async with self.engine.session_state(GROUP) as state:
            with self.assertRaises(asyncio.CancelledError):
                async with chat_expression_attention(self.engine, state, GROUP, outcome, [row]) as allowed:
                    self.assertTrue(allowed)
                    state.participation[GROUP] = dict(newer)
                    raise asyncio.CancelledError()
            self.assertEqual(state.participation[GROUP], newer)

    async def test_failed_started_judgement_keeps_attempt_and_merges_outer_admission(self):
        row = await self.chat(message_id="windmill")
        outcome = TickOutcome(GROUP)
        async with self.engine.session_state(GROUP) as state:
            with capture_background_admission() as outer:
                with self.assertRaises(ValueError):
                    async with chat_expression_attention(self.engine, state, GROUP, outcome, [row]) as allowed:
                        self.assertTrue(allowed)
                        record_background_admission(started=True)
                        raise ValueError("synthetic")
                self.assertTrue(outer.started)
            self.assertEqual(len(state.participation[GROUP]["attempts"]), 1)
            self.assertEqual(state.participation[GROUP]["decision"], "failed")

    async def test_shared_life_uses_actual_target_scene_and_keeps_other_observation(self):
        self.cadence()
        row = await self.chat(message_id="elsewhere", sid=OTHER)
        outcome = TickOutcome(GROUP)
        async with self.engine.session_state(GROUP) as state:
            state.note_chat(user_id="__self__", name="me", text="刚说过", origin=GROUP,
                            now=self.clock.now() - 1, is_self=True)
            group_record = {"request_id": "group", "sent_at": self.clock.now(), "decision": "joined"}
            state.participation[GROUP] = dict(group_record)
            async with chat_expression_attention(self.engine, state, OTHER, outcome, [row]) as allowed:
                self.assertTrue(allowed)
                record_background_admission(started=True)
            self.assertEqual(state.participation[GROUP], group_record)
            self.assertEqual(len(state.participation[OTHER]["attempts"]), 1)

    async def test_actual_destination_observation_override_controls_shared_attention(self):
        self.cadence()
        scoped = self.engine._base_runtime["world"].model_dump(mode="json", by_alias=True)
        scoped["decider"]["participation_level"] = "quiet"
        scoped["decider"]["participation_hourly_max"] = 1
        self.engine.store.save_scope(OTHER, scoped, {"schedules": []}, ["settings"])
        self.engine.reload_config()
        row = await self.chat(message_id="destination", sid=OTHER)
        outcome = TickOutcome(GROUP)
        async with self.engine.session_state(GROUP) as state:
            state.participation[GROUP] = {"attempts": [self.clock.now() - 10]}
            async with chat_expression_attention(self.engine, state, OTHER, outcome, [row]) as allowed:
                self.assertFalse(allowed)
            self.assertEqual(state.participation[OTHER]["opportunity"]["reason"],
                             "本轮观察预算已用完，等待新现场")
            self.assertEqual(state.participation[GROUP]["attempts"], [self.clock.now() - 10])
            self.assertFalse(state.participation[OTHER].get("attempts"))
