"""Life projects stay in-world: no computer setup, tools, or fabricated delivery."""
import copy
import json
import unittest

from core import life_matters as matters
from core.engine import TickOutcome
from core.life_memory import clean_memory_settings
from core.models import WorldConfig
from core.state import WorldState
from tests import test_engine as fixture

SESSION = fixture.SESSION


class SimulationTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    async def prepare(self, *, kind="project", minutes=5):
        self.llm.replies = [json.dumps({"steps": [{"text": "为自己的收藏编写分类脚本", "action": "life_simulation",
                                                  "minutes": minutes, "day_offset": 0}]})]
        async with self.engine.session_state(SESSION) as state:
            state.state, state.energy = "idle", .9
            item = self.engine.add_life_matter(state, "用 Python 整理自己的动漫收藏", session=SESSION, kind=kind)
            mid = item["id"]
        await self.engine.advance_life_matters(SESSION)
        return mid

    async def tick_work(self, count):
        for _ in range(count):
            self.clock.advance(60)
            async with self.engine.session_state(SESSION) as state:
                await self.engine._tick_continuous(state, self.engine.node(state.node_id), TickOutcome(SESSION))

    async def test_simulation_with_empty_action_library_finishes_only_after_online_work(self):
        raw = self.store.raw_world()
        raw["actions"] = []
        self.store.save_world(raw)
        self.engine.reload_config()
        mid = await self.prepare()
        state = await self.engine.load_state(SESSION, cold_start=False)
        self.assertEqual(state.current_action["type"], "life_simulation")
        self.assertIn("分类脚本", self.engine.activity_view(state)["label"])
        self.assertEqual(matters.find(state, mid)["progress"], 0)
        await self.tick_work(4)
        state = await self.engine.load_state(SESSION, cold_start=False)
        self.assertEqual(matters.find(state, mid)["steps"][0]["status"], "pending")
        self.assertEqual(len(self.llm.calls), 1)
        await self.tick_work(1)
        self.clock.advance(3600)
        state = await self.engine.load_state(SESSION, cold_start=False)
        self.llm.replies = [json.dumps({"outcome": "completed", "reason": "完成世界内的分类脚本模拟",
            "stage": {"summary": "完成世界内模拟的分类草稿", "content": "按标题保存收藏条目；这里只留下世界内草稿，没有运行或验证现实脚本。",
                      "basis_step_ids": [step["id"] for step in matters.find(state, mid)["steps"]]}})]
        await self.engine.advance_life_matters(SESSION)
        state = await self.engine.load_state(SESSION, cold_start=False)
        item = matters.find(state, mid)
        self.assertEqual(item["status"], "completed")
        self.assertEqual(item["steps"][0]["result"]["kind"], "life_simulation")
        self.assertEqual(state.life_artifacts[mid]["scope"], "simulation")
        self.assertIn("世界内模拟", item["result"])
        self.assertIn("不代表操作了现实文件", matters.context_block(state, session_id=SESSION))
        self.assertEqual(len(self.llm.calls), 2)
        self.assertEqual(self.tools.calls, [])
        self.assertEqual(self.messenger.sent, [])
        self.assertNotIn("life_simulation", state.life_interests)

    async def test_pause_and_resume_keep_partial_simulation_time(self):
        mid = await self.prepare(minutes=10)
        await self.tick_work(3)
        async with self.engine.session_state(SESSION) as state:
            self.engine.manage_life_matter(state, mid, "pause")
            self.assertIsNone(state.current_action)
            self.assertEqual(matters.find(state, mid)["steps"][0]["worked_minutes"], 3)
        self.clock.advance(3600)
        await self.engine.advance_life_matters(SESSION)
        self.assertIsNone((await self.engine.load_state(SESSION, cold_start=False)).current_action)
        async with self.engine.session_state(SESSION) as state:
            self.engine.manage_life_matter(state, mid, "resume")
        await self.engine.advance_life_matters(SESSION)
        state = await self.engine.load_state(SESSION, cold_start=False)
        self.assertEqual(state.current_action["duration_ticks"], 7)
        self.assertEqual(self.tools.calls, [])

    async def test_sleep_and_low_energy_prevent_new_simulation(self):
        for status, energy in [("sleeping", .9), ("idle", .1)]:
            async with self.engine.session_state(SESSION) as state:
                state.state, state.energy = status, energy
                self.engine.add_life_matter(state, "在世界内画一幅画")
            before = len(self.llm.calls)
            await self.engine.advance_life_matters(SESSION)
            self.assertEqual(len(self.llm.calls), before)

    async def test_simulated_preparation_never_fulfils_a_real_promise(self):
        mid = await self.prepare(kind="promise")
        await self.tick_work(5)
        self.clock.advance(3600)
        self.llm.replies = ['{"outcome":"completed","reason":"准备好了"}']
        before = len(self.llm.calls)
        await self.engine.advance_life_matters(SESSION)
        state = await self.engine.load_state(SESSION, cold_start=False)
        item = matters.find(state, mid)
        self.assertEqual(item["status"], "blocked")
        self.assertEqual(item["blocker"], "evidence")
        self.assertEqual(len(self.llm.calls), before)
        self.assertNotIn(mid, state.life_artifacts)
        self.assertEqual(self.messenger.sent, [])


