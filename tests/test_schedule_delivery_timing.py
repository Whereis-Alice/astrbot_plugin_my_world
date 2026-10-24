"""Deferred clock work retains broadcast failure isolation and sends exactly once."""
import asyncio
import unittest
from unittest.mock import patch

from tests import test_engine as fixture


class ScheduleDeliveryTimingTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp
    add_group = fixture.EngineTestCase.add_group
    add_schedule = fixture.EngineTestCase.add_schedule
    schedule_outcome = fixture.EngineTestCase.schedule_outcome
    set_state = fixture.EngineTestCase.set_state
    get_state = fixture.EngineTestCase.get_state
    ctx = fixture.EngineTestCase.ctx

    def delayed_clock(self):
        original = self.engine._tick_session

        async def delayed(session):
            async with self.engine.session_state(session):
                await asyncio.sleep(.4)
            return await original(session)

        return patch.object(self.engine, "_tick_session", side_effect=delayed)

    async def test_deferred_broadcast_attempts_both_destinations_once(self):
        with self.delayed_clock():
            await fixture.EngineTestCase.test_schedule_broadcast_keeps_going_when_one_target_fails(self)

    async def test_slow_clock_respects_delay_then_enters_sleep_when_it_expires(self):
        with self.delayed_clock():
            await fixture.EngineTestCase.test_delay_window_keeps_a_drowsy_girl_from_sleeping_it_off(self)

    async def test_deferred_schedule_keeps_the_selected_group_destination(self):
        with self.delayed_clock():
            await fixture.EngineTestCase.test_schedule_can_pin_a_session_group(self)

    async def test_deferred_schedule_keeps_the_selected_private_destination(self):
        with self.delayed_clock():
            await fixture.EngineTestCase.test_schedule_can_pin_one_session_inside_a_group(self)

    async def test_deferred_schedule_keeps_explicit_private_routing(self):
        with self.delayed_clock():
            await fixture.EngineTestCase.test_schedule_lands_in_the_session_it_is_pinned_to(self)

    finish_schedule_tick = fixture.EngineTestCase.finish_schedule_tick
