"""Reading opportunities survive brief contention without replaying consumed chat."""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from tests import test_chat_browse as fixture

S, G, P = fixture.S, fixture.G, fixture.P


class BrowseFollowupTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.ChatBrowseTests.asyncSetUp
    configure = fixture.ChatBrowseTests.configure
    note = fixture.ChatBrowseTests.note
    prepare = fixture.ChatBrowseTests.prepare
    complete = fixture.ChatBrowseTests.complete
    add_group = fixture.ChatBrowseTests.add_group

    async def blocked_reading(self, source=S, **config):
        action = await self.prepare(source=source, message_id="original", reply_enabled=True, **config)
        release = asyncio.Event()
        owner = asyncio.create_task(release.wait())
        self.addAsyncCleanup(self.stop_owner, owner)
        self.engine.register_incoming(source, "另一条直接回复", expected_owner=owner)
        await self.complete(action, decision='{"inner":"想问问花的情况。","intent":"continue_chat",'
                            '"say":"花现在开得怎么样？","reply_to":"m1"}')
        return action, owner, release

    async def stop_owner(self, owner):
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)

    async def settled(self):
        tasks = list(getattr(self.engine, "_participation_settlers", {}).values())
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks), 7)

    async def test_busy_reading_rechecks_once_after_reply_finishes_without_new_message(self):
        action, owner, release = await self.blocked_reading()
        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual(self.messenger.sent, [])
        release.set()
        await owner
        await self.settled()
        self.assertEqual(self.messenger.sent, [(S, ["花现在开得怎么样？"])])
        self.assertEqual(len(self.llm.calls), 2)
        state = await self.engine.load_state(S)
        thought = next(row for row in state.thoughts if row.get("browse_id") == action["id"])
        self.assertTrue(thought["browse_replied"])
        self.assertEqual(thought["reply"], "花现在开得怎么样？")
        self.assertEqual(len([row for row in state.thoughts if row.get("browse_id") == action["id"]]), 1)

    async def test_reply_consuming_the_scene_cancels_the_reading_opportunity(self):
        _, owner, release = await self.blocked_reading()
        async with self.engine.session_state(S) as state:
            self.engine.mark_chat_replied(state, S)
        release.set()
        await owner
        await self.settled()
        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual(self.messenger.sent, [])

    async def test_waiting_does_not_occupy_a_model_worker_and_close_cleans_up(self):
        await self.blocked_reading()
        await asyncio.sleep(.05)
        self.assertTrue(self.engine._participation_settlers)
        self.assertFalse(getattr(self.engine, "_background_running_jobs", 0))
        await self.engine.close_background()
        await asyncio.sleep(0)
        self.assertEqual(self.engine._participation_settlers, {})
        self.assertEqual(self.engine._participation_browses, {})

    async def test_zero_wait_preserves_the_previous_no_retry_behavior(self):
        _, owner, release = await self.blocked_reading(reply_wait_seconds=0)
        release.set()
        await owner
        await self.settled()
        self.assertEqual(len(self.llm.calls), 1)
        self.assertFalse(getattr(self.engine, "_participation_settlers", {}))

    async def test_wait_deadline_and_original_scene_age_cannot_be_extended_by_new_chat(self):
        _, owner, release = await self.blocked_reading(reply_wait_seconds=1)
        self.clock.advance(2)
        async with self.engine.session_state(S) as state:
            self.note(state, "后来全新的话题", message_id="new-topic")
        release.set()
        await owner
        await self.settled()
        self.assertEqual(len(self.llm.calls), 1)
        state = await self.engine.load_state(S)
        self.assertEqual(state.thoughts[-1]["detail"]["browse_reply"]["status"], "expired")

    async def test_expired_original_chat_is_not_refreshed_by_later_chat(self):
        _, owner, release = await self.blocked_reading()
        self.clock.advance(901)
        async with self.engine.session_state(S) as state:
            self.note(state, "后来全新的话题", message_id="new-topic")
        release.set()
        await owner
        await self.settled()
        self.assertEqual(len(self.llm.calls), 1)

    async def test_later_chat_is_context_but_not_a_reply_target_for_deferred_reading(self):
        _, owner, release = await self.blocked_reading()
        async with self.engine.session_state(S) as state:
            self.note(state, "后来有人告诉大家花开了", message_id="later")
        self.llm.replies = ['{"intent":"continue_chat","say":"错接新话题","reply_to":"m2"}'] * 2
        release.set()
        await owner
        await self.settled()
        self.assertEqual(self.messenger.sent, [])
        self.assertEqual(len(self.llm.calls), 3)  # One decision, one bounded format repair.
        scene = json.loads(self.llm.calls[1]["prompt"].split("\n")[1])
        self.assertTrue(scene[0]["can_reply"])
        self.assertNotIn("can_reply", scene[1])
        self.assertIn("后来有人告诉大家花开了", self.llm.calls[1]["prompt"])

    async def test_waiting_can_end_in_a_real_listening_decision_without_retry(self):
        _, owner, release = await self.blocked_reading()
        self.llm.replies = ['{"intent":"listen","motivation":"他们已经商量好了。"}']
        release.set()
        await owner
        await self.settled()
        self.assertEqual(len(self.llm.calls), 2)
        state = await self.engine.load_state(S)
        self.assertEqual(state.thoughts[-1]["detail"]["speech"]["intent"], "listen")
        self.assertEqual(self.messenger.sent, [])
        self.assertFalse(self.engine._participation_browses)

    async def test_revoked_permission_ends_the_wait_without_model_calls(self):
        _, owner, release = await self.blocked_reading()
        self.engine.world.proactive_session_whitelist = []
        release.set()
        await owner
        await self.settled()
        self.assertEqual(len(self.llm.calls), 1)
        state = await self.engine.load_state(S)
        self.assertEqual(state.thoughts[-1]["detail"]["browse_reply"]["status"], "paused")

    async def test_disabled_reading_and_changed_persona_end_the_wait(self):
        for change in ("enabled", "persona", "source", "replacement", "checked", "rest"):
            with self.subTest(change=change):
                await self.asyncSetUp()
                _, owner, release = await self.blocked_reading()
                async with self.engine.session_state(S) as state:
                    if change == "enabled":
                        self.engine.world.chat_browse.enabled = False
                    elif change == "persona":
                        self.persona.text = "另一个角色"
                    elif change == "source":
                        self.engine.world.chat_browse.sessions = [G]
                    elif change == "replacement":
                        state.chat_browsing["sessions"][S]["browse_id"] = "new-reading"
                    elif change == "checked":
                        state.participation[S] = {"checked_seq": state.chat_seq}
                    else:
                        state.energy = .1
                release.set()
                await owner
                await self.settled()
                self.assertEqual(len(self.llm.calls), 1)
                self.assertEqual(self.messenger.sent, [])

    async def test_clearing_chat_or_reading_cancels_waiting(self):
        for clear in ("chat", "thought"):
            with self.subTest(clear=clear):
                await self.asyncSetUp()
                _, owner, release = await self.blocked_reading()
                if clear == "chat":
                    await self.engine.clear_chat_context(S)
                else:
                    await self.engine.clear_thoughts(S, "browse")
                release.set()
                await owner
                await self.settled()
                self.assertEqual(len(self.llm.calls), 1)
                self.assertEqual(self.messenger.sent, [])

    async def test_private_reading_retries_with_message_driven_off_without_ambient_opportunity(self):
        self.add_group(sessions=[S, P])
        # Browse has its own explicit reply authorization.
        raw = self.store.raw_world()
        raw.setdefault("decider", {})["enabled"] = False
        raw["decider"]["message_driven"] = False
        self.store.save_world(raw)
        self.engine.reload_config()
        _, owner, release = await self.blocked_reading(source=P)
        release.set()
        await owner
        await self.settled()
        self.assertEqual(self.messenger.sent, [(P, ["花现在开得怎么样？"])])
        self.assertFalse(self.engine._participation_pending)
        state = await self.engine.load_state(S)
        self.assertFalse((state.participation.get(P) or {}).get("attempts"))

    async def test_waiting_display_expires_without_mutating_persisted_history(self):
        await self.blocked_reading(reply_wait_seconds=1)
        self.clock.advance(2)
        payload = await self.engine.thoughts_payload(S)
        self.assertEqual(payload["thoughts"][0]["detail"]["browse_reply"]["status"], "expired")
        await self.engine.close_background()
        state = await self.engine.load_state(S)
        state.thoughts[-1]["detail"]["browse_reply"].update(status="waiting", until=self.clock.now() + 120)
        await self.engine.save_state(state)
        payload = await self.engine.thoughts_payload(S)
        self.assertEqual(payload["thoughts"][0]["detail"]["browse_reply"]["status"], "expired")
        self.assertEqual((await self.engine.load_state(S)).thoughts[-1]["detail"]["browse_reply"]["status"], "waiting")

    async def test_ordinary_opportunity_covering_scene_prevents_duplicate_browse_judgement(self):
        _, owner, release = await self.blocked_reading()
        self.engine.world.decider.settle_seconds = 0
        self.engine.queue_participation(S)
        self.engine.participation_sample = lambda *args: True
        self.llm.replies = ['{"inner":"先听他们继续。","intent":"listen"}']
        release.set()
        await owner
        await self.settled()
        self.assertEqual(len(self.llm.calls), 2)
        self.assertEqual(self.messenger.sent, [])

    async def test_brief_activity_finishes_before_rechecking_the_same_reading(self):
        from core.engine.participation_queue import wake
        action = await self.prepare(message_id="original", reply_enabled=True)
        async with self.engine.session_state(S) as state:
            state.current_plan = {"source": "reply", "id": "finishing", "actions": []}
        await self.complete(action, decision='{"intent":"continue_chat","say":"花开得真好。","reply_to":"m1"}')
        self.assertEqual(len(self.llm.calls), 1)
        async with self.engine.session_state(S) as state:
            state.current_plan = None
        wake(self.engine, S)
        await self.settled()
        self.assertEqual(self.messenger.sent, [(S, ["花开得真好。"])])

    async def test_worker_admission_after_deadline_cannot_call_the_model(self):
        _, owner, release = await self.blocked_reading(reply_wait_seconds=2)
        self.engine.world.limits.background_concurrency = 1
        entered, unblock = asyncio.Event(), asyncio.Event()

        async def occupy():
            entered.set()
            await unblock.wait()
        blocker = self.engine.queue_background("test:occupy", occupy)
        try:
            await asyncio.wait_for(entered.wait(), 2)
            release.set()
            await owner
            async with asyncio.timeout(2):
                while "participation:" + self.engine.state_key(S) not in self.engine._background_jobs:
                    await asyncio.sleep(.01)
            self.clock.advance(3)
            unblock.set()
            await blocker
            await self.settled()
            self.assertEqual(len(self.llm.calls), 1)
            self.assertEqual(self.messenger.sent, [])
        finally:
            unblock.set()
            await asyncio.gather(blocker, return_exceptions=True)

    async def test_other_session_in_same_life_keeps_its_ordinary_opportunity(self):
        self.add_group(sessions=[S, G])
        _, owner, release = await self.blocked_reading()
        self.engine.world.decider.settle_seconds = 0
        self.engine.participation_sample = lambda *args: True
        async with self.engine.session_state(G) as state:
            self.note(state, "群二的花也开了", origin=G, message_id="group-two")
        self.engine.queue_participation(G)
        release.set()
        await owner
        self.llm.replies = ['{"inner":"原群的花。","intent":"continue_chat","say":"花现在开得怎么样？","reply_to":"m1"}',
                            '{"inner":"另一个群也聊花。","intent":"join_chat","say":"群二也开花了呀。"}']
        await self.settled()
        self.assertEqual({target for target, _ in self.messenger.sent}, {S, G})
        self.assertEqual(len(self.llm.calls), 3)

    async def test_life_remapping_cancels_the_original_ticket(self):
        _, owner, release = await self.blocked_reading()
        self.add_group(sessions=[S, G], main=G)
        release.set()
        await owner
        await self.settled()
        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual(self.messenger.sent, [])

    async def test_waiting_without_proven_message_id_never_calls_a_speech_model(self):
        _, owner, release = await self.blocked_reading()
        async with self.engine.session_state(S) as state:
            state.recent_chat[0]["message_id"] = ""
        release.set()
        await owner
        await self.settled()
        self.assertEqual(len(self.llm.calls), 1)

    async def test_relationship_context_and_profile_switches_avoid_lookups(self):
        for switch in ("relationship_context", "profile"):
            with self.subTest(switch=switch):
                await self.asyncSetUp()
                action = await self.prepare(relationship_context=switch != "relationship_context")
                if switch == "profile":
                    self.engine.world.profile.enabled = False
                    async with self.engine.session_state(S) as state:
                        state.current_action["identity"] = self.engine._browse_identity()
                self.engine.reading_person = Mock(side_effect=AssertionError("Unexpected profile lookup"))
                await self.complete(action)
                self.assertEqual(json.loads(self.llm.calls[0]["prompt"])["people"], [])

    async def test_summary_budget_bounds_author_lookups_and_skips_unknown_ids(self):
        from core.chat_browse import reading_people
        lookup = Mock(return_value=SimpleNamespace(name="甲" * 100, affinities=["熟悉"],
            level=SimpleNamespace(name="信任"), digest="花" * 500, facts=[]))
        rows = [{"user_id": str(i), "self": False} for i in range(100)]
        rows.append({"name": "昵称猜不出身份", "self": False})
        rows.append({"user_id": "self", "self": True})
        people = reading_people(rows, lookup)
        self.assertLessEqual(lookup.call_count, 6)
        self.assertLessEqual(len(json.dumps(people, ensure_ascii=False, separators=(",", ":"))), 1200)
        self.assertNotIn("self", [call.args[0] for call in lookup.call_args_list])

    async def test_local_structured_interests_are_included_without_copying_other_fields(self):
        self.engine.profiles.touch(S, "42", "小明", now=self.clock.now())
        service = SimpleNamespace(enabled=True, detail_for=Mock(return_value={"fields": [
            {"name": "爱好", "value": "栽花", "shared": False},
            {"name": "喜欢的话题", "value": "园艺", "shared": False},
            {"name": "爱好", "value": "其他范围的兴趣", "shared": True},
            {"name": "健康状况", "value": "无关隐私", "shared": False}]}))
        self.engine.profile_weaver_service = service
        action = await self.prepare()
        await self.complete(action)
        people = json.loads(self.llm.calls[0]["prompt"])["people"]
        self.assertIn("栽花", json.dumps(people, ensure_ascii=False))
        self.assertNotIn("其他范围的兴趣", json.dumps(people, ensure_ascii=False))
        self.assertNotIn("无关隐私", json.dumps(people, ensure_ascii=False))
        service.detail_for.assert_called_once_with(S, "42")

    async def test_reading_model_gets_only_actual_authors_from_source_profiles(self):
        self.add_group(sessions=[S, G, P])
        self.engine.profiles.touch(S, "42", "小明", now=self.clock.now())
        self.engine.profiles.set_digest(S, "42", "喜欢种花，平时会分享窗边植物的近况。")
        self.engine.profiles.touch(G, "42", "另一个群的名字", now=self.clock.now())
        self.engine.profiles.set_digest(G, "42", "别的群的秘密")
        self.engine.profiles.touch(P, "42", "私聊身份", now=self.clock.now())
        self.engine.profiles.set_digest(P, "42", "私聊秘密")
        self.engine.profiles.touch(S, "99", "旁观者", now=self.clock.now())
        self.engine.profiles.set_digest(S, "99", "从没参与这次阅读")
        action = await self.prepare()
        before = self.engine.profiles.list_people(S)
        await self.complete(action)
        data = json.loads(self.llm.calls[0]["prompt"])
        self.assertEqual(data["source_session"], S)
        self.assertEqual(data["messages"][0]["user_id"], "42")
        self.assertEqual([row["user_id"] for row in data["people"]], ["42"])
        self.assertIn("喜欢种花", json.dumps(data["people"], ensure_ascii=False))
        for excluded in ("别的群的秘密", "私聊秘密", "从没参与这次阅读"):
            self.assertNotIn(excluded, json.dumps(data, ensure_ascii=False))
        self.assertEqual(before, self.engine.profiles.list_people(S))
        self.assertEqual(len(self.llm.calls), 1)
