"""Complete personal activities without requesting an impossible expression."""
import unittest
from unittest.mock import AsyncMock

from core.engine import TickOutcome
from core.json_actions import PlannedAction
from core.models import ActionDef
from tests import test_participation as participation
from tests import test_chat_cadence as cadence

GROUP = participation.GROUP


class ExpressionGateTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = participation.ParticipationTests.asyncSetUp
    chat = participation.ParticipationTests.chat
    incoming = cadence.PeerRuntimeTests.incoming
    two_turns = cadence.PeerRuntimeTests.two_turns

    async def finish_personal_activity(self, kind="life_share"):
        definition = ActionDef(id="sort_bookmarks", name="整理书签", category="continuous",
            duration=60, on_complete={"trigger":"llm_followup", "effects":{"energy":"+0.05"}})
        self.engine.world.actions.append(definition)
        outcome = TickOutcome(GROUP, speech_kind=kind)
        self.engine._llm_followup = AsyncMock(return_value=False)
        async with self.engine.session_state(GROUP) as state:
            state.energy = .6
            action = {"type":definition.id, "session":GROUP, "elapsed_ticks":1}
            state.current_action = action
            await self.engine._finish_action(state, self.engine.node(state.node_id), outcome, action)
            self.assertIsNone(state.current_action)
            self.assertAlmostEqual(state.energy, .65)
        return outcome

    async def test_unrelated_bare_image_cannot_trigger_an_impossible_completion_expression(self):
        self.engine.world.life_share_session_whitelist = []
        await self.chat("［图片］", raw_text="", has_images=True, message_id="unrelated-image")
        await self.finish_personal_activity()
        self.engine._llm_followup.assert_not_awaited()

    async def test_peer_pause_cannot_trigger_an_impossible_completion_expression(self):
        await self.two_turns()
        await self.incoming(message_id="next-peer")
        self.engine.world.life_share_session_whitelist = []
        await self.finish_personal_activity()
        self.engine._llm_followup.assert_not_awaited()

    async def test_fresh_text_can_still_be_considered_after_completion(self):
        self.engine.world.life_share_session_whitelist = []
        await self.chat("那张风车照片我想再看看", message_id="current-question")
        await self.finish_personal_activity()
        self.engine._llm_followup.assert_awaited_once()

    async def test_native_reply_keeps_its_completion_expression_for_an_image(self):
        self.engine.world.life_share_session_whitelist = []
        await self.chat("［图片］", raw_text="", has_images=True, message_id="native-image")
        await self.finish_personal_activity("reply")
        self.engine._llm_followup.assert_awaited_once()

    async def test_peer_pause_does_not_block_authorized_life_expression(self):
        await self.two_turns()
        await self.incoming(message_id="next-peer")
        await self.finish_personal_activity()
        self.engine._llm_followup.assert_awaited_once()

    async def test_disabled_browse_reply_does_not_borrow_message_participation_permission(self):
        self.engine.world.chat_browse.enabled = False
        await self.chat("那张风车照片我想再看看", message_id="current-question")
        await self.finish_personal_activity("browse_reply")
        self.engine._llm_followup.assert_not_awaited()

    async def test_an_accepted_expression_can_finish_its_prepared_tail(self):
        self.engine.world.decider.peer_chat_max_turns = 1
        await self.incoming(message_id="first-peer")
        result = await self.engine.maybe_participate(GROUP)
        await self.incoming(message_id="next-peer")
        async with self.engine.session_state(GROUP) as state:
            completed = await self.engine._start_action(state, self.engine.node(state.node_id), result,
                self.engine.world.action_map()["say"],
                PlannedAction(type="say", messages=["还有最后一个细节。"]), depth=1, autonomous=True)
        self.assertIsNot(completed, False)
        self.assertIn("还有最后一个细节。", result.messages)

    async def test_cross_session_expression_uses_the_destination_media_policy(self):
        raw = self.engine.store.raw_world()
        raw.setdefault("decider", {})["media_only_participation"] = "normal"
        self.engine.store.save_world(raw)
        scoped = self.engine._base_runtime["world"].model_dump(mode="json", by_alias=True)
        scoped["decider"]["media_only_participation"] = "directed"
        self.engine.store.save_scope(participation.OTHER, scoped, {"schedules":[]}, ["settings"])
        self.engine.reload_config()
        await self.chat("［图片］", sid=participation.OTHER, raw_text="", has_images=True,
                        message_id="other-image")
        async with self.engine.session_state(participation.OTHER) as state:
            with self.engine.config_scope(GROUP):
                allowed, _, _ = self.engine.expression_options(state, participation.OTHER, "participation")
        self.assertNotIn("join_chat", allowed)
        self.assertNotIn("continue_chat", allowed)

    async def test_stricter_media_policy_cancels_an_inflight_image_comment(self):
        raw = self.engine.store.raw_world()
        raw.setdefault("decider", {})["media_only_participation"] = "normal"
        self.engine.store.save_world(raw)
        self.engine.reload_config()
        await self.chat("［图片］", raw_text="", has_images=True, message_id="inflight-image")

        async def change_policy(*args, **kwargs):
            current = self.engine.store.raw_world()
            current["decider"]["media_only_participation"] = "directed"
            self.engine.store.save_world(current)
            self.engine.reload_config()
            return '{"inner":"有点想接这张图。","say":"画面挺有趣的。","intent":"join_chat"}'

        self.engine._ask_llm = change_policy
        result = await self.engine.maybe_participate(GROUP)
        self.assertEqual(self.messenger.sent, [])
        self.assertEqual(result.speech_decisions[0]["status"], "not_sent")
