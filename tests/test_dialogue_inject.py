"""注入式接管的回归测试。

新的主路径（默认 ``reply_mode=inject``）只做三件事：
改人设、加处境、放行；调模型、跑工具循环、发送都交回宿主。
这一组用例盯的就是这条边界：请求不被替换、工具不被裁剪、别家插件的
system_prompt 与附加内容还在，主回复由宿主发出，插件只记账。
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
import types
import unittest

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)

import fake_astrbot  # noqa: E402

fake_astrbot.install()

_TMP_DIR = tempfile.mkdtemp(prefix="my-world-inject-")
os.environ["MY_WORLD_DATA_DIR"] = _TMP_DIR


def load_plugin_module():
    if "mw_inject_plugin" in sys.modules:
        return sys.modules["mw_inject_plugin"]
    spec = importlib.util.spec_from_file_location(
        "mw_inject_plugin",
        os.path.join(PLUGIN_DIR, "main.py"),
        submodule_search_locations=[PLUGIN_DIR],
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["mw_inject_plugin"] = module
    spec.loader.exec_module(module)
    return module


MODULE = load_plugin_module()


class At:
    def __init__(self, qq: str) -> None:
        self.qq = qq


class Event(fake_astrbot._AstrMessageEvent):
    def __init__(
        self,
        text: str,
        *,
        session: str,
        wake: bool = True,
        mention: bool = True,
        sender_id: str = "42",
        sender_name: str = "tester",
        message_id: str = "4711",
    ) -> None:
        super().__init__()
        self._text = text
        self._wake = wake
        self._sender_id = sender_id
        self._sender_name = sender_name
        self.unified_msg_origin = session
        self.message_obj = types.SimpleNamespace(
            message=[At("1")] if mention else [], message_id=message_id
        )

    def get_message_str(self) -> str:
        return self._text

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_sender_name(self) -> str:
        return self._sender_name

    def is_wake_up(self) -> bool:
        return self._wake

    def get_platform_name(self) -> str:
        return "aiocqhttp"


class Tool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"{name} 的描述"
        self.parameters = {"type": "object", "properties": {}}
        self.active = True
        self.handler_module_path = "other_plugin"


class Request:
    """够用的 ProviderRequest 替身：带工具集、带附加内容块。"""

    def __init__(self, prompt: str, system_prompt: str = "") -> None:
        self.prompt = prompt
        self.system_prompt = system_prompt
        self.contexts: list[dict] = []
        self.func_tool = types.SimpleNamespace(tools=[Tool("alpha"), Tool("beta")])
        self.extra_user_content_parts: list = []
        self.image_urls: list[str] = []
        self.audio_urls: list[str] = []
        self.conversation = None


class DialogueInjectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = MODULE

    def _plugin(self, session: str):
        context = self.module.Context()
        plugin = self.module.MyWorldPlugin(
            context,
            self.module.AstrBotConfig(
                {"enabled": True, "tick_interval": 60, "reply_mode": "inject"}
            ),
        )
        plugin.store.add_session(session, session_type="group", platform="aiocqhttp")
        plugin.engine.reload_config()
        return plugin, context

    def test_inject_keeps_native_request_and_tools_untouched(self):
        session = "aiocqhttp:GroupMessage:inject-keep"
        plugin, context = self._plugin(session)
        calls: list[dict] = []

        async def llm_generate(**kwargs):
            calls.append(kwargs)
            raise AssertionError("注入模式不应该由插件自己调模型")

        context.llm_generate = llm_generate
        host_system = (
            "# Persona Instructions\n\n"
            "HOST_PERSONA_SHOULD_BE_REPLACED\n\n"
            "# Tool Call Rules\n\nTOOL_RULES_KEEP_ME\n\n"
            "<worldtree_persona>树设定</worldtree_persona>"
        )
        req = Request("在吗", host_system)
        tools_before = list(req.func_tool.tools)
        event = Event("在吗", session=session)

        asyncio.run(plugin.on_llm_request(event, req))

        self.assertFalse(event.is_stopped(), "注入模式必须放行，让宿主继续跑")
        self.assertEqual(calls, [], "注入模式不允许插件自己调模型")
        self.assertEqual(req.func_tool.tools, tools_before, "工具集一个字都不能动")
        self.assertIn("TOOL_RULES_KEEP_ME", req.system_prompt, "宿主的工具说明要保留")
        self.assertIn("<worldtree_persona>", req.system_prompt, "别家插件内容要保留")
        self.assertNotIn("HOST_PERSONA_SHOULD_BE_REPLACED", req.system_prompt)
        self.assertIn("你是谁", req.system_prompt, "她的人设要写进去")
        self.assertIn("世界规则", req.system_prompt)
        extra_texts = [str(getattr(part, "text", "")) for part in req.extra_user_content_parts]
        marker = "[我的世界·此刻]"
        if extra_texts:
            joined = "\n".join(extra_texts)
            self.assertIn(marker, joined)
            self.assertIn("你的状态", joined)
            self.assertTrue(
                all(getattr(part, "_no_save", False) for part in req.extra_user_content_parts),
                "附加块必须是临时内容，不能写进对话历史",
            )
        else:
            # 宿主老版本没有 extra_user_content_parts：退回 prompt 尾巴
            self.assertIn(marker, req.prompt)
        turn = event.get_extra("mw_dialogue_turn")
        self.assertIsInstance(turn, dict)
        self.assertEqual(turn.get("session_id"), session)
        plugin.db.raw.close()

    def test_motion_vision_and_helper_context_survive_injection(self):
        """视觉插件与 helper_tools / 世界树的注入不能被本插件吃掉。"""

        session = "aiocqhttp:GroupMessage:inject-vision"
        plugin, _context = self._plugin(session)
        req = Request("看看这个视频")
        # 模拟 motion_vision：一帧画面解读 + 图片本体
        frame = self.module.TextPart(text="[动态视觉] 视频第 3 秒：她在拆快递")
        mark = getattr(frame, "mark_as_temp", None)
        if callable(mark):
            mark()
        req.extra_user_content_parts.append(frame)
        req.image_urls = ["https://img/frame3.jpg"]
        # 模拟 helper_tools 的环境感知与世界树设定
        req.system_prompt = (
            "# Persona Instructions\n\nHOST_PERSONA\n\n"
            "<worldtree_persona>树下有只猫</worldtree_persona>"
        )
        req.extra_user_content_parts.append(
            self.module.TextPart(text="[环境感知] 现在是白天，外面在下雨")
        )
        event = Event("看看这个视频", session=session)

        asyncio.run(plugin.on_llm_request(event, req))

        texts = [
            str(getattr(part, "text", "")) for part in req.extra_user_content_parts
        ]
        joined = "\n".join(texts)
        self.assertIn("拆快递", joined)
        self.assertIn("环境感知", joined)
        self.assertIn("[我的世界·此刻]", joined)
        self.assertEqual(req.image_urls, ["https://img/frame3.jpg"])
        self.assertIn("<worldtree_persona>", req.system_prompt)
        plugin.db.raw.close()

    def test_anamnesis_temporary_memory_and_tool_history_survive_injection(self):
        session = "aiocqhttp:GroupMessage:inject-memory"
        plugin, _context = self._plugin(session)
        req = Request("还记得上次种的薄荷吗")
        reference = self.module.TextPart(text="--- BEGIN HISTORICAL MEMORY REFERENCE ---\n上周在庭院种了薄荷\n--- END HISTORICAL MEMORY REFERENCE ---")
        mark = getattr(reference, "mark_as_temp", None)
        if callable(mark):
            mark()
        req.extra_user_content_parts.append(reference)
        history = [{"role": "assistant", "content": "曾经的对话"},
                   {"role": "user", "content": "外部记忆工具的历史记录"}]
        req.contexts = [dict(item) for item in history]
        event = Event("还记得上次种的薄荷吗", session=session)
        asyncio.run(plugin.on_llm_request(event, req))
        self.assertIn(reference, req.extra_user_content_parts)
        self.assertEqual(sum("上周在庭院种了薄荷" in str(getattr(part, "text", "")) for part in req.extra_user_content_parts), 1)
        self.assertEqual(req.contexts, history)
        plugin.db.raw.close()

    def test_two_people_at_once_are_not_merged(self):
        """两个人几乎同时 @ 她：各回各的，第二个人不能被并进第一轮、也不能被吞。"""

        session = "aiocqhttp:GroupMessage:inject-two-people"
        plugin, _context = self._plugin(session)
        first_event = Event(
            "今天开会吗",
            session=session,
            sender_id="1001",
            sender_name="小明",
            message_id="two-people-a",
        )
        first_req = Request("今天开会吗")
        asyncio.run(plugin.on_llm_request(first_event, first_req))
        second_event = Event(
            "你晚上有空吗",
            session=session,
            sender_id="1002",
            sender_name="小红",
            message_id="two-people-b",
        )
        second_req = Request("你晚上有空吗")
        asyncio.run(plugin.on_llm_request(second_event, second_req))

        self.assertFalse(first_event.is_stopped())
        self.assertFalse(
            second_event.is_stopped(), "第二个人的事件不能在她答第一条时被吞掉"
        )
        self.assertIsInstance(second_event.get_extra("mw_dialogue_turn"), dict)

        snapshots = [
            item
            for item in plugin.engine._prompt_snapshots
            if str(item.get("session_id") or "") == session
        ]
        self.assertGreaterEqual(len(snapshots), 2)
        latest = [str(item) for item in (snapshots[0].get("batch_texts") or [])]
        self.assertTrue(any("你晚上有空吗" in item for item in latest))
        self.assertFalse(
            any("今天开会吗" in item for item in latest), "别人那条不能进这一轮"
        )
        earlier = [str(item) for item in (snapshots[1].get("batch_texts") or [])]
        self.assertTrue(any("今天开会吗" in item for item in earlier))
        self.assertFalse(any("你晚上有空吗" in item for item in earlier))
        plugin.db.raw.close()

    def test_merged_messages_are_both_written_into_native_prompt(self):
        """合并了第二条时，宿主的 req.prompt 必须换成完整 batch，不能只留第一条。"""

        session = "aiocqhttp:GroupMessage:inject-merged-prompt"
        plugin, _context = self._plugin(session)
        plugin.engine.world.reply_style.merge_continuous_messages = True
        plugin.engine.world.reply_style.merge_wait_seconds = 0
        first = Event(
            "第一句：在吗",
            session=session,
            message_id="merged-a",
        )
        second = Event(
            "第二句：我还想问晚餐",
            session=session,
            message_id="merged-b",
        )
        plugin.engine.register_incoming(
            session,
            second.get_message_str(),
            identity={
                "message_id": "merged-b",
                "user_id": second.get_sender_id(),
                "user_name": second.get_sender_name(),
                "raw_text": second.get_message_str(),
                "model_text": second.get_message_str(),
            },
        )
        req = Request("第一句：在吗")
        asyncio.run(plugin.on_llm_request(first, req))

        self.assertIn("第一句：在吗", req.prompt)
        self.assertIn("第二句：我还想问晚餐", req.prompt)
        self.assertFalse(first.is_stopped())
        plugin.db.raw.close()

    def test_continuous_merge_is_off_by_default(self):
        session = "aiocqhttp:GroupMessage:inject-no-auto-merge"
        plugin, _context = self._plugin(session)
        first = Event("第一条", session=session, message_id="no-merge-a")
        second = Event("第二条", session=session, message_id="no-merge-b")
        second_record = plugin.engine.register_incoming(
            session,
            second.get_message_str(),
            identity={
                "message_id": "no-merge-b",
                "user_id": second.get_sender_id(),
                "user_name": second.get_sender_name(),
                "raw_text": second.get_message_str(),
                "model_text": second.get_message_str(),
            },
        )
        first_req = Request("第一条")
        asyncio.run(plugin.on_llm_request(first, first_req))
        self.assertNotIn("第二条", first_req.prompt)
        self.assertFalse(plugin.engine.was_absorbed(second_record))

        second_req = Request("第二条")
        asyncio.run(plugin.on_llm_request(second, second_req))
        self.assertFalse(second.is_stopped())
        self.assertIn("第二条", second_req.prompt)
        plugin.db.raw.close()

    def test_after_message_sent_records_reply_and_advances_waterline(self):
        session = "aiocqhttp:GroupMessage:inject-record"
        plugin, _context = self._plugin(session)
        calls: list[str] = []
        reflected: list[dict] = []
        original_mark = plugin.engine.mark_reply_batch_replied

        async def mark(session_id: str) -> None:
            calls.append(session_id)
            await original_mark(session_id)

        async def reflect(*args, **kwargs):
            reflected.append(dict(kwargs))
            return {"reasoning": {"inner": "假装复盘过了"}}

        plugin.engine.mark_reply_batch_replied = mark
        plugin.engine.reflect_turn = reflect
        event = Event("在吗", session=session)
        event.set_extra(
            "mw_dialogue_turn",
            {
                "session_id": session,
                "user_id": "42",
                "user_name": "tester",
                "user_text": "在吗",
                "reply": "在的，怎么啦",
                "provider_reasoning": "",
                "persona_id": "",
                "tools": [],
            },
        )

        async def drive():
            await plugin.after_message_sent(event)
            for _ in range(20):
                if reflected:
                    break
                await asyncio.sleep(0.01)

        asyncio.run(drive())
        self.assertEqual(calls, [session])
        self.assertEqual(len(reflected), 1)
        self.assertEqual(str(reflected[0].get("reply_text")), "在的，怎么啦")

        async def read_state():
            state = await plugin.engine.load_state(session, cold_start=False)
            return [
                str(item.get("text") or "")
                for item in (state.recent_chat or [])
                if item.get("is_self")
            ]

        said = asyncio.run(read_state())
        self.assertIn("在的，怎么啦", said)
        plugin.db.raw.close()

    def test_reflect_turn_writes_thoughts_and_applies_deltas(self):
        session = "aiocqhttp:GroupMessage:inject-reflect"
        plugin, _context = self._plugin(session)
        payload = (
            '{"reasoning": {"env": "客厅", "state": "放松", "mood": "轻快", '
            '"who": "他在打招呼", "inner": "他来了呀", "intent": "应一声"},'
            '"memory": "他回来打了声招呼", "chat_note": "寒暄",'
            '"valence_delta": 0.2, "affinity_delta": 0.1, "tone": "normal",'
            '"addressing": "me", "open_topic": "", "heart_knot": "", "grudge": "",'
            '"forgive": false, "own_topic": "", "own_topic_done": false,'
            '"actions": []}'
        )

        async def fake_judge(*_args, **_kwargs):
            return payload

        plugin.engine._ask_judge = fake_judge

        async def drive():
            return await plugin.engine.reflect_turn(
                session,
                user_id="42",
                user_name="tester",
                user_text="在吗",
                reply_text="在的，怎么啦",
            )

        result = asyncio.run(drive())
        self.assertIsInstance(result, dict)
        self.assertEqual(result["reasoning"]["inner"], "他来了呀")
        self.assertEqual(result["tone"], "normal")

        async def read_state():
            return await plugin.engine.load_state(session, cold_start=False)

        state = asyncio.run(read_state())
        self.assertEqual(
            str((state.last_reflection.get("reasoning") or {}).get("inner")),
            "他来了呀",
        )
        # 复盘里那句「心里冒出来的话」也进「内心活动」那一栏：
        # 以前只有 think 动作才写，模型不选那个动作就永远看不到更新。
        self.assertIn("他来了呀", [str(item.get("content")) for item in state.thoughts])
        self.assertTrue(state.reflection_log)
        plugin.db.raw.close()

    def test_legacy_tool_actions_are_kept_but_disabled(self):
        session = "aiocqhttp:GroupMessage:inject-legacy"
        plugin, _context = self._plugin(session)
        actions = {action.id: action for action in plugin.engine.world.actions}
        legacy = [
            action for action in actions.values() if bool(getattr(action, "legacy", False))
        ]
        self.assertTrue(legacy, "旧版工具 / 指令动作应该被标出来")
        legacy_switch_on = bool(
            os.environ.get("MY_WORLD_LEGACY_TOOL_ACTIONS", "").strip()
        )
        for action in legacy:
            if not legacy_switch_on:
                # 生产默认：旧版动作必须停用（同进程里别的用例打开过迁移开关时跳过这条）
                self.assertFalse(action.enabled, f"{action.id} 应该被停用")
            self.assertIn(action.kind, ("legacy_tool", "legacy_command"))
            self.assertTrue(
                action.tool_list()
                or str(action.trigger_command or "").strip()
                or str(action.llm_level or "") in ("tool", "command"),
                f"{action.id} 应该有工具 / 指令型特征",
            )
        plugin.db.raw.close()


if __name__ == "__main__":
    unittest.main()
