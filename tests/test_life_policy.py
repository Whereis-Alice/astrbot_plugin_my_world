"""Regression checks for event rhythm, bounded recall and weighted routes."""

import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from core.db import Database
from core.engine.models import TickOutcome
from core.events import EventPackage
from core.defaults import default_world
from core.event_rhythm import in_event_hours, pick_tone
from core.map_inspection import inspect_routes
from core.memory import INTERACTION, SCENE, MemoryEngine
from core.models import EventAutoConfig, WorldConfig, parse_world
from core.pathfinding import find_path, nearest_node, travel_cost
from core.state import WorldState
from tests import test_events as events

SESSION = events.SESSION


class RoutePolicyTests(unittest.TestCase):
    def test_faster_route_can_have_more_stops(self):
        graph = {"a": [("d", 20), ("b", 2)], "b": [("c", 2)], "c": [("d", 2)], "d": []}
        self.assertEqual(find_path(graph, "a", "d"), ["a", "b", "c", "d"])
        self.assertEqual(travel_cost(graph, "a", "d"), 6)
        self.assertEqual(nearest_node(graph, "a", ["d", "b"]), "b")
        self.assertIsNone(find_path(graph, "d", "a"))

    def test_parallel_edges_cycles_and_equal_costs_are_stable(self):
        graph = {"a": [("b", 8), ("b", 1), ("c", 1)], "b": [("a", 1), ("d", 1)], "c": [("d", 1)], "d": []}
        self.assertEqual(find_path(graph, "a", "d"), ["a", "b", "d"])
        self.assertEqual(travel_cost(graph, "a", "d"), 2)

    def test_inspector_finds_one_way_traps_and_islands(self):
        world = WorldConfig.model_validate({
            "nodes": [{"id": value} for value in ["a", "b", "island"]],
            "edges": [{"from": "a", "to": "b", "ticks": 3, "bidirectional": False}],
        })
        report = inspect_routes(world, "a", "b")
        self.assertEqual(report["unreachable"], ["island"])
        self.assertEqual(report["no_return"], ["b"])
        self.assertEqual(report["ticks"], 3)
        self.assertIsNone(inspect_routes(world, "b", "a")["path"])
        with self.assertRaises(ValueError):
            inspect_routes(world, "missing")


class RecallPolicyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Database(Path(directory.name) / "state.db")
        self.addCleanup(self.db.close)
        self.world, _ = parse_world(default_world())
        self.memory = MemoryEngine(self.db, self.world)

    def remember(self, content, kind=SCENE, **extra):
        return self.memory.remember(session_id="s", persona_id="p", node_id="bedroom", content=content, memory_type=kind, **extra)

    def recall(self, **extra):
        return self.memory.recall(session_id="s", persona_id="p", **extra)

    def test_life_only_keeps_life_and_explicit_chat_lookup(self):
        self.remember("一起讨论过周末旅行", INTERACTION)
        self.remember("在花园种了一盆薄荷")
        self.world.memory.automatic_recall = "life_only"
        self.assertEqual([item.content for item in self.recall(automatic=True)], ["在花园种了一盆薄荷"])
        self.assertEqual(len(self.recall()), 2)
        self.world.memory.automatic_recall = "manual"
        self.assertEqual(self.recall(automatic=True), [])
        self.assertEqual(len(self.recall()), 2)

    def test_budget_skips_long_facts_without_truncating_or_deleting(self):
        long = "很长的完整记忆" * 80
        self.remember(long, weight=1)
        self.remember("种了一盆薄荷", weight=.4)
        self.world.memory.recall_max_chars = 80
        selected = self.recall(automatic=True)
        self.assertEqual([item.content for item in selected], ["种了一盆薄荷"])
        self.assertIn(long, [item.content for item in self.recall()])
        self.world.memory.recall_max_chars = 0
        self.assertEqual(self.recall(automatic=True), [])

    def test_strict_keyword_does_not_return_unrelated_memories(self):
        self.remember("在花园种了一盆薄荷")
        self.assertEqual(self.recall(keyword="毕业考试", strict_keyword=True), [])

    def test_same_fact_across_scopes_is_only_recalled_once(self):
        self.remember("在花园种了一盆薄荷", scope="group")
        self.remember("在花园种了一盆薄荷。", scope="persona")
        self.assertEqual(len(self.recall()), 1)


class TonePolicyTests(unittest.TestCase):
    def test_time_window_supports_midnight_and_all_day(self):
        self.assertTrue(in_event_hours(2, 22, 7))
        self.assertFalse(in_event_hours(7, 22, 7))
        self.assertTrue(in_event_hours(20, 0, 0))

    def test_zero_weight_is_never_selected_and_distribution_is_balanced(self):
        rng = random.Random(42)
        weights = EventAutoConfig().tone_weights
        samples = [pick_tone(weights, rng) for _ in range(10000)]
        self.assertGreater(samples.count("daily") + samples.count("good"), 6800)
        self.assertLess(samples.count("bad"), 1200)
        self.assertEqual({pick_tone({"good": 1, "bad": 0}, rng) for _ in range(100)}, {"good"})
        for invalid in [{"bad": -1}, {"daily": 0}, {"unknown": 1}, {"daily": float("nan")}]:
            with self.assertRaises(ValueError):
                EventAutoConfig(tone_weights=invalid)


class AutomaticEventTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = events.EventEngineTest.asyncSetUp

    async def test_disabling_new_events_still_advances_existing_threads(self):
        self.engine.world.events.auto.enabled = False
        self.engine._advance_pending_help = AsyncMock()
        self.engine._continue_thread = AsyncMock(return_value=False)
        self.engine._start_event = AsyncMock()
        state = WorldState(session_id=SESSION, node_id=self.node_id)
        await self.engine._maybe_run_event(state, self.engine.node(self.node_id), TickOutcome(session_id=SESSION))
        self.engine._continue_thread.assert_awaited_once()
        self.engine._start_event.assert_not_awaited()

    async def test_cap_and_backoff_prevent_new_generation(self):
        self.engine._advance_pending_help = AsyncMock()
        self.engine._continue_thread = AsyncMock(return_value=False)
        self.engine._roll_event_tier = Mock(return_value=("micro", {}))
        self.engine._start_event = AsyncMock()
        state = WorldState(session_id=SESSION, node_id=self.node_id, node_since=self.clock.now() - 10000)
        state.auto_event_day = self.engine.local_now().date().isoformat()
        state.auto_event_count = self.engine.world.events.auto.max_per_day
        await self.engine._maybe_run_event(state, self.engine.node(self.node_id), TickOutcome(session_id=SESSION))
        self.engine._start_event.assert_not_awaited()
        state.auto_event_count = 0
        state.auto_event_retry_at = self.clock.now() + 300
        await self.engine._maybe_run_event(state, self.engine.node(self.node_id), TickOutcome(session_id=SESSION))
        self.engine._start_event.assert_not_awaited()

    async def test_only_successful_roots_count_and_state_survives_reload(self):
        self.engine._settle_micro = AsyncMock()
        self.engine._generate_event_package = AsyncMock(return_value=None)
        async with self.engine.session_state(SESSION) as state:
            outcome = TickOutcome(session_id=SESSION)
            await self.engine._start_event(state, self.engine.node(state.node_id), outcome, tier="micro", source="roll")
            self.assertEqual(state.auto_event_count, 0)
            self.assertGreater(state.auto_event_retry_at, self.clock.now())
        restored = await self.engine.load_state(SESSION, cold_start=False)
        self.assertGreater(restored.auto_event_retry_at, self.clock.now())
        self.engine._generate_event_package = AsyncMock(return_value=EventPackage(title="热茶刚好", hook="茶泡好了", tier="micro"))
        async with self.engine.session_state(SESSION) as state:
            await self.engine._start_event(state, self.engine.node(state.node_id), TickOutcome(session_id=SESSION), tier="micro", source="roll")
            self.assertEqual(state.auto_event_count, 1)
            self.assertEqual(state.auto_event_retry_at, 0)
            self.assertIn(state.event_threads[-1]["tone"], EventAutoConfig().tone_weights)
        restored = await self.engine.load_state(SESSION, cold_start=False)
        self.assertEqual(restored.auto_event_count, 1)

    async def test_daily_limit_resets_on_the_next_local_day(self):
        self.engine._advance_pending_help = AsyncMock()
        self.engine._continue_thread = AsyncMock(return_value=False)
        self.engine._event_block_reason = Mock(return_value="")
        self.engine._event_gap_reason = Mock(return_value="")
        self.engine._roll_event_tier = Mock(return_value=("micro", {}))
        self.engine._start_event = AsyncMock()
        state = WorldState(session_id=SESSION, node_id=self.node_id)
        state.auto_event_day = self.engine.local_now().date().isoformat()
        state.auto_event_count = self.engine.world.events.auto.max_per_day
        self.clock.advance(86400)
        await self.engine._maybe_run_event(state, self.engine.node(self.node_id), TickOutcome(session_id=SESSION))
        self.engine._start_event.assert_awaited_once()
        self.assertEqual(state.auto_event_count, 0)
        self.assertEqual(state.auto_event_day, self.engine.local_now().date().isoformat())

    async def test_busy_switch_and_manual_submission_have_separate_gates(self):
        state = WorldState(session_id=SESSION, node_id=self.node_id, node_since=self.clock.now() - 600)
        state.current_action = {"type": "read", "desc": "看书"}
        self.assertEqual(self.engine._event_block_reason(state, self.clock.now()), "")
        self.assertTrue(self.engine._event_block_reason(state, self.clock.now(), manual=True))
        self.engine.world.events.while_busy = False
        self.assertTrue(self.engine._event_block_reason(state, self.clock.now()))
