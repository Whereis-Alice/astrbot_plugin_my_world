"""群名片归属随插件启停变化，黑名单与会话范围不能误判。"""

from types import SimpleNamespace as NS
import unittest

from core.plugin_compat import DYNAMIC_CARD_PLUGIN, group_card_owner

SESSION = "aiocqhttp:GroupMessage:123"


class CardOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.star = NS(
            name=DYNAMIC_CARD_PLUGIN, root_dir_name=DYNAMIC_CARD_PLUGIN,
            activated=True, star_cls=NS(config={"common": {"enabled": True}}),
        )
        self.stars = [self.star]
        self.context = NS(
            get_all_stars=lambda: self.stars,
            get_config=lambda _session: {"plugin_set": ["*"]},
        )

    def owner(self, session=SESSION, event=None):
        return group_card_owner(self.context, session, event)

    def test_loaded_plugin_owns_the_card(self):
        self.assertIn("灵动名片", self.owner())

    def test_late_start_disable_and_reload_are_not_cached(self):
        self.stars.clear()
        self.assertEqual(self.owner(), "")
        self.stars.append(self.star)
        self.assertTrue(self.owner())
        self.star.activated = False
        self.assertEqual(self.owner(), "")
        self.star.activated = True
        self.star.star_cls = None
        self.assertEqual(self.owner(), "")
        self.star.star_cls = NS(config={})
        self.assertTrue(self.owner())

    def test_config_off_and_legacy_config(self):
        self.star.star_cls.config = {"common": {"enabled": False}}
        self.assertEqual(self.owner(), "")
        self.star.star_cls.config = {"general": {"enabled": False}}
        self.assertEqual(self.owner(), "")

    def test_group_and_origin_blacklists(self):
        for field, value in (("blacklist_group_ids", 123), ("blacklist_unified_origins", SESSION)):
            self.star.star_cls.config = {"common": {field: [value]}}
            self.assertEqual(self.owner(), "")
            self.assertTrue(self.owner("aiocqhttp:GroupMessage:456"))

    def test_session_plugin_scope(self):
        self.context.get_config = lambda _session: {"plugin_set": ["astrbot_plugin_my_world"]}
        self.assertEqual(self.owner(), "")
        self.context.get_config = lambda _session: {"plugin_set": [DYNAMIC_CARD_PLUGIN]}
        self.assertTrue(self.owner())
        self.assertEqual(self.owner(event=NS(unified_msg_origin=SESSION, plugins_name=[])), "")

    def test_private_chat_and_another_groups_event(self):
        self.assertEqual(self.owner("aiocqhttp:PrivateMessage:123"), "")
        self.assertTrue(self.owner(event=NS(unified_msg_origin="other", plugins_name=[])))

    def test_missing_registry_does_not_break_standalone_install(self):
        self.assertEqual(group_card_owner(NS(), SESSION), "")


if __name__ == "__main__":
    unittest.main()
