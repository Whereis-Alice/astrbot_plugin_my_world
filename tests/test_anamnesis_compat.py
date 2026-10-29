"""Replay Anamnesis's actual injection branch against the native My World hook.

Set MY_WORLD_ANAMNESIS_SOURCE to an audited source checkout for this optional integration.
No server, provider, account or external database is accessed.
"""

import ast
import asyncio
import os
import re
from pathlib import Path
import types
import unittest
from unittest.mock import patch

from tests import test_dialogue_inject as fixture

UPSTREAM = Path(os.environ.get("MY_WORLD_ANAMNESIS_SOURCE", "__missing_anamnesis_fixture__"))
SCENE_UPSTREAM = Path(os.environ.get("MY_WORLD_SCENE_MEMORY_SOURCE", "__missing_scene_fixture__"))
MODULE = fixture.MODULE


@unittest.skipUnless(
    (UPSTREAM / "core/event_handler_modules/memory_recall.py").is_file(),
    "Set MY_WORLD_ANAMNESIS_SOURCE for two-plugin integration",
)
class AnamnesisCompatibilityTests(unittest.TestCase):
    module = MODULE
    _plugin = fixture.DialogueInjectTests._plugin

    @classmethod
    def setUpClass(cls):
        tree = ast.parse(
            (UPSTREAM / "core/event_handler_modules/memory_recall.py").read_text(encoding="utf-8")
        )
        branch = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "injection_method"
            and isinstance(node.test.comparators[0], ast.Constant)
            and node.test.comparators[0].value == "user_message_before"
        )
        cls.branch = compile(
            ast.fix_missing_locations(ast.Module(body=[branch], type_ignores=[])),
            "anamnesis_injection_branch",
            "exec",
        )

    def inject(self, req, mode, session):
        class Part:
            def __init__(self, text):
                self.text = text
                self._no_save = False

            def mark_as_temp(self):
                self._no_save = True
                return self

        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "anam_recall_stub_test",
                        "type": "function",
                        "function": {"name": "anamnesis_recall_memory", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "anam_recall_stub_test", "content": "外部聊天记忆"},
        ]
        env = {
            "req": req,
            "injection_method": mode,
            "memory_str": "<Anamnesis-Memory>外部聊天记忆：他从前喜欢咖啡</Anamnesis-Memory>",
            "memory_list": [{}],
            "recalled_memories": [{}],
            "session_id": session,
            "logger": types.SimpleNamespace(debug=lambda *a, **k: None),
            "actual_query": "热茶",
            "recall_session_id": session,
            "use_persona_filtering": True,
            # Current Anamnesis prepares these options before this injection branch.
            "format_options": {"max_chars": 6000, "entry_max_chars": 1200},
            "TextPart": Part,
            "self": types.SimpleNamespace(config_manager={"recall_engine.top_k": 5}),
            "format_memories_for_fake_tool_call": lambda *a, **k: messages,
        }
        exec(self.branch, env)
        return messages

    def test_all_injection_modes_and_both_hook_orders_preserve_native_request(self):
        for mode in (
            "extra_user_content",
            "user_message_before",
            "user_message_after",
            "fake_tool_call",
        ):
            for first in (True, False):
                with self.subTest(mode=mode, anamnesis_first=first):
                    session = f"aiocqhttp:GroupMessage:compat-{mode}-{first}"
                    plugin, _ = self._plugin(session)
                    self.addCleanup(plugin.db.raw.close)
                    event = fixture.Event("今天我更喜欢热茶", session=session)
                    req = fixture.Request("今天我更喜欢热茶", "# Tool Rules\n保持宿主工具规则")
                    req.contexts = [{"role": "user", "content": "昨天的真实聊天"}]
                    tools = list(req.func_tool.tools)
                    if first:
                        self.inject(req, mode, session)
                    asyncio.run(plugin.on_llm_request(event, req))
                    if not first:
                        self.inject(req, mode, session)
                    self.assertFalse(event.is_stopped())
                    self.assertEqual(req.func_tool.tools, tools)
                    self.assertIn("保持宿主工具规则", req.system_prompt)
                    text = (
                        req.prompt
                        + "\n"
                        + "\n".join(
                            getattr(part, "text", "") for part in req.extra_user_content_parts
                        )
                    )
                    if mode != "fake_tool_call":
                        self.assertIn("外部聊天记忆", text)
                    else:
                        self.assertTrue(
                            any(
                                row.get("tool_call_id") == "anam_recall_stub_test"
                                for row in req.contexts
                            )
                        )
                    state = asyncio.run(plugin.engine.load_state(session, cold_start=False))
                    self.assertNotIn("外部聊天记忆", " ".join(row["snapshot"] for row in plugin.db.raw._query("SELECT snapshot FROM memory_source")))
                    self.assertNotIn(
                        "外部聊天记忆",
                        " ".join(row.get("text", "") for row in state.recent_chat),
                    )

    def test_merged_reply_keeps_before_and_after_memory_without_turning_it_into_evidence(self):
        for mode in ("user_message_before", "user_message_after"):
            session = "aiocqhttp:GroupMessage:compat-merge-" + mode
            plugin, _ = self._plugin(session)
            self.addCleanup(plugin.db.raw.close)
            event = fixture.Event("今天喝热茶", session=session)
            req = fixture.Request("今天喝热茶")
            self.inject(req, mode, session)
            original = plugin.engine.handle_reply

            async def merged(ctx, **kwargs):
                plan = await original(ctx, **kwargs)
                plan.batch = [*plan.batch, {"user_id": "42", "text": "等会去看书"}]
                plan.user_prompt = "今天喝热茶\n等会去看书"
                return plan

            with patch.object(plugin.engine, "handle_reply", side_effect=merged):
                asyncio.run(plugin.on_llm_request(event, req))
            self.assertIn("等会去看书", req.prompt)
            self.assertIn(
                "外部聊天记忆",
                " ".join(getattr(part, "text", "") for part in req.extra_user_content_parts),
            )
            turn = event.get_extra("mw_dialogue_turn")
            self.assertNotIn("外部聊天记忆", turn["user_text"])

    @unittest.skipUnless((SCENE_UPSTREAM / "main.py").is_file(), "Set MY_WORLD_SCENE_MEMORY_SOURCE for three-plugin integration")
    def test_scene_source_cleanup_and_injection_with_all_memory_modes(self):
        # Execute upstream's own cleanup and injection methods with no server imports.
        tree = ast.parse((SCENE_UPSTREAM / "main.py").read_text(encoding="utf-8"))
        method_names = {"_strip_internal_scene_markers", "_clean_request_internal_markers", "_inject_scene"}
        methods = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name in method_names]
        regex_names = {"_INTERNAL_SCENE_BLOCK_RE", "_INTERNAL_REPLY_DIRECTION_BLOCK_RE", "_INTERNAL_SCENE_MARKER_RE"}
        assignments = [ast.Assign(targets=[node.target], value=node.value) for node in tree.body
                       if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                       and node.target.id in regex_names]
        scene_keys = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ExtraKeys")
        marker = next(node.value.value for node in scene_keys.body
                      if isinstance(node, ast.AnnAssign) and node.target.id == "SCENE_INJECTED_MARKER")
        cls = ast.ClassDef(name="Scene", bases=[], keywords=[], body=methods, decorator_list=[])
        env = {"re": re, "Any": object, "ProviderRequest": object, "TextPart": self.module.TextPart,
               "ExtraKeys": types.SimpleNamespace(SCENE_INJECTED_MARKER=marker),
               "logger": types.SimpleNamespace(debug=lambda *a, **k: None, error=lambda *a, **k: None)}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[*assignments, cls], type_ignores=[])),
                     "scene_memory_injection", "exec"), env)
        scene = env["Scene"]()
        for mode in ("extra_user_content", "user_message_before", "user_message_after", "fake_tool_call"):
            for native_first in (False, True):
                with self.subTest(mode=mode, native_first=native_first):
                    session = f"aiocqhttp:GroupMessage:real-scene-{mode}-{native_first}"
                    plugin, _ = self._plugin(session)
                    self.addCleanup(plugin.db.raw.close)
                    req = fixture.Request("今天更喜欢热茶", "# Rules\n宿主规则")
                    event = fixture.Event(req.prompt, session=session)
                    if native_first:
                        asyncio.run(plugin.on_llm_request(event, req))
                    self.inject(req, mode, session)
                    scene._clean_request_internal_markers(req)
                    scene._inject_scene(req, "<conversation_scene>当前群聊与引用归属</conversation_scene>")
                    if not native_first:
                        asyncio.run(plugin.on_llm_request(event, req))
                    body = req.system_prompt + req.prompt + str(req.contexts)
                    body += "\n".join(part.text for part in req.extra_user_content_parts)
                    self.assertEqual(body.count("当前群聊与引用归属"), 1)
                    self.assertIn("外部聊天记忆", body)
                    self.assertIn("[我的世界·此刻]", body)
                    self.assertIn("宿主规则", body)
                    self.assertFalse(event.is_stopped())
                    if not native_first:
                        preview = asyncio.run(plugin.engine.preview_last_reply_request(session))
                        self.assertIn("当前群聊与引用归属", preview)
                        self.assertIn("外部聊天记忆", preview)
