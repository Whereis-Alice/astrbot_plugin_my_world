"""Registered life routes use actual scoped stores and enforce provenance boundaries."""
import sys
import types
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from tests import test_scope_api as scope


class LifeApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = scope.ScopeApiTests.asyncSetUp

    async def call(self, endpoint, *, method="GET", **data):
        request = types.SimpleNamespace(method=method, query=data, json=AsyncMock(return_value=data))
        module = sys.modules[self.plugin.api_life.__module__]
        with patch.object(module, "request", request):
            return await self.routes[endpoint]()

    async def test_reading_life_does_not_rewrite_the_saved_decision(self):
        engine = self.plugin.engine
        saved = {"kind": "continue", "reason": "保留正在做的事", "at": 123, "matter_id": "lm_example"}
        async with engine.session_state(self.session) as state:
            state.life_decision = dict(saved)

        response = await self.call("life", session=self.session)

        self.assertEqual(response["status_code"], 200)
        self.assertIn("choice", response["data"])
        state = await engine.load_state(self.session, cold_start=False)
        self.assertEqual(state.life_decision, saved)

    async def test_create_pause_edit_and_manual_result_leave_history(self):
        result = await self.call("life/update", method="POST", session=self.session, operation="create", text="学一首歌")
        self.assertEqual(result["status_code"], 200)
        mid = result["data"]["item"]["id"]
        for operation, extras in [("pause", {}), ("resume", {}), ("edit", {"text":"慢慢学会另一首歌"}), ("complete", {"reason":"管理员听完演奏，确认已完成"})]:
            result = await self.call("life/update", method="POST", session=self.session, id=mid, operation=operation, **extras)
            self.assertEqual(result["status_code"], 200, result)
        result = (await self.call("life", session=self.session))["data"]
        self.assertEqual(result["items"][0]["status"], "completed")
        self.assertTrue(result["items"][0]["history"][-1]["manual"])

    async def test_private_promise_cannot_be_read_or_mutated_from_shared_group(self):
        other = "test:FriendMessage:42"
        self.plugin.store.add_session(other)
        self.plugin.engine.reload_config()
        # Share an actual life state without granting the other origin's private promise.
        with patch.object(self.plugin.engine, "state_key", return_value=self.session):
            async with self.plugin.engine.session_state(self.session) as state:
                item = self.plugin.engine.add_life_matter(state, "私下约定", who="42", session=other, kind="promise")
            data = (await self.call("life", session=self.session))["data"]
            self.assertEqual(data["items"], [])
            result = await self.call("life/update", method="POST", session=self.session, id=item["id"], operation="pause")
            self.assertEqual(result["status_code"], 400)

    async def test_evidence_endpoint_checks_memory_scope(self):
        mid = self.plugin.engine.memory.remember(session_id="test:FriendMessage:7", persona_id="", node_id="study", content="另一个会话的原话")
        result = await self.call("memories/evidence", session=self.session, id=mid)
        self.assertEqual(result["status_code"], 400)

    async def test_mutation_guard_is_used(self):
        self.plugin._guard = lambda payload=None: {"status_code":403}
        result = await self.call("life/update", method="POST", session=self.session, operation="create", text="不应写入")
        self.assertEqual(result["status_code"], 403)

    async def test_appointment_keeps_one_matter_and_is_visible_in_today(self):
        engine = self.plugin.engine
        response = await self.call("life/update", method="POST", session=self.session, operation="create", text="每天慢慢练一首曲子")
        item = response["data"]["item"]
        day = engine.local_now().date().isoformat()
        response = await self.call("life/update", method="POST", session=self.session, operation="appointment", id=item["id"],
            revision=item.get("revision", 0), date=day, time="23:59")
        self.assertEqual(response["status_code"], 200, response)
        data = (await self.call("life", session=self.session))["data"]
        self.assertEqual(len(data["items"]), 1)
        appointment = next(row for row in data["agenda"] if row["matter_id"] == item["id"])
        self.assertEqual(appointment["time"], "23:59")
        self.assertEqual(appointment["title"], item["text"])
        self.assertEqual((await self.call("life/update", method="POST", session=self.session, operation="appointment", id=item["id"],
            revision=-1, date=day, time="23:50"))["status_code"], 400)

    async def test_life_budget_uses_the_selected_world_calendar(self):
        engine = self.plugin.engine
        base = self.plugin.store.raw_world()
        base["timezone"] = "UTC"
        self.plugin.store.save_world(base)
        local = {**base, "timezone": "Asia/Shanghai"}
        self.plugin.store.save_scope(self.session, local, {"schedules": []}, ["settings"])
        engine.reload_config()
        stamp = datetime(2026, 10, 19, 16, 5, tzinfo=timezone.utc).timestamp()
        await self.plugin.db.call("kv_set", "background_budget:2026-10-19", {"day": "global"})
        await self.plugin.db.call("kv_set", "background_budget:2026-10-20", {"day": "local"})
        with patch.object(engine, "_now", return_value=stamp):
            data = (await self.call("life", session=self.session))["data"]
        self.assertEqual(data["budget"], {"day": "local"})
        self.assertEqual(engine.world.timezone, "UTC")
