"""Recoverable journal migration, strict retrieval and independent profile learning."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from core.config_store import ConfigStore
from core.db import Database
from core.life_memory import LIFE_DOMAIN, RETIRED_STATE
from core.memory import MemoryEngine
from core.models import parse_world
from core.defaults import default_world
from core.extensions import ExtensionHost
from core.profile import ProfileStore
from core.state import WorldState
from tests import test_consolidate as consolidation
from tests import test_scope_api as api_fixture
from tests import test_engine as engine_fixture


class JournalMigrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "state.db"
        db = Database(self.path)
        world, _ = parse_world(default_world())
        profile = ProfileStore(db, world)
        profile.touch("s", "42", "小明")
        profile.note_fact("s", "42", text="喜欢猫", kind="喜好", evidence="我喜欢猫")
        db.set_vocabulary("s", "猫门", "我们对爱猫同伴的称呼", manual=True)
        self.profiles_before = [tuple(row) for row in db._query("SELECT * FROM user_fact")]
        self.vocabulary_before = db.vocabulary("s")
        self.life_id = db.add_memory(session_id="s", persona_id="p", scope="group", node_id="study", content="修好了风车")
        # Simulate a real pre-migration DB, including old pending work and a dream.
        db._execute("DELETE FROM kv WHERE key='migration:life_journal:1'")
        for kind, source, text in [("interaction", "llm", "聊天往事"), ("scene", "consolidate", "聊天生成的场景"),
                                   ("scene", "import", "来源不明的旧导入"), ("interaction", "dream", "做了个梦：飞上云端")]:
            db._execute("INSERT INTO node_memory(session_id,content,type,source,scope,created_at) VALUES(?,?,?,?,?,?)",
                        ("s", text, kind, source, "group", 100))
        self.old_state = {"node_id": "study", "chat_summary": "滚动摘要", "pending_memory": [{"text": "待处理"}],
                          "recent_chat": [{"text": "短期消息"}], "life_matters": [{"text": "修风车"}]}
        db.save_state("s", self.old_state, 100)
        db._execute("CREATE TABLE memory_batch(batch_key TEXT PRIMARY KEY, payload TEXT)")
        db._execute("INSERT INTO memory_batch VALUES('batch','pending chat')")
        db.close()

    def test_backup_is_complete_and_profiles_vocabulary_and_life_are_preserved(self):
        db = Database(self.path)
        self.addCleanup(db.close)
        rows = db.query_memories()
        self.assertEqual({row["content"] for row in rows}, {"修好了风车", "做了个梦：飞上云端"})
        self.assertEqual(next(row for row in rows if row["source"] == "dream")["type"], "dream")
        self.assertEqual(self.profiles_before, [tuple(row) for row in db._query("SELECT * FROM user_fact")])
        self.assertEqual(self.vocabulary_before, db.vocabulary("s"))
        payload = db.get_state("s")
        self.assertFalse(RETIRED_STATE.intersection(payload))
        self.assertEqual(payload["life_matters"], self.old_state["life_matters"])
        self.assertEqual(payload["recent_chat"], self.old_state["recent_chat"])
        backups = list(self.path.parent.glob("backups/before-life-journal-*.sqlite3"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as backup:
            self.assertEqual(backup.execute("SELECT COUNT(*) FROM node_memory").fetchone()[0], 5)
            self.assertEqual(json.loads(backup.execute("SELECT payload FROM world_state").fetchone()[0]), self.old_state)
            self.assertEqual(backup.execute("SELECT payload FROM memory_batch").fetchone()[0], "pending chat")
        db.close()
        db = Database(self.path)
        self.addCleanup(db.close)
        self.assertEqual(len(list(self.path.parent.glob("backups/*.sqlite3"))), 1)
        # New explicit life imports are not mistaken for pre-migration unknown imports.
        imported = db.add_memory(session_id="s", persona_id="p", scope="group", node_id="study", content="种了花", source="import")
        db.close()
        db = Database(self.path)
        self.addCleanup(db.close)
        self.assertIsNotNone(db.memory_by_id(imported))

    def test_backup_failure_leaves_every_record_untouched(self):
        with patch("tempfile.mkstemp", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                Database(self.path)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM node_memory").fetchone()[0], 5)
            self.assertEqual(json.loads(conn.execute("SELECT payload FROM world_state").fetchone()[0]), self.old_state)

    def test_transaction_failure_rolls_back_and_retry_keeps_both_backups(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("CREATE TRIGGER stop_migration BEFORE DELETE ON node_memory BEGIN SELECT RAISE(ABORT,'retry'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            Database(self.path)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM node_memory").fetchone()[0], 5)
            self.assertEqual(conn.execute("SELECT type FROM node_memory WHERE source='dream'").fetchone()[0], "interaction")
            self.assertFalse(conn.execute("SELECT 1 FROM kv WHERE key='migration:life_journal:1'").fetchone())
            conn.execute("DROP TRIGGER stop_migration")
        db = Database(self.path)
        self.addCleanup(db.close)
        self.assertEqual(len(db.query_memories()), 2)
        self.assertEqual(len(list(self.path.parent.glob("backups/*.sqlite3"))), 2)

    def test_queries_and_explicit_recall_ignore_bypassed_legacy_rows_before_limit(self):
        db = Database(self.path)
        self.addCleanup(db.close)
        world, _ = parse_world(default_world())
        memory = MemoryEngine(db, world)
        for i in range(5):
            db._execute("INSERT INTO node_memory(session_id,content,type,source,scope,created_at) VALUES(?,?,?,?,?,?)",
                        ("s", f"不该召回{i}", "interaction", "runtime", "group", 9999999999))
        self.assertEqual(db.query_memories(limit=1)[0]["id"], self.life_id)
        self.assertEqual(db.memory_stats()["total"], 2)
        for automatic in (True, False):
            self.assertFalse(memory.recall(session_id="s", persona_id="p", mode="global", keyword="不该召回", strict_keyword=True, automatic=automatic))
        with self.assertRaises(ValueError):
            db.update_memory(self.life_id, type="interaction")

    def test_dream_and_inner_are_labeled_when_recalled(self):
        db = Database(self.path)
        self.addCleanup(db.close)
        memory = MemoryEngine(db)
        result = memory.recall(session_id="s", persona_id="", memory_type="dream")
        self.assertIn("梦境，非现实经历", result[0].render())

    def test_matching_dream_cannot_replace_a_real_experience_or_another_scope(self):
        db = Database(self.path)
        self.addCleanup(db.close)
        memory = MemoryEngine(db)
        ids = [memory.remember(session_id="s", persona_id="p", node_id="study", content="修好了风车",
                               memory_type=kind, scope=scope)
               for kind, scope in [("dream", "group"), ("inner", "group"), ("scene", "persona")]]
        self.assertEqual(len(set([self.life_id, *ids])), 4)
        self.assertEqual(db.memory_by_id(self.life_id)["type"], "scene")


class JournalConfigTests(unittest.TestCase):
    def test_old_world_and_session_settings_cannot_restore_chat_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(directory)
            raw = default_world()
            raw["memory"] = {"automatic_recall": "life_only", "dialogue_summary": True}
            raw["context"] = {"chat_overflow": "compress", "chat_history_max": 50}
            store.world_path.write_text(json.dumps(raw), encoding="utf-8")
            store.sessions_path.write_text(json.dumps({"sessions": [{"session_id": "s", "config_overrides": {"settings": raw}}]}), encoding="utf-8")
            world = store.load_world()
            self.assertEqual(world.memory.automatic_recall, "auto")
            self.assertFalse(hasattr(world.memory, "dialogue_summary"))
            self.assertFalse(hasattr(world.context, "chat_overflow"))
            self.assertEqual(world.context.chat_history_max, 50)
            store.load_sessions()
            self.assertNotIn("dialogue_summary", store.raw_sessions()["sessions"][0]["config_overrides"]["settings"]["memory"])
            store.save_world(raw)
            self.assertNotIn("dialogue_summary", store.raw_world()["memory"])
            state = WorldState.from_payload({"chat_summary": "旧摘要", "pending_memory": [1]}, "s")
            self.assertFalse(RETIRED_STATE.intersection(state.to_payload()))


class JournalApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = api_fixture.ScopeApiTests.asyncSetUp
    call = api_fixture.ScopeApiTests.call

    async def test_import_requires_life_domain_and_rejects_whole_mixed_file(self):
        good = {"session_id": self.session, "content": "修好了飞机", "type": "scene", "source": "runtime"}
        response = await self.call("memories/import", method="POST", memories=[good])
        self.assertEqual(response["status_code"], 400)
        response = await self.call("memories/import", method="POST", memory_domain=LIFE_DOMAIN,
                                   memories=[good, {**good, "type": "interaction"}])
        self.assertEqual(response["status_code"], 400)
        self.assertEqual(self.plugin.db.raw.query_memories(), [])
        response = await self.call("memories/import", method="POST", memory_domain=LIFE_DOMAIN, memories=[good])
        self.assertEqual(response["data"]["imported"], 1)
        self.assertEqual((await self.call("memories/export"))["data"]["memory_domain"], LIFE_DOMAIN)

    async def test_import_preserves_dates_and_zero_weight_and_validates_before_writing(self):
        good = {"session_id": self.session, "content": "第一次做飞机模型失败了", "type": "scene", "source": "runtime",
                "created_at": 1700000000, "weight": 0, "tier": "gist", "context": "叶片未转动", "pinned": True}
        for invalid in ({"weight": "bad"}, {"type": []}, {"created_at": float("nan")}, {"participants": "42"}):
            response = await self.call("memories/import", method="POST", memory_domain=LIFE_DOMAIN,
                                       memories=[good, {**good, **invalid}])
            self.assertEqual(response["status_code"], 400)
            self.assertEqual(self.plugin.db.raw.query_memories(), [])
        response = await self.call("memories/import", method="POST", memory_domain=LIFE_DOMAIN, memories=[good])
        self.assertEqual(response["data"]["imported"], 1)
        row = self.plugin.db.raw.query_memories()[0]
        for key in ("created_at", "weight", "tier", "context", "pinned"):
            self.assertEqual(row[key], good[key], key)


class JournalStageTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = consolidation.ConsolidateTestCase.asyncSetUp

    def test_extensions_cannot_relabel_chat_as_a_scene(self):
        host = ExtensionHost(SimpleNamespace(engine=SimpleNamespace(memory=self.memory)))
        state = WorldState(session_id="s")
        for kind in ("interaction", "relation", "unknown"):
            self.assertFalse(host.remember(state, "过去聊过的事", kind=kind))
        self.assertEqual(self.db.query_memories(), [])
        self.assertTrue(host.remember(state, "梦见飞上天空", kind="dream"))
        self.assertEqual(self.db.query_memories()[0]["type"], "dream")

    async def test_life_stage_has_no_chat_or_plans_and_profiles_cannot_create_dreams(self):
        self.memory.remember(session_id="s", persona_id="", node_id="study", content="修好了风车")
        self.profiles.touch("s", "42", "小明")
        self.profiles.set_digest("s", "42", "习惯早起，喜欢画画")
        calls = []
        async def model(system, prompt):
            calls.append(prompt)
            if len(calls) == 1:
                self.assertIn("我喜欢猫", prompt)
                self.assertIn("习惯早起，喜欢画画", prompt)
                self.assertNotIn("修好了风车", prompt)
                return json.dumps({"facts": [{"user_id": "42", "text": "喜欢猫", "evidence": "我喜欢猫"}],
                                   "memories": [{"text": "聊天变成经历"}], "dream": "不该保存的聊天梦"})
            self.assertIn("修好了风车", prompt)
            self.assertNotIn("我喜欢猫", prompt)
            self.assertNotIn("习惯早起，喜欢画画", prompt)
            return json.dumps({"dream": "风车带着我飞上天空", "facts": [{"user_id": "42", "text": "编造"}]})
        result = await self.consolidator.run(session_id="s", llm=model, chat_records=[{"user_id": "42", "text": "我喜欢猫"}])
        self.assertEqual(len(calls), 2)
        self.assertEqual([row["text"] for row in self.profiles.facts("s", "42")], ["喜欢猫"])
        rows = self.db.query_memories()
        self.assertEqual({row["type"] for row in rows}, {"scene", "dream"})
        self.assertFalse(any("聊天" in row["content"] for row in rows))
        self.assertEqual(result.memories, 0)

    async def test_nap_ignores_merge_even_when_the_model_returns_it(self):
        ids = [self.memory.remember(session_id="s", persona_id="", node_id="study", content=text)
               for text in ("修好了风车", "读完了第一章")]
        async def model(system, prompt):
            return json.dumps({"merge": [{"keep_id": ids[0], "drop_ids": [ids[1]], "text": "全部做完了"}]})
        result = await self.consolidator.run(session_id="s", llm=model, mode="nap")
        self.assertTrue(result.ok)
        self.assertEqual(result.merged, 0)
        self.assertEqual([self.db.memory_by_id(mid)["content"] for mid in ids], ["修好了风车", "读完了第一章"])

    async def test_life_review_runs_with_profile_extraction_disabled(self):
        self.world.profile.enabled = False
        self.memory.remember(session_id="s", persona_id="", node_id="study", content="修好了风车")
        calls = []
        async def model(system, prompt):
            calls.append(prompt)
            return '{"dream":"风车的叶片像翅膀"}'
        result = await self.consolidator.run(session_id="s", llm=model, chat_records=[{"user_id": "42", "text": "我喜欢猫"}])
        self.assertEqual(len(calls), 1)
        self.assertNotIn("我喜欢猫", calls[0])
        self.assertTrue(result.dream)

    async def test_profile_extraction_runs_with_life_review_disabled(self):
        self.world.memory.sleep_review = False
        self.memory.remember(session_id="s", persona_id="", node_id="study", content="修好了风车")
        self.profiles.touch("s", "42", "小明")
        calls = []
        async def model(system, prompt):
            calls.append(prompt)
            return '{"facts":[{"user_id":"42","text":"喜欢猫","evidence":"我喜欢猫"}],"dream":"不能写入"}'
        result = await self.consolidator.run(session_id="s", llm=model, chat_records=[{"user_id": "42", "text": "我喜欢猫"}])
        self.assertEqual(len(calls), 1)
        self.assertNotIn("修好了风车", calls[0])
        self.assertEqual(result.facts, 1)
        self.assertFalse(result.dream)


class JournalRhythmTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = engine_fixture.EngineTestCase.asyncSetUp

    async def test_sleep_scheduler_still_reviews_life_when_profiles_are_disabled(self):
        self.engine.world.profile.enabled = False
        calls = []
        async def consolidate(state, **kwargs):
            calls.append(kwargs["mode"])
            return {"ok": True}
        self.engine.consolidate_now = consolidate
        state = await self.engine.load_state(engine_fixture.SESSION, cold_start=False)
        state.state = "sleeping"
        await self.engine.maybe_consolidate(state)
        self.clock.advance(21 * 60)
        await self.engine.maybe_consolidate(state)
        self.assertEqual(calls, ["full"])

    async def test_due_dream_keeps_its_subjective_label(self):
        self.engine.memory.remember(session_id=engine_fixture.SESSION, persona_id="", node_id="study",
                                    content="飞上了天空", memory_type="dream", next_review_at=1)
        state = await self.engine.load_state(engine_fixture.SESSION, cold_start=False)
        self.assertIn("【梦境，非现实经历】", self.engine.review_block(state))
