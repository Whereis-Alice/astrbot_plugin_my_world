"""A confirmed visit updates standalone cards without resuming home activities."""
from __future__ import annotations

from types import SimpleNamespace
import unittest

from core.nickname import compute_nickname
from tests import test_engine as fixture


class StandaloneVisitCardTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    get_state = fixture.EngineTestCase.get_state
    set_state = fixture.EngineTestCase.set_state

    async def asyncSetUp(self):
        await fixture.EngineTestCase.asyncSetUp(self)
        raw = self.store.raw_world()
        raw["nickname_sync"].update(enabled=True, template="{base} {status}", cooldown_seconds=10)
        self.store.save_world(raw)
        self.engine.reload_config()
        await self.set_state(state="idle", cold_start_done=True, bot_base_nickname="爱丽丝",
                             bot_current_nickname="爱丽丝", current_action=None, current_plan=None)
        _, owner = await self.engine._journal_persona(fixture.SESSION)
        self.visit = {"id": "confirmed-visit", "status": "active", "role": "guest",
                      "scope": self.engine.state_key(fixture.SESSION), "arrived_at": self.clock.now() - 60,
                      "local": {"persona_key": owner}, "remote": {"name": "艾拉"}}
        self.engine.visits = SimpleNamespace(config=SimpleNamespace(enabled=True), _loaded=True,
                                             _closed=False, active=lambda _: self.visit)

    async def test_visiting_tick_updates_card_and_home_return_restores_normal_rule(self):
        await self.engine.tick()
        self.assertEqual(self.messenger.cards, [(fixture.SESSION, "爱丽丝 拜访艾拉")])
        self.assertEqual(self.llm.calls, [])
        self.assertEqual(self.messenger.sent, [])
        self.clock.advance(60)
        await self.engine.tick()
        self.assertEqual(len(self.messenger.cards), 1)
        self.visit = None
        async with self.engine.session_state(fixture.SESSION) as state:
            expected = compute_nickname(self.engine.world, state, self.engine.node(state.node_id),
                                        base=state.bot_base_nickname)
            await self.engine._sync_nickname(state, self.engine.node(state.node_id))
        self.assertEqual(self.messenger.cards[-1], (fixture.SESSION, expected))
        self.assertNotIn("拜访", self.messenger.cards[-1][1])

    async def test_external_owner_keeps_the_only_write_path_during_visit(self):
        self.messenger.group_card_owner = lambda _: "随心名片"
        await self.engine.tick()
        self.assertEqual(self.messenger.cards, [])
        self.assertEqual(self.llm.calls, [])

    async def test_confirmed_name_replaces_previous_daily_status_during_visit(self):
        await self.set_state(bot_base_nickname="爱丽丝 摸鱼中 | 事件中")
        self.visit["local"]["name"] = "爱丽丝"
        await self.engine.tick()
        self.assertEqual(self.messenger.cards, [(fixture.SESSION, "爱丽丝 拜访艾拉")])
        state = await self.get_state()
        self.assertEqual(state.bot_base_nickname, "爱丽丝 摸鱼中 | 事件中")

    async def test_administrator_lock_and_temporary_unavailable_state_keep_card(self):
        await self.set_state(bot_nickname_locked=True)
        await self.engine.tick()
        self.assertEqual(self.messenger.cards, [])
        await self.set_state(bot_nickname_locked=False)
        self.engine.visits._loaded = False
        async with self.engine.session_state(fixture.SESSION) as state:
            await self.engine._sync_nickname(state, self.engine.node(state.node_id))
        self.assertEqual(self.messenger.cards, [])
