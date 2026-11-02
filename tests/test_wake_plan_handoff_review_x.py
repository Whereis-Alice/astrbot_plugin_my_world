"""Interrupted rest cannot restart itself when a user keeps the later plan."""
from __future__ import annotations

import copy
import unittest

from tests.fake_astrbot import install

install()

from core.engine import TickOutcome  # noqa: E402
from core.models import ActionDef  # noqa: E402
from core.planner import create_plan  # noqa: E402
from tests import test_engine as fixture  # noqa: E402


class WakePlanHandoffTests(unittest.IsolatedAsyncioTestCase):
    node_id = "bedroom"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp
    ctx = fixture.EngineTestCase.ctx
    get_state = fixture.EngineTestCase.get_state

    async def prepare(self, *, action_id="sleep", clear=False, grace=0, drowsy=False):
        self.engine.world.sleep.clear_plan_on_wake = clear
        self.engine.world.sleep.wake_grace_minutes = grace
        self.engine.world.sleep.drowsy = drowsy
        self.engine.world.sleep.goodnight = False
        self.engine.world.sleep.goodmorning = False
        self.engine.world.sleep.applies_to_nap = True
        self.engine.world.action_map()["read"].scope = "global"
        self.engine.world.action_map()["nap"].scope = "global"
        self.engine.world.decider.enabled = False
        self.engine.world.events.enabled = False
        duration = 28800 if action_id == "sleep" else 1800
        async with self.engine.session_state(fixture.SESSION) as state:
            state.state, state.energy = "idle", .8
            state.current_action = None
            state.current_plan = create_plan(steps=[{"action": action_id, "duration": duration},
                {"action": "read", "duration": 600}], world_time=state.world_time,
                now=self.clock.now(), valid_for=12 * 3600, source="manual")
            self.plan_id = state.current_plan["id"]
            await self.engine._tick_plan(state, self.engine.node(state.node_id),
                                        TickOutcome(fixture.SESSION), depth=0)
            if drowsy:
                self.assertEqual("drowsy", state.state)
                self.assertEqual(1, state.current_plan["current_step"])
                self.assertEqual("deferred", state.current_plan["steps"][0]["status"])
                return
            self.assertEqual(action_id, state.current_action["type"])
            self.assertTrue(state.current_action["from_plan"])
            elapsed = 20 if action_id == "sleep" else 5
            self.clock.advance(elapsed * 60)
            state.world_time += elapsed
            state.life_updated_at = self.clock.now()
            state.current_action["elapsed_ticks"] = elapsed

    async def wake(self):
        await self.engine.handle_incoming(self.ctx(text="醒醒"))
        return await self.get_state()

    async def continue_plan(self):
        async with self.engine.session_state(fixture.SESSION) as state:
            await self.engine._tick_plan(state, self.engine.node(state.node_id),
                                        TickOutcome(fixture.SESSION), depth=0)
        return await self.get_state()

    async def test_kept_plan_without_grace_continues_work_instead_of_restarting_sleep(self):
        await self.prepare()
        state = await self.wake()
        self.assertEqual(self.plan_id, state.current_plan["id"])
        self.assertEqual(1, state.current_plan["current_step"])
        self.assertEqual("skipped", state.current_plan["steps"][0]["status"])
        self.assertEqual("pending", state.current_plan["steps"][1]["status"])
        self.assertEqual(0.33, state.sleep_history[0]["hours"])
        self.assertTrue(state.sleep_history[0]["interrupted"])
        self.assertFalse(state.sleep_history[0]["rested"])
        state = await self.continue_plan()
        self.assertEqual("read", state.current_action["type"])
        self.assertEqual(self.plan_id, state.current_action["plan_id"])
        self.assertEqual(1, state.current_action["plan_index"])
        self.assertEqual([], self.llm.calls)
        self.assertEqual([], self.tools.calls)

    async def test_wake_protection_remains_while_kept_work_can_continue(self):
        await self.prepare(grace=12)
        state = await self.wake()
        self.assertEqual(state.world_time + 12, state.no_sleep_until)
        state = await self.continue_plan()
        self.assertEqual("read", state.current_action["type"])
        self.assertGreater(state.no_sleep_until, state.world_time)

    async def test_default_clear_option_still_discards_the_whole_old_plan(self):
        await self.prepare(clear=True)
        state = await self.wake()
        self.assertIsNone(state.current_plan)
        self.assertIsNone(state.current_action)
        self.assertEqual("idle", state.state)
        self.assertEqual(1, len(state.sleep_history))

    async def test_kept_nap_is_interrupted_once_and_does_not_block_later_work(self):
        await self.prepare(action_id="nap")
        state = await self.wake()
        self.assertEqual("nap", state.sleep_history[0]["kind"])
        self.assertEqual(0.08, state.sleep_history[0]["hours"])
        self.assertTrue(state.sleep_history[0]["interrupted"])
        state = await self.continue_plan()
        self.assertEqual("read", state.current_action["type"])
        self.assertEqual(1, len(state.sleep_history))

    async def test_custom_nap_uses_recovery_kind_and_retains_the_same_plan(self):
        self.engine.world.actions.append(ActionDef(id="corner_rest_x", name="小憩",
            category="continuous", scope="global", recovery_kind="nap", duration=1800,
            during={"state": "napping"}))
        await self.prepare(action_id="corner_rest_x")
        state = await self.wake()
        self.assertEqual("skipped", state.current_plan["steps"][0]["status"])
        self.assertEqual(self.plan_id, state.current_plan["id"])
        self.assertEqual("nap", state.sleep_history[0]["kind"])
        self.assertEqual("read", (await self.continue_plan()).current_action["type"])

    async def test_replacement_plan_is_not_advanced_even_with_a_matching_sleep_label(self):
        await self.prepare()
        async with self.engine.session_state(fixture.SESSION) as state:
            state.current_plan = create_plan(steps=[{"action": "sleep", "duration": 28800},
                {"action": "read", "duration": 600}], world_time=state.world_time,
                now=self.clock.now(), source="manual")
            replacement = copy.deepcopy(state.current_plan)
        state = await self.wake()
        self.assertEqual(replacement, state.current_plan)

    async def test_an_inserted_prefix_keeps_its_cursor_and_only_the_owned_rest_is_skipped(self):
        await self.prepare()
        async with self.engine.session_state(fixture.SESSION) as state:
            state.current_plan["steps"][:0] = [
                {"action": "read", "duration": 600, "status": "pending"},
                {"action": "read", "duration": 600, "status": "pending"},
            ]
            state.current_action["plan_index"] = 2
        state = await self.wake()
        self.assertEqual(0, state.current_plan["current_step"])
        self.assertEqual(["pending", "pending", "skipped", "pending"],
                         [step["status"] for step in state.current_plan["steps"]])
        state = await self.continue_plan()
        self.assertEqual("read", state.current_action["type"])
        self.assertEqual(0, state.current_action["plan_index"])

    async def test_a_replaced_step_is_not_skipped_just_because_the_plan_id_matches(self):
        await self.prepare()
        async with self.engine.session_state(fixture.SESSION) as state:
            state.current_plan["steps"][0] = {"action": "read", "duration": 600, "status": "pending"}
            replacement = copy.deepcopy(state.current_plan)
        state = await self.wake()
        self.assertEqual(replacement, state.current_plan)

    async def test_legacy_timestamp_identity_is_supported(self):
        await self.prepare()
        async with self.engine.session_state(fixture.SESSION) as state:
            state.current_action["plan_id"] = ""
            state.current_plan["id"] = ""
        state = await self.wake()
        self.assertEqual(1, state.current_plan["current_step"])
        self.assertEqual("skipped", state.current_plan["steps"][0]["status"])

    async def test_unconfirmed_or_invalid_action_ownership_keeps_the_pending_plan(self):
        # These ownership cases span more than an hour; avoid a fixed sleep
        # window hiding a later case when the fixture clock crosses morning.
        self.engine.world.sleep.mode = "autonomous"
        for change in ("from_plan", "missing_identity", "missing_index", "invalid_index", "changed_timestamp"):
            with self.subTest(change=change):
                await self.prepare()
                async with self.engine.session_state(fixture.SESSION) as state:
                    if change == "from_plan":
                        state.current_action["from_plan"] = False
                    elif change == "missing_identity":
                        state.current_action.pop("plan_id", None)
                        state.current_action.pop("plan_created_at", None)
                    elif change == "missing_index":
                        state.current_action.pop("plan_index", None)
                    elif change == "invalid_index":
                        state.current_action["plan_index"] = "bad"
                    else:
                        state.current_action["plan_id"] = ""
                        state.current_plan["id"] = ""
                        state.current_action["plan_created_at"] += 1
                    prior = copy.deepcopy(state.current_plan)
                self.assertEqual(prior, (await self.wake()).current_plan)

    async def test_drowsy_rest_not_yet_started_keeps_its_already_deferred_step(self):
        await self.prepare(drowsy=True)
        self.assertTrue(await self.engine.wake_up(fixture.SESSION))
        state = await self.get_state()
        self.assertEqual([], state.sleep_history)
        self.assertEqual({}, state.drowsy_sleep_step)
        self.assertEqual(1, state.current_plan["current_step"])
        self.assertEqual("deferred", state.current_plan["steps"][0]["status"])
        self.assertEqual("read", (await self.continue_plan()).current_action["type"])

    async def test_interrupted_rest_does_not_receive_full_completion_effects_or_records(self):
        await self.prepare()
        self.engine.world.action_map()["sleep"].on_complete.effects = {"curiosity": "=0.987"}
        state = await self.wake()
        self.assertNotEqual(0.987, state.curiosity)
        events = await self.db.call("query_events", session_id=fixture.SESSION, event_type="action_done")
        self.assertFalse(any(row["detail"].get("type") == "sleep" for row in events))
        self.assertEqual("skipped", state.current_plan["steps"][0]["status"])

    async def test_a_second_wake_cannot_skip_the_next_step_or_duplicate_sleep_history(self):
        await self.prepare()
        first = await self.wake()
        async with self.engine.session_state(fixture.SESSION) as state:
            self.engine._wake_up_state(state, self.ctx(text="醒醒"))
        second = await self.get_state()
        self.assertEqual(first.current_plan, second.current_plan)
        self.assertEqual(first.sleep_history, second.sleep_history)
        self.assertEqual("read", (await self.continue_plan()).current_action["type"])
