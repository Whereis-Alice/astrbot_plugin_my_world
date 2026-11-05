"""Shared-life reception follows the actual private or group chat source."""
from __future__ import annotations

import copy
import unittest

from tests import test_engine as engine_fixture
from tests import test_visits_actor as actor_fixture
from core.visits.decisions import VisitDecisionError


class VisitReadinessReviewTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = actor_fixture.VisitActorTests.asyncSetUp

    def group(self, main):
        engine_fixture.EngineTestCase.add_group(self,
            sessions=[engine_fixture.SESSION, engine_fixture.PRIVATE_SESSION], main=main)

    async def conversation(self, scope, origin, *, addressing="", **changes):
        async with self.engine.session_state(scope) as state:
            state.state, state.energy = "idle", .8
            state.current_action, state.current_plan = None, None
            row = {"text": "眼前的交谈", "at": self.clock.now(), "is_self": False,
                   "addressing": addressing, **changes}
            if origin is not None:
                row["origin"] = origin
            state.recent_chat = [row]
        return await self.port.describe(scope)

    async def receive(self, scope, card):
        visit = copy.deepcopy(self.visit)
        visit.update(scope=scope, local=card)
        return await self.port.decide(scope, "receive", visit)

    async def assert_waiting(self, scope, card):
        with self.assertRaises(VisitDecisionError) as caught:
            await self.receive(scope, card)
        self.assertEqual(caught.exception.category, "life_busy")
        self.assertFalse(caught.exception.request_started)
        self.assertFalse(self.llm.calls)

    async def test_group_main_honours_a_private_members_conversation(self):
        scope = engine_fixture.SESSION
        self.group(scope)
        card = await self.conversation(scope, engine_fixture.PRIVATE_SESSION)
        self.assertTrue(card["chat_busy"])
        await self.assert_waiting(scope, card)

    async def test_private_main_does_not_turn_group_chatter_into_a_direct_conversation(self):
        scope = engine_fixture.PRIVATE_SESSION
        self.group(scope)
        card = await self.conversation(scope, engine_fixture.SESSION, addressing="others")
        self.assertFalse(card["chat_busy"])
        self.assertTrue((await self.receive(scope, card))["accept"])
        self.assertEqual(len(self.llm.calls), 1)

    async def test_private_main_still_honours_a_group_message_addressed_to_her(self):
        scope = engine_fixture.PRIVATE_SESSION
        self.group(scope)
        card = await self.conversation(scope, engine_fixture.SESSION, addressing="me")
        self.assertTrue(card["chat_busy"])
        await self.assert_waiting(scope, card)

    async def test_disabled_member_chat_does_not_block_the_remaining_life(self):
        scope = engine_fixture.SESSION
        self.group(scope)
        raw = self.store.raw_sessions()
        for row in raw["sessions"]:
            if row["session_id"] == engine_fixture.PRIVATE_SESSION:
                row["enabled"] = False
        self.store.save_sessions(raw)
        self.engine.reload_config()
        card = await self.conversation(scope, engine_fixture.PRIVATE_SESSION, addressing="me")
        self.assertFalse(card["chat_busy"])

    async def test_retained_chat_from_a_removed_member_does_not_block(self):
        scope = engine_fixture.SESSION
        self.group(scope)
        raw = self.store.raw_sessions()
        raw["groups"] = []
        self.store.save_sessions(raw)
        self.engine.reload_config()
        card = await self.conversation(scope, engine_fixture.PRIVATE_SESSION, addressing="me")
        self.assertFalse(card["chat_busy"])

    async def test_legacy_chat_without_origin_uses_its_saved_session_type(self):
        for scope, addressing, expected in (
            (engine_fixture.SESSION, "others", False),
            (engine_fixture.SESSION, "me", True),
            (engine_fixture.PRIVATE_SESSION, "", True),
        ):
            with self.subTest(scope=scope, addressing=addressing):
                self.group(scope)
                card = await self.conversation(scope, None, addressing=addressing)
                self.assertEqual(card["chat_busy"], expected)

    async def test_native_turn_in_another_shared_member_still_has_priority(self):
        scope = engine_fixture.SESSION
        self.group(scope)
        turn = {"session_id": engine_fixture.PRIVATE_SESSION, "started_at": self.clock.now()}
        self.engine.begin_native_turn(turn)
        self.addCleanup(self.engine.end_native_turn, turn)
        card = await self.conversation(scope, engine_fixture.SESSION, addressing="others")
        self.assertTrue(card["chat_busy"])
        await self.assert_waiting(scope, card)

    async def test_self_internal_and_old_private_rows_do_not_block_reception(self):
        scope = engine_fixture.SESSION
        self.group(scope)
        for changes in ({"is_self": True}, {"internal": True}, {"at": self.clock.now() - 121}):
            with self.subTest(changes=changes):
                card = await self.conversation(scope, engine_fixture.PRIVATE_SESSION, **changes)
                self.assertFalse(card["chat_busy"])
