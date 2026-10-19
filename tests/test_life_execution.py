"""Native life execution must be durable, interruptible and grounded in receipts."""
import asyncio
import copy
import json
import unittest

from core import life_matters as matters
from core.life_execution import LifeExecutionResult, agent_matter_context, block, capability_key
from core.relationship import interaction_trend
from tests import test_engine as fixture

SESSION = fixture.SESSION


class Executor:
    def __init__(self):
        self.ready, self.version, self.calls = True, 1, 0
        self.events = [{"kind": "tool_start", "tool": "shell", "call_id": "c"},
                       {"kind": "tool_end", "tool": "shell", "call_id": "c", "ok": True, "result": "synthetic tests: 3 passed"}]
        self.result = LifeExecutionResult("step_done", "仅用明确标注的测试样例验证了脚本", [{"path": "my_world/script.py", "scope": "sandbox"}])
        self.error = None
        self.entered, self.release = asyncio.Event(), None

    async def capabilities(self, session_id):
        return {"ready": self.ready, "reason": "等待测试沙盒", "version": self.version, "tools": []}

    async def execute(self, *, record, permitted, **kwargs):
        self.calls += 1
        assert await permitted()
        for event in self.events:
            await record(event)
        self.entered.set()
        if self.release:
            await self.release.wait()
        if self.error:
            raise self.error
        return self.result


