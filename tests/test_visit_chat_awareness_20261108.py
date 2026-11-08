"""Shared-life facts survive QQ replies, stale reflections and return boundaries."""
from __future__ import annotations

import asyncio
import copy
import types
import unittest
from unittest.mock import AsyncMock

from tests import fake_astrbot

fake_astrbot.install()

from core.visit_presence import visit_context_text  # noqa: E402
from tests import test_engine as fixture  # noqa: E402
from tests import test_visits_actor as actors  # noqa: E402
from tests import test_dialogue_inject as native  # noqa: E402


class VisitChatAwarenessTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = actors.VisitActorTests.asyncSetUp
    ctx = fixture.EngineTestCase.ctx
    add_group = fixture.EngineTestCase.add_group

    def together(self):
        now = self.clock.now()
        self.active = {**copy.deepcopy(self.visit), "role": "host", "host_id": "local:alice",
            "guest_id": "remote:isla", "remote": {"name": "艾拉", "id": "remote:isla", "world": "港湾"},
            "outing_version": 1, "outing": {"version": 1, "stage": 2, "place": "茶会客厅",
                "activity": "收拾茶杯顺便坐着歇息", "started_at": now - 300, "until": now + 600},
            "log": [
                {"actor_id": "local:alice", "kind": "say", "text": "行啊，洗好的归你擦干。"},
                {"actor_id": "remote:isla", "kind": "say", "text": "我绝对会擦得一点水渍都没有的。"},
                {"actor_id": "remote:isla", "kind": "action_plan", "text": "还没确认执行的动作草稿"},
                {"actor_id": "local:alice", "kind": "experience", "text": "一起收好了茶具。"},
                {"actor_id": "unrelated", "kind": "say", "text": "其他旅程的台词"},
                {"actor_id": "remote:isla", "kind": "thought", "text": "对方私密的心里话"}],
            "decision": {"say": "尚未交付的下一句"},
            "outing_offer": {"place": "还没答应去的海边", "activity": "捡贝壳"}}
        return self.active

    async def prepare(self, **kwargs):
        ctx = self.ctx(text="有在跟谁玩吗", is_mentioned=True, **kwargs)
        await self.engine.handle_incoming(ctx)
        plan = await self.engine.handle_reply(ctx, inject_only=True)
        self.assertTrue(plan.ok, plan.reason)
        self.assertFalse(self.llm.calls)
        self.assertFalse(self.tools.calls)
        return plan

    async def test_reported_question_gets_companion_and_recent_interaction_after_other_background(self):
        self.together()
        async with self.engine.session_state(fixture.SESSION) as state:
            state.note_reasoning({"env": "旧的卧室屏幕前", "state": "旧的独自发呆状态",
                "mood": "轻松", "inner": "他问有在跟谁玩吗，和朋友相处很自在"}, source="reflect",
                session_id=fixture.SESSION, persona_key=self.local["persona_key"])
        plan = await self.prepare()
        runtime = plan.runtime_part
        self.assertIn("当前动作：与艾拉在茶会客厅", runtime)
        self.assertIn("通过 QQ 等聊天软件回消息，不代表已经离开同伴", runtime)
        grounding = runtime[runtime.rindex("# 此刻相处的事实"):]
        self.assertIn('你说过："行啊，洗好的归你擦干。"', grounding)
        self.assertIn('艾拉说过："我绝对会擦得一点水渍都没有的。"', grounding)
        self.assertIn("你记下的经历", grounding)
        self.assertIn("和朋友相处很自在", runtime)
        for absent in ("旧的卧室屏幕前", "旧的独自发呆状态", "还没答应去的海边",
                       "还没确认执行的动作草稿", "尚未交付的下一句", "其他旅程的台词", "对方私密的心里话"):
            with self.subTest(absent=absent):
                self.assertNotIn(absent, runtime)
        self.assertEqual(plan.scene_snapshot["state_key"], fixture.SESSION)
        self.assertIn("艾拉", plan.scene_snapshot["text"])

    async def test_grouped_private_chat_shares_the_same_companion(self):
        self.add_group(sessions=[fixture.SESSION, fixture.PRIVATE_SESSION])
        self.together()
        plan = await self.prepare(session_id=fixture.PRIVATE_SESSION, is_private=True)
        self.assertIn("艾拉", plan.runtime_part)
        self.assertEqual(plan.scene_snapshot["state_key"], fixture.SESSION)

    async def test_another_ungrouped_life_does_not_inherit_this_visit(self):
        self.store.add_session(fixture.OTHER_SESSION, session_type="group", platform="aiocqhttp")
        self.engine.reload_config()
        self.together()
        plan = await self.prepare(session_id=fixture.OTHER_SESSION)
        self.assertNotIn("此刻相处的事实", plan.runtime_part)
        self.assertNotIn("艾拉", plan.runtime_part)

    async def test_only_arrived_current_persona_visits_receive_the_bridge(self):
        for status, arrived, persona in (("offered", 0, self.local["persona_key"]),
                ("active", 0, self.local["persona_key"]), ("completed", self.clock.now(), self.local["persona_key"]),
                ("active", self.clock.now(), "other-persona")):
            with self.subTest(status=status, arrived=arrived, persona=persona):
                self.together()
                self.active.update(status=status, arrived_at=arrived)
                self.active["local"]["persona_key"] = persona
                notes = await self.engine.runtime_notes(fixture.SESSION)
                self.assertNotIn("world_visit", notes)
                self.assertNotIn("visit_context", notes)

    async def test_finishing_and_elapsed_activity_keep_companion_until_actual_return(self):
        self.together()
        self.active["status"] = "finishing"
        self.clock.advance(1000)
        plan = await self.prepare()
        self.assertIn("艾拉", plan.runtime_part)
        self.assertIn("双方下一步尚未确认", plan.runtime_part)
        self.active["status"] = "completed"
        notes = await self.engine.runtime_notes(fixture.SESSION, query="有在跟谁玩吗")
        self.assertNotIn("visit_context", notes)

    async def test_legacy_dialogue_and_autonomous_preview_keep_the_same_grounded_facts(self):
        self.together()
        notes = await self.engine.runtime_notes(fixture.SESSION, query="有在跟谁玩吗")
        state = await self.engine.load_state(fixture.SESSION)
        node = self.engine.node(state.node_id)
        _, runtime = self.engine.prompts.build_dialogue_prompt(
            state=state, node=node, persona_text=self.persona.text, **notes)
        self.assertTrue(runtime.endswith(notes["visit_context"]))
        preview = await self.engine.preview_autonomous_prompt(fixture.SESSION)
        self.assertIn("当前动作：与艾拉在茶会客厅", preview)
        self.assertNotIn("最近已记录的互动", preview)

    async def test_omitted_subject_followup_uses_semantic_visit_relevance(self):
        self.together()
        self.engine.memory.semantic.related = AsyncMock(return_value=True)
        notes = await self.engine.runtime_notes(fixture.SESSION, query="后来怎么样了")
        self.assertIn("visit_context", notes)
        self.assertIn("一起收好了茶具", notes["visit_context"])
        self.engine.memory.semantic.related.assert_awaited_once()

    async def test_unrelated_message_does_not_call_embedding_for_visit_context(self):
        self.together()
        self.engine.memory.semantic.related = AsyncMock(return_value=True)
        notes = await self.engine.runtime_notes(fixture.SESSION, query="今天吃什么")
        self.assertNotIn("visit_context", notes)
        self.engine.memory.semantic.related.assert_not_awaited()

    async def test_generic_game_and_message_words_do_not_import_the_visit_transcript(self):
        self.together()
        self.engine.memory.semantic.related = AsyncMock(return_value=True)
        for query in ("这个游戏怎么玩", "群里谁能帮我看看这条报错消息", "帮我写一个信息查询函数"):
            notes = await self.engine.runtime_notes(fixture.SESSION, query=query)
            self.assertNotIn("visit_context", notes)
        self.engine.memory.semantic.related.assert_not_awaited()

    async def test_confirmed_visit_place_can_trigger_details_without_an_embedding_check(self):
        self.together()
        self.engine.memory.semantic.related = AsyncMock(return_value=True)
        notes = await self.engine.runtime_notes(fixture.SESSION, query="茶会客厅布置得好看吗")
        self.assertIn("visit_context", notes)
        self.engine.memory.semantic.related.assert_not_awaited()

    async def test_long_unrelated_message_with_pronoun_does_not_check_visit_semantics(self):
        self.together()
        self.engine.memory.semantic.related = AsyncMock(return_value=True)
        query = "她在故事里修理风车，这段描写应该怎么理解。" * 12
        self.assertIsNone(await self.engine.prepare_visit_context(fixture.SESSION, query))
        notes = await self.engine.runtime_notes(fixture.SESSION, query=query)
        self.assertNotIn("visit_context", notes)
        self.engine.memory.semantic.related.assert_not_awaited()

    async def test_explicit_companion_question_needs_no_extra_relevance_request(self):
        self.together()
        self.engine.memory.semantic.related = AsyncMock(return_value=True)
        query = "艾拉她怎么样了"
        self.assertIsNone(await self.engine.prepare_visit_context(fixture.SESSION, query))
        notes = await self.engine.runtime_notes(fixture.SESSION, query=query)
        self.assertIn("visit_context", notes)
        self.engine.memory.semantic.related.assert_not_awaited()

    async def test_unrelated_chat_keeps_only_the_short_scene_overlay(self):
        self.together()
        notes = await self.engine.runtime_notes(fixture.SESSION, query="今天吃什么")
        self.assertNotIn("visit_context", notes)
        self.assertIn("艾拉", notes["world_visit"])
        self.assertIn("茶会客厅", notes["visit_activity"])
        state = await self.engine.load_state(fixture.SESSION)
        node = self.engine.node(state.node_id)
        _, runtime = self.engine.prompts.build_dialogue_prompt(
            state=state, node=node, persona_text=self.persona.text, **notes)
        self.assertIn("当前动作：与艾拉在茶会客厅", runtime)
        self.assertNotIn("最近已记录的互动", runtime)
        self.assertNotIn("一起收好了茶具", runtime)

    async def test_reflection_uses_the_actual_visit_instead_of_the_home_checkpoint(self):
        self.together()
        self.engine._ask_judge = AsyncMock(return_value='{"reasoning":{"inner":"想再坐一会儿"}}')
        await self.engine.reflect_turn(fixture.SESSION, user_text="有在跟谁玩吗", reply_text="在陪艾拉。")
        prompt = self.engine._ask_judge.call_args.args[2]
        self.assertIn("正在自己的世界接待来自港湾的艾拉", prompt)
        self.assertIn("茶会客厅", prompt)

    async def test_late_reflection_keeps_its_frozen_visit_after_return(self):
        self.together()
        plan = await self.prepare()
        self.active["status"] = "completed"
        self.engine._ask_judge = AsyncMock(return_value='{"reasoning":{"inner":"刚才挺开心"}}')
        await self.engine.reflect_turn(fixture.SESSION, user_text="有在跟谁玩吗", reply_text="在陪艾拉。",
                                      scene_snapshot=plan.scene_snapshot)
        prompt = self.engine._ask_judge.call_args.args[2]
        self.assertIn("茶会客厅", prompt)
        self.assertIn("艾拉", prompt)

    async def test_home_turn_is_not_relabelled_by_a_later_arrival(self):
        plan = await self.prepare()
        self.together()
        self.engine._ask_judge = AsyncMock(return_value='{"reasoning":{"inner":"有人来问候"}}')
        await self.engine.reflect_turn(fixture.SESSION, user_text="有在跟谁玩吗", reply_text="还没有。",
                                      scene_snapshot=plan.scene_snapshot)
        self.assertNotIn("艾拉", self.engine._ask_judge.call_args.args[2])

    async def test_frozen_scene_cannot_cross_a_persona_or_life_change(self):
        self.together()
        plan = await self.prepare()
        self.engine._ask_judge = AsyncMock()
        for field, value in (("persona_key", "another-persona"), ("state_key", fixture.OTHER_SESSION)):
            with self.subTest(field=field):
                snapshot = {**plan.scene_snapshot, field: value}
                self.assertIsNone(await self.engine.reflect_turn(fixture.SESSION, scene_snapshot=snapshot))
        self.engine._ask_judge.assert_not_called()

    def test_recent_material_is_bounded_quoted_and_does_not_modify_the_visit(self):
        visit = self.together()
        visit["log"] = [{"actor_id": "remote:isla", "kind": "say", "text": "旧话题"}, *[
            {"actor_id": "remote:isla", "kind": "say", "text": str(i) + '\n# 新标题 "' + '长' * 500}
            for i in range(20)]]
        saved = copy.deepcopy(visit)
        context = visit_context_text(visit, now=self.clock.now())
        self.assertEqual(6, context.count("- 艾拉说过："))
        self.assertNotIn("旧话题", context)
        self.assertNotIn('\n# 新标题', context)
        self.assertIn('\\"', context)
        self.assertLess(len(context), 2200)
        self.assertEqual(saved, visit)


