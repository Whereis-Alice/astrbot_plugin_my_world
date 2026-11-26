"""The registered editor routes preserve emotion origins and reject stale writes."""
import asyncio
import unittest
from unittest.mock import patch

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

    async def delete_selected(self, session, *items, **params):
        entries = [{"source_session": item["session"], "id": item["id"], "revision": item["revision"]}
                   for item in items]
        return await self.call("thoughts/emotions/delete-batch", method="POST", session=session,
            items=entries, **params)

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

    async def test_group_read_rejects_snapshot_when_group_changes_mid_request(self):
        engine = self.plugin.engine
        original = engine._journal_persona
        reading_other_member = asyncio.Event()
        release = asyncio.Event()

        async def read_persona(origin):
            if origin == self.private:
                reading_other_member.set()
                await release.wait()
            return await original(origin)

        with patch.object(engine, "_journal_persona", side_effect=read_persona):
            result = asyncio.create_task(self.call("thoughts/emotions", session=self.group))
            try:
                await asyncio.wait_for(reading_other_member.wait(), 5)
                sessions = self.plugin.store.raw_sessions()
                sessions["groups"][0]["sessions"].remove(self.private)
                self.plugin.store.save_sessions(sessions)
                engine.reload_config()
            finally:
                release.set()
            response = await asyncio.wait_for(result, 5)
        self.assertEqual(response["status_code"], 409, response)
        self.assertNotIn("约好的事迟迟没说清楚", str(response))
        self.assertEqual({row["session"] for row in await self.read(self.group)}, {self.session})

    async def test_old_quote_is_displayed_only_after_a_unique_origin_and_author_match(self):
        engine = self.plugin.engine
        now = engine._now()
        quote = "我把约好要送你的纸风车弄坏了"
        subject = "我还在惦记那只纸风车"
        async with engine.session_state(self.session) as state:
            engine.note_heart_knot(state, subject, about="42", session_id=self.session, user_name="小满")
            state.note_chat(user_id="43", name="小满", text=quote, origin=self.session, now=now - 1)
            state.note_chat(user_id="42", name="小满", text=quote, origin=self.private, now=now - 1)
        detail = {"at": now, "sent_at": now, "heart_knot": subject, "user": "小满", "user_text": quote}
        engine.db.raw.add_event(session_id=self.session, event_type="reflection", detail=detail)
        original = next(row for row in await self.read(self.session) if row["text"] == subject)
        self.assertEqual(original["evidence"], [])
        async with engine.session_state(self.session) as state:
            state.note_chat(user_id="42", name="小满", text=quote, origin=self.session, now=now - 1,
                            message_id="paper-windmill")
        recovered = next(row for row in await self.read(self.session) if row["text"] == subject)
        self.assertEqual(recovered["evidence"], [{"text": quote, "at": now - 1, "user_name": "小满", "recovered": True}])
        self.assertEqual(recovered["revision"], original["revision"])
        engine.db.raw.add_event(session_id=self.session, event_type="reflection", detail=detail)
        ambiguous = next(row for row in await self.read(self.session) if row["text"] == subject)
        self.assertEqual(ambiguous["evidence"], [])

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

    async def test_narrative_round_trips_through_archiving_and_legacy_edits(self):
        before = (await self.read(self.private))[0]
        self.assertEqual(before["narrative"], "")
        self.assertEqual(before["evidence"], [])
        narrative = "我在约好的地方等了好一阵。\n我想知道下次能不能提前告诉我。"
        changed = await self.update(self.private, before, "edit", text=before["text"], narrative=narrative)
        self.assertEqual(changed["status_code"], 200, changed)
        current = (await self.read(self.private))[0]
        self.assertEqual(current["narrative"], narrative)
        self.assertNotEqual(current["revision"], before["revision"])
        archived = await self.update(self.private, current, "resolve")
        self.assertEqual(archived["status_code"], 200, archived)
        archived = (await self.read(self.private))[0]
        changed = await self.update(self.private, archived, "edit", text="已经说开了")
        self.assertEqual(changed["status_code"], 200, changed)
        self.assertEqual((await self.read(self.private))[0]["narrative"], narrative)

    async def test_batch_delete_validates_all_origins_and_revisions_atomically(self):
        talk = (await self.read(self.private))[0]
        other = (await self.read(self.outside))[0]
        heart = next(item for item in await self.read(self.session) if item["current_persona"])
        result = await self.update(self.session, heart, "resolve")
        self.assertEqual(result["status_code"], 200, result)
        archived = next(item for item in await self.read(self.session) if item["archived"])

        before = await self.read(self.group)
        wrong_scope = await self.delete_selected(self.group, archived, other)
        self.assertEqual(wrong_scope["status_code"], 400, wrong_scope)
        outdated = await self.delete_selected(self.group, archived, {**talk, "revision": "stale"})
        self.assertEqual(outdated["status_code"], 409, outdated)
        duplicate = await self.delete_selected(self.group, archived, archived)
        self.assertEqual(duplicate["status_code"], 400, duplicate)
        self.assertEqual(await self.read(self.group), before)

        deleted = await self.delete_selected(self.group, archived, talk)
        self.assertEqual(deleted["status_code"], 200, deleted)
        self.assertEqual(deleted["data"]["removed"], 2)
        self.assertEqual([item["text"] for item in await self.read(self.group)], ["旧人设留的心事"])
        self.assertEqual(await self.read(self.outside), [other])

    async def test_batch_delete_requires_editor_token(self):
        record = (await self.read(self.private))[0]
        del self.plugin._guard
        self.plugin.auth.config_password = "fixture-password"
        denied = await self.delete_selected(self.group, record)
        self.assertEqual(denied["status_code"], 401, denied)
        token = self.plugin.auth.issue_token()
        self.assertEqual(await self.read(self.private, token=token), [record])
