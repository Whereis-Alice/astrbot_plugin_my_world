"""Only enabled members of the selected shared group form routing identities."""
from contextlib import nullcontext
from types import SimpleNamespace
import unittest

from tests import test_visits_actor as fixture


class VisitRoutingContextTests(unittest.TestCase):
    def setUp(self):
        self.one = "bot-a:GroupMessage:1091576468"
        self.two = "bot-b:GroupMessage:932436510"
        self.other = "bot-a:GroupMessage:1061036726"
        self.private = "bot-a:FriendMessage:42"
        self.enabled = {self.one, self.two, self.other, self.private}
        self.owners = {self.one: self.one, self.two: self.one,
                       self.other: self.other, self.private: self.private}
        self.platforms = {key: SimpleNamespace(meta=lambda: SimpleNamespace(name="aiocqhttp"))
                          for key in ("bot-a", "bot-b")}
        engine = SimpleNamespace(
            scope_session=lambda scope: self.one if scope == "shared" else scope,
            state_key=lambda scope: self.owners.get(scope, scope),
            is_enabled=lambda scope: scope in self.enabled,
            config_scope=lambda scope: nullcontext(),
            group_sessions=lambda scope: [self.one, self.two] if scope == self.one else [scope])
        plugin = SimpleNamespace(engine=engine,
                                 context=SimpleNamespace(get_platform_inst=self.platforms.get))
        self.port = fixture.VisitActorPort(plugin)

    def test_shared_group_matches_actual_group_ids_without_adapter_instance_ids(self):
        expected = ["aiocqhttp:GroupMessage:1091576468", "aiocqhttp:GroupMessage:932436510"]
        self.assertEqual(self.port.routing_contexts("shared"), expected)
        self.assertEqual(self.port.routing_contexts(self.two), expected)
        self.assertEqual(self.port.routing_contexts(self.other), ["aiocqhttp:GroupMessage:1061036726"])
        self.assertEqual(self.port.routing_contexts(self.private), ["aiocqhttp:FriendMessage:42"])

    def test_disabled_removed_or_regrouped_member_is_not_advertised(self):
        self.enabled.remove(self.two)
        self.assertEqual(self.port.routing_contexts(self.one), ["aiocqhttp:GroupMessage:1091576468"])
        self.enabled.add(self.two)
        self.owners[self.two] = self.other
        self.assertEqual(self.port.routing_contexts(self.one), ["aiocqhttp:GroupMessage:1091576468"])
        self.enabled.remove(self.one)
        self.assertEqual(self.port.routing_contexts(self.one), [])

    def test_missing_platform_and_distinct_platform_families_cannot_match(self):
        self.platforms.pop("bot-b")
        self.assertEqual(self.port.routing_contexts(self.one), ["aiocqhttp:GroupMessage:1091576468"])
        self.platforms["bot-a"] = SimpleNamespace(meta=lambda: SimpleNamespace(name="discord"))
        self.assertEqual(self.port.routing_contexts(self.one), ["discord:GroupMessage:1091576468"])
        self.platforms.clear()
        self.assertEqual(self.port.routing_contexts(self.one), [])