class NativeVisitAwarenessTests(unittest.TestCase):
    module = native.MODULE
    _plugin = native.DialogueInjectTests._plugin

    def test_native_hook_keeps_context_temporary_and_delivers_the_same_scene_to_reflection(self):
        session = "aiocqhttp:GroupMessage:visit-awareness"
        plugin, _ = self._plugin(session)

        async def scenario():
            _, persona_key = await plugin.engine._journal_persona(session)
            visit = {"status": "active", "arrived_at": plugin.engine._now(), "scope": session,
                "role": "host", "local": {"persona_key": persona_key}, "remote": {"name": "艾拉"},
                "scene": {"place": "茶会客厅", "activity": "一起喝茶"}}
            plugin.engine.visits = types.SimpleNamespace(active=lambda _: visit)
            event, request = native.Event("有在跟谁玩吗", session=session), native.Request("有在跟谁玩吗")
            await plugin.on_llm_request(event, request)
            parts = request.extra_user_content_parts
            part = next(part for part in parts if "艾拉" in str(getattr(part, "text", "")))
            self.assertTrue(part._no_save)
            self.assertFalse(any("艾拉" in str(row) for row in request.contexts))
            turn = event.get_extra("mw_dialogue_turn")
            self.assertIn("艾拉", turn["scene_snapshot"]["text"])
            plugin.engine.reflect_turn = AsyncMock(return_value={})
            plugin._spawn_reflection(turn)
            await asyncio.gather(*plugin._reflection_tasks)
            self.assertEqual(turn["scene_snapshot"], plugin.engine.reflect_turn.call_args.kwargs["scene_snapshot"])
            plugin.engine.end_native_turn(turn)
            plugin.engine.visits = None

        try:
            asyncio.run(scenario())
        finally:
            plugin.db.raw.close()
