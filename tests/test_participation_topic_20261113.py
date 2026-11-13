"""Current interjections use their actual topic and grounded activity."""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from core.life_matters import make
from core.models import ActionDef
from tests import test_context_selection as fixture


class ParticipationTopicTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.ContextIntegrationTests.asyncSetUp
    row = fixture.ContextIntegrationTests.row

    def render(self, rows, **kwargs):
        return self.engine.participation_prompt(
            self.state, fixture.GROUP, fixture.PERSONA, rows=rows, **kwargs)[0]

    async def test_new_complete_topic_does_not_reopen_the_previous_life_question(self):
        _, owner = await self.engine._journal_persona(fixture.GROUP)
        self.state.life_matters = [make("修好风车的叶片", now=self.clock.now(),
            session=fixture.GROUP, persona_key=owner)]
        self.state.current_action = {"type": "read_book", "desc": "正在读航空史"}
        system = self.render([
            self.row("你在做什么，风车的叶片修好了吗？", seq=1),
            self.row("今晚这场比赛谁先开球？", seq=2),
        ])
        self.assertNotIn("修好风车的叶片", system)
        self.assertNotIn("正在读航空史", system)

    async def test_short_follow_up_preserves_its_previous_life_topic(self):
        _, owner = await self.engine._journal_persona(fixture.GROUP)
        self.state.life_matters = [make("修好风车的叶片", now=self.clock.now(),
            session=fixture.GROUP, persona_key=owner)]
        system = self.render([
            self.row("风车的叶片修好了吗？", seq=1),
            self.row("后来呢", seq=2),
        ])
        self.assertIn("修好风车的叶片", system)

    async def test_browse_topic_stays_with_read_sources_instead_of_later_chatter(self):
        _, owner = await self.engine._journal_persona(fixture.GROUP)
        self.state.life_matters = [make("修好风车的叶片", now=self.clock.now(),
            session=fixture.GROUP, persona_key=owner)]
        system = self.render([
            self.row("风车的叶片修好了吗？", seq=1),
            self.row("今晚这场比赛谁先开球？", seq=2),
        ], source="browse", reply_seqs=frozenset({1}))
        self.assertIn("修好风车的叶片", system)

    async def test_activity_question_keeps_the_actual_quiet_activity_after_rename(self):
        action = ActionDef(id="quiet_cards", name="轻轻哼歌", category="continuous", idle=True)
        self.engine.world.actions.append(action)
        self.state.state = "idle"
        self.state.idle_activity = {"id": action.id, "node_id": self.state.node_id,
            "until_tick": self.state.world_time + 10, "action_name": "整理旧书签"}
        system = self.render([self.row("你现在在做什么？", seq=1)])
        life = json.loads(system.split("# 此刻的你\n", 1)[1].split("\n", 1)[0])
        self.assertEqual("整理旧书签", life["activity"])
        self.assertNotIn("轻轻哼歌", life["activity"])

    async def test_foreign_personas_current_action_is_not_reported_as_this_character(self):
        self.state.current_action = {"type": "read_book", "desc": "他人的秘密活动",
            "persona_key": "different-character"}
        system = self.render([self.row("你现在在做什么？", seq=1)])
        self.assertNotIn("他人的秘密活动", system)

    async def test_confirmed_visit_is_current_while_the_home_activity_is_paused(self):
        _, owner = await self.engine._journal_persona(fixture.GROUP)
        self.state.current_action = {"type": "read_book", "desc": "家中暂停的阅读"}
        visit = {"status": "finishing", "arrived_at": self.clock.now(), "scope": fixture.GROUP,
            "local": {"persona_key": owner}, "remote": {"name": "艾拉", "world": "港湾"},
            "scene": {"place": "河边", "activity": "一起散步"}}
        self.engine.visits = SimpleNamespace(active=lambda _: visit)
        system = self.render([self.row("你现在在做什么？", seq=1)])
        life = json.loads(system.split("# 此刻的你\n", 1)[1].split("\n", 1)[0])
        self.assertIn("艾拉", life["activity"])
        self.assertIn("一起散步", life["activity"])
        self.assertNotIn("家中暂停的阅读", life["activity"])

    async def test_proposed_or_foreign_personas_visit_is_not_current_activity(self):
        _, owner = await self.engine._journal_persona(fixture.GROUP)
        for arrived, persona in ((0, owner), (self.clock.now(), "different-character")):
            with self.subTest(arrived=arrived, persona=persona):
                visit = {"status": "active", "arrived_at": arrived, "scope": fixture.GROUP,
                    "local": {"persona_key": persona}, "remote": {"name": "艾拉", "world": "港湾"},
                    "scene": {"place": "河边", "activity": "一起散步"}}
                self.engine.visits = SimpleNamespace(active=lambda _: visit)
                system = self.render([self.row("你现在在做什么？", seq=1)])
                self.assertNotIn("一起散步", system)
