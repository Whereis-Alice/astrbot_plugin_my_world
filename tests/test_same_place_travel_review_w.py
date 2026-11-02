"""A satisfied location step must not reset her stay or fabricate a journey."""
from __future__ import annotations

import copy
from datetime import datetime
import unittest

from tests.fake_astrbot import install

install()

# Install the isolated AstrBot API before loading any plugin modules.
from core.engine import TickOutcome  # noqa: E402
from core.json_actions import PlannedAction  # noqa: E402
from core.models import ActionDef, ChainStep  # noqa: E402
from core.planner import create_plan  # noqa: E402
from core.state import WorldState  # noqa: E402
from tests import test_engine as fixture  # noqa: E402


class SamePlaceTravelTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    async def prepare(self, node="study"):
        self.clock._now = datetime(2026, 11, 3, 10, 0).timestamp()
        self.engine.world.actions = [item for item in self.engine.world.actions if item.id != "onsite_work_w"]
        self.engine.world.actions.append(ActionDef(
            id="onsite_work_w", name="看书", category="continuous", scope="node",
            allowed_nodes=["study", "kitchen"], duration=600, visible=False,
        ))
        async with self.engine.session_state(fixture.SESSION) as state:
            state.node_id, state.state = node, "idle"
            state.node_since = self.clock.now() - 7200
            state.last_outdoor_at = self.clock.now() - 100000
            state.life_updated_at = self.clock.now()
            state.energy, state.curiosity, state.boredom = .95, .85, .05
            state.sleep_pressure = 0
            state.current_action = state.current_plan = None
            self.original = state.to_payload()

    async def saved(self):
        return await self.engine.load_state(fixture.SESSION, cold_start=False)

    def assert_place_unchanged(self, state):
        self.assertEqual(self.original["node_id"], state.node_id)
        self.assertEqual(self.original["node_since"], state.node_since)
        self.assertEqual(self.original["last_outdoor_at"], state.last_outdoor_at)
        self.assertFalse(state.pending_arrival)
        self.assertFalse(any(row["kind"] == "move" for row in state.recent_events))
        self.assertEqual([], self.llm.calls)
        self.assertEqual([], self.messenger.sent)

    async def test_same_place_start_does_not_interrupt_work_or_count_a_use(self):
        for field in ("target_node", "target"):
            with self.subTest(field=field):
                await self.prepare()
                async with self.engine.session_state(fixture.SESSION) as state:
                    await self.engine._start_action(state, self.engine.node(state.node_id),
                        TickOutcome(fixture.SESSION), self.engine.world.action_map()["onsite_work_w"],
                        PlannedAction(type="onsite_work_w"), 0, False)
                    prior = state.current_action
                    uses = copy.deepcopy(state.action_usage)
                    await self.engine._start_action(state, self.engine.node(state.node_id),
                        TickOutcome(fixture.SESSION), self.engine.world.action_map()["walk_to"],
                        PlannedAction(type="walk_to", **{field: "study"}), 0, False)
                    self.assertIs(prior, state.current_action)
                    self.assertEqual(uses, state.action_usage)
                    self.assert_place_unchanged(state)

    async def test_schedule_walk_to_present_place_starts_next_activity_immediately(self):
        await self.prepare()
        async with self.engine.session_state(fixture.SESSION) as state:
            status = await self.engine._run_chain(state, [ChainStep(type="walk_to", target_node="study"),
                ChainStep(type="onsite_work_w")], TickOutcome(fixture.SESSION), depth=0)
            self.assertEqual("running", status)
            self.assertEqual("onsite_work_w", state.current_action["type"])
            self.assertNotIn("walk_to", state.action_usage)
            self.assert_place_unchanged(state)

    async def test_only_satisfied_location_steps_complete_schedule_without_interrupting(self):
        await self.prepare()
        async with self.engine.session_state(fixture.SESSION) as state:
            state.current_action = {"type": "onsite_work_w", "elapsed_ticks": 3, "duration_ticks": 10}
            prior = state.current_action
            status = await self.engine._run_chain(state,
                [ChainStep(type="walk_to", target_node="study")], TickOutcome(fixture.SESSION), depth=0)
            self.assertEqual("completed", status)
            self.assertIs(prior, state.current_action)
            self.assert_place_unchanged(state)

    async def test_same_place_schedule_keeps_unrelated_work_and_its_plan_step(self):
        await self.prepare()
        async with self.engine.session_state(fixture.SESSION) as state:
            state.current_plan = create_plan(steps=[{"action": "onsite_work_w"},
                {"action": "onsite_work_w"}], world_time=state.world_time,
                now=self.clock.now(), valid_for=3600)
            state.current_action = {"type": "onsite_work_w", "elapsed_ticks": 3,
                "duration_ticks": 10, "from_plan": True, "plan_id": state.current_plan["id"],
                "plan_created_at": state.current_plan["created_at"], "plan_index": 0}
            prior_action, prior_plan = state.current_action, state.current_plan
            plan_before = copy.deepcopy(prior_plan)
            status = await self.engine._run_chain(state, [ChainStep(type="walk_to", target_node="study")],
                                                  TickOutcome(fixture.SESSION), depth=0)
            self.assertEqual("completed", status)
            self.assertIs(prior_action, state.current_action)
            self.assertIs(prior_plan, state.current_plan)
            self.assertEqual(plan_before, state.current_plan)
            self.assertEqual(3, state.current_action["elapsed_ticks"])
            self.assert_place_unchanged(state)

    async def test_plan_consumes_satisfied_location_steps_and_keeps_next_activity(self):
        await self.prepare()
        async with self.engine.session_state(fixture.SESSION) as state:
            state.current_plan = create_plan(steps=[{"action": "walk_to", "target_node": "study"},
                {"action": "walk_to", "target": "study"}, {"action": "onsite_work_w"}],
                world_time=state.world_time, now=self.clock.now(), valid_for=3600)
            await self.engine._tick_plan(state, self.engine.node(state.node_id), TickOutcome(fixture.SESSION), depth=0)
            self.assertEqual("onsite_work_w", state.current_action["type"])
            self.assertEqual(2, state.current_plan["current_step"])
            self.assertEqual(["done", "done", "pending"],
                             [row["status"] for row in state.current_plan["steps"]])
            self.assert_place_unchanged(state)

    async def test_same_place_step_keeps_due_environment_change(self):
        await self.prepare()
        before = WorldState.from_payload(self.original, fixture.SESSION)
        self.assertTrue(self.engine.decider._change_environment_due(before))
        async with self.engine.session_state(fixture.SESSION) as state:
            await self.engine._run_chain(state, [ChainStep(type="walk_to", target_node="study")],
                                        TickOutcome(fixture.SESSION), depth=0)
            self.assertTrue(self.engine.decider._change_environment_due(state))
            self.assert_place_unchanged(state)

    async def test_same_place_wake_request_keeps_context_and_finishes_search_once(self):
        await self.prepare()
        async with self.engine.session_state(fixture.SESSION) as state:
            state.state = "sleeping"
            state.current_action = {"type": "sleep"}
        request = "醒醒，去书房帮我查今天的新闻"
        evidence = "本地新闻回执：今天的图书馆延长开放时间。"
        answer = "唔……查到了，今天图书馆会晚一点关门。"
        self.tools.results["web_search"] = evidence
        self.llm.replies = [
            '{"reasoning":{"intent":"去书房查新闻"},'
            '"actions":[{"type":"walk_to","target_node":"study"},'
            '{"type":"search_web","intent":"查今天的新闻"}]}',
            '{"done":true}',
            '{"actions":[{"type":"say","messages":["' + answer + '"]}]}',
        ]
        ctx = fixture.EngineTestCase.ctx(self, text=request)
        await self.engine.handle_incoming(ctx)
        incoming = await self.saved()
        self.assertIn("刚被叫醒", incoming.wake_note)
        call_start = len(self.llm.calls)

        outcome = await self.engine.handle_reply(ctx)

        calls = self.llm.calls[call_start:]
        self.assertTrue(calls)
        main_request = calls[0]
        self.assertIn("刚被叫醒", main_request["system_prompt"])
        self.assertIn("不只是叫你起来", main_request["system_prompt"])
        self.assertIn(request, main_request["prompt"])
        # The completed search can ask whether its evidence is sufficient and
        # then express the result. Those calls are not the original wake reply.
        followups = calls[1:]
        self.assertTrue(followups)
        self.assertTrue(any(evidence in call["prompt"] for call in followups))
        self.assertIn(evidence, followups[-1]["prompt"])
        self.assertTrue(all("刚被叫醒" not in call["system_prompt"] for call in followups))
        self.assertEqual([], self.llm.replies)
        self.assertEqual(["web_search"], [name for name, _params in self.tools.calls])
        self.assertTrue(outcome.ok, outcome.error)
        self.assertIn(answer, outcome.messages)

        state = await self.saved()
        self.assertEqual("study", state.node_id)
        self.assertEqual(self.original["node_since"], state.node_since)
        self.assertEqual(self.original["last_outdoor_at"], state.last_outdoor_at)
        self.assertEqual(self.original["world_time"], state.world_time)
        self.assertFalse(state.pending_arrival)
        self.assertFalse(any(row["kind"] == "move" for row in state.recent_events))
        self.assertEqual("", state.wake_note)
        self.assertIsNone(state.current_action)
        self.assertIsNone(state.current_plan)
        done = await self.db.call("query_events", session_id=fixture.SESSION, event_type="action_done")
        self.assertFalse(any(row["detail"].get("type") == "walk_to" for row in done))

    async def saved_walk(self, state, target):
        state.current_plan = create_plan(steps=[{"action": "walk_to", "target_node": target},
            {"action": "onsite_work_w"}], world_time=state.world_time, now=self.clock.now(), valid_for=3600)
        state.current_action = {"type": "walk_to", "target_node": target, "duration_ticks": 1,
            "elapsed_ticks": 0, "from_plan": True, "plan_id": state.current_plan["id"],
            "plan_created_at": state.current_plan["created_at"], "plan_index": 0, "session": fixture.SESSION}
        state.state = "walking"

    async def test_saved_same_place_walk_finishes_without_fictitious_move_or_memory(self):
        await self.prepare()
        memories = await self.db.call("query_memories", session_id=fixture.SESSION, limit=100)
        async with self.engine.session_state(fixture.SESSION) as state:
            await self.saved_walk(state, "study")
        self.clock.advance(60)
        await self.engine._tick_session(fixture.SESSION)
        state = await self.saved()
        self.assertEqual("onsite_work_w", state.current_action["type"])
        self.assert_place_unchanged(state)
        done = await self.db.call("query_events", session_id=fixture.SESSION, event_type="action_done")
        self.assertFalse(any(row["detail"].get("type") == "walk_to" for row in done))
        skipped = await self.db.call("query_events", session_id=fixture.SESSION, event_type="skip")
        self.assertTrue(any(row["detail"].get("code") == "already_at_target" for row in skipped))
        self.assertEqual(memories, await self.db.call("query_memories", session_id=fixture.SESSION, limit=100))

    async def test_saved_zero_distance_does_not_apply_arrival_effects_or_generate_followup(self):
        await self.prepare()
        definition = self.engine.world.action_map()["walk_to"]
        definition.on_complete.effects = {"energy": -.2}
        definition.on_complete.trigger = "llm_followup"
        async with self.engine.session_state(fixture.SESSION) as state:
            await self.saved_walk(state, "study")
            action = state.current_action
            action["elapsed_ticks"] = 1
            self.assertFalse(await self.engine._finish_action(state, self.engine.node(state.node_id),
                              TickOutcome(fixture.SESSION), action))
            self.assertIsNone(state.current_action)
            self.assertEqual(1, state.current_plan["current_step"])
            self.assertEqual(.95, state.energy)
            self.assert_place_unchanged(state)

    async def test_saved_zero_distance_with_long_countdown_continues_in_the_first_beat(self):
        await self.prepare()
        async with self.engine.session_state(fixture.SESSION) as state:
            await self.saved_walk(state, "study")
            state.current_action["duration_ticks"] = 120
        self.clock.advance(60)
        await self.engine._tick_session(fixture.SESSION)
        state = await self.saved()
        self.assertEqual("onsite_work_w", state.current_action["type"])
        self.assertEqual(0, state.current_action["elapsed_ticks"])
        self.assert_place_unchanged(state)

    async def test_saved_zero_distance_does_not_apply_per_minute_or_deferred_completion_effects(self):
        for mode in ("elapsed", "resumed", "settled_deferred"):
            with self.subTest(mode=mode):
                await self.prepare()
                definition = self.engine.world.action_map()["walk_to"]
                definition.on_complete.effects_per_minute = {"energy": "+0.02"}
                async with self.engine.session_state(fixture.SESSION) as state:
                    await self.saved_walk(state, "study")
                    state.energy = .6
                    action = state.current_action
                    action["elapsed_ticks"] = 10
                    if mode == "resumed":
                        action.update(elapsed_ticks=2, resumed_elapsed_ticks=8)
                    elif mode == "settled_deferred":
                        definition.on_complete.effects_per_minute = {"energy": "0.2"}
                        action["effects_settled_minutes"] = 2
                    await self.engine._finish_action(state, self.engine.node(state.node_id),
                                                     TickOutcome(fixture.SESSION), action)
                    self.assertAlmostEqual(.6, state.energy)
                    self.assertIsNone(state.current_action)
                    self.assertEqual(1, state.current_plan["current_step"])
                    self.assert_place_unchanged(state)

    async def test_real_move_keeps_its_configured_per_minute_completion_effect(self):
        await self.prepare()
        self.engine.world.action_map()["walk_to"].on_complete.effects_per_minute = {"energy": "+0.02"}
        async with self.engine.session_state(fixture.SESSION) as state:
            await self.saved_walk(state, "kitchen")
            state.energy = .6
            state.current_action["elapsed_ticks"] = 10
            await self.engine._finish_action(state, self.engine.node(state.node_id),
                                             TickOutcome(fixture.SESSION), state.current_action)
            self.assertEqual("kitchen", state.node_id)
            self.assertAlmostEqual(.8, state.energy)
            self.assertEqual([], self.llm.calls)
            self.assertEqual([], self.messenger.sent)

    async def test_saved_outside_zero_distance_does_not_refresh_outdoor_stamp_at_completion(self):
        outside = next(node.id for node in self.engine.world.nodes if not self.engine.world.is_home_zone(node.id))
        await self.prepare(outside)
        async with self.engine.session_state(fixture.SESSION) as state:
            await self.saved_walk(state, outside)
            action = state.current_action
            action["elapsed_ticks"] = 1
            await self.engine._finish_action(state, self.engine.node(state.node_id), TickOutcome(fixture.SESSION), action)
            self.assert_place_unchanged(state)

    async def test_real_walk_still_records_origin_destination_and_new_dwell_start(self):
        await self.prepare()
        async with self.engine.session_state(fixture.SESSION) as state:
            await self.saved_walk(state, "kitchen")
        self.clock.advance(60)
        await self.engine._tick_session(fixture.SESSION)
        state = await self.saved()
        self.assertEqual("kitchen", state.node_id)
        self.assertEqual(self.clock.now(), state.node_since)
        self.assertEqual("onsite_work_w", state.current_action["type"])
        done = await self.db.call("query_events", session_id=fixture.SESSION, event_type="action_done")
        walk = next(row["detail"] for row in done if row["detail"].get("type") == "walk_to")
        self.assertEqual(("study", "kitchen", True), (walk["from"], walk["to"], walk["moved"]))
        self.assertEqual([], self.llm.calls)
        self.assertEqual([], self.messenger.sent)

