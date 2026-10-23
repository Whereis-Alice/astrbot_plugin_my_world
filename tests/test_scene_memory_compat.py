"""Stable request contract when scene memory and Anamnesis are both enabled."""

import asyncio
import json
import unittest
from unittest.mock import patch

from tests import test_dialogue_inject as fixture


SCENE = ("<!-- scene_memory_fork_v321 -->\n<conversation_scene>"
         "场景独有记录：小明正在问开放时间。</conversation_scene>\n"
         "<!-- scene_memory_reply_direction_v340 -->\n<reply_direction>"
         "引用来自小红，周六不营业；不是当前用户的亲历。</reply_direction>")
MEMORY = "<Anamnesis-Memory>记忆独有记录：小明去年喜欢咖啡</Anamnesis-Memory>"


class SceneMemoryCompatibilityTests(unittest.TestCase):
    module = fixture.MODULE
    _plugin = fixture.DialogueInjectTests._plugin

    def test_scene_and_memory_survive_injection_and_merging_without_becoming_new_evidence(self):
        for mode in ("extra", "before", "after", "tool"):
            for merged in (False, True):
                with self.subTest(mode=mode, merged=merged):
                    session = f"aiocqhttp:GroupMessage:scene-memory-{mode}-{merged}"
                    plugin, _ = self._plugin(session)
                    self.addCleanup(plugin.db.raw.close)
                    event = fixture.Event("周六能去吗", session=session)
                    req = fixture.Request("周六能去吗", "# Tools\n宿主工具规则")
                    req.contexts = [{"role": "assistant", "content": "原来的聊天"}]
                    # Scene supports a system fallback; both placements must survive.
                    if merged:
                        req.system_prompt += "\n" + SCENE
                    else:
                        req.extra_user_content_parts.append(self.module.TextPart(text=SCENE).mark_as_temp())
                    if mode == "before":
                        req.prompt = MEMORY + "\n\n" + req.prompt
                    elif mode == "after":
                        req.prompt += "\n\n" + MEMORY
                    elif mode == "tool":
                        req.contexts.extend([
                            {"role": "assistant", "content": "", "tool_calls": [{"id": "anam_recall_stub_test"}]},
                            {"role": "tool", "tool_call_id": "anam_recall_stub_test", "content": MEMORY},
                        ])
                    else:
                        req.extra_user_content_parts.append(self.module.TextPart(text=MEMORY).mark_as_temp())
                    history = req.contexts
                    original = plugin.engine.handle_reply

                    async def prepare(*args, **kwargs):
                        plan = await original(*args, **kwargs)
                        if merged:
                            plan.batch.append({"user_id": "42", "text": "如果关门就改周日"})
                            plan.user_prompt = "周六能去吗\n如果关门就改周日"
                        return plan

                    with patch.object(plugin.engine, "handle_reply", side_effect=prepare):
                        asyncio.run(plugin.on_llm_request(event, req))
                    self.assertIs(req.contexts, history)
                    self.assertFalse(event.is_stopped())
                    body = req.system_prompt + req.prompt + json.dumps(req.contexts, ensure_ascii=False)
                    body += "\n".join(part.text for part in req.extra_user_content_parts)
                    self.assertEqual(body.count(SCENE), 1)
                    self.assertEqual(body.count(MEMORY), 1)
                    self.assertIn("宿主工具规则", body)
                    self.assertEqual([tool.name for tool in req.func_tool.tools], ["alpha", "beta"])
                    preview = asyncio.run(plugin.engine.preview_last_reply_request(session))
                    for text in ("场景独有记录", "记忆独有记录", "引用来自小红", "周六不营业"):
                        self.assertIn(text, preview)
                    state = asyncio.run(plugin.engine.load_state(session, cold_start=False))
                    evidence = " ".join(row.get("text", "") for row in state.recent_chat)
                    evidence += " ".join(row["snapshot"] for row in plugin.db.raw._query("SELECT snapshot FROM memory_source"))
                    self.assertNotIn("场景独有记录", evidence)
                    self.assertNotIn("记忆独有记录", evidence)
                    if merged:
                        self.assertIn("如果关门就改周日", req.prompt)
