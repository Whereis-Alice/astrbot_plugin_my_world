"""Explicit invitation direction and friend names must survive the tool handoff."""
from __future__ import annotations

import unittest

from tests import test_visit_tools as fixture


class VisitToolTargetTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixture.VisitToolTests.setUp
    invite = fixture.VisitToolTests.invite

    def friends(self):
        self.service.overview.return_value = {"peers": [
            {"id": "peer", "alias": "晴空", "name": "艾拉", "world": "晴空世界"},
            {"id": "leaf", "alias": "圣森", "name": "莉耶芙", "world": "圣森世界"},
        ], "active": None}

    async def test_explicit_come_over_cannot_turn_into_an_outgoing_visit(self):
        self.friends()
        self.event.get_message_str = lambda: "邀请艾拉来自己家坐坐"
        result = await self.invite(direction="outgoing", evidence="邀请艾拉来自己家坐坐")
        self.assertIn("方向", result)
        self.service.invite.assert_not_awaited()

    async def test_explicit_visit_cannot_turn_into_a_come_over_invitation(self):
        self.friends()
        self.event.get_message_str = lambda: "去晴空世界找艾拉玩一会儿"
        result = await self.invite(direction="incoming", evidence="去晴空世界找艾拉玩一会儿")
        self.assertIn("方向", result)
        self.service.invite.assert_not_awaited()

    async def test_named_friend_cannot_be_replaced_by_another_paired_friend(self):
        self.friends()
        for name in ("艾拉", "晴空", "晴空世界", "peer"):
            with self.subTest(name=name):
                text = f"去{name}家玩一会儿"
                self.event.get_message_str = lambda text=text: text
                result = await self.invite(peer_id="leaf", evidence=text)
                self.assertIn("友邻", result)
        self.service.invite.assert_not_awaited()

    async def test_short_evidence_must_keep_direction_and_name_in_its_user_clause(self):
        self.friends()
        self.event.get_message_str = lambda: "邀请艾拉来自己家串门"
        result = await self.invite(peer_id="leaf", direction="incoming", evidence="串门")
        self.assertIn("友邻", result)
        result = await self.invite(direction="outgoing", evidence="串门")
        self.assertIn("方向", result)
        self.service.invite.assert_not_awaited()

    async def test_matching_explicit_direction_and_identity_still_work(self):
        self.friends()
        self.event.get_message_str = lambda: "邀请艾拉来自己家坐坐"
        await self.invite(direction="incoming", evidence="邀请艾拉来自己家坐坐")
        self.service.invite.assert_awaited_once_with(
            "one-life", "peer", reason="一起下棋", direction="incoming")

    async def test_pronouns_and_unspecified_direction_leave_the_choice_to_the_model(self):
        self.friends()
        for direction in ("incoming", "outgoing"):
            self.event.get_message_str = lambda: "和她串门玩一会儿"
            await self.invite(direction=direction, evidence="和她串门玩一会儿")
        self.assertEqual(self.service.invite.await_count, 2)

    async def test_shared_character_name_is_not_treated_as_a_unique_identity(self):
        self.friends()
        for peer in self.service.overview.return_value["peers"]:
            peer["name"] = "爱丽丝"
        self.event.get_message_str = lambda: "去找爱丽丝玩一会儿"
        await self.invite(peer_id="leaf", evidence="去找爱丽丝玩一会儿")
        self.service.invite.assert_awaited_once()

    async def test_ascii_names_do_not_match_inside_unrelated_words(self):
        self.friends()
        self.service.overview.return_value["peers"][1]["name"] = "Ann"
        self.event.get_message_str = lambda: "go visit the annual festival"
        await self.invite(evidence="go visit the annual festival")
        self.service.invite.assert_awaited_once()

    async def test_world_name_can_disambiguate_shared_character_names(self):
        self.friends()
        for peer in self.service.overview.return_value["peers"]:
            peer["name"] = "爱丽丝"
        self.event.get_message_str = lambda: "去晴空世界找爱丽丝玩一会儿"
        result = await self.invite(peer_id="leaf", evidence="去晴空世界找爱丽丝玩一会儿")
        self.assertIn("友邻", result)
        self.service.invite.assert_not_awaited()

    async def test_explicit_own_home_or_world_is_incoming(self):
        self.friends()
        for text in ("请艾拉到自己家串门坐坐", "请艾拉到我的世界串门"):
            self.event.get_message_str = lambda text=text: text
            result = await self.invite(direction="outgoing", evidence=text)
            self.assertIn("方向", result)
        self.service.invite.assert_not_awaited()

    async def test_explicit_english_direction_is_preserved(self):
        self.friends()
        for text, direction in (("invite Isla over", "outgoing"),
                                ("go visit her world", "incoming")):
            self.event.get_message_str = lambda text=text: text
            result = await self.invite(direction=direction, evidence=text)
            self.assertIn("方向", result)
        self.service.invite.assert_not_awaited()

    async def test_world_switch_while_reading_friends_cannot_mutate_previous_life(self):
        self.friends()

        async def switched(scope):
            self.plugin.engine.state_key = lambda origin: "replacement-life"
            return {"peers": [{"id": "peer"}], "active": None}

        self.service.overview.side_effect = switched
        result = await self.invite()
        self.assertIn("变化", result)
        self.service.invite.assert_not_awaited()

    async def test_reload_while_reading_friends_cannot_issue_an_invitation(self):
        self.friends()

        async def retired(scope):
            self.plugin.retired = True
            return {"peers": [{"id": "peer"}], "active": None}

        self.service.overview.side_effect = retired
        await self.invite()
        self.service.invite.assert_not_awaited()

