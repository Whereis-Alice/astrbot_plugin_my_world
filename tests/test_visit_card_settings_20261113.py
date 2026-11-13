"""Display-only provider selection is authenticated, validated and saved atomically."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace as NS
import unittest

from tests import test_visits_web as fixture


class Config(dict):
    def save_config(self):
        self.saves += 1
        if self.fail:
            raise OSError("cannot save")


class VisitCardSettingsTests(unittest.IsolatedAsyncioTestCase):
    call = fixture.VisitEditorApiTests.call

    def setUp(self):
        fixture.VisitEditorApiTests.setUp(self)
        self.config = Config(visits_card_enabled=True, visits_card_model_enabled=True,
                             visits_card_provider_id="", visits_card_max_length=12)
        self.config.saves, self.config.fail = 0, False
        self.plugin.config = self.config
        self.plugin.context.get_all_providers = lambda: [NS(meta=lambda: NS(id="fast", model="small model"))]
        self.payload = {"token": "editor-login", "enabled": True, "model_enabled": True,
                        "provider_id": "fast", "max_length": 10}

    async def test_global_read_lists_display_settings_without_model_credentials(self):
        result = await self.call("visits", payload={"token": "editor-login"})
        status = result["data"]["card_status"]
        self.assertEqual(status["providers"], [{"id": "fast", "label": "small model"}])
        self.assertEqual(status["max_length"], 12)
        self.assertEqual(self.config.saves, 0)

    async def test_saved_settings_change_only_display_config_and_clear_cached_wording(self):
        self.plugin.engine._visit_card_cache = {"old": "old words"}
        result = await self.call("visits/card-status-settings", payload=self.payload)
        self.assertEqual(result["status_code"], 200)
        self.assertEqual(result["data"]["provider_id"], "fast")
        self.assertEqual(self.config["visits_card_provider_id"], "fast")
        self.assertEqual(self.plugin.visits_card_provider_id, "fast")
        self.assertEqual(self.plugin.engine._visit_card_cache, {})
        self.assertEqual(self.config.saves, 1)
        self.service.invite.assert_not_awaited()
        self.service.return_home.assert_not_awaited()

    async def test_invalid_provider_switches_lengths_and_types_cannot_be_saved(self):
        before = dict(self.config)
        for patch in ({"enabled": "true"}, {"model_enabled": 1}, {"max_length": True},
                      {"max_length": 5}, {"max_length": 21}, {"max_length": 10.5},
                      {"provider_id": "not-configured"}, {"provider_id": []}):
            with self.subTest(patch=patch):
                result = await self.call("visits/card-status-settings", payload={**self.payload, **patch})
                self.assertEqual(result["status_code"], 400)
                self.assertEqual(dict(self.config), before)
        self.assertEqual(self.config.saves, 0)

    async def test_failed_config_write_rolls_back_and_does_not_drop_existing_cache(self):
        before = dict(self.config)
        self.config.fail = True
        self.plugin.engine._visit_card_cache = {"old": "words"}
        result = await self.call("visits/card-status-settings", payload=self.payload)
        self.assertEqual(result["status_code"], 400)
        self.assertEqual(dict(self.config), before)
        self.assertEqual(self.plugin.engine._visit_card_cache, {"old": "words"})

    async def test_current_unloaded_provider_can_be_retained_and_blank_means_default(self):
        self.config["visits_card_provider_id"] = "unloaded"
        result = await self.call("visits/card-status-settings", payload={**self.payload, "provider_id": "unloaded"})
        self.assertEqual(result["status_code"], 200)
        result = await self.call("visits/card-status-settings", payload={**self.payload, "provider_id": ""})
        self.assertEqual(result["status_code"], 200)
        self.assertEqual(self.config["visits_card_provider_id"], "")

    def test_plugin_schema_uses_native_provider_picker_for_display_slot(self):
        schema = json.loads((Path(__file__).parents[1] / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["visits_card_provider_id"]["_special"], "select_provider")
        self.assertTrue(schema["visits_card_enabled"]["default"])
