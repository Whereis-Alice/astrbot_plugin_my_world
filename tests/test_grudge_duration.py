"""Existing defaults migrate once, while explicit durations remain configurable."""

import json
import unittest

from core.config_store import ConfigStore
from tests import test_life_audit as audit


class GrudgeDurationTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = audit.LifeAuditTests.asyncSetUp

    async def test_world_defaults_and_saved_overrides(self):
        self.assertEqual(self.store.load_world().profile.grudge_days, 1)
        path = self.store.world_path
        raw = self.store.raw_world()
        raw["profile"].update(grudge_days=7)
        raw["profile"].pop("grudge_duration_version", None)
        path.write_text(json.dumps(raw), encoding="utf-8")
        self.assertEqual(self.store.load_world().profile.grudge_days, 1)
        self.assertEqual(self.store.raw_world()["profile"]["grudge_days"], 1)

        raw = self.store.raw_world()
        raw["profile"]["grudge_days"] = 7
        self.store.save_world(raw)
        self.assertEqual(self.store.load_world().profile.grudge_days, 7)
        self.assertFalse(ConfigStore._migrate_grudge_duration({"profile": {"grudge_days": 3}}))

        sessions = self.store.raw_sessions()
        sessions["sessions"][0]["config_overrides"] = {"settings": {"profile": {"grudge_days": "7"}}}
        self.store.sessions_path.write_text(json.dumps(sessions), encoding="utf-8")
        settings = self.store.load_sessions().sessions[0].config_overrides["settings"]
        self.assertEqual(settings["profile"]["grudge_days"], 1)

    async def test_existing_default_record_ages_from_original_time(self):
        scope = audit.S
        now = self.clock.now()
        async with self.engine.session_state(scope) as state:
            state.grudges = [
                {"reason": "弄坏了我的纸风车", "user_id": "42", "at": now - 86400,
                 "until": now + 6 * 86400},
                {"reason": "对我的约定失信", "user_id": "43", "at": now - 86400,
                 "until": now + 2 * 86400},
            ]
            rows = {row["user_id"]: row for row in self.engine.emotion_records(state, scope)}
            self.assertEqual(rows["42"]["status"], "expired")
            self.assertEqual(rows["42"]["expires_at"], now)
            self.assertEqual(rows["43"]["status"], "active")
            self.engine._decay_grudges(state, now=now)
            rows = {row["user_id"]: row for row in self.engine.emotion_records(state, scope)}
            self.assertTrue(rows["42"]["archived"])
            self.assertEqual(rows["43"]["expires_at"], now + 2 * 86400)

    async def test_new_records_use_twenty_four_hours_or_explicit_custom_duration(self):
        scope = audit.S
        async with self.engine.session_state(scope) as state:
            for uid, days in (("42", 1), ("43", 7)):
                self.engine.world.profile.grudge_days = days
                self.assertTrue(self.engine.note_grudge(state, "对我的约定失信", user_id=uid, force=True))
            self.engine.world.profile.grudge_days = 1
            rows = {row["user_id"]: row for row in self.engine.emotion_records(state, scope)}
            self.assertEqual(rows["42"]["expires_at"], self.clock.now() + 86400)
            self.assertEqual(rows["43"]["expires_at"], self.clock.now() + 7 * 86400)
