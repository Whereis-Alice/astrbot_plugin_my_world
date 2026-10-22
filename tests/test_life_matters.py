"""Behavioral acceptance: sustained work, interruptions, evidence, latency and recovery."""
import asyncio
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from core import life_matters as matters
from core.db import Database
from core.evidence import source_snapshot
from core.memory import MemoryEngine
from core.model_calls import model_context
from core.models import ActionDef, parse_world
from core.defaults import default_world
from core.planner import active_plan, create_plan, migrate_plan
from core.state import WorldState
from tests import test_engine as engine
SESSION = engine.SESSION


class PlanClockTests(unittest.TestCase):
    def test_real_deadline_independent_of_tick_rate(self):
        for seconds in (30, 60, 300):
            state = WorldState("test", world_time=10)
            state.current_plan = create_plan(steps=[{"action": "read"}], world_time=10,
                now=1700000000, tick_seconds=seconds, valid_for=1800)
            state.plan_now = 1700001799
            self.assertIsNotNone(active_plan(state))
            state.plan_now = 1700001800
            self.assertIsNone(active_plan(state))

    def test_legacy_expiry_is_migrated_once(self):
        state = WorldState("test", world_time=35)
        state.current_plan = {"steps": [{"action": "read"}], "created_at": 10, "valid_until": 1810}
        migrate_plan(state, now=1700000000, tick_seconds=60)
        self.assertEqual(state.current_plan["expires_at"], 1700000300)
        migrate_plan(state, now=1700000060, tick_seconds=60)
        self.assertEqual(state.current_plan["expires_at"], 1700000300)


class MattersAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    async def asyncSetUp(self):
        await engine.EngineTestCase.asyncSetUp(self)
        raw = self.store.raw_world()
        for action in raw["actions"]:
            if action["id"] == "read":
                action["duration_min"] = 60
        self.store.save_world(raw)
        self.engine.reload_config()
    ctx = engine.EngineTestCase.ctx
    add_group = engine.EngineTestCase.add_group

    async def state(self):
        return await self.engine.load_state(SESSION, cold_start=False)

    async def test_one_completion_never_removes_other_projects_or_promises(self):
        state = await self.state()
        a = self.engine.add_life_matter(state, "给42看照片", who="42")
        b = self.engine.add_life_matter(state, "和42读书", who="42")
        c = self.engine.add_life_matter(state, "修好风车")
        self.assertEqual(self.engine.mark_own_topic_done(state, user_id="42"), 0)
        self.assertEqual(self.engine.mark_own_topic_done(state, user_id="42", matter_id=a["id"],
                                                       evidence="实际发送结果", confirmed=True), 1)
        self.assertEqual({m["id"] for m in state.own_topics}, {b["id"], c["id"]})
        self.assertEqual(matters.find(state, a["id"])["status"], "completed")

    async def test_project_lasts_seven_days_and_resumes_after_reload(self):
        self.llm.replies = [json.dumps({"motivation": "想把一部长篇慢慢读完", "steps": [
            {"text": f"读第{i+1}部分", "action": "read", "node": "study", "minutes": 5, "day_offset": day}
            for i, day in enumerate((0, 1, 2, 3, 4, 6))]}, ensure_ascii=False)]
        async with self.engine.session_state(SESSION) as state:
            state.state = "idle"
            state.energy = .9
            project = self.engine.add_life_matter(state, "用一周时间读完长篇小说")
            project_id = project["id"]
        start = self.clock.now()
        for index, day in enumerate((0, 1, 2, 3, 4, 6)):
            self.clock._now = start + day * 86400 + index * 60
            await self.engine.advance_life_matters(SESSION)
            state = await self.state()
            self.assertEqual(state.current_action["matter_id"], project_id)
            # Drive actual completion, rather than a model reporting progress.
            for _ in range(5):
                async with self.engine.session_state(SESSION) as state:
                    await self.engine._tick_continuous(state, self.engine.node("study"),
                                                       __import__("core.engine", fromlist=["TickOutcome"]).TickOutcome(SESSION))
            self.engine.reload_config()
            state = await self.state()
            self.assertEqual(sum(s["status"] == "done" for s in matters.find(state, project_id)["steps"]), index + 1)
        state = await self.state()
        item = matters.find(state, project_id)
        self.assertTrue(item["needs_review"])
        self.clock.advance(3600)
        self.llm.replies = ['{"outcome":"completed","reason":"六部分均已实际阅读"}']
        await self.engine.advance_life_matters(SESSION)
        state = await self.state()
        item = matters.find(state, project_id)
        self.assertEqual(item["status"], "completed")
        self.assertEqual(item["progress"], 100)
        self.assertEqual(len(self.llm.calls), 2)
        self.assertIn(project_id, state.life_artifacts)

    async def test_pause_retains_partial_work_and_resume_does_not_restart_the_step(self):
        async with self.engine.session_state(SESSION) as state:
            state.state = "idle"
            item = self.engine.add_life_matter(state, "读一篇长文章")
            item["steps"] = [{"id": "s1", "text": "安静读十分钟", "action": "read", "node": "study", "minutes": 10, "status": "pending"}]
        await self.engine.advance_life_matters(SESSION)
        async with self.engine.session_state(SESSION) as state:
            state.current_action["elapsed_ticks"] = 4
            self.engine.manage_life_matter(state, item["id"], "pause")
            self.assertIsNone(state.current_action)
            self.assertEqual(matters.find(state, item["id"])["steps"][0]["worked_minutes"], 4)
            self.engine.manage_life_matter(state, item["id"], "resume")
        await self.engine.advance_life_matters(SESSION)
        self.assertEqual((await self.state()).current_action["duration_ticks"], 6)

    async def test_resume_recovers_fully_accounted_step_without_replaying_activity(self):
        async with self.engine.session_state(SESSION) as state:
            state.state = "idle"
            item = self.engine.add_life_matter(state, "核对两封信的日期")
            item.update(status="working",steps=[{"id":"dates", "text":"比较邮戳与手写日期",
                "action":"read", "node":"study", "minutes":5, "status":"pending"}])
            state.current_action = {"type":"read", "matter_id":item["id"], "matter_step_id":"dates",
                                    "elapsed_ticks":5, "duration_ticks":30}
            self.engine.manage_life_matter(state, item["id"], "pause")
            step = item["steps"][0]
            self.assertEqual(step["worked_minutes"],5)
            step["bookmark"].update(note="黎明不是完整日期，尚不能确认同一天",open_question="邮戳是哪一天")
            self.engine.manage_life_matter(state, item["id"], "resume")
        calls_before=len(self.llm.calls)
        await self.engine.advance_life_matters(SESSION)
        state = await self.state()
        item = matters.find(state,item["id"])
        self.assertEqual(item["steps"][0]["status"],"done")
        self.assertTrue(item["needs_review"])
        self.assertNotEqual(item["status"],"completed")
        self.assertIn("尚不能确认同一天",matters.pending_review_bookmarks(item)[0]["note"])
        self.assertIsNone(state.current_action)
        self.assertIsNone(state.current_plan)
        self.assertEqual(len(self.llm.calls),calls_before)
        self.assertEqual(self.messenger.sent,[])
        self.assertEqual(item["steps"][0]["result"]["minutes"],5)
        self.assertEqual(item["steps"][0]["result"]["attempt_minutes"],0)
        self.assertEqual(state.life_interests["read"]["minutes"],5)
        events_before=copy.deepcopy(item["history"])
        self.assertFalse(self.engine._complete_accounted_matter_step(state,item,item["steps"][0]))
        self.assertEqual(item["history"],events_before)

    async def test_reload_recovers_only_saved_online_work_and_keeps_promise_unconfirmed(self):
        async with self.engine.session_state(SESSION) as state:
            state.state = "idle"
            item=self.engine.add_life_matter(state,"给42推荐一本书",who="42",kind="promise")
            item.update(status="working",steps=[{"id":"read", "text":"读推荐书的开头",
                "action":"read", "node":"study", "minutes":5, "status":"pending"}])
            state.current_action={"type":"read", "matter_id":item["id"], "matter_step_id":"read",
                                  "elapsed_ticks":5, "duration_ticks":30}
            state.life_updated_at=self.clock.now()
        self.clock.advance(3600)
        self.engine.reload_config()
        async with self.engine.session_state(SESSION) as state:
            self.engine.reconcile_life_time(state)
            step=matters.find(state,item["id"])["steps"][0]
            self.assertEqual(step["worked_minutes"],5)
            evidence_at=step["bookmark"]["at"]
        await self.engine.advance_life_matters(SESSION)
        item=matters.find(await self.state(),item["id"])
        self.assertEqual(item["steps"][0]["status"],"done")
        self.assertEqual(item["steps"][0]["result"]["at"],evidence_at)
        self.assertEqual(item["blocker"],"evidence")
        self.assertEqual(item["status"],"blocked")
        self.assertEqual(self.messenger.sent,[])

    async def test_recovery_does_not_complete_external_or_old_goal_work(self):
        state=await self.state()
        for action,old_goal in [("search_web",False),("say",False),("read",True)]:
            with self.subTest(action=action,old_goal=old_goal):
                item=self.engine.add_life_matter(state,"当前目标"+action)
                step={"id":"old", "text":"以前做的一步", "action":action, "node":"study",
                      "minutes":5, "worked_minutes":5, "status":"pending",
                      "goal":"旧目标" if old_goal else item["text"]}
                item["steps"]=[step]
                self.assertFalse(self.engine._complete_accounted_matter_step(state,item,step,evidence_at=self.clock.now()))
                self.assertEqual(step["status"],"pending")
                self.assertNotEqual(item["status"],"completed")

    async def test_slow_stale_plan_does_not_overwrite_a_manual_pause(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(*args):
            entered.set()
            await release.wait()
            return json.dumps({"steps": [{"text": "读书", "action": "read", "node": "study", "minutes": 5}]})
        self.engine._ask_judge = slow
        async with self.engine.session_state(SESSION) as state:
            state.state = "idle"
            item = self.engine.add_life_matter(state, "继续阅读")
        task = asyncio.create_task(self.engine.advance_life_matters(SESSION))
        await asyncio.wait_for(entered.wait(), 2)
        async with self.engine.session_state(SESSION) as state:
            self.engine.manage_life_matter(state, item["id"], "pause")
        release.set()
        await task
        state = await self.state()
        self.assertEqual(matters.find(state, item["id"])["status"], "paused")
        self.assertIsNone(state.current_action)

    async def test_offline_time_does_not_complete_work_or_send_messages(self):
        state = await self.state()
        item = self.engine.add_life_matter(state, "修理一架纸飞机")
        state.current_action = {"type": "read", "matter_id": item["id"], "elapsed_ticks": 2, "duration_ticks": 10}
        state.life_updated_at = self.clock.now()
        self.clock.advance(6 * 3600)
        elapsed = self.engine.reconcile_life_time(state)
        self.assertEqual(elapsed, 6 * 3600)
        self.assertIsNone(state.current_action)
        self.assertEqual(item["progress"], 0)
        self.assertEqual(self.messenger.sent, [])

    async def test_failed_tool_and_boolean_never_confirm_a_promise(self):
        state = await self.state()
        item = self.engine.add_life_matter(state, "给42发照片", who="42", session=SESSION)
        self.assertFalse(self.engine.confirm_matter_from_turn(state, self.ctx(user_id="42"), item["id"], "图片发送失败",
                    [{"ok": False, "result": "图片发送失败"}]))
        self.assertEqual(item["status"], "active")
        self.assertFalse(self.engine.confirm_matter_from_turn(state, self.ctx(user_id="42"), item["id"], "图片发送失败",
                    [{"ok": True, "result": "图片发送失败"}]))
        other = self.engine.add_life_matter(state, "给42推荐一本书", who="42", session=SESSION)
        self.assertTrue(self.engine.confirm_matter_from_turn(state, self.ctx(user_id="42"), item["id"], "图片已发送",
                    [{"ok": True, "result": "图片已发送"}]))
        self.assertEqual(other["status"], "active")

    async def test_reflection_receives_actual_tools_before_confirming_one_specific_promise(self):
        async with self.engine.session_state(SESSION) as state:
            item = self.engine.add_life_matter(state, "给42发送图片", who="42", session=SESSION, kind="promise")
        self.engine._ask_judge = AsyncMock(return_value=json.dumps({"own_topic_done":item["id"], "own_topic_evidence":"图片已发送给42"}))
        await self.engine.reflect_turn(SESSION, user_id="42", user_text="给我看看", reply_text="给你",
            tool_trace=[{"name":"send_image", "args":{"target":"42"}, "ok":True, "result":"图片已发送给42"}])
        prompt = self.engine._ask_judge.call_args.args[-1]
        self.assertIn("send_image", prompt)
        self.assertIn(item["id"], prompt)
        self.assertEqual(matters.find(await self.state(), item["id"])["status"], "completed")

    async def test_budget_is_durable_unknown_is_not_zero_and_chat_has_its_own_path(self):
        self.engine.world.limits.background_daily_calls = 1
        self.llm.replies = ["后台结果", "对话结果"]
        with model_context("life_matter", "推进项目", SESSION):
            first = await self.engine._background_generate(self.llm, session_id=SESSION, prompt="x", system_prompt="")
            second = await self.engine._background_generate(self.llm, session_id=SESSION, prompt="x", system_prompt="")
        self.assertTrue(first.ok)
        self.assertFalse(second.ok)
        budget = self.db.raw.kv_get("background_budget:" + self.engine._today_key(self.clock.now()))
        self.assertEqual(budget["tokens"], 0)
        self.assertEqual(budget["unknown"], 1)
        self.assertGreater(budget["reserved"], 0)
        with model_context("reply", "被动聊天", SESSION):
            self.assertTrue((await self.engine._background_generate(self.llm, session_id=SESSION, prompt="x", system_prompt="")).ok)

    async def test_chat_reflection_and_manual_creation_do_not_starve_autonomous_life_budget(self):
        self.engine.world.limits.background_daily_calls = 1
        self.llm.replies = ["自主计划", "聊后复盘", "管理员生成", "不应启动"]
        with model_context("plan", "自主决定", SESSION):
            first = await self.engine._background_generate(
                self.llm, session_id=SESSION, prompt="plan", system_prompt=""
            )
        self.assertTrue(first.ok)
        for purpose, prompt in (("reflection", "reflect"), ("creation", "create")):
            with self.subTest(purpose=purpose), model_context(purpose, "人工触发", SESSION):
                reply = await self.engine._background_generate(
                    self.llm, session_id=SESSION, prompt=prompt, system_prompt=""
                )
                self.assertTrue(reply.ok)
        with model_context("plan", "再次自主决定", SESSION):
            blocked = await self.engine._background_generate(
                self.llm, session_id=SESSION, prompt="blocked", system_prompt=""
            )
        self.assertFalse(blocked.ok)
        budget = self.db.raw.kv_get("background_budget:" + self.engine._today_key(self.clock.now()))
        self.assertEqual(budget["calls"], 1)

    async def test_self_start_after_closed_history_and_switch_off_during_inference(self):
        async with self.engine.session_state(SESSION) as state:
            state.world_time = 600
            state.energy = .9
            state.state = "idle"
            old = self.engine.add_life_matter(state, "读旧书")
            self.engine.manage_life_matter(state, old["id"], "complete", reason="读过了")
        self.engine._ask_judge = AsyncMock(return_value='{"text":"每天读一点新书","motivation":"想看看新的故事"}')
        await self.engine.seed_life_matter(SESSION)
        self.assertEqual(len((await self.state()).life_matters), 2)
        async with self.engine.session_state(SESSION) as state:
            for item in state.life_matters:
                item["status"] = "completed"
        self.clock.advance(86400)
        async def disabled(*args):
            self.engine.world.limits.life_matter_self_start = False
            return '{"text":"不应新增"}'
        self.engine._ask_judge = disabled
        await self.engine.seed_life_matter(SESSION)
        self.assertEqual(len((await self.state()).life_matters), 2)

    async def test_edited_goal_replans_and_promises_wait_for_actual_result(self):
        state = await self.state()
        item = self.engine.add_life_matter(state, "给42送一本书", who="42", kind="promise")
        item["steps"] = [{"id":"s1", "text":"读完介绍", "status":"pending", "minutes":5}]
        self.assertTrue(matters.complete_step(state, item["id"], "s1", now=self.clock.now(), receipt={"ok":True, "kind":"in_world_action"}))
        self.assertEqual(item["status"], "blocked")
        self.assertNotIn(item["id"], state.life_artifacts)
        self.engine.manage_life_matter(state, item["id"], "edit", text="改为自己读另一本书")
        self.assertTrue(item["needs_replan"])
        self.assertEqual(item["steps"][0]["status"], "done")

    async def test_interests_need_multiple_days_and_enter_later_context(self):
        state = await self.state()
        state.life_interests["read"] = {"label":"看书", "days":["2026-10-17","2026-10-18"], "sessions":2, "minutes":60}
        self.assertEqual(matters.stable_interests(state), [])
        state.life_interests["read"]["days"].append("2026-10-19")
        state.life_interests["read"]["sessions"] = 3
        self.assertIn("从日子里积累的活动经验", matters.context_block(state, session_id=SESSION))

    async def test_context_eligibility_uses_message_identity_and_keeps_full_original(self):
        text = "这是一段很长的原话，" * 40
        for key in ("first", "second"):
            await self.engine.note_presence(self.ctx(user_id="42", text=text, message_id=key))
        state = await self.state()
        rows = [row for row in state.recent_chat if row.get("user_id") == "42"]
        self.assertEqual(len(rows), 2)
        from unittest.mock import patch
        with patch.object(self.engine, "chat_context_for_reply", return_value=[]):
            self.engine.mark_context_sources(state, self.ctx(user_id="42"), [rows[0]])
        sources = [self.db.raw.source_record(row["source_id"]) for row in rows]
        self.assertEqual([row["eligible"] for row in sources], [True, False])
        self.assertEqual(sources[0]["text"], text)
        self.assertNotIn("pending_memory", state.to_payload())


    async def test_next_stage_keeps_previous_receipts_and_does_not_finish_the_goal(self):
        async with self.engine.session_state(SESSION) as state:
            state.state = "idle"
            item = self.engine.add_life_matter(state, "持续阅读一套书")
            item.update(needs_review=True, progress=99,
                        steps=[{"id":"old", "text":"读完第一本", "status":"done", "minutes":20,
                                "result":{"ok":True,"result":"实际阅读20分钟"}}])
        self.engine._ask_judge = AsyncMock(side_effect=[
            '{"outcome":"continue","reason":"接下来读第二本"}',
            '{"steps":[{"text":"读第二本的开头","action":"read","node":"study","minutes":5,"day_offset":1}]}'])
        await self.engine.advance_life_matters(SESSION)
        state = await self.state()
        self.assertTrue(matters.find(state, item["id"])["needs_replan"])
        await self.engine.advance_life_matters(SESSION)
        current = matters.find(await self.state(), item["id"])
        self.assertEqual(current["steps"][0]["result"]["result"], "实际阅读20分钟")
        self.assertEqual(current["steps"][1]["status"], "pending")
        self.assertEqual(current["status"], "waiting")

    async def test_locked_slow_life_does_not_block_another_character_clock(self):
        other = "test:GroupMessage:200"
        self.store.add_session(other)
        self.engine.reload_config()
        await self.engine.load_state(other)
        lock = self.engine.lock(self.engine.state_key(SESSION))
        await lock.acquire()
        try:
            before = (await self.engine.load_state(other)).world_time
            await asyncio.wait_for(self.engine.tick(), 3)
            after = (await self.engine.load_state(other)).world_time
            self.assertGreater(after, before)
        finally:
            lock.release()



    async def test_native_self_reply_keeps_its_actual_conversation_in_shared_life(self):
        other = "test:FriendMessage:42"
        self.add_group(sessions=[SESSION, other])
        await self.engine.record_self_reply(other, "这句只在私聊说过", persona_id="p")
        state = await self.state()
        entry = state.recent_chat[-1]
        self.assertEqual(entry["origin"], other)
        source = self.db.raw.source_record(entry["source_id"])
        self.assertEqual(source["session"], other)
        self.assertEqual(source["kind"], "self_statement")
        self.assertTrue(all(row["source"] == "preset" for row in self.db.raw.query_memories()))
        self.assertNotIn("pending_memory", state.to_payload())


    async def test_private_promise_wording_does_not_leak_into_shared_activity_or_plan(self):
        other = "test:FriendMessage:42"
        self.add_group(sessions=[SESSION, other])
        async with self.engine.session_state(SESSION) as state:
            state.state, state.energy = "idle", .9
            item = self.engine.add_life_matter(state, "给42准备私下约定的内容", who="42", session=other, kind="promise")
            item["steps"] = [{"id":"private_step", "text":"私下约定的敏感下一步", "action":"read", "node":"study", "minutes":5, "status":"pending"}]
            await self.engine._start_matter_step(state, item)
            shared = json.dumps([state.current_action, state.current_plan, state.last_plan], ensure_ascii=False)
            self.assertNotIn("私下约定", shared)
            self.assertNotIn(item["text"], self.engine._own_topic_block(state, self.ctx(session_id=SESSION)))
            self.assertIn(item["text"], self.engine._own_topic_block(state, self.ctx(session_id=other)))


class EvidenceAndSemanticTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "state.db")
        self.addCleanup(self.db.close)
        self.world, _ = parse_world(default_world())
        self.world.memory_scope_mode = "group"
        self.world.memory.semantic_source = "custom"
        self.memory = MemoryEngine(self.db, self.world)

    async def test_source_snapshot_and_idempotency_survive_manual_revision(self):
        source = source_snapshot({"user_id": "42", "text": "周末看星星的约定取消了", "at": 1000}, "private")
        self.db.observe_source(source, eligible=True)
        args = dict(session_id="private", persona_id="p", node_id="study", content="周末约定取消了",
                    evidence=[{**source, "eligible": True}], idempotency_key="batch1")
        mid = self.memory.remember(**args)
        self.db.revise_memory(mid, {"content": "周末的约定已经取消，不是完成了"})
        self.assertEqual(self.memory.remember(**args), mid)
        self.assertEqual(len(self.db.query_memories()), 1)
        result = self.db.memory_sources(mid)
        self.assertEqual(result["sources"][0]["text"], "周末看星星的约定取消了")
        self.assertEqual(len(result["revisions"]), 1)

    async def test_semantic_recall_handles_paraphrase_and_filters_scope_before_embedding(self):
        self.world.memory.semantic_enabled = True
        self.world.memory.semantic_endpoint = "https://example.invalid/v1"
        self.world.memory.semantic_model = "test-embedding"
        mid = self.memory.remember(session_id="group", persona_id="p", node_id="study", content="周末看星星的约定取消了")
        self.memory.remember(session_id="private", persona_id="p", node_id="study", content="私聊的秘密")
        calls = []
        async def embedding(texts):
            calls.extend(texts)
            return [[1., 0.] for _ in texts]
        self.memory.semantic.embed = embedding
        await self.memory.semantic.prepare("group", "p", "上次你没来")
        rows = self.memory.recall(session_id="group", persona_id="p", keyword="上次你没来", strict_keyword=True)
        self.assertEqual([row.id for row in rows], [mid])
        self.assertNotIn("私聊的秘密", calls)
        # Revision mismatch forces rebuilding; provider failure leaves the fact intact.
        self.db.revise_memory(mid, {"content": "我们改约周日看星星"})
        self.assertEqual(self.memory.semantic.cached("group", "p", "上次你没来"), {})
        self.memory.semantic.embed = AsyncMock(side_effect=RuntimeError("offline"))
        await self.memory.semantic.prepare("group", "p", "周日")
        self.assertIn("关键词", self.memory.semantic.status)
        self.assertEqual(len(self.db.query_memories(session_id="group")), 1)

    async def test_semantic_large_library_only_reads_scoped_vectors_in_small_batches(self):
        from core.semantic_memory import revision
        self.world.memory.semantic_enabled = True
        self.world.memory.semantic_endpoint = "https://example.invalid/v1"
        self.world.memory.semantic_model = "test-embedding"
        model = revision(self.world.memory.semantic_endpoint+"|"+self.world.memory.semantic_model)
        scoped = []
        with self.db.atomic():
            for i in range(104):
                content = f"synthetic life record {i}"
                mid = self.db.add_memory(session_id="group" if i < 40 else "private", persona_id="p",
                    scope="group", node_id="study", content=content)
                if i < 40:
                    scoped.append(mid)
                self.db._execute("INSERT INTO memory_vector(memory_id,model_key,revision,vector) VALUES(?,?,?,?)",
                    (mid, model, revision(content), json.dumps([1., 0.] if i < 40 else [999.]*4096)))
        queries = []
        original = self.db._query

        def observed(sql, params=()):
            rows = original(sql, params)
            if "FROM memory_vector" in sql:
                self.assertIn("memory_id IN", sql)
                self.assertTrue({row["memory_id"] for row in rows}.issubset(scoped))
                if ",vector" in sql:
                    queries.append(len(rows))
                    self.assertLessEqual(len(rows), 16)
            return rows

        self.memory.semantic.embed = AsyncMock(return_value=[[1., 0.]])
        with patch.object(self.db, "_query", side_effect=observed):
            await self.memory.semantic.prepare("group", "p", "看过的风景")
        self.assertEqual(sum(queries), 40)
        self.assertEqual(set(self.memory.semantic.cached("group", "p", "看过的风景")), set(scoped))
        self.memory.semantic.embed.assert_awaited_once_with(["看过的风景"])
        self.assertEqual(self.memory.semantic.locks, {})

    async def test_automatic_conflict_update_keeps_original_and_revision(self):
        args = dict(session_id="group", persona_id="p", node_id="study")
        mid = self.memory.remember(**args, content="我喜欢下雨")
        updated = self.memory.remember(**args, content="我不喜欢下雨")
        self.assertEqual(updated, mid)
        revisions = self.db.memory_sources(mid)["revisions"]
        self.assertFalse(revisions[0]["manual"])
        self.assertEqual(revisions[0]["before"]["content"], "我喜欢下雨")

    async def test_embedding_requests_record_reported_usage_and_failure(self):
        self.world.memory.semantic_endpoint = "https://example.invalid/v1"
        self.world.memory.semantic_model = "embedding-test"
        response = {"data":[{"index":0,"embedding":[1.,0.]}], "usage":{"prompt_tokens":14,"total_tokens":14}}
        with patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(response).encode())):
            with model_context("embedding", "主动回忆约定", "group"):
                self.assertEqual(await self.memory.semantic.embed(["以前的约定"]), [[1.,0.]])
        row = self.db.model_calls()["items"][0]
        self.assertEqual(row["purpose"], "embedding")
        self.assertEqual(row["usage"]["total"], 14)
        self.assertEqual(row["usage"]["output"], 0)
        self.assertEqual(row["session_id"], "group")
        with patch("urllib.request.urlopen", side_effect=RuntimeError("credential should never be stored")):
            with self.assertRaises(RuntimeError):
                await self.memory.semantic.embed(["query"])
        row = self.db.model_calls()["items"][0]
        self.assertEqual(row["status"], "error")
        self.assertNotIn("credential", row["error"])


class CustomWorldRulesTests(unittest.TestCase):
    def test_removed_defaults_are_never_planned(self):
        from core.decider import Decider
        world, _ = parse_world(default_world())
        world.actions = [a for a in world.actions if a.id not in {"read", "stare"}]
        world.actions.append(ActionDef(id="paint", name="画画", category="continuous", llm_level="template", life_role="interest", scope="global"))
        decider = Decider(world, hour_provider=lambda: 12)
        state = WorldState("s", node_id="bedroom", energy=.7, boredom=.7)
        plan = decider.rule_plan(state)
        self.assertTrue(plan)
        self.assertTrue(all(s["action"] in world.action_map() for s in plan["steps"]))
