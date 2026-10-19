"""Relationships and autonomous life remain coherent across real entry points."""

import asyncio
import copy
import json
import unittest
from unittest.mock import AsyncMock

from core.engine import TickOutcome
from core.planner import create_plan
from core.relationship import friendship_progress
from core.state import WorldState
from tests import test_consolidate as consolidation
from tests import test_engine as engine
from tests import test_profile as profiles

SESSION = profiles.SESSION
DAY = 86400
START = 1_700_000_000.0


class RelationshipRulesTests(unittest.TestCase):
    setUp = profiles.ProfileTestCase.setUp
    _group_key = staticmethod(profiles.ProfileTestCase._group_key)

    def test_self_claim_is_pending_even_when_the_unique_slot_is_empty(self):
        self.store.touch(SESSION, "42", now=START)
        result = self.store.note_bond(SESSION, "42", type="男友", asserted_by="他自称")
        self.assertEqual(result["action"], "claim")
        self.assertEqual(self.store.view(SESSION, "42").affinities, ["陌生人"])
        self.assertEqual(self.store.profile(SESSION, "42")["affinity"], 0)

    def test_accepting_a_claim_sets_no_end_date_and_cannot_resurrect_the_past(self):
        self.store.touch(SESSION, "42", now=START)
        claim = self.store.note_bond(SESSION, "42", type="男友", asserted_by="他自称")
        self.assertTrue(self.store.accept_claim(SESSION, "42", bond_id=claim["id"])["ok"])
        self.assertEqual(self.db.get_user_bond(bond_id=claim["id"])["until"], 0)
        self.store.close_bond(SESSION, "42", bond_id=claim["id"])
        self.assertFalse(self.store.accept_claim(SESSION, "42", bond_id=claim["id"])["ok"])

    def test_ask_policy_does_not_replace_someone_elses_commitment(self):
        self.store.touch(SESSION, "42", now=START)
        self.store.touch(SESSION, "7", now=START)
        self.store.note_bond(SESSION, "42", type="男友")
        claim = self.store.note_bond(SESSION, "7", type="男友", asserted_by="他自称")
        self.assertFalse(self.store.accept_claim(SESSION, "7", bond_id=claim["id"], policy="ask")["ok"])
        self.assertIn("男友", self.store.view(SESSION, "42").affinities)

    def test_unique_slot_checks_every_type_even_if_the_first_type_is_nonunique(self):
        self.world.profile.bonds[0].slot = "romance"
        self.world.profile.bonds[0].unique = False
        self.assertTrue(self.store._unique_slot("romance"))

    def test_negative_relationship_limits_survive_an_identity_relationship(self):
        self.store.touch(SESSION, "42", now=START)
        self.store.note_bond(SESSION, "42", type="家人")
        self.store.note_bond(SESSION, "42", type="敌人")
        self.store.adjust_affinity(SESSION, "42", 100)
        view = self.store.view(SESSION, "42")
        self.assertEqual(view.level_index, 0)
        self.assertTrue(view.negative)

    def test_recent_conversation_preserves_affection_but_old_distance_can_soften(self):
        self.store.touch(SESSION, "42", now=START)
        self.store.adjust_affinity(SESSION, "42", 20)
        self.store.note_talked(SESSION, "42", now=START + 6 * DAY)
        self.assertEqual(self.store.decay_affinity(SESSION, now=START + 8 * DAY), [])
        self.assertTrue(self.store.decay_affinity(SESSION, now=START + 14 * DAY))
        self.store.adjust_affinity(SESSION, "42", -200)
        self.assertTrue(self.store.decay_affinity(SESSION, now=START + 15 * DAY))

    def test_friendship_needs_distinct_direct_conversation_days_and_affection(self):
        self.store.touch(SESSION, "42", now=START)
        self.store.adjust_affinity(SESSION, "42", 20)
        for index in range(30):
            self.store.note_talked(SESSION, "42", now=START + index)
        self.assertEqual(self.store.view(SESSION, "42").affinities, ["陌生人"])
        self.store.note_talked(profiles.PRIVATE, "42", now=START + DAY)
        self.assertEqual(self.store.view(SESSION, "42").affinities, ["陌生人"])
        self.store.note_talked(SESSION, "42", now=START + DAY)
        self.assertEqual(self.store.view(SESSION, "42").affinities, ["群友"])
        for day in range(2, 5):
            self.store.note_talked(SESSION, "42", now=START + day * DAY)
        view = self.store.view(SESSION, "42")
        self.assertEqual(view.affinities, ["朋友"])
        self.assertEqual(view.talk_days, 5)
        self.assertIn("群友", [bond["type"] for bond in view.past])
        for day in range(5, 20):
            self.store.note_talked(SESSION, "42", now=START + day * DAY)
        self.assertEqual(self.store.view(SESSION, "42").affinities, ["朋友"])

    def test_presence_and_insufficient_affection_cannot_create_friendship(self):
        for day in range(8):
            self.store.touch(SESSION, "42", now=START + day * DAY)
        self.assertEqual(friendship_progress(self.store, SESSION, "42")["talk_days"], 0)
        for day in range(8):
            self.store.note_talked(SESSION, "42", now=START + day * DAY)
        self.assertEqual(self.store.view(SESSION, "42").affinities, ["陌生人"])

    def test_manual_relations_and_the_auto_switch_are_respected(self):
        self.store.touch(SESSION, "42", now=START)
        self.store.note_bond(SESSION, "42", type="群友", asserted_by="管理员设置")
        self.store.adjust_affinity(SESSION, "42", 100)
        for day in range(8):
            self.store.note_talked(SESSION, "42", now=START + day * DAY)
        self.assertEqual(self.store.view(SESSION, "42").affinities, ["群友"])
        self.store.touch(SESSION, "7", now=START)
        self.world.profile.relationship_auto_enabled = False
        self.store.adjust_affinity(SESSION, "7", 100)
        for day in range(8):
            self.store.note_talked(SESSION, "7", now=START + day * DAY)
        self.assertEqual(self.store.view(SESSION, "7").affinities, ["陌生人"])

    def test_model_affection_rejects_nonfinite_values(self):
        self.store.touch(SESSION, "42", now=START)
        self.assertEqual(self.store.apply_reply_affinity(SESSION, "42", float("nan")), 0)
        self.assertEqual(self.store.apply_reply_affinity(SESSION, "42", float("inf")), 0)
        self.assertFalse(self.store.adjust_affinity(SESSION, "42", float("nan"))["ok"])

    def test_boundary_crossing_is_configurable_and_does_not_change_identity(self):
        self.store.touch(SESSION, "42", now=START)
        self.store.note_bond(SESSION, "42", type="朋友")
        self.store.adjust_affinity(SESSION, "42", 100)
        before = self.store.view(SESSION, "42").level_index
        self.world.profile.bond_by_name("朋友").allow_boundary_crossing = True
        view = self.store.view(SESSION, "42")
        self.assertGreater(view.level_index, before)
        self.assertEqual(view.affinities, ["朋友"])
        self.store.note_bond(SESSION, "42", type="敌人")
        self.assertEqual(self.store.view(SESSION, "42").level_index, 0)

    def test_automatic_friendship_cannot_replace_a_manually_only_identity(self):
        self.store.touch(SESSION, "42", now=START)
        self.store.note_bond(SESSION, "42", type="男友", asserted_by="管理员设置")
        result = self.store.note_bond(SESSION, "42", type="朋友", automatic=True)
        self.assertEqual(result["action"], "claim")
        self.assertEqual(self.store.view(SESSION, "42").affinities, ["男友"])

    def test_custom_level_gaps_do_not_jump_to_maximum_intimacy(self):
        config = self.world.profile
        config.levels[2].max_affinity = 5
        self.assertEqual(config.level_index(7), 2)
        self.assertEqual(config.level_index(-1000), 0)

    def test_binding_an_identity_does_not_erase_negative_feelings(self):
        self.store.touch(SESSION, "42", now=START)
        self.store.adjust_affinity(SESSION, "42", -30)
        self.store.note_bond(SESSION, "42", type="男友")
        self.assertEqual(self.store.view(SESSION, "42").affinity, -30)

    def test_admin_reasserting_an_existing_relation_stops_automatic_replacement(self):
        self.store.touch(SESSION, "42", now=START)
        self.store.note_bond(SESSION, "42", type="群友")
        self.store.note_bond(SESSION, "42", type="群友", asserted_by="管理员设置")
        self.store.adjust_affinity(SESSION, "42", 100)
        for day in range(8):
            self.store.note_talked(SESSION, "42", now=START + day * DAY)
        self.assertEqual(self.store.view(SESSION, "42").affinities, ["群友"])

    def test_crossing_a_distance_ceiling_does_not_stop_ordinary_friendship_stages(self):
        self.world.profile.bond_by_name("群友").allow_boundary_crossing = True
        self.store.touch(SESSION, "42", now=START)
        self.store.adjust_affinity(SESSION, "42", 30)
        for day in range(5):
            self.store.note_talked(SESSION, "42", now=START + day * DAY)
        self.assertEqual(self.store.view(SESSION, "42").affinities, ["朋友"])


class AutonomousContinuityTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = engine.EngineTestCase.asyncSetUp

    async def test_paused_speech_keeps_quiet_activity_without_ordinary_model_calls(self):
        raw = self.store.raw_world()
        raw["proactive_chat_paused"] = True
        self.store.save_world(raw)
        self.engine.reload_config()
        self.engine.decider.rule_plan = lambda *args, **kwargs: create_plan(
            steps=[{"action": "read", "duration": 120}], world_time=10,
            valid_for=3600, reason="继续读书", source="rule")
        async with self.engine.session_state(engine.SESSION) as state:
            state.current_action = None
            state.current_plan = None
            state.state = "idle"
        self.engine._ask_llm_for_plan = AsyncMock()
        await self.engine.maybe_decide(engine.SESSION, force=True)
        state = await self.engine.load_state(engine.SESSION)
        self.assertEqual(state.current_action["type"], "read")
        self.assertEqual(state.current_action["speech_kind"], "quiet")
        self.engine._ask_llm_for_plan.assert_not_awaited()
        self.assertEqual(self.messenger.flat_messages, [])

    async def test_arrival_while_paused_continues_a_quiet_rule_plan(self):
        raw = self.store.raw_world()
        raw["proactive_chat_paused"] = True
        self.store.save_world(raw)
        self.engine.reload_config()
        state = WorldState(engine.SESSION, node_id="study", state="idle", world_time=10)
        self.engine._quiet_rule_plan = AsyncMock()
        await self.engine.decide_after_arrival(state, self.engine.node("study"), TickOutcome(engine.SESSION))
        self.engine._quiet_rule_plan.assert_awaited_once()

    async def test_background_timeout_and_cancellation_release_the_call(self):
        channel = type("Slow", (), {"generate": AsyncMock(side_effect=lambda **kwargs: None)})()
        async def stalled(**kwargs):
            await asyncio.Event().wait()
        channel.generate.side_effect = stalled
        self.engine.world.limits.background_llm_timeout_seconds = 0.02
        with self.assertRaises(TimeoutError):
            await self.engine._background_generate(channel, session_id=engine.SESSION)
        self.engine.world.limits.background_llm_timeout_seconds = 120
        self.engine._background_failures.clear()  # Test cancellation separately from failure backoff.
        task = asyncio.create_task(self.engine._background_generate(channel))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_same_promise_to_two_people_is_not_merged(self):
        state = WorldState(engine.SESSION, node_id="study")
        self.engine.note_own_topic(state, "周末一起去看星星", user_id="42")
        self.engine.note_own_topic(state, "周末一起去看星星", user_id="7")
        self.assertEqual({item["who"] for item in state.own_topics}, {"42", "7"})

    async def test_expired_intention_is_remembered_as_unfinished_once(self):
        state = WorldState(engine.SESSION, node_id="study")
        self.engine.note_own_topic(state, "修好旧风车", user_id="42")
        state.life_matters[0]["kind"] = "idea"
        now = self.clock.now() + 10 * DAY
        self.engine._decay_own_topics(state, now=now)
        self.engine._decay_own_topics(state, now=now + DAY)
        rows = self.db.raw.query_memories(session_id=engine.SESSION)
        found = [row for row in rows if "修好旧风车" in row["content"]]
        self.assertEqual(len(found), 1)
        self.assertIn("尚未确认完成", found[0]["content"])
        self.assertFalse(state.own_topics)

    async def test_miss_preview_explains_a_pause_and_does_not_reset_counters(self):
        self.engine.profiles.touch(engine.SESSION, "42", now=self.clock.now())
        self.engine.profiles.note_bond(engine.SESSION, "42", type="朋友")
        state = WorldState(engine.SESSION, node_id="study", miss={"42": 0.9})
        raw = self.store.raw_world()
        raw["proactive_chat_paused"] = True
        self.store.save_world(raw)
        self.engine.reload_config()
        before = copy.deepcopy(state.to_payload())
        overview = self.engine.miss_overview(state)
        self.assertEqual(state.to_payload(), before)
        self.assertFalse(overview["people"][0]["will_reach_out"])
        self.assertIn("已暂停", overview["people"][0]["reason"])


class RelationshipEvidenceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = consolidation.ConsolidateTestCase.asyncSetUp

    async def run_relation(self, records, *, confirmation=""):
        uid = consolidation.USER
        self.profiles.touch(consolidation.SESSION, uid, now=START)
        payload = {"relations": [{"user_id": uid, "type": "男友", "evidence": "我是你男朋友",
                                  "asserted_by": "她的判断", "confirmation": confirmation}]}
        return await self.consolidator.run(session_id=consolidation.SESSION,
            llm=AsyncMock(return_value=json.dumps(payload, ensure_ascii=False)), chat_records=records, now=START)

    async def test_fabricated_or_another_persons_quote_is_rejected(self):
        result = await self.run_relation([{"user_id": "someone-else", "text": "我是你男朋友"}])
        self.assertEqual(result.relations, 0)
        self.assertEqual(self.profiles.view(consolidation.SESSION, consolidation.USER).affinities, ["陌生人"])

    async def test_one_sided_statement_cannot_become_a_commitment(self):
        await self.run_relation([{"user_id": consolidation.USER, "text": "我是你男朋友"}])
        self.assertEqual(self.profiles.view(consolidation.SESSION, consolidation.USER).affinities, ["陌生人"])
        self.assertEqual(len(self.profiles.bonds(consolidation.SESSION, consolidation.USER, statuses=["claimed"])), 1)

    async def test_two_grounded_quotes_allow_a_confirmed_commitment(self):
        self.world.profile.bond_by_name("男友").auto_form = True
        await self.run_relation([{"user_id": consolidation.USER, "text": "我是你男朋友"},
                                {"is_self": True, "text": "好，我认你当我的男朋友"}],
                                confirmation="我认你当我的男朋友")
        self.assertIn("男友", self.profiles.view(consolidation.SESSION, consolidation.USER).affinities)

    async def test_special_relationship_stays_manual_only_by_default_even_with_confirmation(self):
        await self.run_relation([{"user_id": consolidation.USER, "text": "我是你男朋友"},
                                {"is_self": True, "text": "我认你当我的男朋友"}],
                                confirmation="我认你当我的男朋友")
        self.assertEqual(self.profiles.view(consolidation.SESSION, consolidation.USER).affinities, ["陌生人"])

    async def test_confirmation_to_someone_else_or_in_another_session_is_not_reused(self):
        self.world.profile.bond_by_name("男友").auto_form = True
        for records in (
            [{"user_id": consolidation.USER, "text": "我是你男朋友", "origin": "group"},
             {"user_id": "other", "text": "看这里", "origin": "group"},
             {"is_self": True, "text": "我认你当我的男朋友", "origin": "group"}],
            [{"user_id": consolidation.USER, "text": "我是你男朋友", "origin": "group"},
             {"is_self": True, "text": "我认你当我的男朋友", "origin": "private"}],
        ):
            await self.run_relation(records, confirmation="我认你当我的男朋友")
            self.assertEqual(self.profiles.view(consolidation.SESSION, consolidation.USER).affinities, ["陌生人"])

    async def test_consolidation_shares_the_daily_affection_limit_with_chat(self):
        uid = consolidation.USER
        self.profiles.touch(consolidation.SESSION, uid, now=START)
        for _ in range(10):
            self.profiles.apply_reply_affinity(consolidation.SESSION, uid, 3, now=START)
        before = self.profiles.profile(consolidation.SESSION, uid)["affinity"]
        result = await self.consolidator.run(session_id=consolidation.SESSION,
            llm=AsyncMock(return_value=json.dumps({"affinity": [{"user_id": uid, "delta": 1000}]})),
            chat_records=[{"user_id": uid, "text": "普通聊天"}], now=START)
        self.assertEqual(result.affinity, 0)
        self.assertEqual(self.profiles.profile(consolidation.SESSION, uid)["affinity"], before)