class RetiredComputerStateTests(unittest.TestCase):
    def test_legacy_prepared_promise_waits_for_evidence_without_occupying_new_work(self):
        item = matters.make("给现实用户寄明信片", now=1, session="s", kind="promise")
        item.update(status="blocked", steps=[{"action": "read", "status": "done"}])
        state = WorldState("s", life_matters=[item], life_migrated=True)
        matters.ensure(state, 10)
        self.assertEqual(item["blocker"], "evidence")

    def test_ordinary_reading_in_progress_keeps_its_steps_and_timing(self):
        item = matters.make("继续读书", now=1, session="s")
        item.update(status="working", remaining_condition="", steps=[
            {"id": "read", "action": "read", "status": "pending", "worked_minutes": 4}])
        state = WorldState("s", life_matters=[item], life_migrated=True)
        matters.ensure(state, 10)
        self.assertEqual(item["status"], "working")
        self.assertEqual(item["steps"][0]["worked_minutes"], 4)
        self.assertNotIn("needs_replan", item)
        self.assertEqual(item["history"], [])

    def test_cleanup_is_idempotent_preserves_goal_pause_and_closed_history(self):
        for status in ("blocked", "working", "paused", "completed"):
            with self.subTest(status=status):
                item = matters.make("旧的编程事项", now=1, session="s")
                item.update(status=status, blocker="environment", obstacle="旧环境不可用", execution_review_required=True,
                            executions=[{"status": "running"}], artifacts=[{"path": "old.py"}], capability_key="old",
                            steps=[{"id": "native", "action": "native_agent", "status": "pending"},
                                   {"id": "read", "action": "read", "status": "done", "text": "读过一些资料"}])
                state = WorldState("s", life_matters=[item], life_migrated=True,
                                   life_artifacts={"old": {"scope": "sandbox"}, "keep": {"scope": "in_world", "result": "读书"}})
                matters.ensure(state, 10)
                self.assertEqual(item["text"], "旧的编程事项")
                self.assertEqual(item["status"], status if status in {"paused", "completed"} else "active")
                self.assertEqual([s["id"] for s in item["steps"]], ["read"])
                self.assertFalse({"executions", "artifacts", "execution_review_required", "capability_key"}.intersection(item))
                self.assertEqual(set(state.life_artifacts), {"keep"})
                snapshot = copy.deepcopy(state)
                matters.ensure(state, 20)
                self.assertEqual(state, snapshot)

    def test_old_execution_settings_cannot_survive_permissive_models_or_overrides(self):
        old = {"life_execution": {"enabled": True, "trusted_tools": {"old": "fingerprint"}},
               "sessions": [{"config_overrides": {"settings": {"life_execution": {"sandbox": "old"}, "bot_name": "她"}}}]}
        cleaned = clean_memory_settings(old)
        self.assertNotIn("life_execution", cleaned)
        self.assertEqual(cleaned["sessions"][0]["config_overrides"]["settings"], {"bot_name": "她"})
        self.assertNotIn("life_execution", WorldConfig.model_validate(old).model_dump())
        self.assertIn("life_execution", old)
