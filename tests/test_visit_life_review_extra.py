"""A finished visit still invalidates the home decision prepared before arrival."""
from __future__ import annotations

import asyncio
import types
import unittest

from core.engine.life_inference import life_work
from core.ports import LLMReply
from tests import test_engine as fixture


class FinishedVisitInferenceTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    def track_presence(self):
        epochs = {}
        self.engine.visits = types.SimpleNamespace(
            active=lambda scope: None, presence_epoch=lambda scope: epochs.get(scope, 0))
        return epochs

    async def home_decision(self):
        @life_work()
        async def work(engine):
            async with engine.session_state(fixture.SESSION) as state:
                await engine._budgeted_generate(self.llm, session_id=fixture.SESSION,
                                               system_prompt="home", prompt="plan")
                state.current_plan = {"id": "prepared-before-trip"}
                return "committed"

        return await work(self.engine)

    async def test_arrival_and_return_during_inference_discard_home_plan(self):
        epochs = self.track_presence()
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed(**kwargs):
            entered.set()
            await release.wait()
            return LLMReply(text='{"accept":true}', ok=True)

        self.llm.generate = delayed
        task = asyncio.create_task(self.home_decision())
        self.addAsyncCleanup(self.cancel_task, task)
        await asyncio.wait_for(entered.wait(), 2)
        epochs[self.engine.state_key(fixture.SESSION)] = 1
        release.set()
        self.assertIsNone(await asyncio.wait_for(task, 2))
        state = await self.engine.load_state(fixture.SESSION, cold_start=False)
        self.assertIsNone(state.current_plan)
        self.assertFalse(self.engine.lock(state.session_id).locked())

    async def test_arrival_and_return_after_preparation_do_not_spend_model_call(self):
        epochs = self.track_presence()
        original = self.engine._budgeted_generate

        async def visit_before_admission(channel, **kwargs):
            epochs[self.engine.state_key(fixture.SESSION)] = 1
            return await original(channel, **kwargs)

        self.engine._budgeted_generate = visit_before_admission
        self.assertIsNone(await self.home_decision())
        self.assertEqual(self.llm.calls, [])
        state = await self.engine.load_state(fixture.SESSION, cold_start=False)
        self.assertIsNone(state.current_plan)

    async def test_other_life_arrival_does_not_discard_current_home_plan(self):
        epochs = self.track_presence()

        async def other_life_travels(**kwargs):
            epochs[fixture.OTHER_SESSION] = 1
            return LLMReply(text='{"accept":true}', ok=True)

        self.llm.generate = other_life_travels
        self.assertEqual(await self.home_decision(), "committed")
        state = await self.engine.load_state(fixture.SESSION, cold_start=False)
        self.assertEqual(state.current_plan["id"], "prepared-before-trip")

    @staticmethod
    async def cancel_task(task):
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
