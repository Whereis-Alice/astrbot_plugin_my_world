"""Shared appointments consider only destinations that can accept their speech."""

import unittest
from datetime import datetime

from tests import test_engine as fixture


class ScheduleLandingPermissionTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp
    add_group = fixture.EngineTestCase.add_group

    async def prepare_schedule(self, allowed, *, action_chain, conditions=None):
        self.add_group(sessions=[fixture.SESSION, fixture.PRIVATE_SESSION, fixture.OTHER_SESSION])
        self.store.save_world({**self.store.raw_world(), "life_share_session_whitelist": allowed})
        self.store.save_schedules({**self.store.raw_schedules(), "schedules": [{
            "id": "appointment", "enabled": True, "time": "12:00", "sessions": ["team"],
            "action_chain": action_chain, "conditions": conditions or {},
        }]})
        self.engine.reload_config()
        self.clock.set_struct(datetime(2026, 11, 26, 11, 59))
        async with self.engine.session_state(fixture.SESSION) as state:
            state.state, state.current_action, state.current_plan = "idle", None, None
            state.energy, state.cold_start_done = .8, True
        self.assertEqual([], await self.engine.run_schedules())
        self.clock.set_struct(datetime(2026, 11, 26, 12, 0))

    async def test_only_authorized_room_receives_announcement_without_model_choice(self):
        await self.prepare_schedule([fixture.PRIVATE_SESSION], action_chain=[{
            "type": "say", "messages": ["午餐做好了"],
        }])
        outcomes = await self.engine.run_schedules()
        self.assertEqual([fixture.PRIVATE_SESSION], [item.session_id for item in outcomes])
        self.assertEqual([], self.llm.calls)
        await self.engine._deliver(outcomes[0])
        self.assertEqual([(fixture.PRIVATE_SESSION, ["午餐做好了"])], self.messenger.sent)

    async def test_paused_speech_keeps_the_appointment_without_a_room_choice(self):
        await self.prepare_schedule([], action_chain=[{"type": "read", "duration": 300}])
        outcomes = await self.engine.run_schedules()
        self.assertEqual([fixture.SESSION], [item.session_id for item in outcomes])
        self.assertEqual([], self.llm.calls)
        state = await self.engine.load_state(fixture.SESSION)
        self.assertEqual("read", state.current_action["type"])
        self.assertEqual([], self.messenger.sent)

    async def test_failed_conditions_do_not_ask_for_an_announcement_room(self):
        await self.prepare_schedule([fixture.SESSION, fixture.PRIVATE_SESSION],
            action_chain=[{"type": "say", "messages": ["午餐做好了"]}],
            conditions={"max_energy": .1})
        self.assertEqual([], await self.engine.run_schedules())
        self.assertEqual([], self.llm.calls)
        self.assertEqual([], self.messenger.sent)

    async def test_invalid_number_falls_back_to_an_authorized_room(self):
        await self.prepare_schedule([fixture.PRIVATE_SESSION, fixture.OTHER_SESSION],
            action_chain=[{"type": "say", "messages": ["午餐做好了"]}])
        self.llm.replies = ["²"]
        outcomes = await self.engine.run_schedules()
        self.assertEqual([fixture.PRIVATE_SESSION], [item.session_id for item in outcomes])
        self.assertEqual(1, len(self.llm.calls))
        await self.engine._deliver(outcomes[0])
        self.assertEqual([(fixture.PRIVATE_SESSION, ["午餐做好了"])], self.messenger.sent)
