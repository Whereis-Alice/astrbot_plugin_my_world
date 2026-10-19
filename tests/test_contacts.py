"""Unified profile scope, tool reads, migration and quiet-life regressions."""

import json
import importlib.util
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

from tests import test_engine as engine_tests
from tests import test_profile_weaver_integration as integration
from tests import test_world_generator as generator_tests
from core.contact_knowledge import contact_knowledge
from core.profile_weaver_service import ProfileWeaverService


SESSION = engine_tests.SESSION


class _Event(integration._Event):
    unified_msg_origin = SESSION


class ContactKnowledgeTests(unittest.TestCase):
    def test_only_imported_field_copies_are_hidden_and_sources_survive(self):
        fields = {"fields": [{"name": "爱好", "value": "摄影"}], "notes": ["喜欢热茶"]}
        facts = [
            {"id": 1, "text": "爱好：画画", "evidence": "导入自心迹画像"},
            {"id": 2, "text": "约定：周六拍照", "evidence": "导入自心迹画像"},
            {"id": 3, "text": "今天开始学摄影", "evidence": "我今天报名了"},
            {"id": 4, "kind": "note", "text": "喜欢热茶", "evidence": "导入自心迹画像"},
        ]
        result = contact_knowledge(fields, facts)
        self.assertEqual([item["id"] for item in result["facts"]], [2, 3])
        self.assertEqual(len(facts), 4)
        self.assertEqual(result["mirrored_fact_count"], 2)


