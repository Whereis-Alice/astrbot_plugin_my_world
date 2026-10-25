"""Persona-first relationship rules and successful-contact accounting regressions."""
import asyncio
import unittest
from unittest.mock import AsyncMock

from core.defaults import default_world
from core.engine import MessageContext, TickOutcome
from core.json_actions import PlannedAction
from core.models import IntimacyLevel, parse_world
from core.planner import create_plan
from core.prompt import PromptBuilder
from core.relationship_prompts import FAMILIARITY_DESCRIPTIONS, INHERITED_LEVEL_PROMPTS
from tests import test_engine as fixtures

S = fixtures.SESSION


class RelationshipFreedomTests(unittest.TestCase):
    def test_optional_descriptions_preserve_legacy_edits_and_explicit_empty_values(self):
        world = parse_world(default_world())[0]
        self.assertFalse(world.profile.familiarity_descriptions_enabled)
        for level in world.profile.levels:
            self.assertEqual(level.familiarity_description, FAMILIARITY_DESCRIPTIONS[level.name])
        blank = IntimacyLevel.model_validate({"name": "朋友", "familiarity_description": ""})
        self.assertEqual(blank.familiarity_description, "")
        edited = IntimacyLevel.model_validate({"name": "自定义档", "familiarity_description": "彼此逐渐熟悉。"})
        self.assertEqual(IntimacyLevel.model_validate(edited.model_dump()).familiarity_description, "彼此逐渐熟悉。")
        self.assertEqual(IntimacyLevel.model_validate({"name": "自定义档"}).familiarity_description, "")

    def test_defaults_provide_distance_without_behavior_scripts(self):
        world = parse_world(default_world())[0]
        for level in world.profile.levels:
            self.assertEqual((level.prompt, level.deny), ("", []))
            self.assertNotIn("address", level.model_dump())
            self.assertNotIn("proactive_per_day", level.model_dump())
        self.assertTrue(all(not bond.prompt for bond in world.profile.bonds))

    def test_stock_rules_migrate_but_authored_notes_and_boundaries_survive(self):
        raw = dict(name="朋友", prompt=INHERITED_LEVEL_PROMPTS["朋友"],
                   deny=["hug", "kiss", "leg_photo"], address="主人", proactive_per_day=4)
        migrated = IntimacyLevel.model_validate(raw)
        self.assertEqual((migrated.prompt, migrated.deny), ("", []))
        self.assertNotIn("address", migrated.model_dump())
        self.assertNotIn("proactive_per_day", migrated.model_dump())
        explicit = IntimacyLevel.model_validate({**raw, "prompt_customized": True, "deny_customized": True})
        self.assertEqual((explicit.prompt, explicit.deny), (raw["prompt"], raw["deny"]))
        edited = IntimacyLevel.model_validate({**raw, "prompt": "习惯一起散步", "deny": ["custom_action"]})
        self.assertEqual((edited.prompt, edited.deny), ("习惯一起散步", ["custom_action"]))

    def test_retired_motive_caps_are_removed_preserving_explicit_disable(self):
        raw = default_world()
        raw["profile"]["miss_push_daily_max"] = 0
        raw["state_dynamics"]["desire_push_daily_max"] = 0
        world = parse_world(raw)[0]
        self.assertFalse(world.profile.miss_push_enabled)
        self.assertFalse(world.state_dynamics.desire_push_enabled)
        self.assertNotIn("miss_push_daily_max", world.profile.model_dump())
        self.assertNotIn("desire_push_daily_max", world.state_dynamics.model_dump())


class SuccessfulContactTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixtures.EngineTestCase.asyncSetUp
    set_state = fixtures.EngineTestCase.set_state
    get_state = fixtures.EngineTestCase.get_state
    add_group = fixtures.EngineTestCase.add_group

    async def prepare(self, total=6, person=2):
        self.engine.world.profile.proactive_daily_max = total
        self.engine.world.profile.proactive_person_daily_max = person
        await self.set_state(state="idle", current_action=None, current_plan=None)

    def outcome(self, token="contact", kind="outreach", text="过来看看你"):
        result = TickOutcome(S, contact_id=token, contact_people={"42"}, speech_kind=kind)
        result.add_speech(text)
        return result

    async def test_one_plan_split_across_deliveries_is_counted_once(self):
        await self.prepare(total=1, person=1)
        await self.engine._deliver(self.outcome())
        await self.engine._deliver(self.outcome(text="再接着说一句"))
        self.assertEqual(len(self.messenger.sent), 2)
        self.assertEqual(self.engine.contact_status(S, "42")["left"], 0)
        self.assertEqual(self.engine.contact_status(S, "42")["person_left"], 0)
        await self.engine._deliver(self.outcome(token="another"))
        self.assertEqual(len(self.messenger.sent), 2)

    async def test_failed_platform_send_does_not_consume_allowance(self):
        await self.prepare(total=1, person=1)
        self.messenger.send_text = AsyncMock(return_value=False)
        await self.engine._deliver(self.outcome())
        self.assertEqual(self.engine.contact_status(S, "42")["person_left"], 1)
        self.assertEqual(self.engine.contact_status(S)["left"], 1)
        self.assertEqual(self.engine._contact_pending, {})

    async def test_replies_events_schedules_and_debug_only_output_do_not_consume(self):
        await self.prepare(total=0, person=0)
        self.engine.world.life_share_min_interval_seconds = 0
        for kind in ("reply", "event", "schedule"):
            await self.engine._deliver(self.outcome(kind=kind))
        debug = TickOutcome(S)
        debug.add_debug("动作调试")
        await self.engine._deliver(debug)
        self.assertEqual(len(self.messenger.sent), 4)
        self.assertEqual(self.engine.profiles.proactive_quota_left(S, "42"), 0)
        self.assertEqual(self.db.raw.kv_get(f"proactive_contacts:{S}", {}), {})

    async def test_live_flush_and_final_delivery_share_one_contact(self):
        await self.prepare(total=1, person=1)
        self.engine.say_sink = AsyncMock(return_value=True)
        outcome = self.outcome()
        async with self.engine.session_state(S):
            await asyncio.wait_for(self.engine._flush_say(outcome), timeout=2)
        outcome.add_speech("处理完了")
        await self.engine._deliver(outcome)
        self.assertEqual(self.engine.say_sink.await_count, 1)
        self.assertEqual(self.messenger.sent[-1][1], ["处理完了"])
        self.assertEqual(self.engine.contact_status(S)["left"], 0)

    async def test_concurrent_sends_cannot_overshoot_total_limit(self):
        await self.prepare(total=1)
        entered, finish = asyncio.Event(), asyncio.Event()
        async def delayed(*args):
            entered.set()
            await finish.wait()
            return True
        self.messenger.send_text = AsyncMock(side_effect=delayed)
        first = asyncio.create_task(self.engine._deliver(self.outcome("one")))
        await asyncio.wait_for(entered.wait(), 2)
        try:
            await self.engine._deliver(self.outcome("two"))
            self.assertEqual(self.messenger.send_text.await_count, 1)
        finally:
            finish.set()
            await first
        self.assertEqual(self.engine.contact_status(S)["left"], 0)

    async def test_poke_success_and_following_message_count_once(self):
        await self.prepare(total=1, person=1)
        outcome = self.outcome(text="")
        async with self.engine.session_state(S) as state:
            await self.engine._execute_actions(state, self.engine.node(state.node_id), outcome,
                [PlannedAction(type="poke", target="42")], depth=0, autonomous=True)
        self.assertEqual(self.messenger.pokes, [(S, "42")])
        outcome.add_speech("在忙吗")
        await self.engine._deliver(outcome)
        self.assertEqual(self.engine.contact_status(S, "42")["person_left"], 0)
        self.assertEqual(self.messenger.sent[-1][1], ["在忙吗"])

    async def test_image_success_counts_when_text_failed_and_next_calendar_day_resets(self):
        await self.prepare(total=1, person=1)
        self.messenger.send_text = AsyncMock(return_value=False)
        outcome = self.outcome()
        outcome.add_images(["https://example.invalid/picture.png"])
        await self.engine._deliver(outcome)
        self.assertTrue(self.messenger.sent_images)
        self.assertEqual(self.engine.contact_status(S, "42")["person_left"], 0)
        self.clock.advance(86400)
        self.assertEqual(self.engine.contact_status(S, "42")["person_left"], 1)
        self.assertEqual(self.engine.contact_status(S)["left"], 1)

    async def test_group_total_is_shared_but_person_counts_stay_in_actual_conversation(self):
        self.add_group()
        await self.prepare(total=2, person=1)
        await self.engine._deliver(self.outcome())
        other = fixtures.OTHER_SESSION
        self.assertEqual(self.engine.contact_status(other)["left"], 1)
        self.assertEqual(self.engine.contact_status(other, "42")["person_left"], 1)
        self.assertTrue(self.engine.contact_status(other, "42")["reason"])
        await self.engine.note_presence(MessageContext(other, user_id="42", text="看到了", is_mentioned=True))
        second = TickOutcome(other, contact_id="second", contact_people={"42"}, speech_kind="outreach")
        second.add_speech("在这边接着聊")
        await self.engine._deliver(second)
        self.assertEqual(self.engine.contact_status(S)["left"], 0)
        self.assertEqual(self.engine.contact_status(other, "42")["person_left"], 0)

    async def test_private_destination_identifies_person_without_an_explicit_action(self):
        private = fixtures.PRIVATE_SESSION
        self.store.add_session(private, session_type="private", platform="aiocqhttp")
        self.engine.reload_config()
        await self.prepare(total=6, person=1)
        uid = private.rsplit(":", 1)[-1]
        outcome = TickOutcome(S, speech_kind="outreach")
        outcome.add_speech("想起一件事", private)
        await self.engine._deliver(outcome)
        self.assertEqual(self.engine.contact_status(private, uid)["person_left"], 0)
        self.assertEqual(self.engine.contact_status(S)["left"], 6)
        self.assertEqual(self.messenger.sent[-1], (private, ["想起一件事"]))

    async def test_zero_total_cap_retains_quiet_life_and_zero_hourly_limit_stays_closed(self):
        await self.prepare(total=0)
        self.engine.world.limits.max_autonomous_per_hour = 0
        state = await self.get_state()
        state.autonomous_hour_marker = -1
        self.assertFalse(self.engine._within_hourly_limit(state))
        self.assertIn("总额度", self.engine.contact_status(S)["reason"])

    async def test_plan_metadata_survives_continuation_and_does_not_charge_planning(self):
        await self.prepare(total=1, person=1)
        plan = create_plan(steps=[{"action": "say", "messages": ["先说一句"]},
                                  {"action": "say", "messages": ["再说一句"]}], world_time=0)
        plan["contact_people"] = ["42"]
        outcome = TickOutcome(S)
        async with self.engine.session_state(S) as state:
            await self.engine._apply_plan(state, self.engine.node(state.node_id), outcome, plan)
            self.assertEqual(self.engine.contact_status(S)["left"], 1)
            await self.engine._tick_plan(state, self.engine.node(state.node_id), outcome, depth=0)
        self.assertEqual(outcome.contact_id, plan["id"])
        self.assertIn("42", outcome.contact_people)
        await self.engine._deliver(outcome)
        self.assertEqual(self.engine.contact_status(S)["left"], 0)

    async def test_default_prompt_uses_identity_and_distance_with_optional_authored_notes(self):
        await self.prepare()
        profiles = self.engine.profiles
        profiles.touch(S, "42", "小明")
        profiles.note_bond(S, "42", type="朋友")
        view = profiles.view(S, "42")
        builder = PromptBuilder(self.engine.world)
        plain = builder.profile_block(view)
        self.assertIn("朋友", plain)
        self.assertIn("遵循原有人设", plain)
        self.assertNotIn("这一档还不能做", plain)
        self.assertNotIn("下一档", plain)
        self.engine.world.profile.bond_by_name("朋友").prompt = "同住一条街"
        view.level.prompt = "最近常一起散步"
        view.level.deny = ["hug"]
        custom = builder.profile_block(view)
        self.assertIn("同住一条街", custom)
        self.assertIn("最近常一起散步", custom)
        self.assertIn("这一档还不能做", custom)

    async def test_description_toggle_changes_only_current_distance_context(self):
        await self.prepare()
        profiles = self.engine.profiles
        profiles.touch(S, "42", "小明")
        profiles.note_bond(S, "42", type="朋友")
        profiles.note_fact(S, "42", text="喜欢猫", kind="喜好", evidence="我喜欢猫")
        builder = PromptBuilder(self.engine.world)
        view = profiles.view(S, "42")
        view.level.prompt = "最近常一起散步"
        view.level.deny = ["hug"]
        baseline = builder.profile_block(view)
        self.assertNotIn("当前熟悉程度", baseline)
        self.engine.world.profile.familiarity_descriptions_enabled = True
        enabled = builder.profile_block(view)
        line = f"- 当前熟悉程度：{view.level.familiarity_description}\n"
        self.assertEqual(enabled.replace(line, ""), baseline)
        self.engine.world.profile.familiarity_descriptions_enabled = False
        self.assertEqual(builder.profile_block(view), baseline)
        self.assertEqual(view.affinities, ["朋友"])
        self.assertEqual(view.level.prompt, "最近常一起散步")
        self.assertEqual(view.level.deny, ["hug"])
        view.level.familiarity_description = ""
        self.engine.world.profile.familiarity_descriptions_enabled = True
        self.assertEqual(builder.profile_block(view), baseline)

    async def test_engine_uses_actual_cold_distance_and_keeps_special_identity(self):
        await self.prepare()
        self.engine.world.profile.familiarity_descriptions_enabled = True
        profiles = self.engine.profiles
        profiles.touch(S, "42")
        profiles.note_bond(S, "42", type="男友", asserted_by="管理员设置")
        profiles.adjust_affinity(S, "42", 100)
        ctx = MessageContext(S, user_id="42", text="你好")
        async with self.engine.session_state(S) as state:
            warm = self.engine.profile_block(state, ctx)
            self.assertIn(FAMILIARITY_DESCRIPTIONS["特别的人"], warm)
            self.engine.note_grudge(state, "说了伤人的话", user_id="42", force=True)
            cold = self.engine.profile_block(state, ctx)
            self.assertIn(FAMILIARITY_DESCRIPTIONS["亲近"], cold)
            self.assertNotIn(FAMILIARITY_DESCRIPTIONS["特别的人"], cold)
            self.assertIn("男友", cold)
        self.assertEqual(profiles.view(S, "42").affinities, ["男友"])
