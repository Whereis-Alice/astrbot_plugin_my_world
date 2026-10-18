"""Registered Web API handlers must retain config and operational scope semantics."""

import os
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

from tests.test_web_routes import load_plugin_module
from core.profile_weaver_store import AuditActor


class ScopeApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.module = load_plugin_module()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        with patch.dict(os.environ, {"MY_WORLD_DATA_DIR": directory.name}):
            self.plugin = self.module.MyWorldPlugin(
                self.module.Context(), self.module.AstrBotConfig({"enabled": True, "web_enabled": True}),
            )
        self.addAsyncCleanup(self.plugin.db.close)
        self.plugin._guard = lambda payload=None: None
        self.plugin._vision_cache_stats = AsyncMock(return_value={})
        self.plugin.engine.weather_payload = AsyncMock(return_value={})
        self.session = "test:GroupMessage:100"
        self.plugin.store.add_session(self.session)
        self.plugin.engine.reload_config()
        self.routes = {row[0].split("/astrbot_plugin_my_world/", 1)[1]: row[1]
                       for row in self.plugin.context.registered_web_apis}

    async def call(self, endpoint, *, method="GET", **data):
        request = types.SimpleNamespace(method=method, query=data, json=AsyncMock(return_value=data))
        # 处理器现在住在 web.api 里，patch 要落在它真正读的那个模块上
        with patch.object(self.module.web.api, "request", request):
            return await self.routes[endpoint]()

    async def test_scope_roundtrip_and_restoration(self):
        world = self.plugin.store.raw_world()
        world["name"] = "Local"
        result = await self.call("config/scope", method="POST", config_scope=self.session,
                                 world=world, schedules={"schedules": []}, blocks=["settings"])
        self.assertEqual(result["status_code"], 200)
        local = (await self.call("config", config_scope=self.session))["data"]
        self.assertEqual(local["world"]["name"], "Local")
        self.assertEqual(local["scope_blocks"], ["settings"])
        self.assertNotEqual(local["global_config"]["world"]["name"], "Local")
        global_data = (await self.call("config"))["data"]
        self.assertNotEqual(global_data["world"]["name"], "Local")
        self.assertEqual(self.plugin.engine.config_scope_id, "")

    async def test_global_pause_does_not_change_schedule_or_event_settings(self):
        before = self.plugin.engine.world.events.model_dump(mode="json")
        result = await self.call("config/proactive", method="POST", config_scope=self.session, enabled=False)
        self.assertEqual(result["status_code"], 200)
        after = self.plugin.store.raw_world()
        self.assertTrue(after["proactive_chat_paused"])
        self.assertEqual(self.plugin.engine.world.events.model_dump(mode="json"), before)
        self.assertFalse(self.plugin.engine.proactive_chat_allowed(self.session))
        result = await self.call("config/proactive", method="POST", enabled="false")
        self.assertEqual(result["status_code"], 400)
        stale = self.plugin.store.raw_world()
        stale["proactive_chat_paused"] = False
        result = await self.call("config/world", method="POST", world=stale)
        self.assertEqual(result["status_code"], 200)
        self.assertTrue(self.plugin.engine.world.proactive_chat_paused)

    async def test_local_global_mutations_rejected_and_other_scope_parameters_preserved(self):
        before = self.plugin.store.raw_world()
        result = await self.call("restore-default", method="POST", config_scope=self.session, scope="all")
        self.assertEqual(result["status_code"], 400)
        self.assertEqual(self.plugin.store.raw_world(), before)
        result = await self.call("config", scope="scene")
        self.assertEqual(result["status_code"], 200)
        result = await self.call("config", config_scope="missing")
        self.assertEqual(result["status_code"], 400)

    async def test_voice_samples_in_local_scope_do_not_modify_global_persona(self):
        world = self.plugin.store.raw_world()
        before = world.get("persona", {})
        self.plugin.store.save_scope(self.session, world, {"schedules": []}, ["settings"])
        self.plugin.engine.reload_config()
        result = await self.call("voice-samples/save", method="POST", config_scope=self.session,
                                 samples=[{"text": "Local sample", "scene": "daily"}])
        self.assertEqual(result["status_code"], 200)
        self.assertEqual(self.plugin.store.raw_world().get("persona", {}), before)
        with self.plugin.engine.config_scope(self.session):
            self.assertEqual(self.plugin.engine.world.persona.samples[0]["text"], "Local sample")

    async def test_map_inspector_uses_draft_without_saving(self):
        before = self.plugin.store.raw_world()
        draft = {"zones": [{"id": "home"}], "zone_edges": [],
                 "nodes": [{"id": name, "zone_id": "home"} for name in ["a", "b", "c"]],
                 "edges": [{"from": "a", "to": "b", "ticks": 2, "bidirectional": False}]}
        result = await self.call("map/inspect", method="POST", map=draft, start="a", target="b")
        self.assertEqual(result["status_code"], 200)
        self.assertEqual(result["data"]["path"], ["a", "b"])
        self.assertEqual(result["data"]["no_return"], ["b"])
        self.assertEqual(result["data"]["unreachable"], ["c"])
        self.assertEqual(self.plugin.store.raw_world(), before)
        invalid = await self.call("map/inspect", method="POST", map={"nodes": []})
        self.assertEqual(invalid["status_code"], 400)

    async def test_native_memory_query_stays_in_current_session_and_works_without_auto_recall(self):
        self.plugin.engine.world.memory.automatic_recall = "manual"
        for session, content in [(self.session, "在庭院种下了薄荷"), ("other:GroupMessage:99", "在远方的秘密庭院种了薄荷")]:
            self.plugin.engine.memory.remember(session_id=session, persona_id="", node_id="bedroom", content=content)
        event = types.SimpleNamespace(unified_msg_origin=self.session, get_extra=lambda *args: "")
        result = await self.plugin.tool_memory_lookup(event, topic="薄荷")
        self.assertIn("种下了薄荷", result)
        self.assertNotIn("秘密", result)
        self.assertIn("没有找到", await self.plugin.tool_memory_lookup(event, topic="毕业考试"))

    async def test_contact_note_merges_from_profile_weaver_facts(self):
        """通讯录备注：本插件这边没写过时，借「心迹画像」搬来的那条（只补空）。"""

        store = self.plugin.engine.profiles
        store.touch(self.session, "42", "小明")
        store.note_fact(
            self.session,
            "42",
            text="上个月搬了新家",
            kind="note",
            evidence="导入自心迹画像",
            confidence=0.9,
        )
        result = await self.call(
            "profile/person", method="POST", session=self.session, user_id="42"
        )
        person = (result.get("data") or {}).get("person") or {}
        self.assertEqual(person.get("note"), "上个月搬了新家")
        self.assertEqual(person.get("note_from"), "心迹画像")

        # 这边自己写过备注之后，就不再借它的（也不该丢掉自己的）
        store.set_note(self.session, "42", "我自己的备注")
        result = await self.call(
            "profile/person", method="POST", session=self.session, user_id="42"
        )
        person = (result.get("data") or {}).get("person") or {}
        self.assertEqual(person.get("note"), "我自己的备注")
        self.assertNotIn("note_from", person)

    async def test_detailed_profile_merges_contact_name_and_note(self):
        service = self.plugin.profile_weaver_service
        result = service.store.upsert_field(
            user_id="42",
            subject_name="阿岚",
            field_name="昵称",
            value="阿岚",
            session_id=self.session,
            actor=AuditActor("webui", "test", "测试"),
            source_kind="manual_edit",
            evidence="叫我阿岚",
            allow_custom_field=True,
        )
        self.assertTrue(result.ok)
        service.store.upsert_field(
            user_id="42",
            subject_name="阿岚",
            field_name="备注",
            value="最近在准备考试",
            session_id=None,
            actor=AuditActor("webui", "test", "测试"),
            source_kind="manual_edit",
            evidence="最近在准备考试",
            allow_custom_field=True,
        )
        self.plugin.engine.profiles.touch(self.session, "42", "阿岚")
        result = await self.call(
            "profile/person", method="POST", session=self.session, user_id="42"
        )
        person = (result.get("data") or {}).get("person") or {}
        self.assertEqual(person.get("call_me"), "阿岚")
        self.assertEqual(person.get("note"), "最近在准备考试")
        self.assertEqual(person.get("note_from"), "详细画像")

    async def test_profile_library_key_edit_and_delete_roundtrip(self):
        service = self.plugin.profile_weaver_service
        service.store.upsert_field(
            user_id="42",
            subject_name="阿岚",
            field_name="爱好",
            value="摄影",
            session_id=self.session,
            actor=AuditActor("webui", "test", "测试"),
            source_kind="manual_edit",
            evidence="我喜欢摄影",
            allow_custom_field=True,
        )
        key = service.profile_key(self.session, "42")
        library = await self.call(
            "profile/weaver/library",
            session=self.session,
            q="阿岚",
        )
        self.assertEqual(library["status_code"], 200)
        self.assertEqual(library["data"]["items"][0]["key"], key)

        contacts = await self.call("profile/contacts", session=self.session)
        self.assertEqual(contacts["status_code"], 200)
        self.assertEqual(contacts["data"]["people"][0]["user_id"], "42")

        detail = await self.call("profile/contact", session=self.session, user_id="42")
        self.assertEqual(detail["status_code"], 200)
        self.assertEqual(detail["data"]["identity"]["display_name"], "阿岚")
        records = await self.call(
            "profile/contact/records", session=self.session, user_id="42"
        )
        self.assertEqual(records["status_code"], 200)
        self.assertEqual(records["data"]["preferred_key"], key)

        edited = await self.call(
            "profile/weaver/field",
            method="POST",
            key=key,
            action="upsert",
            field="爱好",
            value="摄影、爬山",
        )
        self.assertEqual(edited["status_code"], 200, edited)
        values = {
            item["name"]: item["value"]
            for item in edited["data"]["profile"]["fields"]
        }
        self.assertEqual(values["爱好"], "摄影、爬山")

        readonly_key = service.detail_by_key(key)
        self.assertTrue(readonly_key)
        removed = await self.call(
            "profile/weaver/delete", method="POST", keys=[key]
        )
        self.assertEqual(removed["status_code"], 200)
        self.assertFalse(service.detail_by_key(key))

    async def test_profile_webui_read_only_blocks_field_writes(self):
        service = self.plugin.profile_weaver_service
        service.store.upsert_field(
            user_id="42",
            subject_name="阿岚",
            field_name="爱好",
            value="摄影",
            session_id=None,
            actor=AuditActor("webui", "test", "测试"),
            source_kind="manual_edit",
            evidence="我喜欢摄影",
            allow_custom_field=True,
        )
        key = service.profile_key(self.session, "42")
        self.plugin.config["weaver_webui_allow_edit"] = False
        result = await self.call(
            "profile/weaver/field",
            method="POST",
            key=key,
            action="upsert",
            field="爱好",
            value="摄影、爬山",
        )
        self.assertEqual(result["status_code"], 400)
        self.assertIn("只读", result.get("message", ""))

    async def test_unified_identity_edit_writes_both_contact_layers(self):
        self.plugin.engine.profiles.touch(self.session, "42", "阿岚")
        result = await self.call(
            "profile/contact",
            method="POST",
            session=self.session,
            user_id="42",
            call_me="岚岚",
            call_him="小鲸鱼",
            note="最近睡得很晚",
            digest="常熬夜的学生",
        )
        self.assertEqual(result["status_code"], 200)
        data = result["data"]
        self.assertEqual(data["identity"]["call_me"], "岚岚")
        self.assertEqual(data["identity"]["call_him"], "小鲸鱼")
        self.assertEqual(data["identity"]["note"], "最近睡得很晚")
        weaver_fields = {
            item["name"]: item["value"] for item in data["weaver"]["fields"]
        }
        self.assertEqual(weaver_fields["昵称"], "岚岚")
        self.assertIn("最近睡得很晚", data["weaver"]["notes"])

    async def test_session_isolated_contact_never_reads_or_deletes_other_session(self):
        service = self.plugin.profile_weaver_service
        other_session = "test:GroupMessage:200"
        for session_id, field, value in (
            (self.session, "爱好", "摄影"),
            (other_session, "所在地", "另一个会话"),
        ):
            service.store.upsert_field(
                user_id="42",
                subject_name="阿岚",
                field_name=field,
                value=value,
                session_id=session_id,
                actor=AuditActor("webui", "test", "测试"),
                source_kind="manual_edit",
                evidence=value,
                allow_custom_field=True,
            )
        detail = await self.call(
            "profile/contact",
            session=self.session,
            user_id="42",
        )
        values = {
            item["name"]: item["value"]
            for item in detail["data"]["weaver"]["fields"]
        }
        self.assertEqual(values["爱好"], "摄影")
        self.assertNotIn("所在地", values)
        self.assertEqual(detail["data"]["identity"]["record_count"], 1)
        self.assertEqual(len(detail["data"]["weaver_records"]), 1)

        deleted = await self.call(
            "profile/forget",
            method="POST",
            session=self.session,
            user_id="42",
        )
        self.assertEqual(deleted["status_code"], 200)
        remaining = service.detail_for(other_session, "42")
        self.assertEqual(
            {item["name"]: item["value"] for item in remaining["fields"]}["所在地"],
            "另一个会话",
        )

    async def test_profile_chat_command_roundtrip(self):
        event = types.SimpleNamespace(
            unified_msg_origin=self.session,
            message_str="/mw profile set 爱好 摄影",
            get_sender_id=lambda: "42",
            get_sender_name=lambda: "阿岚",
            is_admin=lambda: False,
            is_private_chat=lambda: True,
            get_messages=lambda: [],
            plain_result=lambda text: text,
        )
        saved = await self.plugin._cmd_profile(event, ["set", "爱好", "摄影"])
        self.assertIn("已写入", saved)
        shown = await self.plugin._cmd_profile(event, ["show"])
        self.assertIn("爱好", shown)
        self.assertIn("摄影", shown)

    async def test_original_profile_commands_are_installed(self):
        event = types.SimpleNamespace(
            unified_msg_origin=self.session,
            message_str="/设置画像 爱好 摄影",
            get_message_str=lambda: "/设置画像 爱好 摄影",
            get_sender_id=lambda: "42",
            get_sender_name=lambda: "阿岚",
            is_admin=lambda: False,
            is_private_chat=lambda: True,
            get_messages=lambda: [],
            plain_result=lambda text: text,
        )
        setter = getattr(self.plugin, "cmd_inherited_set_profile")
        self.assertEqual(setter.__module__, self.module.__name__)
        replies = [item async for item in setter(event)]
        self.assertTrue(replies)
        self.assertIn("已写入", replies[0])

        event.message_str = "/我的画像"
        shower = getattr(self.plugin, "cmd_inherited_show_my_profile")
        replies = [item async for item in shower(event)]
        self.assertTrue(replies)
        self.assertIn("爱好", replies[0])
        self.assertIn("摄影", replies[0])

    def test_original_profile_commands_are_visible_to_astrbot_registry(self):
        import fake_astrbot

        # 别的测试会替换 astrbot.core.star.star_handler 桩模块，所以读这份稳定注册表。
        registry = fake_astrbot.STAR_HANDLERS
        module_path = self.module.__name__
        handlers = registry.get_handlers_by_event_type("adapter")
        commands: set[str] = set()
        for handler in handlers:
            if handler.handler_module_path != module_path:
                continue
            for event_filter in handler.event_filters:
                names = getattr(event_filter, "get_complete_command_names", None)
                if callable(names):
                    commands.update(str(name) for name in names())
        for command in (
            "我的画像",
            "设置画像",
            "删除画像",
            "锁定画像",
            "解锁画像",
            "画像历史",
            "清空画像",
            "画像帮助",
            "查询画像",
            "画像统计",
            "画像备份",
            "画像备份列表",
            "画像恢复备份",
            "合并画像",
        ):
            self.assertIn(command, commands)
