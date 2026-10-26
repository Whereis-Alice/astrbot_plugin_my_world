"""Ordinary identity stages are separate from distance tiers and commitments."""
import unittest

from core.config_store import ConfigStore
from core.defaults import default_profile
from core.models import ProfileConfig, parse_world
from core.relationship import friendship_progress
from tests import test_profile as profiles

SESSION = profiles.SESSION
START = 1760000000
DAY = 86400


class FriendshipStagesTests(unittest.TestCase):
    setUp = profiles.ProfileTestCase.setUp
    _group_key = staticmethod(profiles.ProfileTestCase._group_key)

    def test_new_defaults_have_five_ordinary_identities(self):
        config = ProfileConfig.model_validate(default_profile())
        self.assertEqual([config.default_bond, *[s.bond for s in config.friendship_stages]],
                         ["陌生人", "群友", "朋友", "好友", "挚友"])
        for name in ("群友", "朋友", "好友", "挚友"):
            bond = config.bond_by_name(name)
            self.assertTrue(bond and bond.auto_form and bond.group and not bond.unique and not bond.negative)
        self.assertFalse(config.familiarity_descriptions_enabled)

    def test_relationship_stage_order_does_not_depend_on_equal_distance_caps(self):
        self.world.profile.bond_by_name("朋友").cap = self.world.profile.bond_by_name("群友").cap
        self.store.touch(SESSION, "42", now=START)
        self.store.adjust_affinity(SESSION, "42", 30)
        for day in range(5):
            self.store.note_talked(SESSION, "42", now=START + day * DAY)
        self.assertEqual(self.store.view(SESSION, "42").affinities, ["朋友"])

    def test_all_five_stages_grow_on_distinct_direct_conversation_days(self):
        self.store.touch(SESSION, "42", now=START)
        self.store.adjust_affinity(SESSION, "42", 100)
        expected = {0: "陌生人", 1: "群友", 4: "朋友", 9: "好友", 19: "挚友"}
        for day in range(20):
            self.store.note_talked(SESSION, "42", now=START + day * DAY)
            if day in expected:
                self.assertEqual(self.store.view(SESSION, "42").affinities, [expected[day]])
        self.assertIsNone(friendship_progress(self.store, SESSION, "42")["next"])

    def test_legacy_relation_tables_without_stages_keep_their_available_chain(self):
        store = ConfigStore(self._tmp.name)
        store.ensure_files()
        raw = store.raw_world()
        raw["profile"]["bonds"] = [b for b in raw["profile"]["bonds"] if b["name"] not in {"好友", "挚友"}]
        raw["profile"].pop("friendship_stages", None)
        store.save_world(raw, snapshot=False)
        world = store.load_world()
        self.assertEqual([s.bond for s in world.profile.friendship_stages], ["群友", "朋友"])
        self.assertFalse(any(b.name in {"好友", "挚友"} for b in world.profile.bonds))
        self.assertNotIn("friendship_stages", store.raw_world()["profile"])

    def test_explicit_empty_or_custom_stages_are_preserved(self):
        for stages in ([], [{"bond": "朋友", "min_talk_days": 30, "min_affinity": 40}]):
            raw = default_profile()
            raw["friendship_stages"] = stages
            parsed = ProfileConfig.model_validate(raw)
            self.assertEqual([s.model_dump(mode="json") for s in parsed.friendship_stages], stages)

    def test_empty_legacy_bond_table_recovers_the_complete_default_stages(self):
        store = ConfigStore(self._tmp.name)
        store.ensure_files()
        raw = store.raw_world()
        raw["profile"]["bonds"] = []
        raw["profile"].pop("friendship_stages", None)
        world, warnings = parse_world(raw)
        self.assertIn("关系表为空，已按默认关系补回", warnings)
        self.assertEqual([s.bond for s in world.profile.friendship_stages], ["群友", "朋友", "好友", "挚友"])
        self.assertTrue(all(world.profile.bond_by_name(s.bond) for s in world.profile.friendship_stages))

    def test_special_identities_never_advance_into_ordinary_friendship(self):
        for index, name in enumerate(("主人", "男友", "家人", "闺蜜")):
            uid = str(index + 100)
            self.store.touch(SESSION, uid, now=START)
            self.store.note_bond(SESSION, uid, type=name)
            self.store.adjust_affinity(SESSION, uid, 100)
            for day in range(25):
                self.store.note_talked(SESSION, uid, now=START + day * DAY)
            self.assertEqual(self.store.view(SESSION, uid).affinities, [name])
            self.assertFalse(friendship_progress(self.store, SESSION, uid)["eligible"])
