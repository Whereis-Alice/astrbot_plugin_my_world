"""Automatic forgiveness is scoped to one incident and a fresh repair message."""

import json
import unittest
from unittest.mock import AsyncMock

from tests import test_engine as fixture


SESSION = fixture.SESSION
USER = "42"


class GrudgeForgivenessTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    async def seed(self, text="他折坏了我的纸风车"):
        async with self.engine.session_state(SESSION) as state:
            self.assertTrue(self.engine.note_grudge(state, text, user_id=USER, force=True))
            return self.engine.grudge_for(state, USER)["id"]

    async def status(self, record_id):
        async with self.engine.session_state(SESSION) as state:
            return next(row["status"] for row in self.engine.emotion_records(state, SESSION) if row["id"] == record_id)

    async def reflect(self, text, record_id, *, message_id="one", quote=None, at=None,
                      addressing="me", legacy=False, reply_to=None, help_repair=False):
        entry = {"user_id": USER, "text": text, "message_id": message_id,
                 "at": self.clock.now() if at is None else at, "reply_to": reply_to or {}}
        [source] = self.engine.interaction_sources(SESSION, USER, text, [entry])
        payload = {"addressing": addressing, "forgive": True if legacy else {
            "record_id": record_id, "kind": "apology", "source_id": source["id"], "quote": quote or text,
        }}
        if help_repair:
            payload["interaction"] = {"kind": "repair", "source_id": source["id"], "quote": quote or text,
                                      "reason": "他修好了弄坏的风车并道歉"}
        self.engine._ask_judge = AsyncMock(return_value=json.dumps(payload, ensure_ascii=False))
        return await self.engine.reflect_turn(SESSION, user_id=USER, user_text=text, reply_text="嗯。",
                                              interaction_entries=[entry])

    async def test_topic_or_boolean_alone_cannot_resolve_an_active_record(self):
        record = await self.seed()
        await self.reflect("你还在意上次折坏纸风车的事吗？", record, legacy=True)
        self.assertIn(record, self.engine._ask_judge.call_args.args[2])
        self.assertEqual(await self.status(record), "active")

    async def test_forgiveness_requires_current_record_direct_quote_and_known_target(self):
        record = await self.seed()
        apology = "对不起，我不该把你的纸风车折坏。"
        invalid = (
            (apology, "other-record", {}),
            (f"他说‘{apology}’，我只是传个话。", record, {"quote": apology}),
            (apology, record, {"reply_to": {"is_self": False}}),
            (apology, record, {"addressing": "unclear"}),
            (apology, record, {"at": self.clock.now() - 60}),
        )
        for index, (text, requested, details) in enumerate(invalid):
            with self.subTest(details=details, requested=requested):
                await self.reflect(text, requested, message_id=str(index), **details)
                self.assertEqual(await self.status(record), "active")

    async def test_real_repair_can_score_and_resolve_but_same_message_cannot_resolve_a_new_record(self):
        self.engine.profiles.touch(SESSION, USER)
        record = await self.seed()
        apology = "对不起，我把折坏的纸风车修好了，以后借之前会先问你。"
        result = await self.reflect(apology, record, help_repair=True)
        self.assertAlmostEqual(result["affinity_delta"], .20)
        self.assertEqual(await self.status(record), "resolved")
        self.clock.advance(60)
        later = await self.seed("他又折坏了我的纸风车")
        # A redelivery can arrive again at a new timestamp but keeps its platform ID.
        result = await self.reflect(apology, later, help_repair=True)
        self.assertEqual(result["affinity_delta"], 0)
        self.assertEqual(await self.status(later), "active")

    async def test_another_persona_cannot_clear_the_original_characters_record(self):
        record = await self.seed()
        self.persona.text = "另一位园艺师，她有自己的日常。"
        await self.reflect("对不起，我昨天折坏你的纸风车了。", record)
        self.assertNotIn(record, self.engine._ask_judge.call_args.args[2])
        self.assertEqual(await self.status(record), "active")