class ProfileToolLifecycleTests(unittest.TestCase):
    def test_lookup_registration_and_cleanup_with_legacy_plugin_present_or_absent(self):
        spec = importlib.util.spec_from_file_location(
            'contacts_tools_test', Path(__file__).resolve().parents[1] / 'adapters/profile_weaver_tools.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(module)
        for legacy, enabled, expected in (
            (True, True, {'mw_profile_lookup'}),
            (False, True, {'mw_profile_lookup', 'profileweaver_view_profile',
                           'profileweaver_remember_field', 'profileweaver_forget_field'}),
            (False, False, set()),
        ):
            registered, removed = [], []
            plugin = types.SimpleNamespace(
                profile_weaver_service=types.SimpleNamespace(llm_tools_enabled=enabled),
                context=types.SimpleNamespace(add_llm_tools=lambda *items: registered.extend(items),
                                              unregister_llm_tool=removed.append), logger=integration._Logger())
            module.register_profile_tools(plugin, legacy_active=legacy, toolset_type=None)
            self.assertEqual({tool.name for tool in registered}, expected)
            self.assertEqual(plugin.profile_weaver_service.prompt_enabled, not legacy)
            module.unregister_profile_tools(plugin)
            self.assertEqual(set(removed), expected)


class ContactsEngineTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = engine_tests.EngineTestCase.asyncSetUp
    get_state = engine_tests.EngineTestCase.get_state
    set_state = engine_tests.EngineTestCase.set_state

    async def asyncSetUp(self):
        await engine_tests.EngineTestCase.asyncSetUp(self)
        plugin = types.SimpleNamespace(config={}, store=self.store, engine=self.engine,
                                       context=types.SimpleNamespace(), logger=integration._Logger())
        self.service = ProfileWeaverService(plugin, str(self.data_dir / "external"))
        self.engine.profile_weaver_service = self.service
        self.engine.profiles.touch(SESSION, "42", "阿岚")

    def field(self, name, value, session=SESSION, user_id="42", subject="阿岚"):
        return self.service.store.upsert_field(
            user_id=user_id, subject_name=subject, session_id=session,
            field_name=name, value=value, actor=self.service.actor(_Event(), "user_command"),
            source_kind="manual_edit", evidence=f"我的{name}是{value}", allow_custom_field=True,
        )

    async def test_injection_combines_shared_fields_and_current_scope_and_deduplicates_imports(self):
        self.field("爱好", "本群摄影")
        self.field("职业", "学生", session=None)
        self.field("爱好", "全局画画", session=None)
        self.field("所在地", "别群的城市", session="other:GroupMessage:9")
        self.engine.profiles.note_fact(SESSION, "42", text="爱好：过时的内容", kind="profile",
                                       evidence="导入自心迹画像")
        self.engine.profiles.note_fact(SESSION, "42", text="约定：周六拍照", kind="约定",
                                       evidence="导入自心迹画像")
        ctx = engine_tests.MessageContext(session_id=SESSION, user_id="42", user_name="阿岚", text="你好")
        block = self.engine.profile_block(await self.get_state(), ctx)
        self.assertIn("本群摄影", block)
        self.assertIn("学生", block)
        self.assertNotIn("别群的城市", block)
        self.assertNotIn("全局画画", block)
        self.assertNotIn("过时的内容", block)
        self.assertIn("周六拍照", block)
        self.assertIn("mw_profile_lookup", block)

    async def test_lookup_reads_native_experiences_and_scoped_fields_without_mutation(self):
        self.field("爱好", "摄影")
        self.engine.profiles.note_fact(SESSION, "42", text="周五面试", kind="约定", evidence="我周五去面试")
        before = len(self.engine.profiles.list_people(SESSION))
        event = _Event()
        result = self.service.handle_llm_lookup(event)
        self.assertIn("摄影", result)
        self.assertIn("周五面试", result)
        self.assertNotIn("摄影", self.service.handle_llm_lookup(_Event(), topic="面试"))
        self.field("职业", "别群职业", session="other:GroupMessage:9", user_id="999")
        self.assertNotIn("别群职业", self.service.handle_llm_lookup(_Event(), user_id="999"))
        self.assertEqual(before, len(self.engine.profiles.list_people(SESSION)))
        self.assertFalse(getattr(event, "_profileweaver_llm_tool_used", False))

    async def test_merged_fields_and_notes_keep_their_original_edit_target(self):
        self.field("职业", "学生", session=None)
        self.field("备注", "共享备注", session=None)
        self.field("备注", "本群备注")
        detail = self.service.detail_for(SESSION, "42")
        field = next(item for item in detail['fields'] if item['name'] == '职业')
        self.assertTrue(field['shared'])
        self.assertEqual(field['record_key'], '42')
        notes = {entry['text']: entry for entry in detail['note_entries']}
        self.assertEqual(notes['共享备注']['record_key'], '42')
        self.assertFalse(notes['本群备注']['shared'])
        self.assertEqual(notes['本群备注']['index'], 1)

    async def test_ambiguous_names_require_an_id_and_do_not_expose_full_records(self):
        self.engine.profiles.touch(SESSION, "51", "小林")
        self.engine.profiles.touch(SESSION, "52", "小林")
        result = self.service.handle_llm_lookup(_Event(), query="小林")
        self.assertIn("51", result)
        self.assertIn("52", result)
        self.assertIn("多位", result)
        self.assertNotIn('"experiences"', result)

    async def test_cooldown_and_hourly_quota_do_not_block_sleep(self):
        self.clock.set_struct(datetime(2026, 10, 18, 1, 0))
        for gates in ({"cooldown_until": 1000}, {"autonomous_hour_marker": 0, "autonomous_count_hour": 999}):
            await self.set_state(**{ "state": "idle", "current_action": None, "current_plan": None,
                                    "energy": 0.05, "sleep_pressure": 1.0, "cooldown_until": 0, **gates})
            before = len(self.llm.calls)
            await self.engine.maybe_decide(SESSION, force=True)
            state = await self.get_state()
            self.assertTrue(state.current_plan or state.current_action or state.state in {"drowsy", "sleeping"})
            self.assertEqual(len(self.llm.calls), before)
            self.assertEqual(self.messenger.flat_messages, [])

    async def test_quiet_plan_keeps_silence_after_the_cooldown_ends(self):
        from core.planner import create_plan
        from core.engine.models import TickOutcome

        self.engine.decider.rule_plan = lambda state, **kwargs: create_plan(
            steps=[{"action": "read", "duration": 60}], world_time=state.world_time,
            valid_for=3600, reason="安静看书", source="rule")
        await self.set_state(cooldown_until=1000, current_plan=None, current_action=None, state="idle")
        await self.engine.maybe_decide(SESSION, force=True)
        state = await self.get_state()
        self.assertEqual(state.current_action["speech_kind"], "quiet")
        self.clock.advance(120)
        await self.set_state(cooldown_until=0)
        outcome = TickOutcome(session_id=SESSION)
        async with self.engine.session_state(SESSION) as live:
            await self.engine._tick_continuous(live, self.engine.node(live.node_id), outcome)
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.flat_messages, [])


