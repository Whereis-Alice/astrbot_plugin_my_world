"""An outing reminder belongs to an accepted plan, not local request preparation."""
import asyncio
import unittest
from datetime import datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from core.engine import TickOutcome
from core.engine.life_inference import life_work
from core.ports import LLMReply
from tests import test_engine as fixture
from tests.test_outdoor_hint_review_s import HintHarness

S = fixture.SESSION
STAY = '{"plan":[{"action":"stare","duration":60}],"reason":"想继续在家歇一会儿"}'


class OutdoorAdmissionTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"

    async def asyncSetUp(self):
        await fixture.EngineTestCase.asyncSetUp(self)
        raw = self.store.raw_world()
        custom = HintHarness().world.model_dump(mode="json")
        for key in ("zones", "nodes", "edges", "zone_edges", "actions"):
            raw[key] = custom[key]
        raw["actions"].append({"id": "stare", "name": "歇一会儿", "scope": "global",
            "category": "continuous", "llm_level": "template", "life_role": "interest"})
        raw["limits"].update(background_pacing=False, background_daily_calls=20,
                             background_daily_tokens=0)
        self.store.save_world(raw)
        self.engine.reload_config()
        self.clock._now = datetime(2026, 11, 3, 15, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        async with self.engine.session_state(S) as state:
            state.node_id, state.state, state.energy = "alice_bedroom", "idle", .9
            state.outdoor_hint_at = 0
        self.llm.replies = [STAY]

    async def plan(self, state):
        self.outcome = TickOutcome(S)
        return await self.engine._ask_llm_for_plan(state, self.engine.node(state.node_id),
                                                  self.outcome, force=True, quiet=True)

    async def test_busy_unissued_request_leaves_reminder_for_a_successful_retry(self):
        async with self.engine.session_state(S) as state:
            self.engine._background_active = self.engine.world.limits.background_concurrency
            self.assertIsNone(await self.plan(state))
            self.assertEqual((self.outcome.llm_calls, state.llm_plan_count_hour, state.last_llm_plan_at), (0, 0, 0))
        self.assertEqual((await self.engine.load_state(S)).outdoor_hint_at, 0)
        self.assertEqual(self.llm.calls, [])
        self.engine._background_active = 0
        async with self.engine.session_state(S) as state:
            self.assertIsNotNone(await self.plan(state))
            self.assertEqual((self.outcome.llm_calls, state.llm_plan_count_hour), (1, 1))
        self.assertIn("hobby_corner", self.llm.calls[-1]["system_prompt"])
        self.assertEqual((await self.engine.load_state(S)).outdoor_hint_at, self.clock.now())
        self.assertEqual(self.messenger.sent, [])

    async def test_exhausted_background_budget_does_not_consume_outing_reminder(self):
        self.engine.world.limits.background_daily_calls = 1
        day = self.engine._today_key(self.clock.now())
        await self.db.call("reserve_background", day, calls=1, tokens=0, reserve=0)
        await self.db.call("settle_background", day, reserve=0, measured=0)
        async with self.engine.session_state(S) as state:
            self.assertIsNone(await self.plan(state))
            self.assertEqual((self.outcome.llm_calls, state.llm_plan_count_hour, state.last_llm_plan_at), (0, 0, 0))
        self.assertEqual(self.llm.calls, [])
        self.assertEqual((await self.engine.load_state(S)).outdoor_hint_at, 0)

    async def test_failed_provider_and_its_backoff_do_not_consume_outing_reminder(self):
        self.llm.generate = AsyncMock(side_effect=TimeoutError("offline test"))
        async with self.engine.session_state(S) as state:
            self.assertIsNone(await self.plan(state))
            self.assertEqual((self.outcome.llm_calls, state.llm_plan_count_hour), (1, 1))
        self.assertEqual(self.llm.generate.await_count, 1)
        self.assertEqual((await self.engine.load_state(S)).outdoor_hint_at, 0)
        async with self.engine.session_state(S) as state:
            self.assertIsNone(await self.plan(state))
            self.assertEqual((self.outcome.llm_calls, state.llm_plan_count_hour), (0, 1))
        self.assertEqual(self.llm.generate.await_count, 1)
        self.assertEqual((await self.engine.load_state(S)).outdoor_hint_at, 0)

    async def test_malformed_reply_and_empty_plan_leave_reminder_pending(self):
        for reply in ("invalid json", '{"plan":[]}'):
            with self.subTest(reply=reply):
                self.llm.replies = [reply]
                async with self.engine.session_state(S) as state:
                    self.assertIsNone(await self.plan(state))
                    self.assertEqual(self.outcome.llm_calls, 1)
                self.assertEqual((await self.engine.load_state(S)).outdoor_hint_at, 0)

    async def test_a_valid_stay_home_plan_consumes_the_reminder_once(self):
        self.llm.replies = [STAY, STAY]
        async with self.engine.session_state(S) as state:
            plan = await self.plan(state)
            self.assertEqual(plan["steps"][0]["action"], "stare")
        self.clock.advance(60)
        async with self.engine.session_state(S) as state:
            self.assertIsNotNone(await self.plan(state))
        self.assertIn("hobby_corner", self.llm.calls[0]["system_prompt"])
        self.assertNotIn("如果想离开家换个环境", self.llm.calls[1]["system_prompt"])
        self.assertEqual((await self.engine.load_state(S)).outdoor_hint_at, self.clock.now() - 60)

    async def test_cancellation_keeps_an_old_reminder_timestamp(self):
        self.engine._ask_llm = AsyncMock(side_effect=asyncio.CancelledError())
        previous = self.clock.now() - 11 * 3600
        async with self.engine.session_state(S) as state:
            state.outdoor_hint_at = previous
            with self.assertRaises(asyncio.CancelledError):
                await self.plan(state)
        self.assertEqual((await self.engine.load_state(S)).outdoor_hint_at, previous)

    async def test_valid_result_cannot_overwrite_a_newer_outing_reminder(self):
        async with self.engine.session_state(S) as state:
            async def newer_hint(*args, **kwargs):
                state.outdoor_hint_at = self.clock.now() + 60
                return STAY
            self.engine._ask_llm = AsyncMock(side_effect=newer_hint)
            self.assertIsNotNone(await self.plan(state))
            self.assertEqual((self.outcome.llm_calls, state.llm_plan_count_hour), (1, 1))
        self.assertEqual((await self.engine.load_state(S)).outdoor_hint_at, self.clock.now() + 60)

    async def test_discarded_life_decision_preserves_the_new_world_state(self):
        async def changed_life(**kwargs):
            async with self.engine.session_state(S) as latest:
                latest.node_id = "hobby_corner"
                latest.outdoor_hint_at = self.clock.now() + 60
            return LLMReply(text=STAY, ok=True)
        self.llm.generate = AsyncMock(side_effect=changed_life)

        @life_work()
        async def run(engine):
            async with self.engine.session_state(S) as state:
                return await self.plan(state)
        self.assertIsNone(await run(self.engine))
        self.assertEqual(self.llm.generate.await_count, 1)
        latest = await self.engine.load_state(S)
        self.assertEqual(latest.node_id, "hobby_corner")
        self.assertEqual(latest.outdoor_hint_at, self.clock.now() + 60)
        self.assertIsNone(latest.current_plan)
        self.assertEqual(self.messenger.sent, [])


if __name__ == "__main__":
    unittest.main()
