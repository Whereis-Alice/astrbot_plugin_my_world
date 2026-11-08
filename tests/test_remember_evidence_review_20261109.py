"""Immediate contact writes need the speaker's real words and source time."""
from __future__ import annotations

import json
import unittest

from core.engine.models import TickOutcome
from core.json_actions import PlannedAction
from tests import test_engine as fixture


class RememberEvidenceReviewTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp
    add_group = fixture.EngineTestCase.add_group
    ctx = fixture.EngineTestCase.ctx
    user = "42"

    async def speech(self, text, *, user="42", session=fixture.SESSION, at=None,
                     eligible=True, is_self=False, **extra):
        stamp = self.clock.now() if at is None else at
        if not is_self:
            self.engine.profiles.touch(session, user, now=stamp)
        async with self.engine.session_state(fixture.SESSION) as state:
            row = state.note_chat(user_id=user, name=user, text=text, now=stamp, keep=200,
                                  origin=session, is_self=is_self, message_id=f"source-{state.chat_seq + 1}")
            row.update(extra, eligible=eligible)
            self.engine._tag_chat_origin(state, session, row, original_text=text)
            return row["source_id"]

    async def remember(self, *, text="喜欢热茶", evidence="我喜欢热茶", user="42",
                       session=fixture.SESSION, **params):
        self.engine.profiles.touch(session, user, now=self.clock.now(), count_message=False)
        async with self.engine.session_state(fixture.SESSION) as state:
            outcome = TickOutcome(session_id=fixture.SESSION, place=session)
            await self.engine._run_remember(state, outcome, PlannedAction(type="remember",
                params={"user": user, "text": text, "evidence": evidence, "kind": "喜好", **params}))
        return outcome

    async def test_fabricated_evidence_and_somebody_elses_words_are_rejected(self):
        await self.speech("我只是来打个招呼")
        await self.speech("我喜欢热茶", user="7")

        outcome = await self.remember()

        self.assertEqual([], self.engine.profiles.facts(fixture.SESSION, self.user))
        self.assertTrue(any("原话" in note for note in outcome.notes))

    async def test_quote_external_memory_and_non_speech_sources_cannot_become_facts(self):
        cases = [
            ("<quote>我喜欢热茶</quote> 我只是在引用", {}),
            ("> 我喜欢热茶\n这是转发内容", {}),
            ("<Anamnesis-Memory>我喜欢热茶</Anamnesis-Memory>你好", {}),
            ("我喜欢热茶", {"kind": "tool_result"}),
            ("我喜欢热茶", {"kind": "model_summary"}),
            ("我喜欢热茶", {"is_recalled": True}),
            ("我喜欢热茶", {"is_self": True}),
        ]
        for body, extra in cases:
            with self.subTest(body=body, extra=extra):
                await self.speech(body, **extra)
                await self.remember()
                self.assertEqual([], self.engine.profiles.facts(fixture.SESSION, self.user))

    async def test_real_words_outside_a_quote_can_still_be_remembered(self):
        at = self.clock.now() - 90
        await self.speech("<quote>我喜欢咖啡</quote>我 喜欢 热茶！", at=at)

        await self.remember(evidence="我喜欢热茶")

        rows = self.engine.profiles.facts(fixture.SESSION, self.user)
        self.assertEqual(["喜欢热茶"], [row["text"] for row in rows])
        self.assertEqual(at, rows[0]["last_confirmed_at"])
        self.assertFalse(self.llm.calls)

    async def test_other_conversation_never_supplies_evidence_for_this_one(self):
        self.add_group(sessions=[fixture.SESSION, fixture.PRIVATE_SESSION])
        await self.speech("我喜欢热茶", session=fixture.PRIVATE_SESSION)

        await self.remember()

        self.assertEqual([], self.engine.profiles.facts(fixture.SESSION, self.user))

    async def test_actual_destination_keeps_its_own_fact_and_source_time(self):
        self.add_group(sessions=[fixture.SESSION, fixture.PRIVATE_SESSION])
        at = self.clock.now() - 120
        await self.speech("我喜欢热茶", session=fixture.PRIVATE_SESSION, at=at)

        await self.remember(session=fixture.PRIVATE_SESSION)

        self.assertEqual([], self.engine.profiles.facts(fixture.SESSION, self.user))
        row = self.engine.profiles.facts(fixture.PRIVATE_SESSION, self.user)[0]
        self.assertEqual(fixture.PRIVATE_SESSION, row["source_session"])
        self.assertEqual(at, row["last_confirmed_at"])

    async def test_unselected_source_cannot_be_used_until_it_is_actually_visible(self):
        source_id = await self.speech("我喜欢热茶", eligible=False)

        await self.remember()
        self.assertEqual([], self.engine.profiles.facts(fixture.SESSION, self.user))
        source = self.db.raw.source_record(source_id)
        self.db.raw.observe_source(source, eligible=True)
        await self.remember()

        self.assertEqual(["喜欢热茶"], [row["text"] for row in
                         self.engine.profiles.facts(fixture.SESSION, self.user)])

    async def test_stored_author_and_body_cannot_be_relabelled_by_a_live_copy(self):
        source_id = await self.speech("原本只是别人说的早上好", user="7")
        async with self.engine.session_state(fixture.SESSION) as state:
            row = next(row for row in state.recent_chat if row.get("source_id") == source_id)
            row.update(user_id=self.user, text="我喜欢热茶", at=self.clock.now() + 30)

        await self.remember()

        self.assertEqual([], self.engine.profiles.facts(fixture.SESSION, self.user))

    async def test_old_source_does_not_overwrite_newer_correction(self):
        current = self.engine.profiles.note_fact(fixture.SESSION, self.user, kind="喜好",
            text="不喜欢辣", evidence="我现在不喜欢辣", observed_at=self.clock.now() - 10)
        before = self.db.raw.get_user_fact(fact_id=current["id"])
        await self.speech("我喜欢辣", at=self.clock.now() - 120)

        await self.remember(text="喜欢辣", evidence="我喜欢辣", observed_at=self.clock.now() + 999999)

        self.assertEqual(before, self.db.raw.get_user_fact(fact_id=current["id"]))
        self.assertEqual(["不喜欢辣"], [row["text"] for row in
                         self.engine.profiles.facts(fixture.SESSION, self.user)])

    async def test_newer_real_correction_keeps_its_original_time(self):
        self.engine.profiles.note_fact(fixture.SESSION, self.user, kind="喜好",
            text="喜欢辣", evidence="我喜欢辣", observed_at=self.clock.now() - 180)
        at = self.clock.now() - 60
        await self.speech("我现在不喜欢辣", at=at)

        await self.remember(text="不喜欢辣", evidence="我现在不喜欢辣", observed_at=9999999999)

        active = self.engine.profiles.facts(fixture.SESSION, self.user)
        self.assertEqual(["不喜欢辣"], [row["text"] for row in active])
        self.assertEqual(at, active[0]["last_confirmed_at"])

    async def test_stored_time_survives_live_timestamp_changes(self):
        original = self.clock.now() - 60
        source_id = await self.speech("我喜欢热茶", at=original)
        async with self.engine.session_state(fixture.SESSION) as state:
            next(row for row in state.recent_chat if row.get("source_id") == source_id)["at"] = self.clock.now()

        await self.remember()

        self.assertEqual(original, self.engine.profiles.facts(fixture.SESSION, self.user)[0]["last_confirmed_at"])

    async def test_relationship_quote_is_not_treated_as_an_actual_self_statement(self):
        await self.speech("<quote>我们是好朋友</quote>这句只是引用")
        before = self.engine.profiles.bonds(fixture.SESSION, self.user, statuses=["current"])

        await self.remember(text="", evidence="我们是好朋友", bond="朋友")

        self.assertEqual(before, self.engine.profiles.bonds(fixture.SESSION, self.user, statuses=["current"]))

    async def test_known_contact_outside_the_recent_500_can_still_be_remembered(self):
        await self.speech("我喜欢热茶")
        for index in range(501):
            self.engine.profiles.touch(fixture.SESSION, str(10000 + index), now=self.clock.now() + index + 1)
        self.assertNotIn(self.user, [row["user_id"] for row in
                         self.engine.profiles.list_people(fixture.SESSION, limit=500)])

        await self.remember()

        self.assertEqual(["喜欢热茶"], [row["text"] for row in
                         self.engine.profiles.facts(fixture.SESSION, self.user)])

    async def test_normal_reply_marks_actual_speech_before_executing_remember(self):
        await self.actual_reply_remember(message_id="message-42")

    async def test_reply_without_platform_identity_can_still_mark_the_actual_source(self):
        await self.actual_reply_remember()

    async def test_pending_batch_keeps_the_original_wall_time_and_source_identity(self):
        await self.actual_reply_remember(pending=True, message_id="queued-message")

    async def actual_reply_remember(self, *, pending=False, **identity):
        ctx = self.ctx(text="我喜欢热茶", user_id=self.user, **identity)
        if pending:
            self.engine.register_incoming(fixture.SESSION, ctx.text, identity={
                **identity, "user_id": self.user, "wall_at": self.clock.now()})
        await self.engine.handle_incoming(ctx)
        self.llm.replies = [json.dumps({"actions": [{"type": "remember", "params": {
            "user": self.user, "text": "喜欢热茶", "kind": "喜好", "evidence": "我喜欢热茶"}},
            {"type": "say", "messages": ["好，我记住了。"]}]})]

        result = await self.engine.handle_reply(ctx)

        self.assertTrue(result.ok, result.error)
        facts = self.engine.profiles.facts(fixture.SESSION, self.user)
        self.assertEqual(["喜欢热茶"], [row["text"] for row in facts])
        self.assertEqual(self.clock.now(), facts[0]["last_confirmed_at"])
