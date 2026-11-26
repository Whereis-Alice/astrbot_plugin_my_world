"""Affinity and lasting feelings follow fresh, directly evidenced behavior."""

import json
import unittest
from unittest.mock import AsyncMock

from tests import test_engine as fixture


SESSION = fixture.SESSION
USER = "42"


class InteractionPolicyTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp

    async def assess(self, entry, *, kind="hurt", quote=None, addressing="me", entries=None):
        text = entry["text"]
        sources = self.engine.interaction_sources(SESSION, USER, text, [entry])
        selected = text if quote is None else quote
        self.engine._ask_judge = AsyncMock(return_value=json.dumps({
            "addressing": addressing, "tone": "attack" if kind in {"hurt", "breach"} else "normal",
            "affinity_delta": -1,
            "interaction": {"kind": kind, "source_id": sources[0]["id"] if sources else "src_missing",
                            "quote": selected, "reason": "根据本轮原话与上下文判断"},
            "grudge": {"summary": "被他故意针对", "narrative": "这一刻我确实有些难受，想自己安静一会儿。",
                       "evidence": [selected]},
        }, ensure_ascii=False))
        return await self.engine.reflect_turn(SESSION, user_id=USER, user_text=text, reply_text="嗯",
            interaction_entries=entries or [entry])

    def entry(self, text, *, message_id="one", **fields):
        return {"user_id": USER, "message_id": message_id, "text": text, **fields}

    def affinity(self):
        return self.engine.profiles.profile(SESSION, USER)["affinity"]

    async def test_fresh_direct_hurt_can_lower_affinity_and_record_a_grudge(self):
        self.engine.profiles.touch(SESSION, USER)
        entry = self.entry("我就是故意当着大家的面羞辱你的")
        result = await self.assess(entry)
        self.assertAlmostEqual(result["affinity_delta"], -.20)
        self.assertAlmostEqual(self.affinity(), -.20)
        async with self.engine.session_state(SESSION) as state:
            grudge = self.engine.grudge_for(state, USER, session_id=SESSION)
            self.assertEqual(grudge["evidence"][0]["text"], entry["text"])

    async def test_high_affinity_buffers_hurt_and_preserves_capacity_for_breach(self):
        profiles = self.engine.profiles
        profiles.touch(SESSION, USER)
        profiles.adjust_affinity(SESSION, USER, 80, reason="管理员手动设置")
        hurt = await self.assess(self.entry("我就是故意当着大家的面羞辱你的"))
        self.assertAlmostEqual(hurt["affinity_delta"], -.10)
        async with self.engine.session_state(SESSION) as state:
            self.assertIsNone(self.engine.grudge_for(state, USER, session_id=SESSION))
        breach = await self.assess(self.entry("我故意把答应保密的聊天内容公开给所有人", message_id="breach"),
                                   kind="breach")
        self.assertAlmostEqual(breach["affinity_delta"], -.60)
        self.assertAlmostEqual(self.affinity(), 79.3)
        async with self.engine.session_state(SESSION) as state:
            self.assertIsNotNone(self.engine.grudge_for(state, USER, session_id=SESSION))

    async def test_quote_report_media_and_other_recipient_cannot_supply_hurt_evidence(self):
        self.engine.profiles.touch(SESSION, USER)
        quote = "你真恶心"
        turns = (
            self.entry(f"他昨天说‘{quote}’，我觉得不妥", message_id="inline"),
            self.entry(f"<quote>{quote}</quote>这不是我的意思", message_id="platform"),
            self.entry(quote, message_id="caption", raw_text=""),
            self.entry(quote, message_id="someone-else", reply_to={"is_self": False}),
            self.entry(quote, message_id="recalled", is_recalled=True, kind="direct_speech"),
            self.entry(quote, message_id="internal", internal=True),
        )
        for entry in turns:
            with self.subTest(message=entry["message_id"]):
                result = await self.assess(entry, quote=quote)
                self.assertEqual(result["affinity_delta"], 0)
        self.assertEqual(self.affinity(), 0)
        self.assertEqual(self.engine.db.raw.interaction_detail(SESSION, USER)["history"], [])
        async with self.engine.session_state(SESSION) as state:
            self.assertIsNone(self.engine.grudge_for(state, USER, session_id=SESSION))
            self.assertEqual(state.negative_tone_streak, 0)

    async def test_addressing_is_unclear_when_the_same_message_mentions_multiple_people(self):
        self.engine.profiles.touch(SESSION, USER)
        targets = [{"id": "bot", "self": True}, {"id": "someone", "self": False}]
        entry = self.entry("你也太烦了", at_targets=targets, addressing="me")
        result = await self.assess(entry, addressing="unclear")
        self.assertEqual((result["affinity_delta"], result["tone"]), (0, "normal"))

    async def test_numeric_scores_and_plain_grudges_do_not_change_a_neutral_turn(self):
        self.engine.profiles.touch(SESSION, USER)
        self.engine._ask_judge = AsyncMock(return_value='{"addressing":"me", "tone":"attack",'
            '"affinity_delta":-1,"valence_delta":-0.2,"grudge":"他顶嘴"}')
        entry = self.entry("这个选择我不同意，我们换一个吧")
        result = await self.engine.reflect_turn(SESSION, user_id=USER, user_text=entry["text"], reply_text="好",
                                                 interaction_entries=[entry])
        self.assertEqual(result["affinity_delta"], 0)
        self.assertEqual(result["valence_delta"], 0)
        self.assertEqual(result["tone"], "normal")
        self.assertEqual(self.affinity(), 0)
        async with self.engine.session_state(SESSION) as state:
            self.assertIsNone(self.engine.grudge_for(state, USER, session_id=SESSION))
            self.assertEqual(state.negative_tone_streak, 0)

    async def test_positive_behavior_counts_and_the_same_platform_message_never_scores_twice(self):
        profiles = self.engine.profiles
        profiles.touch(SESSION, USER)
        entry = self.entry("我已经帮你把坏掉的书架修好了", at=1)
        first = await self.assess(entry, kind="support")
        self.assertAlmostEqual(first["affinity_delta"], .15)
        repeated = {**entry, "at": 2}
        second = await self.assess(repeated, kind="support", entries=[repeated, self.entry("试试看吧", message_id="tail")])
        self.assertEqual(second["affinity_delta"], 0)
        self.assertAlmostEqual(self.affinity(), .15)
        log = self.engine.db.raw.interaction_detail(SESSION, USER)["history"]
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["evidence"][0]["interaction_kind"], "support")

    async def test_disabled_affinity_still_deduplicates_harm_without_saving_an_evaluation(self):
        self.engine.world.profile.interaction_learning = False
        self.engine.profiles.touch(SESSION, USER)
        entry = self.entry("我就是故意当着大家的面羞辱你的")
        first = await self.assess(entry)
        self.assertEqual(first["affinity_delta"], 0)
        self.assertEqual(first["tone"], "attack")
        async with self.engine.session_state(SESSION) as state:
            self.assertIsNotNone(self.engine.grudge_for(state, USER, session_id=SESSION))
            self.assertTrue(self.engine.resolve_grudge(state, user_id=USER, session_id=SESSION))
        self.clock.advance(86400)
        second = await self.assess(entry)
        self.assertEqual(second["tone"], "normal")
        self.assertEqual(self.affinity(), 0)
        self.assertEqual(self.engine.db.raw.interaction_detail(SESSION, USER)["history"], [])
        async with self.engine.session_state(SESSION) as state:
            self.assertIsNone(self.engine.grudge_for(state, USER, session_id=SESSION))
