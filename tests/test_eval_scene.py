"""Synthetic participation evaluations preserve actual scene envelopes and isolation."""
from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from core.engine.eval_scene import prepare_eval_scene
from tests import test_context_selection as fixtures

GROUP = fixtures.GROUP


class IsolatedEvalTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ContextIntegrationTests.asyncSetUp

    def case(self, rows, **kwargs):
        return {"isolated_scene": True, "chat_rows": rows, **kwargs}

    async def preview(self, rows, **kwargs):
        result = await self.engine.run_eval(GROUP, [self.case(rows, **kwargs)],
                                           variant="participation", preview_only=True)
        self.assertTrue(result["ok"])
        row = result["results"][0]
        self.assertEqual(row["error"], "", row)
        return row

    async def test_preview_keeps_quote_objects_without_live_chat_recall_or_dispatch(self):
        self.state.note_chat(user_id="real-user", name="真实用户", text="REAL CHAT SECRET", now=self.clock.now(), origin=GROUP)
        self.state.thoughts = [{"content": "REAL THOUGHT SECRET", "at": self.clock.now()}]
        await self.engine.save_state(self.state)
        before = copy.deepcopy(await self.db.call("get_state", GROUP))
        with patch.object(self.engine, "chat_window", Mock(side_effect=AssertionError("no live chat"))), \
                patch.object(self.engine, "prepare_context_memory", AsyncMock(side_effect=AssertionError("no recall"))):
            row = await self.preview([
                {"name": "甲", "user_id": "alice", "message_id": "a", "text": "乙，你晚上吃什么？",
                 "at_targets": [{"id": "bob", "name": "乙", "self": False}]},
                {"name": "乙", "user_id": "bob", "message_id": "b", "text": "我去煮面",
                 "reply_to": {"id": "alice", "message_id": "a"}},
            ])
        scene = row["eval_scene"]["visible_scene"]
        self.assertEqual(scene[1]["reply_to"]["ref"], scene[0]["ref"])
        self.assertFalse(scene[0]["at_targets"][0]["self"])
        self.assertNotIn("REAL", row["system_prompt"] + row["user_prompt"])
        self.assertTrue(all(item["user_id"].startswith("eval:") for item in scene))
        self.assertEqual(row["eval_scene"]["quote_targets"], {"m1": "a", "m2": "b"})
        self.assertEqual(await self.db.call("get_state", GROUP), before)
        self.assertEqual(self.llm.calls, [])
        self.assertEqual(self.messenger.sent, [])

    async def test_long_body_and_quote_use_the_actual_archive_envelope(self):
        text = "开头" + "中间细节" * 800 + "最后问题已经解决，别再接这个了"
        row = await self.preview([{ "user_id": "alice", "message_id": "a", "text": text,
            "reply_to": {"id": "bob", "message_id": "missing", "text": text}}])
        expected = copy.deepcopy(self.state)
        expected.recent_chat = []
        retained = expected.note_chat(user_id="alice", name="alice", text=text, now=self.clock.now(), message_id="expected",
                                     origin=GROUP, raw_text=text, reply_to={"id": "bob", "text": text})
        scene = row["eval_scene"]["visible_scene"][0]
        self.assertEqual(scene["text"], retained["text"])
        self.assertEqual(scene["reply_to"]["text"], retained["reply_to"]["text"])
        self.assertLess(len(scene["text"]), len(text))

    async def test_concurrent_scenes_never_inherit_other_rounds(self):
        result = await self.engine.run_eval(GROUP, [
            self.case([{"name": "甲", "text": "第一场的纸飞机"}]),
            self.case([{"name": "乙", "text": "第二场的晚饭"}]),
        ], variant="participation", preview_only=True, concurrency=2)
        first, second = result["results"]
        self.assertNotIn("第二场", first["user_prompt"])
        self.assertNotIn("第一场", second["user_prompt"])
        self.assertNotEqual(first["eval_scene"]["state_key"], second["eval_scene"]["state_key"])
        self.assertEqual(self.llm.calls, [])

    async def test_answered_and_self_turns_remain_visible_without_reply_capability(self):
        row = await self.preview([
            {"name": "甲", "message_id": "a", "text": "已经答过的问题", "answered": True},
            {"name": "艾拉", "message_id": "self", "text": "我之前说过的话", "is_self": True,
             "speech_turn": "turn-one"},
            {"name": "甲", "message_id": "b", "text": "现在的新问题"},
        ])
        scene = row["eval_scene"]["visible_scene"]
        self.assertTrue(scene[0]["answered"])
        self.assertFalse(scene[0].get("can_reply", False))
        self.assertFalse(scene[1].get("can_reply", False))
        self.assertEqual(row["eval_scene"]["quote_targets"], {"m3": "b"})
        self.assertIn("已发言 1 个完整回合", row["system_prompt"])

    async def test_invalid_reference_fails_strictly_and_never_sends(self):
        self.llm.replies = [json.dumps({"inner": "我想说一句", "intent": "join_chat",
            "say": "不该发出的内容", "reply_to": "m99"}, ensure_ascii=False)]
        result = await self.engine.run_eval(GROUP, [self.case([{"text": "当前的话题"}])], variant="participation")
        row = result["results"][0]
        self.assertIn("引用不在", row["error"])
        self.assertEqual(row["reply"], [])
        self.assertEqual(self.llm.calls[0]["session_id"], GROUP)
        self.assertEqual(self.messenger.sent, [])

    async def test_browse_can_reference_old_unanswered_scene_while_message_cannot(self):
        old = [{"name": "甲", "message_id": "old", "text": "昨天做的纸飞机", "age_seconds": 7200}]
        message = await self.preview(old)
        browse = await self.preview(old, source="browse")
        self.assertEqual(message["eval_scene"]["quote_targets"], {})
        self.assertEqual(browse["eval_scene"]["quote_targets"], {"m1": "old"})
        self.assertEqual(browse["eval_scene"]["visible_scene"][0]["age_seconds"], 7200)

    async def test_invalid_scene_inputs_fail_before_model_dispatch(self):
        for rows in ([{"text": "x"}] * 81, [{"text": "x", "age_seconds": float("nan")}],
                     [{"text": "x", "is_self": "false"}], [{"text": "x", "at_targets": {}}],
                     [{"text": "x", "message_id": "a"}, {"text": "y", "message_id": "a"}]):
            with self.subTest(rows=rows[:1]), self.assertRaises(ValueError):
                prepare_eval_scene(self.engine, self.state, GROUP, self.case(rows), index=0, now=self.clock.now())
        self.assertEqual(self.llm.calls, [])

    async def test_preview_variants_never_dispatch_a_director(self):
        result = await self.engine.run_eval(GROUP, [{"user_text": "hi"}], variant="director", preview_only=True)
        self.assertFalse(result["ok"])
        self.assertEqual(self.llm.calls, [])

    async def test_legacy_preview_does_not_prepare_embedding_requests(self):
        with patch.object(self.engine, "prepare_context_memory", AsyncMock()) as prepare:
            result = await self.engine.run_eval(GROUP, [{"user_text": "风车修好了吗"}],
                                               variant="participation", preview_only=True)
        self.assertEqual(result["results"][0]["error"], "")
        prepare.assert_not_awaited()
        self.assertEqual(self.llm.calls, [])


if __name__ == "__main__":
    unittest.main()
