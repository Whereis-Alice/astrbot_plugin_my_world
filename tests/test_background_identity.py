"""Late model results must respect the character and world that requested them."""

import asyncio
import json
import unittest
from hashlib import sha256
from unittest.mock import AsyncMock

from tests import test_engine as fixture

SESSION = fixture.SESSION


class BackgroundIdentityTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp
    add_group = fixture.EngineTestCase.add_group

    async def pending_result(self, kind, change):
        self.requested_persona = self.persona.text
        async with self.engine.session_state(SESSION) as state:
            state.world_time, state.state, state.energy = 600, "idle", .9
        self.engine.profiles.touch(SESSION, "42", "小明")
        before = await self.engine.load_state(SESSION, cold_start=False)
        affinity = self.engine.profiles.profile(SESSION, "42")["affinity"]
        entered, release = asyncio.Event(), asyncio.Event()

        async def judge(*args):
            entered.set()
            await release.wait()
            return json.dumps({
                "text": "慢慢读完一本书", "motivation": "想知道结局",
                "reasoning": {"inner": "很期待这次约定", "intent": "回应感谢"},
                "affinity_delta": .1, "valence_delta": .2,
                "own_topic": "周末一起读书", "vocabulary": [],
            }, ensure_ascii=False)

        self.engine._ask_judge = AsyncMock(side_effect=judge)
        if kind == "reflection":
            work = self.engine.reflect_turn(SESSION, user_id="42", user_text="谢谢你听我说完",
                                            reply_text="嗯", interaction_entries=[{
                                                "user_id": "42", "text": "谢谢你听我说完", "message_id": "one",
                                            }])
        else:
            work = self.engine.seed_life_matter(SESSION)
        task = asyncio.create_task(work)
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await change()
        finally:
            release.set()
            result = await asyncio.wait_for(task, 2)
        after = await self.engine.load_state(SESSION, cold_start=False)
        return before, after, affinity, result

    def seed_marker(self):
        key = f"life_seed:{SESSION}:{self.engine._today_key(self.clock.now())}"
        owner = sha256(self.requested_persona.encode()).hexdigest()[:24]
        return self.db.raw.kv_get(key)["personas"][owner]

    def assert_discarded(self, before, after, affinity, result):
        self.assertIsNone(result)
        self.assertEqual(after.thoughts, before.thoughts)
        self.assertEqual(after.last_reflection, before.last_reflection)
        self.assertEqual(after.life_matters, before.life_matters)
        self.assertEqual(after.valence, before.valence)
        self.assertEqual(self.engine.profiles.profile(SESSION, "42")["affinity"], affinity)
        self.assertEqual(self.db.raw.interaction_detail(SESSION, "42")["history"], [])

    async def test_reflection_cannot_apply_after_persona_changes(self):
        async def change():
            self.persona.text = "你是一个严谨克制的学者。"
        self.assert_discarded(*await self.pending_result("reflection", change))

    async def test_reflection_cannot_apply_after_session_is_disabled(self):
        async def change():
            self.engine.session_config(SESSION).enabled = False
        self.assert_discarded(*await self.pending_result("reflection", change))

    async def test_reflection_cannot_apply_after_world_reloads(self):
        async def change():
            self.engine.reload_config()
        self.assert_discarded(*await self.pending_result("reflection", change))

    async def test_self_start_cannot_create_old_personas_goal(self):
        async def change():
            self.persona.text = "你是一个严谨克制的学者。"
        self.assert_discarded(*await self.pending_result("seed", change))
        marker = self.seed_marker()
        self.assertFalse(marker.get("done"), "discarded inference must remain eligible for a delayed retry")
        self.clock.advance(max(15, self.engine.world.limits.life_matter_interval_minutes) * 60)
        self.engine._ask_judge = AsyncMock(return_value='{"text":"研究书中的论证"}')
        await self.engine.seed_life_matter(SESSION)
        state = await self.engine.load_state(SESSION, cold_start=False)
        self.assertEqual([m["text"] for m in state.life_matters], ["研究书中的论证"])
        self.assertIn(self.persona.text, self.engine._ask_judge.call_args.args[2])

    async def test_self_start_cannot_consume_the_day_while_disabled(self):
        async def change():
            self.engine.session_config(SESSION).enabled = False
        self.assert_discarded(*await self.pending_result("seed", change))
        marker = self.seed_marker()
        self.assertFalse(marker.get("done"))

    async def test_self_start_cannot_apply_after_world_reloads(self):
        async def change():
            self.engine.reload_config()
        self.assert_discarded(*await self.pending_result("seed", change))

    async def test_self_start_waits_if_rest_begins_during_inference(self):
        async def change():
            async with self.engine.session_state(SESSION) as state:
                state.state = "drowsy"
        before, after, affinity, result = await self.pending_result("seed", change)
        self.assert_discarded(before, after, affinity, result)
        self.assertEqual(after.state, "drowsy")

    async def test_unchanged_reflection_still_updates_the_current_character(self):
        async def unchanged():
            pass
        before, after, _, result = await self.pending_result("reflection", unchanged)
        self.assertIsInstance(result, dict)
        self.assertNotEqual(after.thoughts, before.thoughts)
        self.assertNotEqual(after.last_reflection, before.last_reflection)

    async def test_unchanged_self_start_still_creates_one_goal(self):
        async def unchanged():
            pass
        _, after, _, _ = await self.pending_result("seed", unchanged)
        self.assertEqual([m["text"] for m in after.life_matters], ["慢慢读完一本书"])

    async def test_old_world_result_cannot_move_into_a_new_shared_life(self):
        async def change():
            self.add_group(main=fixture.OTHER_SESSION)
        _, after, _, result = await self.pending_result("seed", change)
        self.assertIsNone(result)
        self.assertEqual(after.life_matters, [])


class BackgroundConsolidationTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    async def check_discarded(self, change):
        mid = self.engine.memory.remember(session_id=SESSION, persona_id="", node_id="study", content="修好了风车")
        state = await self.engine.load_state(SESSION, cold_start=False)
        entered, release = asyncio.Event(), asyncio.Event()
        async def model(*args):
            entered.set()
            await release.wait()
            return json.dumps({"forget": [{"id": mid, "text": "改写后的旧经历"}], "dream": "风车飞上天空"})
        self.engine._ask_consolidate = AsyncMock(side_effect=model)
        task = asyncio.create_task(self.engine.consolidate_now(state))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            change()
        finally:
            release.set()
        self.assertIsNone(await asyncio.wait_for(task, 2))
        self.assertEqual(self.db.raw.memory_by_id(mid)["content"], "修好了风车")
        self.assertEqual(len(self.db.raw.query_memories()), 1)
        self.assertEqual(state.consolidate_cursor, 0)
        self.assertFalse(state.last_dream)

    async def test_review_cannot_apply_after_persona_changes(self):
        await self.check_discarded(lambda: setattr(self.persona, "text", "你是严谨克制的学者。"))

    async def test_review_cannot_apply_after_world_reloads(self):
        await self.check_discarded(self.engine.reload_config)

    async def test_review_cannot_apply_after_session_is_disabled(self):
        await self.check_discarded(lambda: setattr(self.engine.session_config(SESSION), "enabled", False))
