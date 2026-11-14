"""Explicit cross-adapter matches use enabled membership and opaque signed proofs."""
from __future__ import annotations

import copy
from contextlib import nullcontext
import json
from types import SimpleNamespace
import unittest

from tests import test_visits_actor as fixture
from core.engine.sessions import SessionsMixin
from core.visits.routing import ConversationRouting, MAX_CONTEXTS


ONE = "onebot:GroupMessage:123456789"
OFFICIAL = "official:GroupMessage:QQ_OPEN_ID=="
OTHER = "onebot:GroupMessage:987654321"
ALIAS = "mw-explicit-conversation-v1:GroupMessage:tea-group"


class MembershipEngine(SessionsMixin):
    def __init__(self, sessions, groups=()):
        self.world = SimpleNamespace(content_safety=SimpleNamespace(session_blocklist=[]))
        self._sessions_index = {session: SimpleNamespace(session_id=session, enabled=True) for session in sessions}
        self._group_index = {f"group-{index}": SimpleNamespace(id=f"group-{index}", sessions=list(members),
                            main_session=members[0]) for index, members in enumerate(groups)}
        self._group_of = {session: group.id for group in self._group_index.values() for session in group.sessions}

    def config_scope(self, session):
        return nullcontext()


def make_port(sessions=(ONE, OFFICIAL, OTHER), *, groups=(), aliases=()):
    engine = MembershipEngine(sessions, groups)
    platforms = {"onebot": SimpleNamespace(meta=lambda: SimpleNamespace(name="aiocqhttp")),
                 "official": SimpleNamespace(meta=lambda: SimpleNamespace(name="qq_official"))}
    plugin = SimpleNamespace(engine=engine, config={"visits_conversation_aliases": list(aliases)},
                             context=SimpleNamespace(get_platform_inst=platforms.get))
    return fixture.VisitActorPort(plugin), platforms


class ProofRouter(ConversationRouting):
    def __init__(self, port, scopes):
        self.actor_port, self.scopes = port, scopes

    def _participating(self):
        return self.scopes


