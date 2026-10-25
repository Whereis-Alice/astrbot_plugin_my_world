"""Reproduce peer ping-pong and media-only opportunities without real sends."""
import unittest

from core.chat_cadence import note_input, note_receipt, pause_until
from core.engine import MessageContext, TickOutcome
from core.engine.speech_receipts import remember_accepted
from core.models import DeciderConfig
from core.participation_opportunity import assess_opportunity, attention_text, media_scene_allowed
from tests import test_participation as participation

GROUP, OTHER = participation.GROUP, participation.OTHER


class CadenceTests(unittest.TestCase):
    def test_actual_send_controls_next_read_and_continuation_eventually_slows(self):
        rows = [{"is_self": True, "at": 990, "text": "我也喜欢这个", "speech_turn": "a"}]
        args = dict(now=1000, rows=rows, new=[{"text": "还有个细节", "addressing": "me"}],
                    observation={"evaluated_at": 900}, attempts=[], persona="", affinities={})
        first = assess_opportunity(**args)
        self.assertFalse(first.ready)
        self.assertEqual(first.due_at, 1010)
        rows.append({"is_self": True, "at": 995, "text": "第二个回应", "speech_turn": "b"})
        second = assess_opportunity(**args)
        self.assertFalse(second.continuation_priority)
        self.assertGreater(second.due_at, first.due_at)
        self.assertTrue(second.continuation)

    def test_split_turn_is_one_and_cooldown_finishes_its_remaining_segments(self):
        ledger = note_input({}, {"user_id": "peer", "seq": 1}, {"peer"})
        ledger = note_receipt(ledger, "a", 1000, 600)
        ledger = note_receipt(ledger, "a", 1010, 600)
        self.assertEqual(len(ledger["turns"]), 1)
        ledger = note_receipt(ledger, "b", 1040, 600)
        args = dict(now=1050, cap=2, pause=300, window=600)
        self.assertEqual(pause_until(ledger, **args), 1340)
        self.assertEqual(pause_until(ledger, token="b", **args), 0)
        self.assertEqual(pause_until(ledger, token="a", **args), 0)
        self.assertEqual(pause_until(ledger, now=1340, cap=2, pause=300, window=600), 0)
        self.assertEqual(pause_until(ledger, cap=0, **{k:v for k,v in args.items() if k != 'cap'}), 0)

    def test_fresh_human_intervention_resets_but_replayed_or_unknown_input_does_not(self):
        ledger = {"input_seq": 9, "peer_active": True, "turns": [{"token":"a", "at":1000}]}
        for row in ({"seq":8, "user_id":"human"}, {"seq":10, "user_id":""},
                    {"seq":10, "user_id":"__event__"}, {"seq":10, "user_id":"human", "is_self":True}):
            self.assertEqual(note_input(ledger, row, {"peer"}), ledger)
        human = note_input(ledger, {"seq":10, "user_id":"human"}, {"peer"})
        self.assertFalse(human["peer_active"])
        self.assertEqual(human["turns"], [])

    def test_long_pause_outlives_counting_window_without_counting_old_turns_again(self):
        ledger = {'peer_active':True}
        for token,at in [('a',1000),('b',1040)]:
            ledger = note_receipt(ledger,token,at,120,retention=600)
        self.assertEqual(pause_until(ledger,now=1250,cap=2,pause=600,window=120),1640)
        ledger = note_receipt(ledger,'c',1645,120,retention=600)
        self.assertEqual(pause_until(ledger,now=1650,cap=2,pause=600,window=120),0)

    def test_quote_and_image_annotations_do_not_supply_topic_or_relationship_relevance(self):
        row = {"text":"路过\n［他引用了你自己的消息，原文：「风车已经修好了」］", "user_id":"close"}
        self.assertEqual(attention_text(row), "路过")
        choice = assess_opportunity(now=1000, rows=[], new=[row], observation={}, attempts=[],
                                    persona="喜欢风车", affinities={"close":1})
        self.assertEqual(choice.relevance, 0)
        self.assertEqual(choice.relationship, 0)

    def test_bare_media_modes_and_caption_keep_their_actual_context(self):
        image = {"text":"［图片］", "has_images":True}
        args = dict(rows=[], new=[image], now=1000)
        self.assertFalse(media_scene_allowed(**args))
        self.assertTrue(media_scene_allowed(**args, mode="normal"))
        self.assertFalse(media_scene_allowed(**args, mode="directed"))
        image["addressing"] = "me"
        self.assertTrue(media_scene_allowed(**args, mode="directed"))
        del image["addressing"]
        args["rows"] = [{"is_self":True, "text":"刚在聊", "at":990}]
        self.assertTrue(media_scene_allowed(**args))
        args["rows"][0]["at"] = 800
        self.assertFalse(media_scene_allowed(**args))
        image["reading_text"] = "看看这张风车照片"
        self.assertTrue(media_scene_allowed(**args))

    def test_new_config_is_bounded_and_existing_config_gets_defaults(self):
        config = DeciderConfig.model_validate({"natural_silence":False})
        self.assertEqual(config.peer_bot_ids, [])
        self.assertEqual(config.continuation_priority_turns, 2)
        self.assertEqual(config.media_only_participation, "contextual")
        for data in ({"peer_chat_max_turns":-1}, {"continuation_interval_seconds":0},
                     {"peer_chat_pause_seconds":4000}, {"media_only_participation":"always_send"}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                DeciderConfig.model_validate(data)

    def test_bare_media_with_short_ack_does_not_create_an_unrelated_scene(self):
        for placeholder in ("［图片］", "「图片」", "[表情包]", "［表情］"):
            with self.subTest(placeholder=placeholder):
                image = {"text":placeholder, "has_images":True}
                self.assertEqual(attention_text(image), "")
                new = [image, {"text":"哈哈"}]
                args = dict(rows=[], new=new, now=1000)
                self.assertFalse(media_scene_allowed(**args))
                choice = assess_opportunity(**args, observation={}, attempts=[], persona="", affinities={})
                self.assertFalse(choice.ready)
                new.append({"text":"今天我有点累"})
                self.assertTrue(media_scene_allowed(**args))

    def test_media_directed_to_someone_else_does_not_use_her_recent_turn_as_context(self):
        args = dict(rows=[{"is_self":True, "text":"刚说过话", "at":990}],
                    new=[{"text":"［图片］", "has_images":True, "addressing":"others"}], now=1000)
        self.assertFalse(media_scene_allowed(**args))
        self.assertTrue(media_scene_allowed(**args, mode="normal"))


class PeerRuntimeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = participation.ParticipationTests.asyncSetUp

    async def incoming(self, uid="peer", text="还有一句", sid=GROUP, **kwargs):
        self.engine.world.decider.peer_bot_ids = ["peer"]
        async with self.engine.session_state(sid) as state:
            self.engine._note_presence(state, MessageContext(session_id=sid, user_id=uid,
                user_name=uid, text=text, **kwargs))

    async def two_turns(self):
        self.llm.replies = [
            '{"inner":"这个细节让我在意。","say":"我想看第一张图纸。","intent":"join_chat"}',
            '{"inner":"原来另一张图纸也有变化。","say":"第二张的叶片更大了。","intent":"join_chat"}',
            '{"inner":"他带来新的问题了。","say":"是第三张出了问题吗？","intent":"join_chat"}',
        ]
        for i in range(2):
            await self.incoming(text=f"新的风车细节{i}", message_id=f"peer-{i}")
            result = await self.engine.maybe_participate(GROUP)
            self.assertIsNotNone(result)
            self.clock.advance(40)
        self.assertEqual(len(self.messenger.sent), 2)

    async def test_real_peer_burst_pauses_and_fresh_human_message_restores_it(self):
        await self.two_turns()
        await self.incoming(text="又接一句", message_id="peer-3")
        self.assertIsNone(await self.engine.maybe_participate(GROUP))
        self.assertEqual(len(self.llm.calls), 2)
        await self.incoming(uid="human", text="我有一个新问题", message_id="human-new")
        await self.engine.maybe_participate(GROUP)
        self.assertEqual(len(self.messenger.sent), 3)

    async def test_browse_and_old_completion_cannot_bypass_the_send_guard(self):
        await self.two_turns()
        self.engine.world.chat_browse.enabled = True
        self.engine.world.chat_browse.reply_enabled = True
        for kind in ("participation", "browse_reply", "continuation", "interject"):
            with self.subTest(kind=kind):
                result = TickOutcome(GROUP, speech_kind=kind, messages=["排队中的旧回应"])
                result.chat_expires_at[GROUP] = self.clock.now()+900
                await self.engine._deliver(result)
                self.assertEqual(len(self.messenger.sent), 2)
                self.assertTrue(any("连续聊过" in note for note in result.notes))
        self.assertTrue(self.engine.contact_speech_allowed(TickOutcome(GROUP), GROUP, "reply"))
        await self.engine._deliver(TickOutcome(GROUP, speech_kind="reply", messages=["正常回应你提出的问题。"] ))
        self.assertEqual(len(self.messenger.sent), 3)
        self.assertGreater(self.engine.peer_chat_pause(GROUP), self.clock.now())

    async def test_peer_cooldown_survives_configuration_reload(self):
        raw = self.engine.store.raw_world()
        raw.setdefault('decider', {})['peer_bot_ids'] = ['peer']
        self.engine.store.save_world(raw)
        self.engine.reload_config()
        await self.two_turns()
        until = self.engine.peer_chat_pause(GROUP)
        self.engine.reload_config()
        self.assertEqual(self.engine.peer_chat_pause(GROUP), until)

    async def test_failed_send_does_not_consume_peer_turn(self):
        await self.incoming()
        self.messenger.send_text = lambda *args: self.failed()
        await self.engine.maybe_participate(GROUP)
        self.assertEqual(self.engine.peer_chat_pause(GROUP), 0)
        _, ledger, _, _ = self.engine._peer_chat_policy(GROUP)
        self.assertEqual(ledger['sessions'][GROUP].get('turns', []), [])

    async def failed(self):
        return False

    async def test_same_expression_segments_and_images_finish_after_threshold(self):
        await self.incoming()
        result = TickOutcome(GROUP, speech_kind="continuation")
        await remember_accepted(self.engine, result, GROUP, "第一段", "a")
        await remember_accepted(self.engine, result, GROUP, "第二段", "b")
        _, ledger, _, _ = self.engine._peer_chat_policy(GROUP)
        self.assertEqual(len(ledger['sessions'][GROUP]['turns']), 1)
        self.engine.world.decider.peer_chat_max_turns = 1
        result.chat_expires_at[GROUP] = self.clock.now()+900
        self.assertTrue(self.engine.contact_speech_allowed(result, GROUP, "continuation"))
        self.assertFalse(self.engine.contact_speech_allowed(TickOutcome(GROUP, speech_kind="continuation",
            chat_expires_at={GROUP:self.clock.now()+900}), GROUP, "continuation"))

    async def test_other_session_human_message_does_not_clear_this_peer_conversation(self):
        await self.two_turns()
        await self.incoming(uid="human", sid=OTHER)
        self.assertGreater(self.engine.peer_chat_pause(GROUP), self.clock.now())
        self.assertEqual(self.engine.peer_chat_pause(OTHER), 0)

    async def test_captionless_image_does_not_call_model_but_explicit_reply_can(self):
        await self.incoming(uid="human", text="［图片］", has_images=True, message_id="image-1")
        self.assertIsNone(await self.engine.maybe_participate(GROUP))
        self.assertEqual(self.llm.calls, [])
        state = await self.engine.load_state(GROUP, cold_start=False)
        self.assertEqual(state.participation[GROUP]['decision'], "observing")
        allowed, _, _ = self.engine.expression_options(state, GROUP, 'life_share')
        self.assertNotIn('join_chat', allowed)
        self.assertNotIn('continue_chat', allowed)
        await self.incoming(uid="human", text="［图片］", has_images=True, addressing="me", message_id="image-2")
        await self.engine.maybe_participate(GROUP)
        self.assertEqual(len(self.llm.calls), 1)

    async def test_platform_qualified_peers_do_not_classify_another_platform_account(self):
        await self.incoming()
        self.engine.world.decider.peer_bot_ids = ["otherplatform:peer"]
        self.assertEqual(self.engine._peer_chat_policy(GROUP)[2], set())
        self.engine.world.decider.peer_bot_ids = ["aiocqhttp:peer"]
        self.assertEqual(self.engine._peer_chat_policy(GROUP)[2], {"peer"})
