"""The registered editor routes preserve emotion origins and reject stale writes."""
import unittest

from tests import test_scope_api as scope


class EmotionRecordApiTests(unittest.IsolatedAsyncioTestCase):
    call = scope.ScopeApiTests.call

    async def asyncSetUp(self):
        await scope.ScopeApiTests.asyncSetUp(self)
        self.private = "test:FriendMessage:42"
        self.outside = "test:GroupMessage:200"
        self.group = "shared-life"
        store = self.plugin.store
        store.add_session(self.private, session_type="private")
        store.add_session(self.outside)
        sessions = store.raw_sessions()
        sessions["groups"] = [
            {"id": self.group, "sessions": [self.session, self.private], "main_session": self.session}
        ]
        store.save_sessions(sessions)
        engine = self.plugin.engine
        engine.reload_config()
        async with engine.session_state(self.session) as state:
            engine.note_heart_knot(state, "还在想风车的事", session_id=self.session)
            now = engine._now()
            state.heart_knots.append({"text": "旧人设留的心事", "since": now, "at": now,
                "until": now + 3600, "strength": .7, "session": self.session, "persona_key": "earlier-persona"})
        async with engine.session_state(self.private) as state:
            engine.note_grudge(state, "约好的事迟迟没说清楚", user_id="42", session_id=self.private, force=True)
        async with engine.session_state(self.outside) as state:
            engine.note_heart_knot(state, "邻居的花该浇水了", session_id=self.outside)

    async def read(self, session, **params):
        result = await self.call("thoughts/emotions", session=session, **params)
        self.assertEqual(result["status_code"], 200, result)
        return result["data"]["items"]

    async def update(self, session, record, operation, **params):
        data = {"session": session, "source_session": record["session"], "id": record["id"],
                "revision": record["revision"], "operation": operation, **params}
        return await self.call("thoughts/emotions/update", method="POST", **data)

    async def test_group_and_origin_lists_respect_selection_without_rewriting_state(self):
        db = self.plugin.db
        before = await db.call("get_state", self.session)
        grouped = await self.read(self.group, config_scope=self.outside)
        self.assertEqual({item["text"] for item in grouped}, {"还在想风车的事", "旧人设留的心事", "约好的事迟迟没说清楚"})
        old = next(item for item in grouped if item["text"] == "旧人设留的心事")
        self.assertFalse(old["current_persona"])
        self.assertFalse(old["affects_dialogue"])
        self.assertTrue(any(item["affects_dialogue"] for item in grouped))
        self.assertEqual([item["session"] for item in await self.read(self.private)], [self.private])
        self.assertEqual([item["session"] for item in await self.read(self.outside)], [self.outside])
        self.assertEqual(await db.call("get_state", self.session), before)

    async def test_operation_uses_exact_origin_and_revision(self):
        original = (await self.read(self.private))[0]
        self.assertEqual((await self.update(self.outside, original, "delete"))["status_code"], 400)
        self.assertEqual((await self.update(self.session, original, "delete", source_session=self.session))["status_code"], 400)
        result = await self.update(self.group, original, "edit", text="还想和你把那件事说开")
        self.assertEqual(result["status_code"], 200, result)
        edited = result["data"]["item"]
        self.assertEqual(edited["text"], "还想和你把那件事说开")
        self.assertEqual((await self.update(self.group, original, "delete"))["status_code"], 409)
        result = await self.update(self.group, edited, "pause")
        self.assertEqual(result["status_code"], 200, result)
        paused = (await self.read(self.private))[0]
        self.assertEqual(paused["status"], "paused")
        self.assertFalse(paused["affects_dialogue"])
        result = await self.update(self.private, paused, "resolve")
        self.assertEqual(result["status_code"], 200, result)
        archived = (await self.read(self.private))[0]
        self.assertTrue(archived["archived"])
        self.assertEqual(archived["status"], "resolved")
        result = await self.update(self.group, archived, "delete")
        self.assertEqual(result["status_code"], 200, result)
        self.assertEqual(await self.read(self.private), [])
        self.assertEqual(len(await self.read(self.session)), 2)

    async def test_requires_editor_token_when_password_is_configured(self):
        record = (await self.read(self.private))[0]
        del self.plugin._guard
        self.plugin.auth.config_password = "fixture-password"
        result = await self.call("thoughts/emotions", session=self.private)
        self.assertEqual(result["status_code"], 401)
        result = await self.update(self.private, record, "delete")
        self.assertEqual(result["status_code"], 401)
        token = self.plugin.auth.issue_token()
        self.assertEqual(await self.read(self.private, token=token), [record])

    async def test_contact_editor_updates_the_same_active_record(self):
        before = (await self.read(self.private))[0]
        payload = {"session": self.private, "user_id": "42", "action": "add", "revision": before["revision"]}
        changed = await self.call("profile/grudge", method="POST", reason="想清楚了再聊聊", **payload)
        self.assertEqual(changed["status_code"], 200, changed)
        after = (await self.read(self.private))[0]
        self.assertEqual(after["id"], before["id"])
        self.assertEqual(after["expires_at"], before["expires_at"])
        self.assertEqual(after["text"], "想清楚了再聊聊")
        stale = await self.call("profile/grudge", method="POST", reason="试图覆盖新的内容", **payload)
        self.assertEqual(stale["status_code"], 409, stale)
