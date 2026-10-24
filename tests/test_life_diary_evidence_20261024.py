"""Real execution must leave usable, private daily-life evidence."""
from __future__ import annotations

import json
import copy
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock

from core.json_actions import PlannedAction
from core.models import ActionDef
from core.planner import create_plan
from core.engine.models import TickOutcome
from core.engine.calendar_diary import _diary_practice_events
from tests import test_engine as fixture

SESSION = fixture.SESSION


class DiaryEvidenceTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    def add_action(self, **kwargs):
        definition = ActionDef(**kwargs)
        self.engine.world.actions.append(definition)
        return definition

    async def sources(self, day=None):
        day = day or self.engine.local_now().date().isoformat()
        start, _ = self.engine._journal_day_bounds(day)
        with self.db.raw._lock:
            self.db.raw._execute(
                "UPDATE event_log SET created_at=?", (start.timestamp() + 60,)
            )
        return await self.engine._diary_sources(SESSION, day)

    async def test_completed_instant_life_can_generate_a_diary(self):
        definition = self.add_action(id="instant-017", name="给窗台的薄荷浇水")
        day = self.engine.local_now().date().isoformat()
        async with self.engine.session_state(SESSION) as state:
            outcome = TickOutcome(SESSION)
            await self.engine._start_action(
                state, self.engine.node(state.node_id), outcome,
                definition, PlannedAction(type=definition.id), 0, True,
            )
            self.assertEqual([], outcome.messages)
        sources = await self.sources(day)
        self.assertIn(definition.name, json.dumps(sources["events"], ensure_ascii=False))
        self.clock.advance(86400)
        self.engine.world.diary.automatic = True
        self.engine._ask_journal = AsyncMock(return_value=json.dumps({
            "title": "窗台上的薄荷", "content": "今天给薄荷浇了水。"
        }, ensure_ascii=False))
        saved = await self.engine.ensure_diary(SESSION, day, automatic=True)
        self.assertIsNotNone(saved)
        self.assertEqual(1, self.engine._ask_journal.await_count)

    async def test_continuous_life_keeps_its_execution_label_and_phase(self):
        definition = self.add_action(id="quiet-029", name="整理旧书架",
                                     category="continuous", duration=120)
        async with self.engine.session_state(SESSION) as state:
            outcome = TickOutcome(SESSION)
            await self.engine._start_action(
                state, self.engine.node(state.node_id), outcome,
                definition, PlannedAction(type=definition.id), 0, True,
            )
            action = dict(state.current_action)
            action["elapsed_ticks"] = action["duration_ticks"]
            definition.name = "已改名的动作"
            await self.engine._finish_action(state, self.engine.node(state.node_id), outcome, action)
        sources = await self.sources()
        rows = [row for row in sources["events"] if row["type"] in {"action_start", "action_done"}]
        self.assertEqual(2, len(rows))
        for row in rows:
            self.assertIn("整理旧书架", row["detail"])
            self.assertNotIn("已改名的动作", row["detail"])

    async def test_quiet_idle_effort_is_diary_evidence_without_claiming_completion(self):
        self.add_action(id="idle-031", name="练习素描", category="continuous",
                        idle=True, duration=600, life_role="interest")
        for definition in self.engine.world.actions:
            if definition.idle and definition.id != "idle-031":
                definition.enabled = False
        async with self.engine.session_state(SESSION) as state:
            state.current_plan = None
            state.current_action = None
            state.state = "idle"
            self.engine._tick_idle_life(state)
            self.assertEqual("idle-031", state.idle_activity["id"])
            state.world_time += 3
            self.clock.advance(180)
            self.engine._settle_idle_practice(state)
            state.idle_activity = {}  # interrupted, never completed
        sources = await self.sources()
        rows = [row for row in sources["events"] if row["type"] == "life_practice"]
        self.assertEqual(1, len(rows))
        self.assertIn("练习素描", rows[0]["detail"])
        self.assertIn("3", rows[0]["detail"])
        self.assertIn("实际投入", rows[0]["detail"])
        self.assertNotIn("完成", rows[0]["detail"])
        self.assertEqual([], self.messenger.sent)
        self.clock.advance(86400)
        self.engine.world.diary.automatic = True
        self.engine._ask_journal = AsyncMock(return_value=json.dumps({
            "title": "画了几笔", "content": "练了几分钟素描，还没有画完。"
        }, ensure_ascii=False))
        day = (self.engine.local_now().date() - timedelta(days=1)).isoformat()
        self.assertIsNotNone(await self.engine.ensure_diary(SESSION, day, automatic=True))

    async def test_narrated_personal_action_keeps_only_its_life_label(self):
        definition = self.add_action(id="instant-037", name="擦拭窗台",
                                     visible=True, template="聊天里的私密台词")
        async with self.engine.session_state(SESSION) as state:
            await self.engine._start_action(state, self.engine.node(state.node_id), TickOutcome(SESSION),
                definition, PlannedAction(type=definition.id, content="私密正文", params={"secret": "私密参数"}), 0, True)
        sources = await self.sources()
        encoded = json.dumps(sources, ensure_ascii=False)
        self.assertIn("擦拭窗台", encoded)
        self.assertNotIn("私密", encoded)

    async def test_chat_and_person_directed_actions_are_excluded_before_event_limit(self):
        definition = self.add_action(id="instant-038", name="整理桌面")
        async with self.engine.session_state(SESSION) as state:
            await self.engine._start_action(state, self.engine.node(state.node_id), TickOutcome(SESSION),
                                           definition, PlannedAction(type=definition.id), 0, True)
            directed = self.add_action(id="instant-039", name="私人动作", target_type="user")
            await self.engine._start_action(state, self.engine.node(state.node_id), TickOutcome(SESSION),
                                           directed, PlannedAction(type=directed.id, target="1002"), 0, True)
        _, owner = await self.engine._journal_persona(SESSION)
        with self.db.raw.atomic():
            for _ in range(320):
                self.db.raw.add_event(session_id=SESSION, persona_id=owner, event_type="action",
                                      detail={"type": "say", "desc": "聊天", "messages": ["私密聊天"]})
            self.db.raw._execute("UPDATE event_log SET detail='broken-json' WHERE id=(SELECT MAX(id) FROM event_log)")
        sources = await self.sources()
        self.assertEqual(1, len(sources["events"]))
        self.assertIn("整理桌面", sources["events"][0]["detail"])
        self.assertNotIn("私人动作", json.dumps(sources, ensure_ascii=False))
        # General diagnostic queries still expose the complete saved log.
        start, end = self.engine._journal_day_bounds(sources["day"])
        all_rows = await self.db.call("event_log_between", [SESSION], start.timestamp(), end.timestamp(), ["action"], 400, owner)
        self.assertEqual(322, len(all_rows))

    async def test_practice_evidence_is_owned_dated_finite_and_read_only(self):
        day = self.engine.local_now().date().isoformat()
        _, owner = await self.engine._journal_persona(SESSION)
        good = {"day": day, "label": "素描", "minutes": 2, "at": self.clock.now()}
        state = await self.engine.load_state(SESSION, cold_start=False)
        state.life_interests = {
            "known": {"personas": {owner: {"episodes": [good,
                {**good, "day": "2000-01-01"}, {**good, "minutes": float("inf")},
                {**good, "minutes": "invalid"}, {**good, "minutes": 0}, None]},
                "other": {"episodes": [{**good, "label": "另一角色的秘密"}]}}},
            "legacy": {"episodes": [{**good, "label": "归属未知"}]},
            "broken": {"personas": {owner: {"episodes": 9}}},
        }
        before = copy.deepcopy(state.life_interests)
        result = _diary_practice_events(state, day, owner)
        self.assertEqual(1, len(result))
        self.assertIn("素描", result[0]["detail"])
        self.assertEqual(before, state.life_interests)
        self.assertEqual(result, _diary_practice_events(state, day, owner))

    async def test_effort_crossing_midnight_stays_on_its_actual_days(self):
        definition = self.add_action(id="idle-041", name="练习素描", category="continuous", life_role="interest")
        day = self.engine.local_now().date().isoformat()
        _, end = self.engine._journal_day_bounds(day)
        self.clock.advance(end.timestamp() + 60 - self.clock.now())
        async with self.engine.session_state(SESSION) as state:
            _, owner = await self.engine._journal_persona(SESSION)
            self.engine.settle_life_practice(state, {"type": definition.id, "persona_key": owner, "elapsed_ticks": 3})
        previous = await self.engine._diary_sources(SESSION, day)
        current_day = self.engine.local_now().date().isoformat()
        current = await self.engine._diary_sources(SESSION, current_day)
        self.assertIn("minutes: 2.0", json.dumps(previous["events"], ensure_ascii=False))
        self.assertIn("minutes: 1.0", json.dumps(current["events"], ensure_ascii=False))

    async def test_generic_simulation_does_not_export_private_plan_intent_as_its_label(self):
        async with self.engine.session_state(SESSION) as state:
            state.current_plan = create_plan(steps=[{"action": "life_simulation", "duration": 120}],
                world_time=state.world_time, now=self.clock.now(), tick_seconds=60)
            definition = self.engine.world.action_map()["life_simulation"]
            outcome = TickOutcome(SESSION)
            await self.engine._start_action(state, self.engine.node(state.node_id), outcome, definition,
                PlannedAction(type=definition.id, intent="关于另一人的私密约定"), 0, True, from_plan=True)
            action = dict(state.current_action)
            action["elapsed_ticks"] = action["duration_ticks"]
            await self.engine._finish_action(state, self.engine.node(state.node_id), outcome, action)
        self.assertNotIn("私密约定", json.dumps(await self.sources(), ensure_ascii=False))
