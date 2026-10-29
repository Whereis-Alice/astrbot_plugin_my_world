"""Unfinished conversation topics keep their original character and room."""
from hashlib import sha256
import unittest

from tests import test_engine as fixture


class OpenTopicScopeTests(unittest.IsolatedAsyncioTestCase):
    node_id = fixture.EngineTestCase.node_id
    asyncSetUp = fixture.EngineTestCase.asyncSetUp
    ctx = fixture.EngineTestCase.ctx
    get_state = fixture.EngineTestCase.get_state
    add_group = fixture.EngineTestCase.add_group

    async def test_a_new_character_does_not_follow_up_the_old_characters_topic(self):
        self.llm.replies = ['{"open_topic":"在等银翼工坊给飞行器寄零件",'
                            '"actions":[{"type":"say","messages":["嗯，知道了"]}]}']
        ctx = self.ctx(text="还有一件事没办好", user_id="friend", user_name="阿梨")
        await self.engine.handle_incoming(ctx)
        await self.engine.handle_reply(ctx)
        first_persona = sha256(self.persona.text.encode("utf-8")).hexdigest()[:24]
        self.persona.text = "你是一个与之前角色独立的探险家。"
        self.clock.advance(70 * 60)
        self.llm.replies = ['{"actions":[{"type":"say","messages":["我在"]}]}']
        followup = self.ctx(text="在吗", user_id="friend", user_name="阿梨")
        await self.engine.handle_incoming(followup)
        await self.engine.handle_reply(followup)
        self.assertNotIn("银翼工坊", self.llm.calls[-1]["system_prompt"])
        state = await self.get_state()
        self.assertEqual(state.open_topics[0].get("persona_key"), first_persona)
        self.assertEqual(state.open_topics[0]["asked"], 0)

    async def test_matching_topics_from_two_rooms_do_not_refresh_each_others_source(self):
        self.add_group(sessions=[fixture.SESSION, fixture.PRIVATE_SESSION], main=fixture.SESSION)
        group_ctx = self.ctx(text="还有件事", user_id="friend", user_name="阿梨")
        private_ctx = self.ctx(text="还有件事", user_id="friend", user_name="阿梨",
                               session_id=fixture.PRIVATE_SESSION)
        async with self.engine.session_state(fixture.SESSION) as state:
            self.engine.note_open_topic(state, group_ctx, "在等工坊寄零件")
        async with self.engine.session_state(fixture.PRIVATE_SESSION) as state:
            self.engine.note_open_topic(state, private_ctx, "在等工坊寄零件，私下那架飞行器")
            self.assertEqual(len(state.open_topics), 2)
            self.assertEqual({topic["session"] for topic in state.open_topics},
                             {fixture.SESSION, fixture.PRIVATE_SESSION})
        self.clock.advance(70 * 60)
        async with self.engine.session_state(fixture.SESSION) as state:
            topics = self.engine.prompts._due_open_topics(state, fixture.SESSION)
            self.assertEqual([topic["text"] for topic in topics], ["在等工坊寄零件"])

    async def test_same_topic_recorded_by_two_characters_remains_two_topics(self):
        ctx = self.ctx(text="还有件事", user_id="friend", user_name="阿梨")
        async with self.engine.session_state(fixture.SESSION) as state:
            self.engine.note_open_topic(state, ctx, "在等工坊寄零件")
        self.persona.text = "你是独立的探险家。"
        async with self.engine.session_state(fixture.SESSION) as state:
            self.engine.note_open_topic(state, ctx, "在等工坊寄零件")
            self.assertEqual(len(state.open_topics), 2)
            keys = {topic.get("persona_key") for topic in state.open_topics}
            self.assertEqual(len(keys), 2)

    async def test_expired_topic_is_hidden_before_the_next_reply(self):
        async with self.engine.session_state(fixture.SESSION) as state:
            state.open_topics = [{"text": "一个早就过期的未完话题", "who": "friend",
                                  "session": fixture.SESSION, "at": self.clock.now() - 30 * 86400,
                                  "next_ask_at": self.clock.now() - 1, "asked": 0}]
        self.llm.replies = ['{"actions":[{"type":"say","messages":["我在"]}]}']
        ctx = self.ctx(text="在吗", user_id="friend", user_name="阿梨")
        await self.engine.handle_incoming(ctx)
        await self.engine.handle_reply(ctx)
        self.assertNotIn("早就过期的未完话题", self.llm.calls[-1]["system_prompt"])

    async def test_legacy_topic_without_a_room_stays_with_the_state_owner(self):
        self.add_group(sessions=[fixture.SESSION, fixture.PRIVATE_SESSION], main=fixture.SESSION)
        async with self.engine.session_state(fixture.SESSION) as state:
            state.open_topics = [{"text": "代表会话遗留的话题", "who": "friend",
                                  "at": self.clock.now() - 3600, "next_ask_at": self.clock.now() - 1}]
            self.assertEqual(self.engine.prompts._due_open_topics(state, fixture.PRIVATE_SESSION), [])
            self.assertEqual(len(self.engine.prompts._due_open_topics(state, fixture.SESSION)), 1)
            self.engine.mark_open_topics_asked(state, self.ctx(
                session_id=fixture.PRIVATE_SESSION, user_id="friend"))
            self.assertEqual(state.open_topics[0].get("asked", 0), 0)

    async def test_reply_does_not_expire_another_characters_topic(self):
        topic = {"text": "另一个角色遗留的话题", "who": "friend", "persona_key": "other-character",
                 "session": fixture.SESSION, "at": self.clock.now() - 30 * 86400,
                 "next_ask_at": self.clock.now() - 1, "asked": 0}
        async with self.engine.session_state(fixture.SESSION) as state:
            state.open_topics = [dict(topic)]
            self.engine.mark_open_topics_asked(state, self.ctx(user_id="friend"))
            self.assertEqual(state.open_topics, [topic])


if __name__ == "__main__":
    unittest.main()
