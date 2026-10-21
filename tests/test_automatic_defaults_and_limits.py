"""New-world autonomy and explicit zero budgets remain stable across hours."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.config_store import ConfigStore
from core.defaults import default_schedules
from core.engine.limits import LimitsMixin
from core.models import DeciderConfig, Limits, parse_schedules
from core.state import WorldState


class AutomaticDefaultsTests(unittest.TestCase):
    def test_natural_silence_defaults_off_for_new_and_existing_worlds(self):
        self.assertFalse(DeciderConfig().natural_silence)
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(directory)
            store.ensure_files()
            self.assertFalse(store.load_world().decider.natural_silence)
            raw = store.raw_world()
            raw.setdefault("decider", {}).pop("natural_silence", None)
            store.save_world(raw)
            self.assertFalse(store.load_world().decider.natural_silence)
            raw["decider"]["natural_silence"] = True
            store.save_world(raw)
            self.assertTrue(store.load_world().decider.natural_silence)

    def test_autonomous_background_budget_defaults_to_one_thousand_calls(self):
        self.assertEqual(Limits().background_daily_calls, 1000)

    def test_fresh_world_arranges_two_plans_without_an_extra_toggle(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(directory)
            store.ensure_files()
            auto = store.load_schedules().auto
            self.assertTrue(auto.enabled)
            self.assertEqual((auto.per_day, auto.hour), (2, 8))

    def test_existing_disabled_or_missing_auto_setting_is_preserved(self):
        for raw in ({"schedules": []}, {"schedules": [], "auto": {"enabled": False}}):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "schedules.json"
                path.write_text(json.dumps(raw), encoding="utf-8")
                store = ConfigStore(directory)
                store.ensure_files()
                self.assertFalse(store.load_schedules().auto.enabled)
                self.assertEqual(json.loads(path.read_text("utf-8")), raw)
        self.assertFalse(parse_schedules({})[0].auto.enabled)
        self.assertTrue(parse_schedules(default_schedules())[0].auto.enabled)


class HourlyAutonomyLimitsTests(unittest.TestCase):
    def test_zero_stops_calls_and_shares_in_every_hour(self):
        engine = LimitsMixin()
        engine.tick_seconds = 60
        engine.world = SimpleNamespace(limits=Limits())
        for field, allowed, count in (
            ("max_arrival_decisions_per_hour", "_arrival_decision_allowed", "_count_arrival_decision"),
            ("max_share_per_hour", "_share_allowed", "_count_share"),
            ("max_llm_text_per_hour", "_llm_text_allowed", "_count_llm_text"),
        ):
            with self.subTest(field=field):
                setattr(engine.world.limits, field, 0)
                state = WorldState("test:GroupMessage:1")
                for hour in (0, 1, 24):
                    state.world_time = hour * 60
                    self.assertFalse(getattr(engine, allowed)(state))
                setattr(engine.world.limits, field, 1)
                self.assertTrue(getattr(engine, allowed)(state))
                getattr(engine, count)(state)
                self.assertFalse(getattr(engine, allowed)(state))
                state.world_time += 60
                self.assertTrue(getattr(engine, allowed)(state))