class LegacySyncTests(unittest.TestCase):
    setUp = integration.ProfileWeaverIntegrationTests.setUp
    service = integration.ProfileWeaverIntegrationTests.service
    def test_deleted_field_stays_deleted_even_if_an_unrelated_legacy_field_changes(self):
        service = self.service({})
        service.store.delete_field(user_id="42", session_id=_Event.unified_msg_origin,
                                   field_selector="爱好", actor=service.actor(_Event(), "user_command"),
                                   source_kind="manual_edit", evidence="删除爱好")
        raw = json.loads(self.profiles_path.read_text("utf-8"))
        record = next(iter(raw.values()))
        record["fields"]["职业"] = "学生"
        self.profiles_path.write_text(json.dumps(raw, ensure_ascii=False), "utf-8")
        restarted = self.service({})
        fields = {item["name"]: item["value"] for item in restarted.detail_for(_Event.unified_msg_origin, "42")["fields"]}
        self.assertNotIn("爱好", fields)
        self.assertEqual(fields["职业"], "学生")


class ScheduleRecoveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = generator_tests.EngineGeneratorTests.asyncSetUp

    async def test_auto_plan_retries_after_failure_and_marks_only_success(self):
        raw = self.store.raw_schedules()
        raw["auto"] = {"enabled": True, "hour": 0, "per_day": 1}
        self.store.save_schedules(raw)
        self.engine.reload_config()
        today = self.engine._today_key(self.engine._now())
        self.engine.generate_schedules = AsyncMock(side_effect=[RuntimeError("offline"), {"schedules": [{
            "id": "today", "date": today, "time": "23:59", "once": True,
            "action_chain": [{"type": "think", "content": "看看书"}],
        }]}])
        with self.assertRaises(RuntimeError):
            await self.engine.maybe_auto_generate_schedules()
        self.assertNotEqual(await self.db.call("kv_get", "schedule.auto_day:global"), today)
        self.assertEqual(await self.engine.maybe_auto_generate_schedules(), "")
        self.assertEqual(self.engine.generate_schedules.call_count, 1)
        self.engine.clock.advance(901)
        self.assertIn("1 条", await self.engine.maybe_auto_generate_schedules())
        self.assertEqual(await self.db.call("kv_get", "schedule.auto_day:global"), today)
        self.assertEqual(await self.engine.maybe_auto_generate_schedules(), "")

    async def test_each_independent_schedule_scope_gets_its_own_auto_plan(self):
        self.store.add_session("test:GroupMessage:200")
        raw = self.store.raw_schedules()
        raw["auto"] = {"enabled": True, "hour": 0}
        self.store.save_schedules(raw)
        self.store.save_scope("test:GroupMessage:200", self.store.raw_world(), raw, ["schedules"])
        self.engine.reload_config()
        seen = []
        async def generated():
            seen.append(self.engine.config_scope_id)
            return ""
        self.engine.maybe_auto_generate_schedules = generated
        await self.engine.auto_generate_schedule_scopes()
        self.assertEqual(seen, ["", "test:GroupMessage:200"])

    async def test_auto_plan_failure_budget_is_bounded_and_survives_reload(self):
        raw = self.store.raw_schedules()
        raw["auto"] = {"enabled": True, "hour": 0}
        self.store.save_schedules(raw)
        self.engine.reload_config()
        self.engine.generate_schedules = AsyncMock(side_effect=RuntimeError("offline"))
        for _ in range(3):
            with self.assertRaises(RuntimeError):
                await self.engine.maybe_auto_generate_schedules()
            self.engine.clock.advance(3 * 3600)
            self.engine.reload_config()
        self.assertEqual(await self.engine.maybe_auto_generate_schedules(), "")
        self.assertEqual(self.engine.generate_schedules.call_count, 3)

    async def test_schedule_generation_includes_current_life_and_unfinished_topics(self):
        async with self.engine.session_state(generator_tests.SESSION) as state:
            state.energy = 0.12
            self.engine.add_life_matter(state, "想把没看完的书读完")
        self.creator.replies = ['{"schedules": []}']
        await self.engine.generate_schedules(1, "once")
        prompt = self.creator.calls[-1]["prompt"]
        self.assertIn("0.12", prompt)
        self.assertIn("想把没看完的书读完", prompt)
        self.assertIn("现在之后", prompt)
