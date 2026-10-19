"""Starting points must never become live cross-conversation synchronization."""

import copy
import math
import tempfile
import unittest
from pathlib import Path

from core.db import Database
from core.defaults import default_world
from core.models import parse_world
from core.profile import ProfileStore
from tests import test_scope_api as scope


class SocialDirectoryApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = scope.ScopeApiTests.asyncSetUp
    call = scope.ScopeApiTests.call
    other = "test:PrivateMessage:987654"
    uid = "12345678"

    def add_session(self, sid=None):
        self.plugin.store.add_session(sid or self.other)
        self.plugin.engine.reload_config()

    async def apply(self, **changes):
        return await self.call("relationships/apply", method="POST", user_id=self.uid,
            **{"affinity": 42, "relations": ["主人", "朋友"], "revision": 0, **changes})

    async def test_unified_apply_preserves_knowledge_and_sets_exact_values(self):
        self.add_session()
        store = self.plugin.engine.profiles
        store.touch(self.session, self.uid, "群昵称")
        store.note_fact(self.session, self.uid, text="喜欢热茶", evidence="喜欢热茶")
        result = await self.apply(name="管理页称呼")
        self.assertEqual(result["status_code"], 200)
        self.assertEqual(result["data"]["applied_sessions"], 2)
        for sid in [self.session, self.other]:
            view = store.view(sid, self.uid)
            self.assertEqual(view.affinity, 42)
            self.assertCountEqual(view.affinities, ["主人", "朋友"])
        self.assertEqual(store.view(self.session, self.uid).name, "群昵称")
        self.assertEqual(store.view(self.session, self.uid).facts[0]["text"], "喜欢热茶")
        row = store.profile(self.other, self.uid)
        self.assertEqual(row["message_count"], 0)
        self.assertEqual(row["first_seen_at"], 0)
        self.assertEqual(row["last_seen_at"], 0)
        self.assertEqual(row["payload"]["days"], [])

    async def test_runtime_drift_remains_local_across_touch_and_reload(self):
        self.add_session()
        await self.apply()
        store = self.plugin.engine.profiles
        store.apply_reply_affinity(self.other, self.uid, 3)
        store.note_bond(self.other, self.uid, type="女友")
        store.touch(self.other, self.uid, "新昵称")
        self.plugin.engine.reload_config()
        store = self.plugin.engine.profiles
        self.assertEqual(store.view(self.session, self.uid).affinity, 42)
        self.assertEqual(store.view(self.other, self.uid).affinity, 55)
        self.assertIn("女友", store.view(self.other, self.uid).affinities)
        self.assertNotIn("女友", store.view(self.session, self.uid).affinities)
        self.assertEqual(store.db.relationship_baseline(self.uid)["affinity"], 42)

    async def test_new_session_copies_latest_baseline_once(self):
        await self.apply(affinity=10, relations=["主人"])
        self.add_session()
        store = self.plugin.engine.profiles
        self.assertIsNone(store.profile(self.other, self.uid))
        row = store.touch(self.other, self.uid, "新群昵称")
        self.assertEqual(row["affinity"], 10)  # Do not inflate the explicit setting to 30.
        self.assertEqual(store.view(self.other, self.uid).level.name, "朋友")
        store.adjust_affinity(self.other, self.uid, 6)
        store.touch(self.other, self.uid)
        self.assertEqual(store.profile(self.other, self.uid)["affinity"], 16)
        self.assertEqual(store.profile(self.session, self.uid)["affinity"], 10)
        self.assertEqual(store.profile(self.other, self.uid)["payload"]["relationship_baseline_revision"], 1)

    async def test_grouped_sessions_share_life_but_not_relationship_state(self):
        self.add_session()
        raw = self.plugin.store.raw_sessions()
        raw["groups"] = [{"id": "together", "name": "同一个她", "sessions": [self.session, self.other],
                          "main_session": self.session}]
        self.plugin.store.save_sessions(raw)
        self.plugin.engine.reload_config()
        engine = self.plugin.engine
        self.assertEqual(engine.state_key(self.session), engine.state_key(self.other))
        await self.apply()
        engine.profiles.adjust_affinity(self.other, self.uid, -5)
        self.assertEqual(engine.profiles.view(self.session, self.uid).affinity, 42)
        self.assertEqual(engine.profiles.view(self.other, self.uid).affinity, 37)
        engine.profiles.note_talked(self.other, self.uid, now=1792368000)
        self.assertEqual(engine.profiles.view(self.session, self.uid).talk_days, 0)
        self.assertEqual(engine.profiles.view(self.other, self.uid).talk_days, 1)
        main_quota = engine.profiles.proactive_quota_left(self.session, self.uid)
        member_quota = engine.profiles.proactive_quota_left(self.other, self.uid)
        engine.profiles.note_proactive(self.other, self.uid)
        self.assertEqual(engine.profiles.proactive_quota_left(self.session, self.uid), main_quota)
        self.assertEqual(engine.profiles.proactive_quota_left(self.other, self.uid), member_quota - 1)
        response = await self.call("profile/contact", session=self.other, user_id=self.uid)
        self.assertEqual(response["data"]["person"]["affinity"], 37)
        async with engine.session_state(self.session) as state:
            engine.note_grudge(state, "还记着上次的约定", user_id=self.uid, session_id=self.other, force=True)
        response = await self.call("profile/contact", session=self.other, user_id=self.uid)
        self.assertEqual(response["data"]["relationship"]["progress"]["talk_days"], 1)
        person = (await self.call("relationships/person", user_id=self.uid))["data"]
        member = next(item for item in person["sessions"] if item["session"] == self.other)
        self.assertTrue(member["cold"])
        self.assertNotEqual(member["level"], member["base_level"])

    async def test_unique_conflict_rolls_back_all_scopes_and_baseline(self):
        self.add_session()
        store = self.plugin.engine.profiles
        store.touch(self.other, "98765")
        store.note_bond(self.other, "98765", type="女友")
        store.touch(self.session, self.uid)
        store.adjust_affinity(self.session, self.uid, 7)
        result = await self.apply(relations=["男友"])
        self.assertEqual(result["status_code"], 400)
        self.assertIsNone(store.db.relationship_baseline(self.uid))
        self.assertEqual(store.view(self.session, self.uid).affinity, 7)
        self.assertEqual(store.view(self.session, self.uid).affinities, ["陌生人"])
        self.assertIn("女友", store.view(self.other, "98765").affinities)

    async def test_explicit_local_edit_does_not_change_baseline_or_other_scope(self):
        self.add_session()
        await self.apply()
        result = await self.call("relationships/session", method="POST", session=self.other,
            user_id=self.uid, affinity=-20, relations=["男友"])
        self.assertEqual(result["status_code"], 200)
        store = self.plugin.engine.profiles
        self.assertEqual(store.view(self.other, self.uid).affinity, -20)
        self.assertEqual(store.view(self.other, self.uid).level.name, "冷淡")
        self.assertEqual(store.view(self.session, self.uid).affinity, 42)
        self.assertEqual(store.db.relationship_baseline(self.uid)["relations"], ["主人", "朋友"])

    async def test_local_editor_uses_the_selected_conversations_own_identity_types(self):
        self.add_session()
        world = self.plugin.store.raw_world()
        world["profile"]["bonds"].append({"name": "书友", "slot": "book_friend", "floor": 0, "cap": 3})
        self.plugin.store.save_scope(self.other, world, {"schedules": []}, ["settings"])
        self.plugin.engine.reload_config()
        result = await self.call("relationships/session", method="POST", session=self.other,
            user_id=self.uid, affinity=12, relations=["书友"])
        self.assertEqual(result["status_code"], 200)
        with self.plugin.engine.config_scope(self.other):
            self.assertIn("书友", self.plugin.engine.profiles.view(self.other, self.uid).affinities)
        self.assertIsNone(self.plugin.engine.profiles.profile(self.session, self.uid))
        self.assertIsNone(self.plugin.db.raw.relationship_baseline(self.uid))

    async def test_remove_baseline_keeps_existing_states_and_future_sessions_start_normally(self):
        await self.apply()
        result = await self.call("relationships/baseline", method="POST", user_id=self.uid, revision=1)
        self.assertEqual(result["status_code"], 200)
        self.add_session()
        store = self.plugin.engine.profiles
        self.assertEqual(store.view(self.session, self.uid).affinity, 42)
        self.assertEqual(store.touch(self.other, self.uid)["affinity"], 0)
        self.assertEqual(store.view(self.other, self.uid).affinities, ["陌生人"])

    async def test_reapplying_is_explicit_and_rejects_stale_edits(self):
        self.add_session()
        await self.apply()
        store = self.plugin.engine.profiles
        store.adjust_affinity(self.other, self.uid, 4)
        self.assertEqual((await self.apply(affinity=12))["status_code"], 400)
        result = await self.apply(affinity=12, relations=["家人"], revision=1)
        self.assertEqual(result["status_code"], 200)
        self.assertEqual(store.view(self.other, self.uid).affinity, 12)
        self.assertEqual(store.db.relationship_baseline(self.uid)["revision"], 2)
        self.assertTrue(store.affinity_logs(self.other, self.uid))

    async def test_catalog_merges_only_by_qq_and_filters_without_writing(self):
        self.add_session()
        store = self.plugin.engine.profiles
        store.touch(self.session, self.uid, "阿岚")
        store.touch(self.other, self.uid, "岚")
        store.touch(self.session, "87654321", "阿岚")
        before = len(store.db.all_user_profiles())
        result = (await self.call("relationships", q="12345678"))["data"]
        self.assertEqual(result["total"], 1)
        self.assertEqual(len(result["people"][0]["sessions"]), 2)
        self.assertIsNone(result["people"][0]["baseline"])
        self.assertEqual(len(store.db.all_user_profiles()), before)
        self.assertEqual((await self.call("relationships", filter="configured"))["data"]["total"], 0)

    async def test_catalog_search_and_paging_include_people_after_the_first_200(self):
        with self.plugin.db.raw.atomic():
            for index in range(205):
                self.plugin.db.raw.upsert_user_profile(group_id=self.session, user_id=str(1000000 + index),
                    payload={"qq_name": f"远方{index}"}, affinity=0, first_seen_at=0,
                    last_seen_at=index, message_count=0, digest="")
        page = (await self.call("relationships", page=9))["data"]
        self.assertEqual(page["total"], 205)
        self.assertEqual(page["pages"], 9)
        self.assertEqual(len(page["people"]), 13)
        person = (await self.call("relationships", q="远方204"))["data"]
        self.assertEqual(person["total"], 1)
        self.assertEqual(person["people"][0]["user_id"], "1000204")

    async def test_invalid_values_and_conflicting_identities_never_write(self):
        for values in [dict(affinity=math.inf), dict(affinity=math.nan), dict(affinity=True),
                       dict(affinity=1000), dict(relations=["朋友", "女友"]),
                       dict(relations=["陌生人", "主人"]), dict(relations=["不存在的关系"]),
                       dict(relations="主人")]:
            with self.subTest(values=values):
                self.assertEqual((await self.apply(**values))["status_code"], 400)
        for uid in ["<img>", "123 456", "012345", "1;DROP TABLE", ""]:
            result = await self.call("relationships/apply", method="POST", user_id=uid,
                affinity=0, relations=[], revision=0)
            self.assertEqual(result["status_code"], 400)
        self.assertIsNone(self.plugin.engine.profiles.db.relationship_baseline(self.uid))

    async def test_global_apply_ignores_the_config_scope_selected_on_other_pages(self):
        self.add_session()
        result = await self.apply(config_scope=self.other)
        self.assertEqual(result["status_code"], 200)
        self.assertEqual(result["data"]["applied_sessions"], 2)

    async def test_seed_can_be_set_before_any_sessions_exist(self):
        self.plugin.store.save_sessions({"sessions": [], "groups": []})
        self.plugin.engine.reload_config()
        result = await self.apply()
        self.assertEqual(result["status_code"], 200)
        self.assertEqual(result["data"]["applied_sessions"], 0)
        self.add_session()
        self.assertEqual(self.plugin.engine.profiles.touch(self.other, self.uid)["affinity"], 42)

    async def test_remember_writes_to_actual_place_within_a_shared_life(self):
        self.add_session()
        raw = self.plugin.store.raw_sessions()
        raw["groups"] = [{"id": "together", "sessions": [self.session, self.other], "main_session": self.session}]
        self.plugin.store.save_sessions(raw)
        self.plugin.engine.reload_config()
        engine = self.plugin.engine
        await self.apply()
        from core.engine.models import TickOutcome
        from core.json_actions import PlannedAction

        async with engine.session_state(self.session) as state:
            await engine._run_remember(state, TickOutcome(session_id=self.session, place=self.other),
                PlannedAction(type="remember", params={"user": self.uid, "text": "喜欢热茶",
                    "evidence": "我喜欢热茶", "affinity_delta": 2}))
        self.assertEqual(engine.profiles.profile(self.session, self.uid)["affinity"], 42)
        self.assertEqual(engine.profiles.profile(self.other, self.uid)["affinity"], 44)
        self.assertEqual(engine.profiles.facts(self.session, self.uid), [])
        self.assertEqual(engine.profiles.facts(self.other, self.uid)[0]["source_session"], self.other)

    async def test_invalid_numeric_rules_and_new_unique_conflicts_preserve_configuration(self):
        initial = (await self.call("relationships/rules"))["data"]
        for field in ("affinity_min", "affinity_max", "affinity_initial"):
            draft = copy.deepcopy(initial["profile"])
            draft[field] = math.nan
            result = await self.call("relationships/rules", method="POST", profile=draft, fingerprint=initial["fingerprint"])
            self.assertEqual(result["status_code"], 400)
        store = self.plugin.engine.profiles
        for uid in (self.uid, "98765"):
            store.touch(self.session, uid)
            store.note_bond(self.session, uid, type="朋友")
        draft = copy.deepcopy(initial["profile"])
        next(item for item in draft["bonds"] if item["name"] == "朋友")["unique"] = True
        result = await self.call("relationships/rules", method="POST", profile=draft, fingerprint=initial["fingerprint"])
        self.assertEqual(result["status_code"], 400)
        self.assertFalse(self.plugin.engine.world.profile.bond_by_name("朋友").unique)

    async def test_rules_add_identity_with_manual_defaults_and_protect_used_names(self):
        response = (await self.call("relationships/rules"))["data"]
        profile = copy.deepcopy(response["profile"])
        profile["bonds"].append({"name": "同学", "slot": "classmate", "floor": 2, "cap": 4})
        result = await self.call("relationships/rules", method="POST", profile=profile,
                                 fingerprint=response["fingerprint"])
        self.assertEqual(result["status_code"], 200)
        bond = self.plugin.engine.world.profile.bond_by_name("同学")
        self.assertFalse(bond.auto_form)
        self.assertFalse(bond.allow_boundary_crossing)
        self.assertEqual((await self.apply(relations=["同学", "朋友"]))["status_code"], 200)
        latest = (await self.call("relationships/rules"))["data"]
        latest["profile"]["bonds"] = [item for item in latest["profile"]["bonds"] if item["name"] != "同学"]
        result = await self.call("relationships/rules", method="POST", profile=latest["profile"],
                                 fingerprint=latest["fingerprint"])
        self.assertEqual(result["status_code"], 400)
        self.assertIsNotNone(self.plugin.engine.world.profile.bond_by_name("同学"))

    async def test_rules_keep_stored_slots_consistent_and_reject_conflicting_seed_identities(self):
        await self.apply()
        loaded = (await self.call("relationships/rules"))["data"]
        draft = copy.deepcopy(loaded["profile"])
        owner = next(item for item in draft["bonds"] if item["name"] == "主人")
        owner["slot"] = "my_owner"
        result = await self.call("relationships/rules", method="POST", profile=draft, fingerprint=loaded["fingerprint"])
        self.assertEqual(result["status_code"], 200)
        saved = self.plugin.engine.profiles.bonds(self.session, self.uid, statuses=["current"])
        self.assertEqual(next(item for item in saved if item["type"] == "主人")["slot"], "my_owner")
        loaded = (await self.call("relationships/rules"))["data"]
        draft = copy.deepcopy(loaded["profile"])
        next(item for item in draft["bonds"] if item["name"] == "主人")["group"] = "close"
        result = await self.call("relationships/rules", method="POST", profile=draft, fingerprint=loaded["fingerprint"])
        self.assertEqual(result["status_code"], 400)
        self.assertEqual(self.plugin.engine.world.profile.bond_by_name("主人").group, "")

    async def test_global_rules_preserve_independent_rules_and_shared_life_members(self):
        self.add_session()
        member = "test:GroupMessage:777"
        self.add_session(member)
        local_world = self.plugin.store.raw_world()
        original_slot = next(b for b in local_world["profile"]["bonds"] if b["name"] == "主人")["slot"]
        self.plugin.store.save_scope(self.other, local_world, {"schedules": []}, ["settings"])
        sessions = self.plugin.store.raw_sessions()
        sessions["groups"] = [{"id": "local", "sessions": [self.other, member], "main_session": self.other}]
        self.plugin.store.save_sessions(sessions)
        self.plugin.engine.reload_config()
        for sid in (self.other, member):
            with self.plugin.engine.config_scope(sid):
                store = self.plugin.engine.profiles
                for uid in (self.uid, "98765"):
                    store.touch(sid, uid)
                    store.note_bond(sid, uid, type="朋友")
                store.note_bond(sid, self.uid, type="主人", asserted_by="管理员设置")
        store = self.plugin.engine.profiles
        store.touch(self.session, self.uid)
        store.note_bond(self.session, self.uid, type="主人", asserted_by="管理员设置")
        loaded = (await self.call("relationships/rules"))["data"]
        next(b for b in loaded["profile"]["bonds"] if b["name"] == "朋友")["unique"] = True
        next(b for b in loaded["profile"]["bonds"] if b["name"] == "主人")["slot"] = "global_owner"
        result = await self.call("relationships/rules", method="POST", **loaded)
        self.assertEqual(result["status_code"], 200, result)
        rows = self.plugin.db.raw._query("SELECT group_id,slot FROM user_bond WHERE type='主人' AND status='current'")
        self.assertEqual({r["group_id"]: r["slot"] for r in rows},
                         {self.session: "global_owner", self.other: original_slot, member: original_slot})
        with self.plugin.engine.config_scope(member):
            self.assertFalse(self.plugin.engine.world.profile.bond_by_name("朋友").unique)

    async def test_unrelated_rule_edit_tolerates_existing_overlaps_but_rejects_new_ones(self):
        store = self.plugin.engine.profiles
        store.touch(self.session, self.uid)
        for name in ("朋友", "女友", "主人"):
            bond = store.config.bond_by_name(name)
            self.plugin.db.raw.add_user_bond(group_id=self.session, user_id=self.uid,
                                             type=name, slot=bond.slot)
        loaded = (await self.call("relationships/rules"))["data"]
        loaded["profile"]["affinity_initial"] = 1
        result = await self.call("relationships/rules", method="POST", **loaded)
        self.assertEqual(result["status_code"], 200, result)
        loaded = (await self.call("relationships/rules"))["data"]
        next(b for b in loaded["profile"]["bonds"] if b["name"] == "主人")["group"] = "close"
        result = await self.call("relationships/rules", method="POST", **loaded)
        self.assertEqual(result["status_code"], 400)

    async def test_new_local_session_reports_seed_conflicts_without_replacing_an_identity(self):
        await self.apply()
        self.add_session()
        local = self.plugin.store.raw_world()
        next(b for b in local["profile"]["bonds"] if b["name"] == "主人")["group"] = "close"
        self.plugin.store.save_scope(self.other, local, {"schedules": []}, ["settings"])
        self.plugin.engine.reload_config()
        with self.plugin.engine.config_scope(self.other):
            store = self.plugin.engine.profiles
            row = store.touch(self.other, self.uid)
            self.assertEqual(row["affinity"], 42)
            self.assertEqual(store.view(self.other, self.uid).affinities, ["主人"])
            self.assertIn("朋友", row["payload"]["relationship_baseline_issue"])
            self.assertIn("替代组", row["payload"]["relationship_baseline_issue"])


class ProfileIsolationMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Database(Path(temporary.name) / "state.db")
        self.addCleanup(self.db.close)
        self.world = parse_world(default_world())[0]

    def test_old_shared_profiles_split_once_without_losing_history(self):
        source, target = "qq:GroupMessage:100", "qq:PrivateMessage:200"
        store = ProfileStore(self.db, self.world)
        store.touch(source, "12345", "旧友")
        store.note_fact(source, "12345", text="喜欢热茶", evidence="喜欢热茶")
        store.note_bond(source, "12345", type="主人", asserted_by="管理员设置")
        self.db.kv_set("affinity_daily", {source: {"day": "2026-10-19", "people": {"12345": {"model_up": 3}}}})
        before = store.affinity_logs(source, "12345")
        self.db.split_profile_sessions({source: source, target: source})
        self.assertEqual(store.view(target, "12345").affinity, 30)
        self.assertEqual(store.view(target, "12345").facts[0]["text"], "喜欢热茶")
        self.assertEqual(len(store.affinity_logs(target, "12345")), len(before))
        self.assertNotEqual(store.bonds(source, "12345")[0]["id"], store.bonds(target, "12345")[0]["id"])
        self.assertEqual(self.db.kv_get("affinity_daily")[source], self.db.kv_get("affinity_daily")[target])
        store.adjust_affinity(source, "12345", 7)
        self.db.split_profile_sessions({source: source, target: source, "new": source})
        self.assertEqual(store.view(target, "12345").affinity, 30)
        self.assertIsNone(store.view("new", "12345"))

    def test_transaction_rolls_back_profiles_seed_and_nested_writes(self):
        with self.assertRaisesRegex(ValueError, "rollback"):
            with self.db.atomic():
                self.db.save_relationship_baseline("12345", {"affinity": 30, "relations": []})
                with self.db.atomic():
                    self.db.kv_set("example", "written")
                raise ValueError("rollback")
        self.assertIsNone(self.db.relationship_baseline("12345"))
        self.assertIsNone(self.db.kv_get("example"))
