"""Background waiting cannot exhaust a character's unissued life decisions."""
import asyncio
import copy
import unittest
from hashlib import sha256
from unittest.mock import AsyncMock

from core import life_matters as matters
from core.background_budget import record_background_admission
from tests import test_engine as fixture

SESSION = fixture.SESSION


class MatterRequestAdmissionTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    async def prepare(self, *, review=False, partial=False):
        async with self.engine.session_state(SESSION) as state:
            state.state, state.energy, state.world_time = "idle", .9, 600
            if not review and not partial:
                return None
            item = self.engine.add_life_matter(state, "读一部长篇故事")
            item["steps"] = [{"id": "first", "text": "读第一章", "action": "read", "node": "study",
                "minutes": 5, "worked_minutes": 5, "status": "done",
                "result": {"ok": True, "kind": "in_world_action", "minutes": 5}}]
            if partial:
                item["steps"].append({"id": "next", "text": "明天读第二章", "action": "read",
                    "node": "study", "minutes": 5, "status": "pending",
                    "not_before": self.clock.now() + 86400})
            item["needs_review"] = review
            return item["id"]

    async def state(self):
        return await self.engine.load_state(SESSION, cold_start=False)

    async def defer(self, *_args, **_kwargs):
        record_background_admission(deferred=True, reason="后台通道忙", next_at=self.clock.now() + 60)
        return None

    def seed_marker(self):
        key = f"life_seed:{self.engine.state_key(SESSION)}:{self.engine._today_key(self.clock.now())}"
        persona = sha256(self.persona.text.encode()).hexdigest()[:24]
        return self.engine._life_seed_marker(key, persona)

    async def test_busy_seed_requests_preserve_daily_allowance_and_automatically_retry(self):
        await self.prepare()
        self.engine._ask_judge = AsyncMock(side_effect=self.defer)
        for _ in range(4):
            await self.engine.seed_life_matter(SESSION)
            self.assertEqual(self.seed_marker()["attempts"], 0)
            self.assertEqual((await self.state()).life_matters, [])
            calls = self.engine._ask_judge.await_count
            await self.engine.seed_life_matter(SESSION)
            self.assertEqual(self.engine._ask_judge.await_count, calls)
            self.clock.advance(60)
        self.engine._ask_judge = AsyncMock(return_value='{"text":"整理一组书签"}')
        await self.engine.seed_life_matter(SESSION)
        self.assertEqual([item["text"] for item in (await self.state()).life_matters], ["整理一组书签"])
        self.assertTrue(self.seed_marker()["done"])

    async def test_busy_planning_never_creates_a_false_blocked_history(self):
        await self.prepare()
        async with self.engine.session_state(SESSION) as state:
            item_id = self.engine.add_life_matter(state, "读一部长篇故事")["id"]
        self.engine._ask_judge = AsyncMock(side_effect=self.defer)
        for _ in range(4):
            await self.engine.advance_life_matters(SESSION)
            item = matters.find(await self.state(), item_id)
            self.assertEqual(item["plan_attempts"], 0)
            self.assertEqual(item["status"], "active")
            self.assertFalse(any(row["kind"] == "blocked" for row in item["history"]))
            self.clock.advance(60)
        self.engine._ask_judge = AsyncMock(return_value='{"steps":[{"text":"读第一章","action":"read",'
            '"node":"study","minutes":5,"day_offset":0,"time_window":"any"}]}')
        await self.engine.advance_life_matters(SESSION)
        self.assertEqual((await self.state()).current_action["matter_id"], item_id)

    async def test_real_busy_channel_never_dispatches_or_spends_project_attempts(self):
        await self.prepare()
        async with self.engine.session_state(SESSION) as state:
            item_id = self.engine.add_life_matter(state, "读一部长篇故事")["id"]
        self.engine._background_active = self.engine.world.limits.background_concurrency
        try:
            for _ in range(4):
                await self.engine.advance_life_matters(SESSION)
                item = matters.find(await self.state(), item_id)
                self.assertEqual(item["plan_attempts"], 0)
                self.assertEqual(item["status"], "active")
                self.clock.advance(60)
        finally:
            self.engine._background_active = 0
        self.assertEqual(self.llm.calls, [])

    async def test_unissued_stage_review_retains_pending_evidence_and_attempt(self):
        item_id = await self.prepare(review=True)
        self.engine._ask_judge = AsyncMock(side_effect=self.defer)
        await self.engine.advance_life_matters(SESSION)
        item = matters.find(await self.state(), item_id)
        self.assertEqual(item["plan_attempts"], 0)
        self.assertTrue(item["needs_review"])
        self.assertFalse(item["steps"][0].get("stage_noted"))

    async def test_missing_model_retains_the_unissued_daily_idea(self):
        await self.prepare()
        self.engine.helper_llm = self.engine.judge_llm = None
        for _ in range(4):
            await self.engine.seed_life_matter(SESSION)
            self.assertEqual(self.seed_marker()["attempts"], 0)
            self.clock.advance(60)
        self.assertEqual(self.llm.calls, [])

    async def test_failed_persona_preparation_refunds_the_reserved_plan_attempt(self):
        await self.prepare()
        async with self.engine.session_state(SESSION) as state:
            item_id = self.engine.add_life_matter(state, "读一部长篇故事")["id"]
        original = self.engine._persona_text
        async def unavailable_when_preparing(session):
            if not self.engine.is_busy(session):
                raise RuntimeError("persona preparation unavailable")
            return await original(session)
        self.engine._persona_text = unavailable_when_preparing
        with self.assertRaisesRegex(RuntimeError, "persona preparation unavailable"):
            await self.engine.advance_life_matters(SESSION)
        self.engine._persona_text = original
        item = matters.find(await self.state(), item_id)
        self.assertEqual(item["plan_attempts"], 0)
        self.assertEqual(self.llm.calls, [])

    async def test_unissued_partial_note_does_not_spend_daily_note_limit(self):
        item_id = await self.prepare(partial=True)
        self.engine.world.limits.life_matter_notes_per_day = 1
        self.engine._ask_judge = AsyncMock(side_effect=self.defer)
        for _ in range(3):
            await self.engine.note_life_progress(SESSION)
            item = matters.find(await self.state(), item_id)
            self.assertEqual(item["note_review"]["attempts"], 0)
            self.clock.advance(60)
        self.engine._ask_judge = AsyncMock(return_value='{"outcome":"continue","stage":{'
            '"summary":"第一章读完了","content":"读完第一章，线索仍待后续章节解释。",'
            '"next_focus":"接着读第二章","basis_step_ids":["first"]}}')
        await self.engine.note_life_progress(SESSION)
        item = matters.find(await self.state(), item_id)
        self.assertEqual(item["note_review"]["attempts"], 1)
        self.assertTrue(item["steps"][0]["stage_noted"])

    async def test_started_malformed_requests_still_exhaust_three_real_attempts(self):
        await self.prepare()
        async def malformed(*_args, **_kwargs):
            record_background_admission(started=True)
            return "unusable model response"
        self.engine._ask_judge = AsyncMock(side_effect=malformed)
        for _ in range(4):
            await self.engine.seed_life_matter(SESSION)
            self.clock.advance(3600)
        self.assertEqual(self.engine._ask_judge.await_count, 3)
        self.assertEqual(self.seed_marker()["attempts"], 3)

    async def test_cancellation_before_dispatch_refunds_the_original_attempt(self):
        await self.prepare()
        entered = asyncio.Event()
        async def wait(*_args, **_kwargs):
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                record_background_admission(deferred=True, reason="模型路由准备取消")
        self.engine._ask_judge = AsyncMock(side_effect=wait)
        task = asyncio.create_task(self.engine.seed_life_matter(SESSION))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.seed_marker()["attempts"], 0)

    async def test_refund_does_not_overwrite_a_human_change(self):
        await self.prepare()
        async with self.engine.session_state(SESSION) as state:
            item = self.engine.add_life_matter(state, "读一部长篇故事")
            item.update(work_day=self.engine._today_key(self.clock.now()), plan_attempts=1,
                        last_attempt_at=self.clock.now())
            snapshot = copy.deepcopy(item)
        async with self.engine._matter_attempt(SESSION, snapshot, "安排下一小步"):
            async with self.engine.session_state(SESSION) as state:
                self.engine.manage_life_matter(state, item["id"], "pause", reason="先搁置")
            record_background_admission(deferred=True)
        latest = matters.find(await self.state(), item["id"])
        self.assertEqual(latest["status"], "paused")
        self.assertEqual(latest["plan_attempts"], 1)
