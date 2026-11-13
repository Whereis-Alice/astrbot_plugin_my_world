"""Display inference shares the real engine's audited background budget and lifecycle."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS
import unittest

from core.group_card_presence import engine_group_card_override
from tests import test_engine as fixture
from tests.stub_ports import StubLLM


class VisitCardBudgetTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"

    async def asyncSetUp(self):
        await fixture.EngineTestCase.asyncSetUp(self)
        self.engine.world.nickname_sync.enabled = True
        self.engine.world.limits.background_daily_calls = 1
        self.engine.world.limits.background_daily_tokens = 0
        self.engine.world.limits.background_pacing = False
        _, owner = await self.engine._journal_persona(fixture.SESSION)
        self.visit = {"id": "visit", "status": "active", "role": "guest", "arrived_at": self.clock.now() - 60,
                      "scope": self.engine.state_key(fixture.SESSION), "local": {"persona_key": owner},
                      "remote": {"name": "艾拉"}, "outing_version": 1,
                      "outing": {"version": 1, "stage": 0, "place": "庭院", "activity": "喝茶",
                                 "started_at": self.clock.now() - 60, "until": self.clock.now() + 600}}
        self.engine.visits = NS(config=NS(enabled=True), _closed=False, _loaded=True, active=lambda _: self.visit)
        self.card = StubLLM(['{"status":"和艾拉喝茶","basis":"activity"}'])
        self.engine.visits_card = self.card
        self.engine.visits_card_options = lambda: {"visits_card_enabled": True, "visits_card_model_enabled": True}

    async def settle(self):
        await asyncio.gather(*self.engine._background_jobs.values())

    async def test_card_generation_spends_one_background_call_and_uses_only_its_channel(self):
        self.assertEqual((await engine_group_card_override(self.engine, fixture.SESSION))["status"], "喝茶中")
        await self.settle()
        self.assertEqual((await engine_group_card_override(self.engine, fixture.SESSION))["status"], "和艾拉喝茶")
        self.assertEqual(len(self.card.calls), 1)
        self.assertFalse(self.llm.calls)
        self.assertFalse(self.messenger.sent)
        budget = await self.db.call("background_status", self.engine._today_key(self.clock.now()))
        self.assertEqual(budget["calls"], 1)

    async def test_exhausted_background_budget_keeps_fact_fallback_without_provider_dispatch(self):
        day = self.engine._today_key(self.clock.now())
        await self.db.call("reserve_background", day, calls=1, tokens=0, reserve=1)
        await engine_group_card_override(self.engine, fixture.SESSION)
        await self.settle()
        self.assertEqual((await engine_group_card_override(self.engine, fixture.SESSION))["status"], "喝茶中")
        self.assertFalse(self.card.calls)