class ExecutionContextTests(unittest.TestCase):
    def test_large_history_stays_in_ledger_while_prompt_keeps_recent_receipts(self):
        matter = {"id": "lm_script", "text": "写脚本后等待真实素材", "steps": [
            {"id": "one", "text": "已完成的写入", "status": "done", "receipt": {"tool_receipts": ["x"*20000]}}
        ], "executions": [{"id": str(i), "summary": "历史依据", "events": [
            {"kind": "tool_end", "tool": "shell", "ok": True, "result": "x"*600} for _ in range(120)]
        } for i in range(12)]}
        original = copy.deepcopy(matter)
        context = agent_matter_context(matter)
        self.assertLess(len(json.dumps(context)), 20000)
        self.assertEqual(context["steps"][0]["status"], "done")
        self.assertEqual([row["id"] for row in context["recent_executions"]], ["10", "11"])
        self.assertEqual(matter, original)


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    ctx = fixture.EngineTestCase.ctx
    add_group = fixture.EngineTestCase.add_group

    async def asyncSetUp(self):
        await fixture.EngineTestCase.asyncSetUp(self)
        self.executor = Executor()
        self.engine.life_executor = self.executor
        async with self.engine.session_state(SESSION) as state:
            state.state, state.energy = "idle", .9
            self.item = self.engine.add_life_matter(state, "编写动漫素材校验脚本，再等待真实素材")
            self.item["steps"] = [{"id": "prepare", "action": "native_agent", "text": "实现校验脚本并用synthetic样例测试",
                                   "node": "study", "minutes": 15, "status": "pending"}]

    async def item_now(self):
        state = await self.engine.load_state(SESSION, cold_start=False)
        return matters.find(state, self.item["id"])

    async def test_success_uses_real_receipts_and_sandbox_artifact_scope(self):
        await self.engine.advance_life_matters(SESSION)
        item = await self.item_now()
        self.assertEqual(item["steps"][0]["status"], "done")
        self.assertTrue(item["needs_review"])
        self.assertEqual(item["executions"][0]["status"], "succeeded")
        state = await self.engine.load_state(SESSION, cold_start=False)
        self.assertEqual(state.life_artifacts[item["id"]]["scope"], "sandbox")
        self.assertNotEqual(item["status"], "completed")

    async def test_self_report_without_tool_does_not_complete(self):
        self.executor.events = []
        await self.engine.advance_life_matters(SESSION)
        self.assertEqual((await self.item_now())["steps"][0]["status"], "pending")

    async def test_unavailable_environment_waits_for_change_without_model_loop(self):
        self.executor.ready = False
        await self.engine.advance_life_matters(SESSION)
        self.assertEqual((await self.item_now())["blocker"], "environment")
        for _ in range(3):
            self.clock.advance(86400)
            await self.engine.advance_life_matters(SESSION)
        self.assertEqual(self.executor.calls, 0)
        self.executor.ready = True
        await self.engine.advance_life_matters(SESSION)
        self.assertEqual(self.executor.calls, 1)
        self.assertEqual(len(self.llm.calls), 0)

    async def test_partial_failure_and_manual_resume_cannot_replay(self):
        self.executor.error = RuntimeError("provider timeout after file was written")
        await self.engine.advance_life_matters(SESSION)
        item = await self.item_now()
        self.assertTrue(item["execution_review_required"])
        self.assertEqual(item["executions"][0]["status"], "partial_failed")
        async with self.engine.session_state(SESSION) as state:
            self.engine.manage_life_matter(state, item["id"], "resume")
        self.clock.advance(86400)
        await self.engine.advance_life_matters(SESSION)
        self.assertEqual(self.executor.calls, 1)

    async def test_inflight_pause_releases_lock_and_preserves_stale_receipts(self):
        self.executor.release = asyncio.Event()
        task = asyncio.create_task(self.engine.advance_life_matters(SESSION))
        await asyncio.wait_for(self.executor.entered.wait(), 2)
        async with asyncio.timeout(.5):
            async with self.engine.session_state(SESSION) as state:
                self.engine.manage_life_matter(state, self.item["id"], "pause")
                state.world_time = 1234
        self.executor.release.set()
        await task
        item = await self.item_now()
        self.assertEqual(item["status"], "paused")
        self.assertEqual(item["steps"][0]["status"], "pending")
        self.assertEqual(len(item["executions"][0]["events"]), 2)
        self.assertFalse(item["executions"][0]["accepted"])
        self.assertTrue(item["execution_review_required"])
        self.assertEqual((await self.engine.load_state(SESSION, cold_start=False)).world_time, 1234)

    async def test_restart_does_not_replay_unsettled_execution(self):
        async with self.engine.session_state(SESSION) as state:
            item = matters.find(state, self.item["id"])
            item.update(status="working", executions=[{"id": "old", "status": "running", "events": [{"kind": "tool_start"}]}])
        await self.engine.advance_life_matters(SESSION)
        item = await self.item_now()
        self.assertEqual(item["blocker"], "uncertain")
        self.assertTrue(item["execution_review_required"])
        self.assertEqual(self.executor.calls, 0)

    async def test_pending_materials_preserve_artifacts_and_allow_other_life(self):
        self.executor.result = LifeExecutionResult("waiting", "脚本已写好，未取得真实截图", [{"path": "synthetic.py"}], "没有真实素材", "等待真实素材目录")
        await self.engine.advance_life_matters(SESSION)
        item = await self.item_now()
        self.assertEqual(item["blocker"], "materials")
        self.assertEqual(len(item["artifacts"]), 1)
        self.assertTrue(item["execution_review_required"])
        self.assertEqual(item["executions"][0]["status"], "partial_waiting")
        async with self.engine.session_state(SESSION) as state:
            self.assertEqual(self.engine.life_choice(state)["kind"], "idle")
            other = self.engine.add_life_matter(state, "读另一篇短文")
            other["steps"] = [{"id": "read", "text": "静静读五分钟", "action": "read", "node": "study", "minutes": 5, "status": "pending"}]
        self.clock.advance(3600)
        await self.engine.advance_life_matters(SESSION)
        state = await self.engine.load_state(SESSION, cold_start=False)
        self.assertEqual(state.current_action["matter_id"], other["id"])
        self.assertEqual(self.executor.calls, 1)

    async def test_normal_waiting_resume_cannot_replay_partial_operations(self):
        self.executor.result = LifeExecutionResult("waiting", "脚本已写入，缺少真实素材", condition="提供真实目录")
        await self.engine.advance_life_matters(SESSION)
        async with self.engine.session_state(SESSION) as state:
            self.engine.manage_life_matter(state, self.item["id"], "resume")
        self.clock.advance(86400)
        await self.engine.advance_life_matters(SESSION)
        self.assertEqual(self.executor.calls, 1)
        self.assertTrue((await self.item_now())["execution_review_required"])

    async def test_returned_tool_error_can_have_effects_and_requires_review(self):
        self.executor.events[1].update(ok=False, result="exit_code=1 after partial file write")
        self.executor.error = RuntimeError("model unavailable after tool error")
        await self.engine.advance_life_matters(SESSION)
        self.assertEqual((await self.item_now())["blocker"], "uncertain")
        self.clock.advance(86400)
        await self.engine.advance_life_matters(SESSION)
        self.assertEqual(self.executor.calls, 1)

    async def test_concurrent_advance_starts_native_step_once(self):
        self.executor.release = asyncio.Event()
        task = asyncio.create_task(self.engine.advance_life_matters(SESSION))
        await asyncio.wait_for(self.executor.entered.wait(), 2)
        self.clock.advance(3600)
        await asyncio.wait_for(self.engine.advance_life_matters(SESSION), 2)
        self.executor.release.set()
        await task
        self.assertEqual(self.executor.calls, 1)
        self.assertEqual(self.engine._life_runs, {})

    async def test_deleted_matter_cleans_up_native_task_registration(self):
        self.executor.release = asyncio.Event()
        task = asyncio.create_task(self.engine.advance_life_matters(SESSION))
        await asyncio.wait_for(self.executor.entered.wait(), 2)
        async with self.engine.session_state(SESSION) as state:
            state.life_matters = []
            matters.sync(state)
        self.executor.release.set()
        await task
        self.assertEqual(self.engine._life_runs, {})
        self.assertIsNone(await self.item_now())

    async def test_transport_failure_without_effects_can_backoff_retry(self):
        self.executor.events, self.executor.error = [], RuntimeError("connection unavailable")
        await self.engine.advance_life_matters(SESSION)
        item = await self.item_now()
        self.assertEqual(item["blocker"], "transient")
        self.assertFalse(item.get("execution_review_required"))
        await self.engine.advance_life_matters(SESSION)
        self.assertEqual(self.executor.calls, 1)
        self.clock.advance(3600)
        await self.engine.advance_life_matters(SESSION)
        self.assertEqual(self.executor.calls, 2)

    async def test_timeout_without_message_does_not_lose_failure_state(self):
        self.executor.events, self.executor.error = [], TimeoutError()
        await self.engine.advance_life_matters(SESSION)
        item = await self.item_now()
        self.assertEqual(item["blocker"], "transient")
        self.assertEqual(item["executions"][0]["error"], "TimeoutError")

    async def test_native_plan_keeps_executable_preparation_when_materials_missing(self):
        async with self.engine.session_state(SESSION) as state:
            matters.find(state, self.item["id"])["steps"] = []
        self.llm.replies = [json.dumps({"steps": [{"text": "先用synthetic样例写脚本", "action": "native_agent", "minutes": 15, "day_offset": 0}], "blocked": "真实素材待提供"})]
        await self.engine.advance_life_matters(SESSION)
        item = await self.item_now()
        self.assertEqual(item["status"], "active")
        self.assertEqual(item["remaining_condition"], "真实素材待提供")
        self.assertEqual(item["steps"][0]["action"], "native_agent")

    async def test_completed_model_cannot_override_known_missing_condition(self):
        async with self.engine.session_state(SESSION) as state:
            item = matters.find(state, self.item["id"])
            item.update(needs_review=True, remaining_condition="还没有真实素材")
        self.llm.replies = ['{"outcome":"completed","reason":"样例测试通过"}']
        await self.engine.advance_life_matters(SESSION)
        self.assertEqual((await self.item_now())["status"], "blocked")

    async def test_running_native_step_is_not_reconciled_as_an_interruption(self):
        self.executor.release = asyncio.Event()
        task = asyncio.create_task(self.engine.advance_life_matters(SESSION))
        await asyncio.wait_for(self.executor.entered.wait(), 2)
        async with self.engine.session_state(SESSION) as state:
            self.engine.reconcile_matters(state)
            self.assertEqual(matters.find(state, self.item["id"])["status"], "working")
            self.assertEqual(self.engine.life_choice(state)["kind"], "continue")
        self.executor.release.set()
        await task

    async def test_life_rerank_only_reorders_known_ids_and_keeps_sources(self):
        self.engine.world.memory.life_rerank = True
        ids = [self.engine.memory.remember(session_id=SESSION, persona_id="", node_id="study", content=text,
                    source="life_matter", memory_type="scene") for text in ("风车：阅读说明书", "风车：试做模型", "风车：今天画草稿")]
        self.llm.replies = [json.dumps({"ids": [999999, ids[1], ids[1], True]})]
        items, reason = await self.engine.lookup_life_memories(SESSION, "", "风车")
        self.assertEqual(items[0].id, ids[1])
        self.assertEqual({row.id for row in items}, set(ids))
        self.assertIn("重排", reason)
        self.assertEqual(len(self.llm.calls), 1)

    async def test_life_rerank_discards_memories_changed_during_model_wait(self):
        self.engine.world.memory.life_rerank = True
        ids = [self.engine.memory.remember(session_id=SESSION, persona_id="", node_id="study", content=text,
                    source="life_matter", memory_type="scene") for text in ("风车：读了说明", "风车：画了草稿", "风车：试做模型")]

        async def delayed_rerank(*args, **kwargs):
            self.db.raw.update_memory(ids[0], content="管理员改正后的内容")
            self.db.raw.update_memory(ids[1], source="runtime")
            return json.dumps({"ids": ids})

        self.engine._ask_judge = delayed_rerank
        items, _ = await self.engine.lookup_life_memories(SESSION, "", "风车")
        self.assertEqual([row.id for row in items], [ids[2]])
        self.assertEqual(self.db.raw.memory_by_id(ids[0])["content"], "管理员改正后的内容")

    async def test_stale_blocked_goal_edit_is_eligible_for_new_plan(self):
        async with self.engine.session_state(SESSION) as state:
            item = matters.find(state, self.item["id"])
            block(item, kind="materials", reason="缺图", now=self.clock.now(), key=capability_key(await self.executor.capabilities(SESSION)))
            self.engine.manage_life_matter(state, item["id"], "edit", text="先做纯代码样例")
        self.llm.replies = [json.dumps({"steps": [{"text": "编写synthetic样例", "action": "native_agent", "day_offset": 0}]})]
        await self.engine.advance_life_matters(SESSION)
        self.assertEqual((await self.item_now())["blocker"], "")


