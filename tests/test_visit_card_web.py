"""Card destinations require an explicit actual conversation and durable opt-in."""

import types
import unittest
from unittest.mock import Mock

from tests import test_visits_web as visits_web


class CardConfig(dict):
    def __init__(self, **values):
        super().__init__(values)
        self.save_config = Mock()


class VisitCardSettingsApiTests(unittest.IsolatedAsyncioTestCase):
    setUp = visits_web.VisitEditorApiTests.setUp
    call = visits_web.VisitEditorApiTests.call

    def configure(self, **values):
        self.plugin.config = CardConfig(**values)
        self.plugin.engine.sessions = types.SimpleNamespace(sessions=[
            types.SimpleNamespace(session_id="qq:GroupMessage:100", note="一起生活的群聊", enabled=True),
            types.SimpleNamespace(session_id="qq:FriendMessage:200", note="同一生活的私聊", enabled=True),
            types.SimpleNamespace(session_id="qq:GroupMessage:300", note="暂时停用的群聊", enabled=False),
        ])

    async def save(self, enabled=False, sessions=None):
        return await self.call("visits/record-card-settings", payload={"token": "editor-login", "enabled": enabled,
                                                                     "sessions": [] if sessions is None else sessions})

    async def test_defaults_are_off_and_offer_actual_conversations_separately(self):
        self.configure()
        result = await self.call("visits")
        card = result["data"]["record_card"]
        self.assertFalse(card["enabled"])
        self.assertEqual(card["sessions"], [])
        self.assertEqual([item["session_id"] for item in card["choices"]],
                         ["qq:GroupMessage:100", "qq:FriendMessage:200", "qq:GroupMessage:300"])
        self.assertFalse(card["choices"][-1]["enabled"])
        self.plugin.config.save_config.assert_not_called()

    async def test_saving_requires_no_current_life_and_never_expands_shared_destinations(self):
        self.configure()
        result = await self.save(True, ["qq:FriendMessage:200", "qq:FriendMessage:200"])
        self.assertEqual(result["status_code"], 200)
        self.assertEqual(result["data"]["sessions"], ["qq:FriendMessage:200"])
        self.assertEqual(self.plugin.config["visits_record_card_sessions"], ["qq:FriendMessage:200"])
        self.assertTrue(self.plugin.config["visits_record_card_enabled"])
        self.plugin.config.save_config.assert_called_once_with()
        self.service.set_enabled_scopes.assert_not_awaited()

    async def test_disabled_selection_can_be_retained_but_unknown_or_group_alias_cannot(self):
        self.configure()
        result = await self.save(False, ["qq:GroupMessage:300"])
        self.assertEqual(result["status_code"], 200)
        self.plugin.config.save_config.reset_mock()
        for values in (["shared-life"], ["qq:GroupMessage:999"], [" qq:GroupMessage:100"], [""], [None],
                       [123], [[]], {}, "qq:GroupMessage:100", ["qq:GroupMessage:100"] * 257):
            with self.subTest(values=values):
                result = await self.save(True, values)
                self.assertEqual(result["status_code"], 400)
        self.assertEqual(self.plugin.config["visits_record_card_sessions"], ["qq:GroupMessage:300"])
        self.plugin.config.save_config.assert_not_called()

    async def test_master_toggle_requires_a_real_boolean_and_list_is_required(self):
        self.configure()
        for enabled in (None, 0, 1, "true", [], {}):
            with self.subTest(enabled=enabled):
                result = await self.save(enabled, ["qq:GroupMessage:100"])
                self.assertEqual(result["status_code"], 400)
        for payload in ({}, {"enabled": True}, {"sessions": []}, {"enabled": True, "sessions": None}):
            result = await self.call("visits/record-card-settings", payload={"token": "editor-login", **payload})
            self.assertEqual(result["status_code"], 400)
        self.assertEqual(self.plugin.config, {})
        self.plugin.config.save_config.assert_not_called()

    async def test_save_failure_rolls_back_config_and_does_not_echo_internal_error(self):
        for values in ({}, {"visits_record_card_enabled": False, "visits_record_card_sessions": ["qq:GroupMessage:100"]}):
            with self.subTest(values=values):
                self.configure(**values)
                self.plugin.config.save_config.side_effect = OSError("secret storage path")
                result = await self.save(True, ["qq:FriendMessage:200"])
                self.assertEqual(result["status_code"], 400)
                self.assertIn("保存失败", result["message"])
                self.assertNotIn("secret", str(result))
                self.assertEqual(self.plugin.config, values)

    async def test_auth_disabled_and_retirement_block_writes(self):
        self.configure()
        result = await self.call("visits/record-card-settings", authenticated=False, enabled=True, sessions=[])
        self.assertEqual(result["status_code"], 401)
        self.plugin.enabled = False
        result = await self.save(True, [])
        self.assertEqual(result["status_code"], 503)
        self.plugin.enabled = True
        self.plugin.retired = True
        result = await self.save(True, [])
        self.assertEqual(result["status_code"], 503)
        self.plugin.config.save_config.assert_not_called()
