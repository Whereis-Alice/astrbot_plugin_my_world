"""A running quiet activity keeps its actual name through configuration edits."""
import unittest

from core.models import ActionDef
from tests import test_engine as fixture

S = fixture.SESSION


class IdleLabelContinuityTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    def quiet_decisions(self):
        self.engine.decider.should_ask_llm = lambda *_args, **_kwargs: False
        self.engine.decider.rule_plan = lambda *_args, **_kwargs: None

    async def start_idle(self, *, description="翻看桌上的书签"):
        raw = self.store.raw_world()
        for action in raw["actions"]:
            if action.get("idle"):
                action["enabled"] = False
        raw["actions"].append(ActionDef(
            id="idle_label", name="整理书签", description=description,
            category="continuous", idle=True, life_role="interest",
            duration_mode="fixed", duration=600,
        ).model_dump(mode="json"))
        raw.setdefault("idle_life", {}).update(duration_min_minutes=10, duration_max_minutes=10)
        raw["sleep"]["mode"] = "activity"
        raw["state_dynamics"].update(energy_decay_per_min=0, daily_mood_enabled=False)
        self.store.save_world(raw)
        self.engine.reload_config()
        self.quiet_decisions()
        async with self.engine.session_state(S) as state:
            state.state, state.energy = "idle", .9
            state.current_action = state.current_plan = None
            state.life_updated_at = self.clock.now()
            self.engine._tick_idle_life(state)
        return await self.engine.load_state(S, cold_start=False)

    def rename(self):
        raw = self.store.raw_world()
        action = next(row for row in raw["actions"] if row["id"] == "idle_label")
        action.update(name="轻轻哼歌", description="哼一段新的旋律")
        self.store.save_world(raw)
        self.engine.reload_config()
        self.quiet_decisions()

    async def tick(self):
        self.clock.advance(60)
        await self.engine._tick_session(S)
        return await self.engine.load_state(S, cold_start=False)

    async def test_current_activity_and_reply_context_keep_original_name_after_reload(self):
        await self.start_idle()
        self.rename()
        state = await self.engine.load_state(S, cold_start=False)
        view = self.engine.activity_view(state)
        self.assertEqual("整理书签", view["label"])
        self.assertEqual("翻看桌上的书签", view["detail"])
        prompt = self.engine.prompts._in_progress_block(state)
        self.assertIn("整理书签", prompt)
        self.assertIn("翻看桌上的书签", prompt)
        self.assertNotIn("轻轻哼歌", prompt)
        self.assertEqual([], self.llm.calls)
        self.assertEqual([], self.messenger.sent)

    async def test_rename_before_first_progress_keeps_executed_name_in_practice(self):
        await self.start_idle()
        self.rename()
        state = await self.tick()
        record = state.life_interests["idle_label"]
        self.assertEqual("整理书签", record["label"])
        self.assertEqual(["整理书签"], [row["label"] for row in record["episodes"]])
        self.assertEqual((1, 1), (record["sessions"], record["minutes"]))

    async def test_rename_after_progress_does_not_split_one_activity_into_two_names(self):
        await self.start_idle()
        await self.tick()
        self.rename()
        state = await self.tick()
        record = state.life_interests["idle_label"]
        self.assertEqual("整理书签", record["label"])
        self.assertEqual(["整理书签"], [row["label"] for row in record["episodes"]])
        self.assertEqual((1, 2), (record["sessions"], record["minutes"]))

    async def test_next_actual_activity_uses_new_name_without_rewriting_previous_effort(self):
        await self.start_idle()
        await self.tick()
        self.rename()
        for _ in range(9):
            state = await self.tick()
        self.assertEqual("轻轻哼歌", self.engine.activity_view(state)["label"])
        state = await self.tick()
        record = state.life_interests["idle_label"]
        self.assertEqual("轻轻哼歌", record["label"])
        self.assertEqual(["整理书签", "轻轻哼歌"], [row["label"] for row in record["episodes"]])
        self.assertEqual((2, 11), (record["sessions"], record["minutes"]))
        self.assertEqual([], self.llm.calls)
        self.assertEqual([], self.messenger.sent)

    async def test_missing_legacy_snapshot_keeps_definition_fallback(self):
        await self.start_idle()
        async with self.engine.session_state(S) as state:
            state.idle_activity.pop("action_name", None)
            state.idle_activity.pop("action_description", None)
        self.rename()
        state = await self.tick()
        view = self.engine.activity_view(state)
        self.assertEqual("轻轻哼歌", view["label"])
        self.assertEqual("哼一段新的旋律", view["detail"])
        self.assertEqual("轻轻哼歌", state.life_interests["idle_label"]["label"])

    async def test_explicit_empty_description_is_preserved_until_next_activity(self):
        await self.start_idle(description="")
        self.rename()
        state = await self.engine.load_state(S, cold_start=False)
        self.assertEqual("", self.engine.activity_view(state)["detail"])

    async def test_disabled_activity_is_stopped_without_completion_reward(self):
        await self.start_idle()
        raw = self.store.raw_world()
        action = next(row for row in raw["actions"] if row["id"] == "idle_label")
        action.update(enabled=False, on_complete={"effects": {"energy": "=1"}})
        self.store.save_world(raw)
        self.engine.reload_config()
        self.quiet_decisions()
        async with self.engine.session_state(S) as state:
            state.energy = .5
            self.engine._tick_idle_life(state)
            self.assertEqual({}, state.idle_activity)
            self.assertEqual(.5, state.energy)
        self.assertEqual([], self.llm.calls)
