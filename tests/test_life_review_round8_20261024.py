"""Current-character life rhythm and promise completion through real entrypoints."""
import copy
import json
import unittest
from datetime import datetime
from unittest.mock import AsyncMock

from core import life_matters as matters
from core.context_sources import life_items
from core.models import ActionDef
from tests import test_life_audit as audit
from tests import test_life_api as api
from tests.test_independent_life import quiet_action

S = audit.S


class LifeRhythmReviewTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = audit.LifeAuditTests.asyncSetUp
    idle = audit.LifeAuditTests.idle

    async def practice(self):
        await self.idle()
        for _ in range(3):
            self.clock.advance(86400)
            async with self.engine.session_state(S) as state:
                self.engine.settle_life_practice(state, {"type": "read", "elapsed_ticks": 20,
                    "persona_key": self.engine._matter_persona_key(state)})

    async def test_another_character_does_not_receive_the_previous_reading_rhythm(self):
        await self.practice()
        original = self.persona.text
        async with self.engine.session_state(S) as state:
            self.assertTrue(self.engine.life_overview(state, S)["rhythms"])
        self.persona.text = "另一位独立的角色，没有此前的阅读经历。"
        async with self.engine.session_state(S) as state:
            self.assertEqual(self.engine.life_overview(state, S)["rhythms"], [])
        self.persona.text = original
        async with self.engine.session_state(S) as state:
            self.assertTrue(self.engine.life_overview(state, S)["rhythms"])

    async def test_another_character_does_not_receive_the_previous_activity_context(self):
        await self.practice()
        self.persona.text = "另一位独立的角色，没有此前的阅读经历。"
        async with self.engine.session_state(S) as state:
            request = self.engine.context_request(state, S, task="plan", rows=[])
            await self.engine.prepare_context_memory(request)
            self.assertEqual([item for item in life_items(state, request) if item.kind == "experience"], [])
            self.assertNotIn("实际活动", matters.activity_basis(state,
                now=self.clock.now(), persona_key=request.life_persona))

    async def test_snapshot_selects_the_current_characters_practice_without_rewriting(self):
        await self.practice()
        original = self.persona.text
        before = copy.deepcopy((await self.engine.load_state(S, cold_start=False)).life_interests)
        self.persona.text = "另一位独立的角色。"
        self.assertEqual((await self.engine.snapshot(S))["life_interests"], {})
        self.persona.text = original
        result = (await self.engine.snapshot(S))["life_interests"]
        self.assertEqual(result["read"]["minutes"], 60)
        self.assertNotIn("personas", result["read"])
        self.assertEqual((await self.engine.load_state(S, cold_start=False)).life_interests, before)

    async def test_each_character_keeps_their_own_practice_and_context(self):
        await self.practice()
        original = self.persona.text
        self.persona.text = "另一位独立的角色。"
        async with self.engine.session_state(S) as state:
            key = self.engine._matter_persona_key(state)
            self.engine.settle_life_practice(state, {"type": "read", "elapsed_ticks": 7, "persona_key": key})
            self.assertEqual(matters.practice_records(state, persona_key=key)["read"]["minutes"], 7)
            self.assertEqual(matters.stable_interests(state, persona_key=key), [])
            self.assertEqual(state.life_interests["read"]["minutes"], 67)
        self.persona.text = original
        async with self.engine.session_state(S) as state:
            request = self.engine.context_request(state, S, task="plan", rows=[])
            await self.engine.prepare_context_memory(request)
            experience = [item for item in life_items(state, request) if item.kind == "experience"]
            self.assertEqual(len(experience), 1)
            self.assertIn("60 分钟", experience[0].summary)

    async def test_incremental_practice_survives_save_reload_and_midnight_once(self):
        await self.idle()
        self.clock._now = datetime(2026, 10, 20, 0, 10).timestamp()
        async with self.engine.session_state(S) as state:
            key = self.engine._matter_persona_key(state)
            state.current_action = {"type": "read", "duration_ticks": 60, "elapsed_ticks": 20, "persona_key": key}
            self.engine.settle_life_practice(state, state.current_action)
        self.engine.reload_config()
        self.clock.advance(300)
        async with self.engine.session_state(S) as state:
            state.current_action["elapsed_ticks"] = 25
            self.engine.settle_life_practice(state, state.current_action)
            self.engine.settle_life_practice(state, state.current_action)
            record = matters.practice_records(state, persona_key=key)["read"]
            self.assertEqual((record["minutes"], record["sessions"]), (25, 1))
            self.assertEqual(record["days"], ["2026-10-19", "2026-10-20"])
            self.assertEqual([row["minutes"] for row in record["episodes"]], [10, 15])
            self.assertEqual(state.current_action["practice_accounted_ticks"], 25)

    async def test_legacy_practice_is_preserved_unmarked_and_not_counted_twice(self):
        await self.idle()
        legacy = {"label": "旧阅读", "sessions": 2, "minutes": 20, "days": ["2026-10-18"], "last_at": 100}
        async with self.engine.session_state(S) as state:
            state.life_interests = {"read": copy.deepcopy(legacy)}
            key = self.engine._matter_persona_key(state)
            self.assertEqual(matters.practice_records(state, persona_key=key), {"read": legacy})
            self.assertNotIn("personas", state.life_interests["read"])
            action = {"type": "read", "elapsed_ticks": 5, "persona_key": key}
            self.engine.settle_life_practice(state, action)
            self.engine.settle_life_practice(state, action)
            self.assertEqual(state.life_interests["read"]["personas"][""], legacy)
            self.assertEqual(matters.practice_records(state, persona_key=key)["read"]["minutes"], 25)
            self.assertEqual(matters.practice_records(state, persona_key=key)["read"]["sessions"], 3)
        self.persona.text = "另一位独立的角色。"
        async with self.engine.session_state(S) as state:
            self.assertEqual(matters.practice_records(state, persona_key=self.engine._matter_persona_key(state))["read"]["minutes"], 20)

    async def test_rule_motivation_uses_only_current_activity_and_rhythm(self):
        await self.practice()
        original = self.persona.text
        self.engine.world.actions.append(ActionDef(id="draw", name="画一会儿", life_role="interest", category="continuous", duration=600))
        async with self.engine.session_state(S) as state:
            state.energy, state.boredom = .9, .7
            plan = self.engine.decider._custom_life_plan(state, grounded_only=True)
            self.assertEqual(plan["steps"][-1]["action"], "draw")
            self.assertIn("看书", plan["reason"])
        self.persona.text = "另一位独立的角色。"
        async with self.engine.session_state(S) as state:
            self.assertIsNone(self.engine.decider._custom_life_plan(state, grounded_only=True))
        self.persona.text = original
        self.clock.advance(2 * 3600)
        async with self.engine.session_state(S) as state:
            plan = self.engine.decider._custom_life_plan(state, grounded_only=True)
            self.assertEqual(plan["steps"][-1]["action"], "read")
            self.assertIn("不同日期", plan["reason"])

    async def test_practice_storage_stays_bounded_for_many_characters(self):
        await self.idle()
        for index in range(35):
            self.persona.text = f"独立角色{index}"
            self.clock.advance(60)
            async with self.engine.session_state(S) as state:
                self.engine.settle_life_practice(state, {"type": "read", "elapsed_ticks": 1,
                    "persona_key": self.engine._matter_persona_key(state)})
        async with self.engine.session_state(S) as state:
            self.assertEqual(len(state.life_interests["read"]["personas"]), 31)
            self.assertEqual(state.life_interests["read"]["minutes"], 35)
            record = matters.practice_records(state, persona_key=self.engine._matter_persona_key(state))["read"]
            self.assertEqual(record["minutes"], 1)
            before = copy.deepcopy(state.life_interests)
            record["episodes"].clear()
            self.assertEqual(state.life_interests, before)

    async def test_native_reflection_cannot_complete_another_characters_promise(self):
        await self.idle()
        async with self.engine.session_state(S) as state:
            item = self.engine.add_life_matter(state, "给42发照片", who="42", session=S, kind="promise",
                persona_key=self.engine._matter_persona_key(state))
            before = copy.deepcopy(item)
        self.persona.text = "另一位独立的角色。"
        self.engine._ask_judge = AsyncMock(return_value=json.dumps({"own_topic_done": item["id"],
            "own_topic_evidence": "图片已发送给42"}))
        await self.engine.reflect_turn(S, user_id="42", user_text="收到了", reply_text="嗯",
            tool_trace=[{"name": "send_image", "ok": True, "result": "图片已发送给42"}])
        async with self.engine.session_state(S) as state:
            self.assertEqual(matters.find(state, item["id"]), before)

    async def test_another_character_does_not_collect_old_idle_completion(self):
        self.engine.world.actions.append(quiet_action("review_idle", on_complete={"effects": {"energy": "+0.2"}}))
        self.engine.world.actions = [self.engine.world.actions[-1]]
        self.engine.world.idle_life.enabled = True
        async with self.engine.session_state(S) as state:
            state.state, state.energy = "idle", .5
            self.engine._tick_idle_life(state)
            until = state.idle_activity["until_tick"]
        self.persona.text = "另一位独立的角色。"
        async with self.engine.session_state(S) as state:
            state.world_time = until
            self.engine._tick_idle_life(state)
            self.assertAlmostEqual(state.energy, .5)

    async def test_same_character_receives_idle_completion_only_once(self):
        self.engine.world.actions = [quiet_action("review_idle", on_complete={"effects": {"energy": "+0.2"}})]
        self.engine.world.idle_life.enabled = True
        async with self.engine.session_state(S) as state:
            state.state, state.energy = "idle", .5
            self.engine._tick_idle_life(state)
            key, until = self.engine._matter_persona_key(state), state.idle_activity["until_tick"]
            self.assertEqual(state.idle_activity["persona_key"], key)
        async with self.engine.session_state(S) as state:
            state.world_time = until
            self.engine._tick_idle_life(state)
            self.engine._tick_idle_life(state)
            self.assertAlmostEqual(state.energy, .7)
            record = matters.practice_records(state, persona_key=key)["review_idle"]
            self.assertEqual(record["sessions"], 1)

    async def test_wrong_character_cannot_settle_a_detached_old_action(self):
        await self.idle()
        async with self.engine.session_state(S) as state:
            action = {"type": "read", "elapsed_ticks": 20, "persona_key": self.engine._matter_persona_key(state)}
        self.persona.text = "另一位独立的角色。"
        async with self.engine.session_state(S) as state:
            self.engine.settle_life_practice(state, action)
            self.assertEqual(state.life_interests, {})

    async def test_same_character_native_reflection_can_complete_their_promise(self):
        await self.idle()
        async with self.engine.session_state(S) as state:
            item = self.engine.add_life_matter(state, "给42发照片", who="42", session=S, kind="promise",
                persona_key=self.engine._matter_persona_key(state))
        self.engine._ask_judge = AsyncMock(return_value=json.dumps({"own_topic_done": item["id"],
            "own_topic_evidence": "图片已发送给42"}))
        await self.engine.reflect_turn(S, user_id="42", user_text="收到了", reply_text="嗯",
            tool_trace=[{"name": "send_image", "ok": True, "result": "图片已发送给42"}])
        async with self.engine.session_state(S) as state:
            self.assertEqual(matters.find(state, item["id"])["status"], "completed")

    async def test_own_topic_completion_rejects_other_character_and_allows_original(self):
        await self.idle()
        original = self.persona.text
        async with self.engine.session_state(S) as state:
            item = self.engine.add_life_matter(state, "给42发照片", who="42", session=S, kind="promise",
                persona_key=self.engine._matter_persona_key(state))
        self.persona.text = "另一位独立的角色。"
        async with self.engine.session_state(S) as state:
            self.assertEqual(self.engine.mark_own_topic_done(state, user_id="42", matter_id=item["id"],
                evidence="图片已发送给42", confirmed=True), 0)
        self.persona.text = original
        async with self.engine.session_state(S) as state:
            self.assertEqual(self.engine.mark_own_topic_done(state, user_id="42", matter_id=item["id"],
                evidence="图片已发送给42", confirmed=True), 1)


class LifePracticeApiReviewTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = api.LifeApiTests.asyncSetUp
    call = api.LifeApiTests.call

    async def test_life_api_and_forgetting_select_current_character_only(self):
        engine = self.plugin.engine
        engine._persona_text = AsyncMock(return_value="角色甲")
        async with engine.session_state(self.session) as state:
            state.state = "idle"
            engine.settle_life_practice(state, {"type": "read", "elapsed_ticks": 5,
                "persona_key": engine._matter_persona_key(state)})
        engine._persona_text.return_value = "角色乙"
        self.assertEqual((await self.call("life", session=self.session))["data"]["interests"], {})
        response = await self.call("life/update", method="POST", session=self.session, operation="forget_experience", action="read")
        self.assertEqual(response["status_code"], 400)
        async with engine.session_state(self.session) as state:
            engine.settle_life_practice(state, {"type": "read", "elapsed_ticks": 3,
                "persona_key": engine._matter_persona_key(state)})
        self.assertEqual((await self.call("life", session=self.session))["data"]["interests"]["read"]["minutes"], 3)
        response = await self.call("life/update", method="POST", session=self.session, operation="forget_experience", action="read")
        self.assertEqual(response["status_code"], 200)
        self.assertEqual((await self.call("life", session=self.session))["data"]["interests"], {})
        engine._persona_text.return_value = "角色甲"
        result = (await self.call("life", session=self.session))["data"]["interests"]
        self.assertEqual(result["read"]["minutes"], 5)
        self.assertNotIn("personas", result["read"])


if __name__ == "__main__":
    unittest.main()
