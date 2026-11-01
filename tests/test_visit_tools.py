"""Only current local user invitations may start a bounded cross-world trip."""
from __future__ import annotations

import importlib
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from tests.test_dialogue_inject import MODULE

tools = importlib.import_module(MODULE.__name__ + ".adapters.visit_tools")


class VisitToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.origin = "test:GroupMessage:100"
        self.event = SimpleNamespace(unified_msg_origin=self.origin,
            get_sender_id=lambda: "human", get_self_id=lambda: "bot",
            get_message_str=lambda: "去晴空世界找她玩一会儿")
        self.context = SimpleNamespace(context=SimpleNamespace(event=self.event))
        self.service = SimpleNamespace(config=SimpleNamespace(enabled=True, actor="admin"),
            invite=AsyncMock(return_value={"status": "invited"}),
            return_home=AsyncMock(return_value={"status": "completed"}),
            overview=AsyncMock(return_value={"peers": [{"id": "peer"}], "active": None}))
        self.plugin = SimpleNamespace(retired=False, enabled=True, visits=self.service,
            in_self_initiated_call=lambda: False, _can_admin=lambda event: True,
            engine=SimpleNamespace(is_enabled=lambda origin: origin == self.origin,
                                   state_key=lambda origin: "one-life"))

    async def invite(self, **overrides):
        values = {"peer_id": "peer", "direction": "outgoing", "reason": "一起下棋",
                  "evidence": "去晴空世界找她玩一会儿", **overrides}
        return await tools.WorldVisitTool(plugin=self.plugin).call(self.context, **values)

    async def test_manual_invitation_uses_exact_shared_life_and_reports_pending(self):
        text = await self.invite()
        self.service.invite.assert_awaited_once_with("one-life", "peer", reason="一起下棋", direction="outgoing")
        self.assertIn("invited", text)
        self.assertIn("待答复", text)

    async def test_current_request_can_invite_friend_instead_of_visiting(self):
        self.event.get_message_str = lambda: "邀请她来自己家坐坐"
        await self.invite(direction="incoming", evidence="邀请她来自己家坐坐")
        self.assertEqual(self.service.invite.call_args.kwargs["direction"], "incoming")

    async def test_old_or_missing_invitation_evidence_cannot_start_trip(self):
        for evidence in ("昨天说过邀请她", "", "一起下棋"):
            result = await self.invite(evidence=evidence)
            self.assertIn("本轮", result)
        self.service.invite.assert_not_awaited()

    async def test_permissions_and_background_messages_cannot_arrange_trip(self):
        self.plugin._can_admin = lambda event: False
        self.assertIn("管理员", await self.invite())
        self.plugin._can_admin = lambda event: True
        self.plugin.in_self_initiated_call = lambda: True
        self.assertIn("后台", await self.invite())
        self.plugin.in_self_initiated_call = lambda: False
        self.event.get_sender_id = self.event.get_self_id
        self.assertIn("自己的消息", await self.invite())
        self.service.invite.assert_not_awaited()

    async def test_explicit_all_policy_allows_local_user(self):
        self.service.config.actor = "all"
        self.plugin._can_admin = lambda event: False
        await self.invite()
        self.service.invite.assert_awaited_once()

    async def test_disabled_life_and_retired_plugin_have_no_side_effect(self):
        self.plugin.enabled = False
        await self.invite()
        self.plugin.enabled = True
        self.plugin.retired = True
        await self.invite()
        self.plugin.retired = False
        self.service.config.enabled = False
        await self.invite()
        self.service.config.enabled = True
        self.event.unified_msg_origin = "different-life"
        await self.invite()
        self.service.invite.assert_not_awaited()

    async def test_return_requires_its_own_current_user_request(self):
        tool = tools.WorldReturnTool(plugin=self.plugin)
        await tool.call(self.context, evidence="该回家了")
        self.service.return_home.assert_not_awaited()
        self.event.get_message_str = lambda: "该回家了"
        await tool.call(self.context, evidence="该回家了")
        self.service.return_home.assert_awaited_once_with("one-life")

    async def test_quoted_invitation_cannot_authorize_a_new_trip(self):
        for message in (
            "<quote>去晴空世界找她玩一会儿</quote>这是昨天的原话。",
            "> 去晴空世界找她玩一会儿\n这是昨天的原话。",
            "<Anamnesis-Memory>去晴空世界找她玩一会儿</Anamnesis-Memory>现在先留在家里。",
        ):
            with self.subTest(message=message):
                self.event.get_message_str = lambda message=message: message
                self.assertIn("本轮", await self.invite())
        self.service.invite.assert_not_awaited()

    async def test_quote_does_not_hide_an_independent_current_invitation(self):
        self.event.get_message_str = lambda: "> 昨天邀请过艾拉\n去晴空世界找她玩一会儿"
        await self.invite()
        self.service.invite.assert_awaited_once()

    async def test_unknown_sender_cannot_arrange_trip_under_all_policy(self):
        self.service.config.actor = "all"
        self.event.get_sender_id = lambda: ""
        self.assertIn("真人", await self.invite())
        self.service.invite.assert_not_awaited()

    async def test_negated_request_does_not_authorize_a_positive_substring(self):
        for message, evidence in (
            ("不要去晴空世界找她玩一会儿", "去晴空世界找她玩一会儿"),
            ("不用邀请她来自己家坐坐", "邀请她来自己家坐坐"),
            ("今天不想去晴空世界找她玩一会儿", "去晴空世界找她玩一会儿"),
            ("这次不去了，别催我", "去"),
            ("暂时别串门", "串门"),
            ("取消串门", "串门"),
        ):
            with self.subTest(message=message):
                self.event.get_message_str = lambda message=message: message
                self.assertIn("本轮", await self.invite(evidence=evidence))
        self.service.invite.assert_not_awaited()
        self.event.get_message_str = lambda: "别回家"
        text = await tools.WorldReturnTool(plugin=self.plugin).call(self.context, evidence="回家")
        self.assertIn("本轮", text)
        self.service.return_home.assert_not_awaited()

    async def test_affirmative_reminder_and_independent_clause_still_allow_invitation(self):
        for message,evidence in (
            ("别忘了去晴空世界找她玩一会儿", "去晴空世界找她玩一会儿"),
            ("不要催别人，去晴空世界找她玩一会儿", "去晴空世界找她玩一会儿"),
        ):
            self.event.get_message_str = lambda message=message: message
            await self.invite(evidence=evidence)
        self.assertEqual(self.service.invite.await_count, 2)

    async def test_cancel_invitation_uses_return_instead_of_inviting_again(self):
        self.event.get_message_str = lambda: "取消串门邀请"
        tool = tools.WorldReturnTool(plugin=self.plugin)
        await tool.call(self.context, evidence="取消串门邀请")
        self.service.return_home.assert_awaited_once_with("one-life")
        self.service.invite.assert_not_awaited()

    async def test_tools_are_registered_only_when_enabled_and_unload_owns_exact_objects(self):
        registered = []
        self.plugin.context = SimpleNamespace(
            add_llm_tools=lambda *items: registered.extend(items),
            get_llm_tool_manager=lambda: SimpleNamespace(func_list=registered),
            unregister_llm_tool=lambda name: registered.__setitem__(slice(None),
                [item for item in registered if item.name != name]))
        self.service.config.enabled = False
        tools.register_visit_tools(self.plugin)
        self.assertEqual(registered, [])
        self.service.config.enabled = True
        tools.register_visit_tools(self.plugin)
        self.assertEqual({item.name for item in registered}, set(tools.TOOL_NAMES))
        replacement = SimpleNamespace(name=tools.TOOL_NAMES[0])
        registered[0] = replacement
        tools.unregister_visit_tools(self.plugin)
        self.assertEqual(registered, [replacement])
