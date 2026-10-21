"""Exact global authorization at generation and actual delivery boundaries."""
import unittest
from unittest.mock import AsyncMock

from core.engine import TickOutcome
from core.json_actions import PlannedAction
from tests import test_config_scope as scoped

A, B = scoped.A, scoped.B


class AutonomousAccessTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = scoped.ConfigScopeTests.asyncSetUp
    override = scoped.ConfigScopeTests.override

    def authorize(self, sessions):
        world = self.store.raw_world()
        world["proactive_session_whitelist"] = sessions
        self.store.save_world(world)
        self.engine.reload_config()

    async def test_empty_whitelist_blocks_every_autonomous_origin_and_preserves_replies(self):
        self.authorize([])
        for kind in ("", "proactive", "interject", "schedule", "event", "sleep", "reply"):
            with self.subTest(kind=kind):
                outcome = TickOutcome(A, speech_kind=kind)
                outcome.add_speech(kind or "legacy")
                outcome.add_images(["https://example.com/test.png"])
                await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.flat_messages, ["reply"])
        self.assertEqual(len(self.messenger.flat_images), 1)
        self.assertFalse(self.db.raw.kv_get("proactive_contacts:" + self.engine.state_key(A)))

    async def test_shared_life_and_scoped_override_cannot_authorize_other_destinations(self):
        self.authorize([A])
        sessions = self.store.raw_sessions()
        sessions["groups"] = [{"id":"shared", "sessions":[A,B], "main_session":A}]
        self.store.save_sessions(sessions)
        self.override(B, proactive_session_whitelist=[B], proactive_chat_paused=False)
        self.assertTrue(self.engine.speech_allowed(A, "event"))
        self.assertFalse(self.engine.speech_allowed(B, "event"))
        self.assertFalse(self.engine.proactive_chat_allowed(B))
        self.assertNotIn("proactive_session_whitelist", self.store.raw_sessions()["sessions"][1]["config_overrides"]["settings"])
        outcome = TickOutcome(A, speech_kind="schedule")
        outcome.add_speech("authorized")
        outcome.broadcast_targets = [B]
        outcome.add_speech("private route", B)
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.sent, [(A,["authorized"])])

    async def test_authorized_broadcast_survives_an_unauthorized_primary_target(self):
        self.authorize([B])
        outcome = TickOutcome(A, speech_kind="schedule", broadcast_targets=[B])
        async with self.engine.session_state(A) as state:
            await self.engine._execute_actions(state, self.engine.node(state.node_id), outcome,
                [PlannedAction(type="say", messages=["see you at tea"])], depth=0, autonomous=True)
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.sent, [(B,["see you at tea"])])

    async def test_revoking_a_queued_event_drops_text_and_images(self):
        outcome = TickOutcome(A, speech_kind="event")
        outcome.add_speech("queued")
        outcome.add_images(["https://example.com/test.png"])
        self.authorize([])
        await self.engine._deliver(outcome)
        self.assertFalse(self.messenger.sent)
        self.assertFalse(self.messenger.flat_images)
        state = await self.engine.load_state(A, cold_start=False)
        self.assertFalse(any(row.get("text") == "queued" for row in state.recent_chat))

    async def test_disabling_authorized_session_stops_queued_speech(self):
        outcome = TickOutcome(A, speech_kind="schedule")
        outcome.add_speech("queued before session disabled")
        self.store.set_session_enabled(A, False)
        self.engine.reload_config()
        await self.engine._deliver(outcome)
        self.assertFalse(self.messenger.sent)
        self.assertTrue(self.engine.speech_allowed(A, "reply"))

    async def test_revocation_during_failed_text_still_blocks_reserved_images(self):
        async def failed_send(*_args):
            self.authorize([])
            return False

        self.engine.world.profile.proactive_daily_max = 1
        self.messenger.send_text = AsyncMock(side_effect=failed_send)
        outcome = TickOutcome(A)
        outcome.add_speech("text before revocation")
        outcome.add_images(["https://example.com/test.png"])
        await self.engine._deliver(outcome)
        self.messenger.send_text.assert_awaited_once()
        self.assertFalse(self.messenger.flat_images)
        self.assertFalse(self.engine._contact_pending)

    async def test_remaining_reply_image_does_not_consume_an_autonomous_contact(self):
        async def failed_send(*_args):
            self.authorize([])
            return False

        self.messenger.send_text = AsyncMock(side_effect=failed_send)
        outcome = TickOutcome(A)
        outcome.add_speech("autonomous text")
        outcome.add_images(["https://example.com/autonomous.png"])
        outcome.speech_kind = "reply"
        outcome.add_images(["https://example.com/reply.png"])
        await self.engine._deliver(outcome)
        self.assertEqual(self.messenger.flat_images, ["https://example.com/reply.png"])
        self.assertFalse(self.db.raw.kv_get("proactive_contacts:" + self.engine.state_key(A)))
        self.assertFalse(self.engine._contact_pending)

    async def test_unauthorized_poke_and_tool_do_not_reach_the_platform(self):
        self.authorize([])
        self.messenger.poke = AsyncMock(return_value=True)
        self.engine._call_tool = AsyncMock()
        async with self.engine.session_state(A) as state:
            outcome = TickOutcome(A, speech_kind="event")
            definition = self.engine.world.action_map()["poke"]
            await self.engine._run_poke(state, self.engine.node(state.node_id), outcome,
                                        definition, PlannedAction(type="poke", target="alice"))
            result = await self.engine._call_one_tool(state, definition, {}, "send_test",
                                                      params={}, outcome=outcome)
        self.assertIsNone(result)
        self.messenger.poke.assert_not_awaited()
        self.engine._call_tool.assert_not_awaited()

    async def test_background_echo_without_an_outcome_cannot_bypass_whitelist(self):
        self.authorize([])
        self.engine.world.echo_types = ["search"]
        self.engine.debug_sink = AsyncMock(return_value=True)
        state = await self.engine.load_state(A, cold_start=False)
        await self.engine._log_event(state, "search", {"queries": ["weather"]})
        self.engine.debug_sink.assert_not_awaited()
        self.authorize([A])
        self.engine.world.echo_types = ["search"]
        await self.engine._log_event(state, "search", {"queries": ["forecast"]})
        self.engine.debug_sink.assert_awaited_once()
