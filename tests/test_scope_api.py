"""Registered Web API handlers must retain config and operational scope semantics."""

import os
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

from tests.test_web_routes import load_plugin_module


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
