"""Quiet reading with isolated feelings and an explicitly optional reply."""
import asyncio
import copy
import json
import unittest
from unittest.mock import AsyncMock, Mock

from core.chat_browse import feeling_context, reading_view, thought_visible
from core.json_actions import PlannedAction
from core.models import ActionDef, ChatBrowseConfig, normalize_legacy_keys
from core.engine.models import MessageContext
from core.ports import LLMReply
from core.speech_policy import reply_target
from core.state import WorldState
from tests import test_engine as fixture

S, G, P = fixture.SESSION, fixture.OTHER_SESSION, fixture.PRIVATE_SESSION


class ChatBrowseTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.EngineTestCase.asyncSetUp
    add_group = fixture.EngineTestCase.add_group

    def configure(self, **values):
        raw = self.store.raw_world()
        raw["chat_browse"] = {"enabled": True, "phone": True, **values}
        self.store.save_world(raw)
        self.engine.reload_config()

    def note(self, state, text="大家聊起了窗边的花", origin=S, **kwargs):
        return state.note_chat(user_id="42", name="小明", text=text,
            now=self.clock.now(), origin=origin, keep=100, **kwargs)

    def test_legacy_chat_thoughts_are_visible_only_to_the_world_owner(self):
        legacy = {"source": "聊完一轮", "quote": "对方原话", "reply": "她的回复"}
        self.assertFalse(thought_visible(legacy, S, self.clock.now()))
        self.assertTrue(
            thought_visible(legacy, S, self.clock.now(), fallback_session=S)
        )
        self.assertFalse(
            thought_visible(legacy, G, self.clock.now(), fallback_session=S)
        )
        tagged = {**legacy, "dialogue_session": G}
        self.assertFalse(
            thought_visible(tagged, S, self.clock.now(), fallback_session=S)
        )
        self.assertTrue(
            thought_visible(tagged, G, self.clock.now(), fallback_session=S)
        )

    async def prepare(self, source=S, message_id="", **config):
        self.configure(**config)
        async with self.engine.session_state(S) as state:
            state.state, state.current_action, state.current_plan = "idle", None, None
            state.energy = .8
            self.note(state, origin=source, message_id=message_id)
            self.assertTrue(self.engine.start_chat_browse(state, "browse_phone"))
            return copy.deepcopy(state.current_action)

    async def complete(self, action, decision=None, **payload):
        self.llm.replies = [json.dumps({"inner": "也想在自己的窗边留一小盆花。", "source_seqs": [action["browse_rows"][0]["seq"]],
            "meaningful": False, "valence_delta": .01, **payload}, ensure_ascii=False)]
        if decision is not None:
            self.llm.replies.append(decision)
        await self.engine._complete_chat_browse(S, action["id"])
        return await self.engine.load_state(S)

    async def test_default_off_missing_device_and_empty_window_cost_nothing(self):
        async with self.engine.session_state(S) as state:
            self.note(state)
            self.assertFalse(self.engine.start_chat_browse(state, "browse_phone"))
        self.configure(phone=False)
        async with self.engine.session_state(S) as state:
            self.assertFalse(self.engine.start_chat_browse(state, "browse_phone"))
            self.assertFalse(self.engine.start_chat_browse(state, "browse_computer"))
        self.configure()
        async with self.engine.session_state(S) as state:
            state.recent_chat = []
            self.assertFalse(self.engine.start_chat_browse(state, "browse_phone"))
            self.assertEqual(state.chat_browsing, {})
        self.assertEqual(self.llm.calls, [])

    async def test_reading_view_is_bounded_and_does_not_follow_reply_cursor(self):
        state = WorldState(S)
        for i in range(30):
            self.note(state, f"{i}:" + "花" * 400)
        state.chat_replied_seq = 999
        config = ChatBrowseConfig(max_messages=20, max_chars=3000)
        rows = reading_view(state, S, config, self.clock.now())
        self.assertLessEqual(len(rows), 20)
        self.assertLessEqual(sum(len(row["text"]) for row in rows), 3000)
        self.assertEqual(rows[-1]["seq"], state.chat_seq)
        self.assertEqual(set(rows[0]), {"seq", "at", "name", "self", "text"})
        self.note(state, "内部记录", internal=True)
        self.note(state, "别的会话", origin=G)
        self.note(state, "只有她的自言自语", is_self=True)
        state.recent_chat[0]["at"] = self.clock.now() + 1
        state.recent_chat[1]["at"] = self.clock.now() - 86401
        view = reading_view(state, S, ChatBrowseConfig(), self.clock.now())
        self.assertFalse(any(row["text"] in {"内部记录", "别的会话"} for row in view))
        state.recent_chat = [state.recent_chat[-1]]
        self.assertEqual(reading_view(state, S, ChatBrowseConfig(), self.clock.now()), [])

    async def test_external_memory_and_media_refs_are_not_read_as_new_messages(self):
        state = WorldState(S)
        self.note(state, "<Anamnesis-Memory>旧聊天秘密</Anamnesis-Memory>现在窗边有花", images=["file:///private/media.png"], raw_text="<Anamnesis-Memory>旧聊天秘密</Anamnesis-Memory>现在窗边有花")
        self.note(state, "<RAG-Faiss-Memory>只有旧回忆</RAG-Faiss-Memory>")
        rows = reading_view(state, S, ChatBrowseConfig(), self.clock.now())
        self.assertEqual([row["text"] for row in rows], ["现在窗边有花\n「图片」"])
        self.assertNotIn("private", json.dumps(rows))

    async def test_large_reading_window_really_retains_and_reads_500_or_1000_messages(self):
        for count in (500, 1000):
            with self.subTest(count=count):
                self.configure(max_messages=count)
                self.engine.world.context.chat_history_max = 20
                state = WorldState(S)
                for i in range(count + 10):
                    state.note_chat(user_id=str(i % 5), name="小明", text=f"第{i}条：今天聊起窗边的花",
                        now=self.clock.now() - 7 * 3600, origin=S, keep=self.engine.chat_history_limit())
                view = reading_view(state, S, self.engine.world.chat_browse, self.clock.now())
                self.assertEqual(len(state.recent_chat), count)
                self.assertEqual(len(view), count)
                self.assertEqual((view[0]["seq"], view[-1]["seq"]), (11, count + 10))
                self.assertLessEqual(sum(len(row["text"]) for row in view), 60000)
                # Reading cache capacity does not enlarge the normal chat prompt view.
                self.engine.world.context.chat_lines = 4
                self.engine.world.decider.chat_window_minutes = 720
                self.assertEqual(len(self.engine.chat_window(state, S)), 4)
        self.configure(enabled=False)
        self.engine.world.context.chat_history_max = 20
        self.assertEqual(self.engine.chat_history_limit(), 20)

    async def test_new_defaults_upgrade_the_old_preset_and_preserve_custom_reading(self):
        config = ChatBrowseConfig()
        self.assertEqual((config.window_hours, config.max_messages, config.max_chars), (12, 500, 60000))
        old = {"enabled": True, "phone": True, "window_hours": 6, "max_messages": 20,
               "max_chars": 3000, "daily_max": 2, "cooldown_minutes": 180}
        updated = normalize_legacy_keys({"chat_browse": old})["chat_browse"]
        self.assertEqual((updated["window_hours"], updated["max_messages"], updated["max_chars"]), (12, 500, 60000))
        self.assertEqual((updated["enabled"], updated["daily_max"], updated["cooldown_minutes"]), (True, 2, 180))
        self.assertEqual(old["max_messages"], 20)
        for changes in ({"max_messages": 35}, {"max_chars": 4500}, {"window_hours": 8}):
            custom = {**old, **changes}
            self.assertEqual(normalize_legacy_keys({"chat_browse": custom})["chat_browse"], custom)
        with self.assertRaises(ValueError):
            ChatBrowseConfig(max_messages=1001)

    async def test_image_reading_keeps_only_real_text_and_a_placeholder(self):
        self.configure()
        async with self.engine.session_state(S) as state:
            self.engine._note_presence(state, MessageContext(session_id=S, user_id="42", user_name="小明",
                text="看看今天的花\n［图像转述：秘密画面描述］", raw_text="看看今天的花",
                has_images=True, chat_images=["file:///private/picture.png"], message_id="image-with-text"))
            state.recent_chat[-1]["captions"] = {"file:///private/picture.png": "不能读出的详细描述"}
            self.engine._note_presence(state, MessageContext(session_id=S, user_id="43", user_name="小红",
                text="［图片转述：另一个秘密］", raw_text="", has_images=True, message_id="image-only"))
            state.note_chat(user_id="44", name="小李", text="旧版的识图描述，来源已无法确认",
                images=["https://private/image"], now=self.clock.now(), origin=S, keep=500, message_id="legacy")
            rows = reading_view(state, S, self.engine.world.chat_browse, self.clock.now())
            self.assertEqual([row["text"] for row in rows], ["看看今天的花\n「图片」", "「图片」", "「图片」"])
            packed = json.dumps(rows, ensure_ascii=False)
            for forbidden in ("秘密", "private", "详细描述", "识图描述", "captions", "images"):
                self.assertNotIn(forbidden, packed)
            restored = WorldState.from_payload(state.to_payload(), S)
            self.assertEqual(reading_view(restored, S, self.engine.world.chat_browse, self.clock.now()), rows)
            self.assertTrue(self.engine.start_chat_browse(state, "browse_phone"))
            self.assertTrue(self.engine._browse_valid(state, state.current_action))

    async def test_image_only_with_no_resolvable_reference_is_still_visible(self):
        state = WorldState(S)
        state.note_chat(user_id="42", name="小明", text="", has_images=True,
                        now=self.clock.now(), origin=S)
        view = reading_view(state, S, ChatBrowseConfig(), self.clock.now())
        self.assertEqual([row["text"] for row in view], ["「图片」"])

    async def test_normal_cache_rollover_preserves_reading_but_manual_clear_cancels_it(self):
        for clear in (False, True):
            with self.subTest(clear=clear):
                action = await self.prepare()
                async def incoming(*args, **kwargs):
                    async with self.engine.session_state(S) as state:
                        for i in range(510):
                            state.note_chat(user_id="43", name="小红", text=f"新消息{i}",
                                now=self.clock.now(), origin=S, keep=500)
                        self.assertTrue(self.engine._browse_valid(state, action))
                    if clear:
                        await self.engine.clear_chat_context(S)
                    return json.dumps({"inner": "翻过的消息让我想起窗边的花", "source_seqs": [action["browse_rows"][0]["seq"]]})
                self.engine._ask_judge = AsyncMock(side_effect=incoming)
                await self.engine._complete_chat_browse(S, action["id"])
                async with self.engine.session_state(S) as state:
                    self.assertEqual(any(row["content"] == "翻过的消息让我想起窗边的花" for row in state.thoughts), not clear)
                    state.chat_browsing, state.recent_chat, state.thoughts = {}, [], []

    async def test_shared_group_source_whitelist_and_computer_location(self):
        self.add_group(sessions=[S, G, P])
        self.configure(phone=False, computer_nodes=["study"], sessions=[P])
        async with self.engine.session_state(G) as state:
            self.note(state, origin=S)
            self.note(state, origin=P)
            self.assertFalse(self.engine.start_chat_browse(state, "browse_phone"))
            self.assertTrue(self.engine.start_chat_browse(state, "browse_computer"))
            self.assertEqual(state.current_action["browse_session"], P)
            self.engine._stop_chat_browse(state, state.current_action)
            state.chat_browsing = {}
            state.node_id = "outside"
            self.assertFalse(self.engine.start_chat_browse(state, "browse_computer"))
        self.store.add_session("aiocqhttp:GroupMessage:9999", session_type="group", platform="aiocqhttp")
        self.engine.reload_config()
        other = await self.engine.load_state("aiocqhttp:GroupMessage:9999")
        self.note(other, origin=P)
        self.assertFalse(self.engine.start_chat_browse(other, "browse_computer"))

    async def test_success_ignores_instructions_and_preserves_reply_affinity_and_profiles(self):
        action = await self.prepare()
        before = await self.engine.load_state(S)
        watermarks = (before.chat_replied_until, before.chat_replied_seq, copy.deepcopy(before.chat_watermarks))
        people = self.engine.profiles.list_people(S)
        state = await self.complete(action, valence_delta=100, actions=[{"type": "say", "messages": ["发出去"]}],
            tools=[{"name": "shell", "params": {"command": "bad"}}], facts=[{"text": "伪造事实"}])
        self.assertIsNone(state.current_action)
        self.assertEqual(state.chat_browsing["sessions"][S]["seq"], action["browse_rows"][-1]["seq"])
        self.assertEqual((state.chat_replied_until, state.chat_replied_seq, state.chat_watermarks), watermarks)
        self.assertEqual(self.engine.profiles.list_people(S), people)
        self.assertLessEqual(abs(state.valence_offset - before.valence_offset), .02001)
        self.assertEqual(self.messenger.sent, [])
        self.assertEqual(self.tools.calls, [])
        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual(self.engine.profiles.list_people(S), [])
        self.assertIn("主观", feeling_context(state, S, self.clock.now()))

    async def test_empty_feeling_consumes_cursor_without_memory_and_no_repeat(self):
        action = await self.prepare()
        state = await self.complete(action, inner="", source_seqs=[])
        self.assertEqual(state.thoughts, [])
        status = self.engine.chat_browse_status(state, S)
        self.assertEqual((status["started_today"], status["completed_today"], status["last_result"]), (1, 1, "quiet"))
        self.assertEqual(status["reason"], "等待最短浏览间隔结束")
        self.clock.advance(7201)
        self.assertFalse(self.engine.start_chat_browse(state, "browse_phone"))
        self.assertFalse(any(row["source"] == "chat_browse" for row in self.engine.memory.db.query_memories(session_id=S)))

    async def test_idle_reading_no_longer_depends_on_random_chance(self):
        self.configure(cooldown_minutes=120)
        async with self.engine.session_state(S) as state:
            self.note(state)
            state.energy = .8
            self.engine.rng = Mock(random=Mock(return_value=1), choice=Mock(return_value="browse_phone"), randint=Mock(return_value=3))
            self.assertTrue(self.engine.maybe_start_chat_browse(state))
        self.engine.rng.random.assert_not_called()

    async def test_enabled_reply_can_still_choose_silence_and_scene_is_not_retried(self):
        raw = self.store.raw_world()
        raw.setdefault("decider", {})["natural_silence"] = True
        self.store.save_world(raw)
        action = await self.prepare(reply_enabled=True)
        state = await self.complete(action, decision='{"say":"","reply_to":""}')
        self.assertEqual(len(self.llm.calls), 2)
        self.assertIn("自然沉默：开启", self.llm.calls[-1]["system_prompt"])
        self.assertEqual(self.messenger.sent, [])
        self.assertFalse(state.thoughts[-1]["browse_replied"])
        self.assertIsNone(await self.engine.maybe_participate(S, source="browse"))
        self.assertEqual(len(self.llm.calls), 2)

    async def test_reply_uses_live_target_and_quote_without_life_sharing_or_message_driven(self):
        action = await self.prepare(reply_enabled=True)
        self.engine.world.life_share_session_whitelist = []
        self.engine.world.decider.message_driven = False
        # A stable reading identity was captured before changing message-driven config.
        async with self.engine.session_state(S) as state:
            state.current_action["identity"] = self.engine._browse_identity()
            self.note(state, "这盆花该放哪里？", message_id="flower-live")
        quotes = []
        async def send(target, messages):
            quotes.append((target, reply_target(target)))
            self.messenger.sent.append((target, list(messages)))
            return True
        self.messenger.send_text = send
        state = await self.complete(action, decision='{"say":"窗边有散光的地方就很好。","reply_to":"m2"}')
        self.assertEqual(self.messenger.sent, [(S, ["窗边有散光的地方就很好。"])])
        self.assertEqual(quotes, [(S, "flower-live")])
        self.assertTrue(state.thoughts[-1]["browse_replied"])
        self.assertFalse(state.awaiting_reply)
        self.assertEqual(self.engine.contact_status(S)["left"], 6)
        self.assertIn("不主动介绍自己的日程", self.llm.calls[1]["system_prompt"])
        self.assertIn("自然沉默：关闭", self.llm.calls[1]["system_prompt"])
        self.assertNotIn("没有想说的就放下", self.llm.calls[1]["system_prompt"])
        self.assertFalse(self.engine.chat_browse_status(state, S)["natural_silence"])

    async def test_expired_chat_is_not_replied_to_after_reading(self):
        action = await self.prepare(reply_enabled=True)
        self.clock.advance(901)
        state = await self.complete(action, decision='{"say":"过时的接话"}')
        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual(self.messenger.sent, [])
        self.assertEqual(state.chat_browsing["sessions"][S]["result"], "feeling")

    async def test_reading_duration_does_not_expire_the_message_she_just_read(self):
        action = await self.prepare(reply_enabled=True, message_id="flower-browsed")
        self.clock.advance(8 * 60)
        quotes = []
        async def send(target, messages):
            quotes.append((target, reply_target(target)))
            self.messenger.sent.append((target, list(messages)))
            return True
        self.messenger.send_text = send
        state = await self.complete(action, decision='{"say":"花开在窗边真好。","reply_to":"m1"}')
        self.assertEqual(len(self.llm.calls), 2)
        self.assertEqual(self.messenger.sent, [(S, ["花开在窗边真好。"])])
        self.assertEqual(quotes, [(S, "flower-browsed")])
        self.assertTrue(state.thoughts[-1]["browse_replied"])

    async def test_browse_reply_expiring_during_generation_is_not_sent(self):
        action = await self.prepare(reply_enabled=True)
        self.clock.advance(8 * 60)
        self.llm.replies = ['{"say":"已经错过的话题"}']
        original = self.llm.generate
        async def slow(**kwargs):
            self.clock.advance(7 * 60 + 1)
            return await original(**kwargs)
        self.llm.generate = slow
        self.engine._ask_judge = AsyncMock(return_value=json.dumps({
            "inner": "看完了，先不说话。", "source_seqs": [action["browse_rows"][0]["seq"]]}))
        await self.engine._complete_chat_browse(S, action["id"])
        state = await self.engine.load_state(S)
        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual(self.messenger.sent, [])
        self.assertFalse(state.thoughts[-1]["browse_replied"])
        self.assertEqual(state.chat_browsing["sessions"][S]["result"], "feeling")

    async def test_revoking_speech_during_reading_preserves_feeling_and_stops_reply(self):
        action = await self.prepare(reply_enabled=True)
        async def revoke(*args, **kwargs):
            self.engine.world.proactive_session_whitelist = []
            return json.dumps({"inner": "读完只留一点自己的想法", "source_seqs": [action["browse_rows"][0]["seq"]]})
        self.engine._ask_judge = AsyncMock(side_effect=revoke)
        await self.engine._complete_chat_browse(S, action["id"])
        state = await self.engine.load_state(S)
        self.assertEqual(state.thoughts[-1]["content"], "读完只留一点自己的想法")
        self.assertEqual(self.llm.calls, [])
        self.assertEqual(self.messenger.sent, [])

    async def test_normal_reply_in_progress_defers_optional_browse_reply(self):
        action = await self.prepare(reply_enabled=True)
        turn = {"session_id": S, "started_at": self.clock.now()}
        self.engine.begin_native_turn(turn)
        state = await self.complete(action, decision='{"say":"不该抢答"}')
        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual(self.messenger.sent, [])
        self.assertTrue(state.thoughts)

    async def test_browse_reply_can_use_private_source_without_leaking_to_shared_group(self):
        self.add_group(sessions=[S, P])
        action = await self.prepare(source=P, reply_enabled=True)
        self.engine.world.life_share_session_whitelist = []
        state = await self.complete(action, decision='{"say":"那就把花放在窗边试试。"}')
        self.assertEqual(self.messenger.sent, [(P, ["那就把花放在窗边试试。"])])
        self.assertEqual(state.thoughts[-1]["browse_session"], P)
        self.assertTrue(state.thoughts[-1]["browse_replied"])
        self.assertEqual((await self.engine.thoughts_payload(S))["thoughts"], [])

    async def test_browse_permission_does_not_grant_speech_in_other_shared_sessions(self):
        self.add_group(sessions=[S, P])
        action = await self.prepare(source=P, reply_enabled=True)
        self.engine.world.proactive_session_whitelist = [S]
        state = await self.complete(action, decision='{"say":"不该发到私聊"}')
        self.assertEqual(self.messenger.sent, [])
        self.assertEqual(len(self.llm.calls), 1)
        self.assertTrue(state.thoughts)

    async def test_clear_browse_subpage_preserves_progress_inner_and_other_source(self):
        self.add_group(sessions=[S, P])
        action = await self.prepare()
        state = await self.complete(action)
        async with self.engine.session_state(S) as state:
            state.add_thought("普通生活想法", source="自主生活")
            state.add_thought("只属于私聊的阅读感想", source="翻消息时")
            state.thoughts[-1].update(browse_session=P, expires_at=self.clock.now() + 3600)
            progress = copy.deepcopy(state.chat_browsing)
        self.assertEqual(await self.engine.clear_thoughts(S, "browse"), 1)
        state = await self.engine.load_state(S)
        self.assertEqual([row["content"] for row in state.thoughts], ["普通生活想法", "只属于私聊的阅读感想"])
        self.assertEqual(state.chat_browsing, progress)
        self.assertEqual(await self.engine.clear_thoughts(S, "inner"), 1)
        self.assertEqual([row["content"] for row in (await self.engine.load_state(S)).thoughts], ["只属于私聊的阅读感想"])

    async def test_empty_feeling_without_citations_still_consumes_the_reading_cursor(self):
        action = await self.prepare()
        self.llm.replies = [json.dumps({"inner": "", "meaningful": False})]
        await self.engine._complete_chat_browse(S, action["id"])
        state = await self.engine.load_state(S)
        self.assertEqual(state.thoughts, [])
        self.assertEqual(state.chat_browsing["sessions"][S]["seq"], action["browse_rows"][-1]["seq"])
        self.assertIsNone(state.current_action)

    async def test_invalid_citation_does_not_commit_progress(self):
        action = await self.prepare()
        state = await self.complete(action, source_seqs=[99999])
        self.assertNotIn("sessions", state.chat_browsing)
        self.assertEqual(state.thoughts, [])

    async def test_cooldown_daily_cap_and_next_day_are_shared(self):
        self.add_group(sessions=[S, G])
        action = await self.prepare()
        await self.complete(action)
        async with self.engine.session_state(G) as state:
            self.note(state, origin=G)
            self.assertFalse(self.engine.start_chat_browse(state, "browse_phone"))
            state.chat_browsing.update(next_at=0, count=3)
            self.assertFalse(self.engine.start_chat_browse(state, "browse_phone"))
        self.clock.advance(86400)
        async with self.engine.session_state(G) as state:
            self.note(state, "新的一天的消息", origin=G)
            self.assertTrue(self.engine.start_chat_browse(state, "browse_phone"))
            self.assertEqual(state.chat_browsing["count"], 1)

    async def test_sleep_low_energy_schedule_and_busy_win(self):
        self.configure()
        async with self.engine.session_state(S) as state:
            self.note(state)
            for change in [{"state": "sleeping"}, {"state": "drowsy"}, {"energy": .2}, {"current_action": {"type": "read"}}]:
                saved = {key: getattr(state, key) for key in change}
                for key, value in change.items():
                    setattr(state, key, value)
                self.assertFalse(self.engine.start_chat_browse(state, "browse_phone"), change)
                for key, value in saved.items():
                    setattr(state, key, value)
            with unittest.mock.patch.object(self.engine, "life_choice", return_value={"kind": "schedule"}):
                self.assertFalse(self.engine.start_chat_browse(state, "browse_phone"))

    async def test_private_feelings_do_not_leak_to_group_or_autonomous_prompt(self):
        self.add_group(sessions=[S, P])
        action = await self.prepare(source=P)
        await self.complete(action, inner="私聊后的独有感受")
        private = await self.engine.thoughts_payload(P)
        group = await self.engine.thoughts_payload(S)
        self.assertEqual(len(private["thoughts"]), 1)
        self.assertEqual(group["thoughts"], [])
        self.assertEqual(private["thoughts"][0]["browse_session"], P)
        self.assertNotIn("私聊后的独有感受", await self.engine.preview_autonomous_prompt(S))
        self.assertNotIn("私聊后的独有感受", await self.engine.preview_autonomous_prompt(P))
        state = await self.engine.load_state(S)
        for sid, visible in [(P, True), (S, False)]:
            _, prompt = self.engine.prompts.build_dialogue_prompt(persona_text="人设", state=state, node=None, current_session=sid)
            self.assertEqual("私聊后的独有感受" in prompt, visible)
        self.clock.advance(86401)
        self.assertEqual((await self.engine.thoughts_payload(P))["thoughts"], [])
        self.assertEqual(feeling_context(state, P, self.clock.now()), "")

    async def test_meaningful_memory_retains_source_but_never_enters_review_or_generic_runtime(self):
        self.add_group(sessions=[S, P])
        action = await self.prepare(source=P)
        state = await self.complete(action, meaningful=True, reason="想在自己的窗边慢慢养花")
        rows = self.engine.memory.db.query_memories(session_id=P)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["source"], rows[0]["type"]), ("chat_browse", "inner"))
        self.assertNotIn("大家聊起", rows[0]["context"])
        memories = self.engine.memory.recall(session_id=P, persona_id="", mode="global", now=self.clock.now(), include_reading=True)
        self.assertEqual(len([row for row in memories if row.source == "chat_browse"]), 1)
        self.assertFalse(any(row.source == "chat_browse" for row in self.engine.memory.recall(session_id=S, persona_id="", mode="global", include_reading=True)))
        self.assertFalse(any(row.source == "chat_browse" for row in self.engine.memory.recall(session_id=P, persona_id="", mode="global")))
        _, passive = self.engine.prompts.build_dialogue_prompt(persona_text="人设", state=state, node=None, current_session=P, memories=memories)
        self.assertEqual(passive.count("也想在自己的窗边留一小盆花。"), 1)
        self.clock.advance(86401)
        _, passive = self.engine.prompts.build_dialogue_prompt(persona_text="人设", state=state, node=None, current_session=P, memories=memories)
        self.assertIn("阅读本会话曾留下的感想", passive)
        self.assertNotIn("也想在", self.engine.prompts.runtime_layer(state, node=None, memories=memories, current_session=P))
        self.assertNotIn("也想在", "\n".join(self.engine.consolidator.memory_lines(S)))
        self.engine.memory.schedule_review(rows[0]["id"], next_at=self.clock.now() - 10)
        self.assertEqual(self.engine.memory.due_reviews(session_ids=[S, P], now=self.clock.now()), [])

    async def test_slow_inference_leaves_lock_available_and_accepts_new_messages(self):
        action = await self.prepare()
        started, release = asyncio.Event(), asyncio.Event()
        async def slow(**kwargs):
            started.set()
            await release.wait()
            return LLMReply(ok=True, text=json.dumps({"inner": "想看看花", "source_seqs": [action["browse_rows"][0]["seq"]]}))
        self.llm.generate = slow
        task = asyncio.create_task(self.engine._complete_chat_browse(S, action["id"]))
        try:
            await asyncio.wait_for(started.wait(), 2)
            async with asyncio.timeout(1):
                async with self.engine.session_state(S) as state:
                    self.note(state, "新来的另一条消息")
            release.set()
            await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        state = await self.engine.load_state(S)
        self.assertEqual(state.thoughts[-1]["content"], "想看看花")
        self.assertLess(state.chat_browsing["sessions"][S]["seq"], state.chat_seq)

    async def test_changed_activity_is_never_cleared_by_stale_job(self):
        action = await self.prepare()
        async with self.engine.session_state(S) as state:
            state.current_action = {"type": "read", "id": "new-reading"}
            state.state = "reading"
        await self.engine._complete_chat_browse(S, action["id"])
        state = await self.engine.load_state(S)
        self.assertEqual(state.current_action["id"], "new-reading")
        self.assertEqual(self.llm.calls, [])
        self.engine._stop_chat_browse(state, {})
        self.assertEqual(state.state, "reading")

    async def test_reading_completion_preserves_a_newly_queued_normal_plan(self):
        action = await self.prepare()
        async with self.engine.session_state(S) as state:
            state.current_plan = {"steps": [{"action": "read"}], "source": "reply", "expires_at": self.clock.now() + 1800}
        state = await self.complete(action)
        self.assertIsNone(state.current_action)
        self.assertEqual(state.current_plan["source"], "reply")

    async def test_stale_result_is_discarded_after_interrupt_edit_persona_or_clear(self):
        for change in ["interrupt", "persona", "clear", "disabled", "day", "location", "evidence"]:
            with self.subTest(change=change):
                action = await self.prepare()
                self.engine._ask_judge = AsyncMock()
                async def mutate(*args, **kwargs):
                    if change == "persona":
                        self.persona.text = "另一份人设"
                    elif change == "disabled":
                        self.configure(enabled=False)
                    elif change == "day":
                        self.clock.advance(86400)
                    async with self.engine.session_state(S) as state:
                        if change == "interrupt":
                            state.current_action = {"type": "read", "id": "new-reading"}
                        elif change == "clear":
                            state.recent_chat = []
                        elif change == "location":
                            state.node_id = "bedroom"
                        elif change == "evidence":
                            state.recent_chat[-1]["text"] = "被编辑的原文"
                    return json.dumps({"inner": "过期感想", "source_seqs": [action["browse_rows"][0]["seq"]]})
                self.engine._ask_judge.side_effect = mutate
                await self.engine._complete_chat_browse(S, action["id"])
                state = await self.engine.load_state(S)
                self.assertFalse(any(row["content"] == "过期感想" for row in state.thoughts))
                if change == "interrupt":
                    self.assertEqual(state.current_action["id"], "new-reading")
                async with self.engine.session_state(S) as state:
                    state.chat_browsing, state.recent_chat, state.thoughts = {}, [], []
                    state.node_id = "study"
                self.persona.text = "你是一个温柔黏人的少女。"

    async def test_failure_restart_and_duplicate_finish_do_not_replay(self):
        action = await self.prepare()
        self.engine._ask_judge = AsyncMock(side_effect=RuntimeError("provider down"))
        await self.engine._complete_chat_browse(S, action["id"])
        state = await self.engine.load_state(S)
        self.assertIsNone(state.current_action)
        self.assertEqual(state.chat_browsing["count"], 1)
        state.current_action = {**action, "inference_started": True}
        restored = WorldState.from_payload(state.to_payload(), S)
        self.assertEqual(restored.chat_browsing, state.chat_browsing)
        self.engine.finish_chat_browse(restored, restored.current_action)
        self.assertIsNone(restored.current_action)
        self.engine._complete_chat_browse = AsyncMock()
        state.current_action = action
        for _ in range(3):
            self.engine.finish_chat_browse(state, action)
        tasks = list(self.engine._background_jobs.values())
        await asyncio.gather(*tasks)
        self.engine._complete_chat_browse.assert_awaited_once()

    async def test_output_cap_call_attribution_and_budget_denial(self):
        from core.model_calls import call_context
        action = await self.prepare()
        observed = []
        async def generate(**kwargs):
            observed.append((kwargs["max_tokens"], call_context()))
            return LLMReply(ok=True, text='{"inner":"", "source_seqs":[]}')
        self.llm.generate = generate
        await self.engine._complete_chat_browse(S, action["id"])
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0][0], 600)
        self.assertEqual(observed[0][1]["purpose"], "chat_browse")
        async with self.engine.session_state(S) as state:
            state.chat_browsing = {}
        self.configure()
        raw = self.store.raw_world()
        raw.setdefault("limits", {})["background_daily_calls"] = 1
        self.store.save_world(raw)
        self.engine.reload_config()
        async with self.engine.session_state(S) as state:
            self.note(state, "另一条新消息")
            self.assertTrue(self.engine.start_chat_browse(state, "browse_phone"))
            action = copy.deepcopy(state.current_action)
        await self.engine._complete_chat_browse(S, action["id"])
        self.assertEqual(len(observed), 1)  # Previous request already used the day's budget.
        state = await self.engine.load_state(S)
        self.assertIsNone(state.current_action)
        self.assertEqual(state.thoughts, [])

    async def test_long_persona_keeps_important_traits_at_the_end(self):
        self.persona.text = "角色原有的世界背景。" * 1000 + "末尾的重要设定：不喜欢花，不能因阅读改变这个喜好。"
        action = await self.prepare()
        await self.complete(action, inner="", source_seqs=[])
        material = json.loads(self.llm.calls[-1]["prompt"])
        self.assertEqual(material["persona"], self.persona.text)
        self.assertIn("不能因阅读改变这个喜好", material["persona"])

    async def test_cancelled_inference_stops_on_next_finish_without_retry(self):
        action = await self.prepare()
        started = asyncio.Event()
        async def generate(**kwargs):
            started.set()
            await asyncio.Event().wait()
        self.llm.generate = generate
        task = asyncio.create_task(self.engine._complete_chat_browse(S, action["id"]))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        async with self.engine.session_state(S) as state:
            self.assertTrue(state.current_action["inference_started"])
            self.engine.finish_chat_browse(state, state.current_action)
            self.assertIsNone(state.current_action)
            self.assertNotIn("sessions", state.chat_browsing)

    async def test_mixed_action_batch_and_extension_cannot_publish(self):
        self.configure()
        self.engine.extensions.action_note = Mock(return_value="不能发出去")
        async with self.engine.session_state(S) as state:
            self.note(state)
            outcome = fixture.TickOutcome(session_id=S)
            actions = [PlannedAction(type="say", messages=["先发"]), PlannedAction(type="browse_phone"),
                       PlannedAction(type="say", messages=["后发"])]
            self.assertTrue(await self.engine._execute_actions(state, None, outcome, actions, depth=0, autonomous=True))
            self.assertEqual(outcome.messages, [])
            self.assertFalse(self.engine._action_speaks(ActionDef(id="browse_phone", visible=True)))
            self.engine.extensions.action_note.assert_not_called()
            self.engine.suspend_life(state, reason="该睡了", source="rest")
            self.assertEqual(state.life_resume, {})

    async def test_decider_can_read_with_proactive_speech_off_and_tick_finishes_silently(self):
        self.configure()
        async with self.engine.session_state(S) as state:
            self.note(state)
        self.engine.advance_life_matters = AsyncMock()
        self.engine.rng = Mock(random=Mock(return_value=0), choice=Mock(return_value="browse_phone"), randint=Mock(return_value=3))
        self.engine.proactive_chat_allowed = Mock(return_value=False)
        await self.engine.maybe_decide(S, force=True)
        state = await self.engine.load_state(S)
        self.assertEqual(state.current_action["type"], "browse_phone")
        self.engine._complete_chat_browse = AsyncMock()
        outcome = fixture.TickOutcome(session_id=S)
        await self.engine._finish_action(state, None, outcome, state.current_action)
        await asyncio.gather(*self.engine._background_jobs.values())
        self.assertEqual(outcome.messages, [])
        self.assertEqual(self.messenger.sent, [])
        self.engine._complete_chat_browse.assert_awaited_once()