class ExplicitConversationAliasTests(unittest.TestCase):
    def test_enabled_cross_adapter_alias_keeps_native_identity_and_does_not_mutate_config_or_membership(self):
        port, _ = make_port(aliases=[ONE + "=tea-group"])
        before = copy.deepcopy((port.plugin.config, port.engine.__dict__))
        self.assertEqual(port.routing_contexts(ONE), ["aiocqhttp:GroupMessage:123456789", ALIAS])
        self.assertEqual(port.routing_contexts(OTHER), ["aiocqhttp:GroupMessage:987654321"])
        self.assertEqual((port.plugin.config, port.engine.__dict__), before)

    def test_official_openid_padding_and_onebot_group_match_only_through_same_hmac_alias(self):
        left, _ = make_port((ONE, OTHER), aliases=[ONE + "=tea-group"])
        right, _ = make_port((OFFICIAL,), aliases=[OFFICIAL + "=tea-group"])
        a, b = ProofRouter(left, [ONE, OTHER]), ProofRouter(right, [OFFICIAL])
        anchor = {"secret": "local-fixture-pair-secret"}
        proof = b._routing_proof(OFFICIAL, anchor)
        self.assertEqual(len(set(a._routing_tokens(ONE, anchor)).intersection(proof["tokens"])), 1)
        a._check_routing_proof(ONE, anchor, proof)
        with self.assertRaises(ValueError):
            a._check_routing_proof(OTHER, anchor, proof)
        with self.assertRaises(ValueError):
            a._check_routing_proof(ONE, {"secret": "different-pair"}, proof)
        wire = json.dumps(proof)
        for private in (ONE, OFFICIAL, "QQ_OPEN_ID", "tea-group", ALIAS):
            self.assertNotIn(private, wire)

    def test_same_tag_across_distinct_enabled_lives_is_suppressed_for_both(self):
        port, _ = make_port(aliases=[ONE + "=tea-group", OTHER + "=tea-group"])
        self.assertNotIn(ALIAS, port.routing_contexts(ONE))
        self.assertNotIn(ALIAS, port.routing_contexts(OTHER))
        self.assertIn("aiocqhttp:GroupMessage:123456789", port.routing_contexts(ONE))

    def test_same_tag_in_group_and_private_chat_uses_separate_proof_namespaces(self):
        private = "official:FriendMessage:QQ_PRIVATE_OPEN_ID"
        port, _ = make_port((ONE, private), aliases=[ONE + "=tea-group", private + "=tea-group"])
        self.assertIn(ALIAS, port.routing_contexts(ONE))
        self.assertIn("mw-explicit-conversation-v1:FriendMessage:tea-group", port.routing_contexts(private))
        router = ProofRouter(port, [ONE, private])
        anchor = {"secret": "fixture-pair-secret"}
        private_proof = router._routing_proof(private, anchor)
        with self.assertRaises(ValueError):
            router._check_routing_proof(ONE, anchor, private_proof)

    def test_same_tag_for_two_members_of_one_shared_life_and_identical_duplicates_are_valid(self):
        port, _ = make_port(groups=[(ONE, OFFICIAL)],
                            aliases=[ONE + "=tea-group", ONE + "=tea-group", OFFICIAL + "=tea-group"])
        expected = ["aiocqhttp:GroupMessage:123456789", ALIAS, "qq_official:GroupMessage:QQ_OPEN_ID=="]
        self.assertEqual(port.routing_contexts(ONE), sorted(expected))
        self.assertEqual(port.routing_contexts(OFFICIAL), sorted(expected))

    def test_same_session_with_different_tags_adopts_neither_assignment(self):
        port, _ = make_port(aliases=[ONE + "=tea-group", ONE + "=other-tag"])
        self.assertEqual(port.routing_contexts(ONE), ["aiocqhttp:GroupMessage:123456789"])

    def test_missing_platform_cannot_advertise_offline_saved_session_alias(self):
        port, platforms = make_port(aliases=[ONE + "=tea-group", OFFICIAL + "=tea-group"])
        platforms.pop("onebot")
        self.assertEqual(port.routing_contexts(ONE), [])
        self.assertEqual(port.routing_contexts(OFFICIAL), [ALIAS, "qq_official:GroupMessage:QQ_OPEN_ID=="])

    def test_disabled_members_and_content_blocked_sessions_never_create_aliases_or_conflicts(self):
        port, _ = make_port(groups=[(ONE, OFFICIAL)], aliases=[OFFICIAL + "=tea-group", OTHER + "=tea-group"])
        port.engine._sessions_index[OFFICIAL].enabled = False
        self.assertEqual(port.routing_contexts(ONE), ["aiocqhttp:GroupMessage:123456789"])
        self.assertIn(ALIAS, port.routing_contexts(OTHER))
        port.engine.world.content_safety.session_blocklist = [OTHER]
        self.assertEqual(port.routing_contexts(OTHER), [])

    def test_actual_main_session_fallback_keeps_enabled_members_alias(self):
        port, _ = make_port(groups=[(ONE, OFFICIAL)], aliases=[ONE + "=tea-group", OFFICIAL + "=tea-group"])
        port.engine._sessions_index[ONE].enabled = False
        self.assertEqual(port.canonical_scope(ONE), OFFICIAL)
        self.assertEqual(port.routing_contexts("group-0"), [ALIAS, "qq_official:GroupMessage:QQ_OPEN_ID=="])
        self.assertEqual(port.routing_contexts(ONE), port.routing_contexts(OFFICIAL))

    def test_unknown_session_or_group_label_does_not_create_a_mapping(self):
        port, _ = make_port(groups=[(ONE, OFFICIAL)], aliases=["group-0=tea-group",
                            "onebot:GroupMessage:unknown=tea-group"])
        self.assertNotIn(ALIAS, port.routing_contexts(ONE))

    def test_regrouping_conflicting_aliases_rechecks_new_life_owners(self):
        port, _ = make_port(groups=[(ONE, OFFICIAL)], aliases=[ONE + "=tea-group", OFFICIAL + "=tea-group"])
        self.assertIn(ALIAS, port.routing_contexts(ONE))
        port.engine._group_index["group-0"].sessions.remove(OFFICIAL)
        port.engine._group_of.pop(OFFICIAL)
        self.assertNotIn(ALIAS, port.routing_contexts(ONE))
        self.assertNotIn(ALIAS, port.routing_contexts(OFFICIAL))

    def test_alias_bounds_and_malformed_values_are_ignored_without_removing_native_contexts(self):
        values = ["", "no-delimiter", ONE + "=", ONE + "=bad tag", ONE + "=bad\nline",
                  ONE + "=" + "a" * 81, ONE + "=bad:namespace", 123, {"session": ONE}]
        port, _ = make_port(aliases=values)
        self.assertEqual(port.routing_contexts(ONE), ["aiocqhttp:GroupMessage:123456789"])
        port.plugin.config["visits_conversation_aliases"] = ONE + "=tea-group"
        self.assertNotIn(ALIAS, port.routing_contexts(ONE))
        port.plugin.config["visits_conversation_aliases"] = [ONE + "=tea-group"] * 129
        self.assertNotIn(ALIAS, port.routing_contexts(ONE))

    def test_long_native_context_is_preserved_while_strict_alias_rejects_it(self):
        session = "official:GroupMessage:" + "a" * 401
        port, _ = make_port((session,), aliases=[session + "=tea-group"])
        self.assertEqual(port.routing_contexts(session), ["qq_official:GroupMessage:" + "a" * 401])

    def test_full_native_context_capacity_cannot_be_overflowed_by_an_alias(self):
        sessions = [f"onebot:GroupMessage:{index}" for index in range(MAX_CONTEXTS)]
        port, _ = make_port(sessions, groups=[sessions], aliases=[sessions[0] + "=tea-group"])
        contexts = port.routing_contexts(sessions[0])
        self.assertEqual(len(contexts), MAX_CONTEXTS)
        self.assertNotIn(ALIAS, contexts)
        self.assertTrue(all(row.startswith("aiocqhttp:GroupMessage:") for row in contexts))


if __name__ == "__main__":
    unittest.main()
