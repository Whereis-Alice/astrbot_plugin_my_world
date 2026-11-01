"""Editor controls enforce authentication, real life scopes and credential isolation."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import types
import unittest
from unittest.mock import AsyncMock, patch

from tests import fake_astrbot

fake_astrbot.install()
spec = importlib.util.spec_from_file_location("visits_editor_under_test", Path(__file__).parents[1] / "web" / "visits_api.py")
assert spec and spec.loader
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)


class VisitEditorApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = types.SimpleNamespace(**{name: AsyncMock(return_value={"ok": True}) for name in
            ("global_overview", "create_pair_code", "pair_world", "remove_connection", "set_enabled_scopes",
             "refresh_connection", "invite", "return_home")})
        self.service.global_overview.return_value = {
            "enabled": True, "peers": [{"id": "friend", "secret": "hidden"}],
            "connections": [{"id": "server", "actors": [{"id": "friend", "private_key": "hidden"}]}],
            "credentials": {"key": "hidden"}, "active": None}
        self.service.create_pair_code.return_value = {"code": "one-use-code", "expires_at": 123}
        context = types.SimpleNamespace(registered_web_apis=[])
        context.register_web_api = lambda *row: context.registered_web_apis.append(row)
        self.plugin = types.SimpleNamespace(retired=False, visits=self.service, context=context,
            auth=types.SimpleNamespace(check=lambda token: token == "editor-login"),
            engine=types.SimpleNamespace(scope_session=lambda value: "main" if value in {"group", "shared-life"} else value,
                                         is_enabled=lambda value: value == "main",
                                         state_key=lambda value: "shared-life"))
        api.register_visit_apis(self.plugin, "plugin")
        self.routes = {row[0].split("/plugin/", 1)[1]: row[1] for row in context.registered_web_apis}

    async def call(self, endpoint, *, payload=None, authenticated=True, **values):
        data = {"session": "group", **values}
        if authenticated:
            data["token"] = "editor-login"
        if payload is not None:
            data = payload
        request = types.SimpleNamespace(query=data, json=AsyncMock(return_value=data))
        with patch.object(api, "request", request):
            return await self.routes[endpoint]()

    async def test_all_read_and_write_routes_require_editor_auth(self):
        for endpoint in self.routes:
            with self.subTest(endpoint=endpoint):
                result = await self.call(endpoint, authenticated=False)
                self.assertEqual(result["status_code"], 401)
        for method in vars(self.service).values():
            method.assert_not_awaited()

    async def test_group_scope_resolves_to_one_enabled_character(self):
        result = await self.call("visits")
        self.assertEqual(result["status_code"], 200)
        self.service.global_overview.assert_awaited_once_with("shared-life")
        self.assertNotIn("secret", str(result))
        self.assertNotIn("credentials", str(result))
        for invalid in ("", "other", 123, [], None):
            result = await self.call("visits/invite", session=invalid, peer_id="friend")
            self.assertEqual(result["status_code"], 400)
        self.service.invite.assert_not_awaited()

    async def test_invitation_direction_and_reason_are_explicit(self):
        result = await self.call("visits/invite", peer_id="friend", direction="incoming", reason="一起下棋")
        self.assertEqual(result["status_code"], 200)
        self.service.invite.assert_awaited_once_with("shared-life", "friend", direction="incoming",
                                                    reason="一起下棋", automatic=False)
        self.service.invite.reset_mock()
        for bad in ("sideways", [], 12):
            result = await self.call("visits/invite", peer_id="friend", direction=bad)
            self.assertEqual(result["status_code"], 400)
        self.service.invite.assert_not_awaited()

    async def test_only_pair_code_endpoint_returns_pairing_credential(self):
        result = await self.call("visits/pair-code")
        self.assertEqual(result["data"]["code"], "one-use-code")
        self.service.create_pair_code.assert_awaited_once_with()
        self.service.pair_world.return_value = {"id": "friend", "secret": "hidden", "code": "one-use-code"}
        result = await self.call("visits/pair", url="https://friend.example", code="one-use-code", alias="晴空")
        self.assertEqual(result["data"], {"id": "friend"})
        self.service.pair_world.assert_awaited_once_with("https://friend.example", "one-use-code", "晴空")

    async def test_service_error_does_not_echo_supplied_code(self):
        self.service.pair_world.side_effect = ValueError("expired one-use-code")
        result = await self.call("visits/pair", url="https://friend.example", code="one-use-code")
        self.assertEqual(result["status_code"], 400)
        self.assertNotIn("one-use-code", str(result))

    async def test_retired_plugin_and_bad_payload_have_no_side_effect(self):
        result = await self.call("visits/pair", payload=["not an object"])
        self.assertEqual(result["status_code"], 400)
        self.plugin.retired = True
        result = await self.call("visits/return")
        self.assertEqual(result["status_code"], 503)
        self.service.return_home.assert_not_awaited()
        self.service.pair_world.assert_not_awaited()

    async def test_handlers_bind_their_own_operations_and_are_owned(self):
        self.assertEqual(len(self.routes), 8)
        self.assertEqual(len(self.plugin._web_api_handlers), 8)
        await self.call("visits/remove", connection_id="server")
        await self.call("visits/return")
        self.service.remove_connection.assert_awaited_once_with("server")
        self.service.return_home.assert_awaited_once_with("shared-life")

    async def test_global_directory_and_pairing_do_not_require_a_selected_life(self):
        payload = {"token": "editor-login"}
        result = await self.call("visits", payload=payload)
        self.assertEqual(result["status_code"], 200)
        self.service.global_overview.assert_awaited_once_with("")
        self.assertNotIn("private_key", str(result))
        for endpoint, values in (
            ("visits/pair-code", {}),
            ("visits/pair", {"url": "https://friend.example", "code": "one-use-code"}),
            ("visits/remove", {"connection_id": "server"}),
            ("visits/refresh", {"connection_id": "server"}),
        ):
            with self.subTest(endpoint=endpoint):
                result = await self.call(endpoint, payload={**payload, **values})
                self.assertEqual(result["status_code"], 200)
        self.service.create_pair_code.assert_awaited_once_with()
        self.service.remove_connection.assert_awaited_once_with("server")
        self.service.refresh_connection.assert_awaited_once_with("server")
        result = await self.call("visits/invite", payload={**payload, "peer_id": "friend"})
        self.assertEqual(result["status_code"], 400)
        self.service.invite.assert_not_awaited()

    async def test_participating_lives_are_validated_and_shared_life_is_selected_once(self):
        result = await self.call("visits/scopes", sessions=["group", "main", "shared-life"])
        self.assertEqual(result["status_code"], 200)
        self.service.set_enabled_scopes.assert_awaited_once_with(["shared-life"])
        self.service.set_enabled_scopes.reset_mock()
        for values in (None, "group", {}, ["other"], [""], [123], [None], ["group"] * 257):
            with self.subTest(values=values):
                result = await self.call("visits/scopes", sessions=values)
                self.assertEqual(result["status_code"], 400)
        self.service.set_enabled_scopes.assert_not_awaited()
        result = await self.call("visits/scopes", sessions=[])
        self.assertEqual(result["status_code"], 200)
        self.service.set_enabled_scopes.assert_awaited_once_with([])

    async def test_refresh_and_remove_require_a_connection_and_strip_credentials(self):
        for endpoint, method in (("visits/refresh", self.service.refresh_connection),
                                 ("visits/remove", self.service.remove_connection)):
            with self.subTest(endpoint=endpoint):
                for value in (None, [], "", "x" * 201):
                    result = await self.call(endpoint, connection_id=value)
                    self.assertEqual(result["status_code"], 400)
                method.assert_not_awaited()
                method.return_value = {"id": "server", "pair_secret": "hidden"}
                result = await self.call(endpoint, connection_id="server")
                self.assertEqual(result["data"], {"id": "server"})

    async def test_retirement_while_request_body_is_read_cannot_modify_the_world(self):
        async def retired_body(**kwargs):
            self.plugin.retired = True
            return {"session": "group", "token": "editor-login", "peer_id": "friend"}
        request = types.SimpleNamespace(json=retired_body)
        with patch.object(api, "request", request):
            result = await self.routes["visits/invite"]()
        self.assertEqual(result["status_code"], 503)
        self.service.invite.assert_not_awaited()

    async def test_disabled_plugin_does_not_issue_pairing_credentials(self):
        self.plugin.enabled = False
        result = await self.call("visits/pair-code")
        self.assertEqual(result["status_code"], 503)
        self.service.create_pair_code.assert_not_awaited()