class TrendTests(unittest.TestCase):
    def test_manual_decay_and_old_records_do_not_change_recent_observation(self):
        now = 2000000000
        logs = [{"at": now-i, "id": i, "delta": 1, "reason": "认真听她说话", "source": "chat"} for i in range(4)]
        logs += [{"at": now, "delta": -100, "source": source} for source in ("manual", "decay", "bond_floor", "presence")]
        logs += [{"at": now-15*86400, "delta": -100, "source": "chat"}]
        original = copy.deepcopy(logs)
        trend = interaction_trend(logs, now=now)
        self.assertEqual((trend["count"], trend["direction"]), (4, "warming"))
        self.assertEqual(logs, original)

    def test_missing_steps_never_report_fake_percentage(self):
        state = fixture.WorldState("s") if hasattr(fixture, "WorldState") else __import__("core.state", fromlist=["WorldState"]).WorldState("s")
        item = matters.make("写个脚本", now=1, session="s")
        state.life_matters, state.life_migrated = [item], True
        self.assertNotIn("0%", matters.context_block(state, session_id="s"))
        block(item, kind="materials", reason="缺素材", condition="提供目录", key=capability_key({}), now=1)
        self.assertEqual(item["resume_at"], 0)

    def test_old_activity_experience_no_longer_influences_daily_routine(self):
        from core.state import WorldState
        state = WorldState("s", life_interests={"read": {"days": ["a", "b", "c"], "sessions": 3,
                                                     "minutes": 90, "last_at": 100}})
        self.assertEqual(len(matters.stable_interests(state, now=200)), 1)
        self.assertEqual(matters.stable_interests(state, now=100+31*86400), [])
