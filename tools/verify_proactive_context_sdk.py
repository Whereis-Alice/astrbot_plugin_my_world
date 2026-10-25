"""Exercise native proactive context with a real SDK and synthetic provider only."""
import asyncio
import importlib
import json
import logging
import sys
import types
import tempfile
import copy
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

from astrbot.core.agent.message import TextPart
from astrbot.core.agent.tool import ToolSet
from astrbot.core.db.po import Conversation
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.provider.provider import Provider
from astrbot.core.provider.entities import LLMResponse
from astrbot.core.star.context import Context

ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType("world_context_sdk_check")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
module = importlib.import_module(package.__name__ + ".adapters.proactive_context")
delivery = importlib.import_module(package.__name__ + ".adapters.proactive_delivery")
models = importlib.import_module(package.__name__ + ".core.engine.models")


class Model(Provider):
    def __init__(self):
        super().__init__({"id": "synthetic", "type": "synthetic", "key": [], "max_context_tokens": 8192}, {})
        self.requests = []

    def get_current_key(self): return ""
    def meta(self): return types.SimpleNamespace(id="synthetic")
    def set_key(self, key): pass
    async def get_models(self): return ["synthetic"]
    async def text_chat(self, **kwargs):
        self.requests.append(kwargs)
        return LLMResponse(role="assistant", completion_text='{"inner":"私密想法","intent":"join_chat","say":"公开台词"}')


class Event(AstrMessageEvent):
    async def send(self, chain, *args, **kwargs):
        raise AssertionError("No real transport may be invoked")


async def check():
    model = Model()
    context = Context.__new__(Context)
    context.subagent_orchestrator = None
    context.kb_manager = None
    config = {"provider_settings": {"proactive_capability": {"add_cron_tools": False}},
        "agent_runner": {"config": {"persona": {"safety_mode": False}}}, "plugin_set": ["*"], "timezone": "Asia/Shanghai"}
    context.get_config = lambda *args, **kwargs: config
    context.provider_manager = types.SimpleNamespace(get_provider_by_id=AsyncMock(return_value=model))
    history = [{"role": "user", "content": "之前的话"}, {"role": "assistant", "content": "之前的回应"}]
    conversation = Conversation(platform_id="test", user_id="room", cid="cid", history=json.dumps(history), persona_id="isla")
    context.conversation_manager = types.SimpleNamespace(get_curr_conversation_id=AsyncMock(return_value="cid"),
        get_conversation=AsyncMock(return_value=conversation), update_conversation=AsyncMock())
    persona = {"prompt": "原生艾拉人设", "tools": [], "name": "艾拉"}
    context.persona_manager = types.SimpleNamespace(resolve_selected_persona=AsyncMock(return_value=("isla", persona, False, False)))
    context.get_llm_tool_manager = lambda: types.SimpleNamespace(get_full_tool_set=lambda: ToolSet())
    message = AstrBotMessage()
    message.type, message.group_id, message.self_id = MessageType.GROUP_MESSAGE, "room", "bot"
    message.message_id, message.message = "source", []
    message.sender = MessageMember(user_id="alice", nickname="Alice")
    event = Event("最近群友的话", message, PlatformMetadata(name="synthetic", description="test", id="test", support_proactive_message=False), "room")
    target = event.unified_msg_origin
    from astrbot.api import FunctionTool
    from astrbot.api.provider import ProviderRequest
    pending = asyncio.get_running_loop().create_future()
    live_tool = FunctionTool(name="runtime_binding", description="synthetic live tool", parameters={})
    live_tool._runtime_owner = types.SimpleNamespace(pending=pending)
    old_request = ProviderRequest(func_tool=ToolSet([live_tool]))
    event.set_extra("provider_request", old_request)
    event.set_extra("plugin_runtime_handle", pending)
    event.set_extra("plugin_facts", {"items": ["本轮已有资料"]})
    engine = types.SimpleNamespace(native_turn_busy=lambda _: False)
    plugin = types.SimpleNamespace(context=context, engine=engine, logger=logging.getLogger("test"),
        last_event=lambda sid: event if sid == target else None, _host_persona_prompt=AsyncMock(return_value=persona["prompt"]),
        take_llm_response=lambda _: None, typing_delay_for=lambda _: 0)
    context.send_message = AsyncMock(return_value=True)
    calls = []

    async def hook(plugin, event, name, *args, **kwargs):
        calls.append(name)
        if name == "OnLLMRequestEvent":
            args[0].extra_user_content_parts.append(TextPart(text="世界书注入").mark_as_temp())
        if name == "OnLLMResponseEvent" and not kwargs.get("accepted"):
            assert "私密想法" not in args[0].completion_text
            args[0].completion_text = "改写后的公开台词"
        return False

    native = module.ProactiveContext(plugin, {"target": target, "rows": []})
    with patch.object(module, "fire_hook", hook):
        options = await native.prepare({"chat_provider_id": "synthetic", "prompt": "后台主动任务", "system_prompt": "插件艾拉人设"})
    assert options["contexts"][:2] == history
    assert options["prompt"] == event.message_str
    assert "原生艾拉人设" not in options["system_prompt"]
    assert "插件艾拉人设" in options["system_prompt"]
    assert any(part.text == "世界书注入" for part in options["extra_user_content_parts"])
    assert any(part._no_save and "后台主动任务" in part.text for part in options["extra_user_content_parts"])
    response = await native.invoke(**options)
    assert response.completion_text.native_event is native.event
    assert len(model.requests) == 1
    serialized = json.dumps(model.requests, ensure_ascii=False, default=lambda obj: obj.model_dump() if hasattr(obj, "model_dump") else str(obj))
    assert "世界书注入" in serialized and "后台主动任务" in serialized and "之前的回应" in serialized
    assert event.get_extra("provider_request") is old_request
    assert native.event.get_extra("plugin_runtime_handle") is None
    assert native.event.get_extra("plugin_facts") == {"items": ["本轮已有资料"]}
    assert not pending.done()
    assert conversation.history == json.dumps(history)
    native.event.get_extra("provider_request").func_tool = ToolSet([live_tool])
    view = models.TickOutcome(target, speech_kind="life_share", native_events={target: native.event})
    view.add_speech("公开台词")
    with patch.object(delivery, "fire_hook", hook):
        async with delivery.delivery(plugin, target, view, echo_count=1):
            assert view.messages == ["改写后的公开台词"]
            assert delivery.prepared_chain(target, view.messages[0], 1)[0].text == view.messages[0]
            assert delivery.prepared_chain(target, view.messages[0], 0) is None
            await delivery.accepted(plugin, target, view.messages[0], 1)
            await delivery.accepted(plugin, target, view.messages[0], 1)
            public_request = delivery.WIRE.get()[(target, 1)]["event"].get_extra("provider_request")
            assert public_request.func_tool.tools[0] is live_tool
            assert public_request.func_tool.tools is not native.event.get_extra("provider_request").func_tool.tools
    messenger = importlib.import_module(package.__name__ + ".adapters.messenger").AstrBotMessenger(plugin)
    policy = importlib.import_module(package.__name__ + ".core.speech_policy")
    from astrbot.core.message.components import At
    with policy.contact_send(target, {"alice"}, 0):
        await messenger.send_text(target, ["最近还在看那本书吗", "我想听你讲讲"])
    chains = [call.args[1].chain for call in context.send_message.call_args_list]
    assert len(chains) == 2
    assert [part.qq for part in chains[0] if isinstance(part, At)] == ["alice"]
    assert not any(isinstance(part, At) for part in chains[1])
    context.conversation_manager.update_conversation.assert_awaited_once()
    saved = context.conversation_manager.update_conversation.call_args.kwargs["history"]
    assert saved == history + [{"role": "assistant", "content": "改写后的公开台词"}]
    assert event.get_sender_id() == "alice"
    assert calls.count("OnLLMRequestEvent") == 1
    from PIL import Image as PILImage
    from astrbot.core.agent.message import ImageURLPart
    from astrbot.core.message.components import Video
    with tempfile.TemporaryDirectory(prefix='my-world-sdk-media-') as folder:
        folder = Path(folder)
        first, second, missing = folder / 'first.png', folder / 'second.png', folder / 'missing.png'
        PILImage.new('RGB', (32, 32), 'red').save(first)
        PILImage.new('RGB', (32, 32), 'blue').save(second)
        paths = [str(missing), str(first), str(second)]
        native_media = module.ProactiveContext(plugin, {'target': target, 'rows': [],
            'image_contexts': [{'url': path, 'text': name} for path, name in zip(paths, ['不可读来源', '来源消息a', '引用消息b'])]})
        with patch.object(module, 'fire_hook', hook):
            media_options = await native_media.prepare({'chat_provider_id': 'synthetic', 'prompt': '看正确的图片',
                                                       'system_prompt': '插件人设', 'image_urls': paths})
        parts = media_options['extra_user_content_parts']
        labelled = [i for i, part in enumerate(parts) if isinstance(part, TextPart)
                    and ('来源消息a' in part.text or '引用消息b' in part.text)]
        assert len(labelled) == 2
        assert all(isinstance(parts[i + 1], ImageURLPart) and parts[i + 1]._no_save for i in labelled)
        assert parts[labelled[0] + 1].image_url.url != parts[labelled[1] + 1].image_url.url
        assert not media_options['image_urls']
        await native_media.invoke(**media_options)
        assert '来源消息a' in json.dumps(model.requests[-1], ensure_ascii=False,
            default=lambda part: part.model_dump() if hasattr(part, 'model_dump') else str(part))
        await native_media.close()
        video_path = folder / 'synthetic-attachment.mp4'
        video_path.write_bytes(b'synthetic attachment; no decoding or real provider')
        video_message = copy.deepcopy(message)
        video_message.message = [Video(file=str(video_path))]
        video_event = Event('［视频］', video_message, event.platform_meta, 'room')
        native_video = module.ProactiveContext(plugin, {'target': target, 'rows': [], 'media_event': video_event})
        seen_video = []

        async def video_hook(plugin, actual, name, *args, **kwargs):
            if name == 'OnLLMRequestEvent':
                seen_video.append(any(isinstance(part, Video) for part in actual.message_obj.message))
                args[0].extra_user_content_parts.append(TextPart(text='普通视频插件收到正确视频源事件').mark_as_temp())
            return False

        with patch.object(module, 'fire_hook', video_hook):
            video_options = await native_video.prepare({'chat_provider_id': 'synthetic', 'prompt': '视频后的现场接话',
                                                       'system_prompt': '插件人设'})
        assert seen_video == [True]
        await native_video.invoke(**video_options)
        assert '普通视频插件收到正确视频源事件' in json.dumps(model.requests[-1], ensure_ascii=False,
            default=lambda part: part.model_dump() if hasattr(part, 'model_dump') else str(part))
        assert event.message_obj.message == []
        reply_message = copy.deepcopy(message)
        reply_message.message_id = 'actual-reply'
        reply_message.sender = MessageMember(user_id='bob', nickname='Bob')
        reply_event = Event('视频后另一位群友的话', reply_message, event.platform_meta, 'room')
        engine.media = types.SimpleNamespace(events={(target, 'actual-reply'): (time.monotonic(), reply_event)})
        public = models.TickOutcome(target, speech_kind='participation', native_events={target: native_video.event})
        public.add_speech('对这位群友接话')
        public.reply_targets[target] = 'actual-reply'
        addressed = []

        async def public_hook(plugin, actual, name, *args, **kwargs):
            if name == 'OnLLMResponseEvent':
                addressed.append(actual.get_sender_id())
            return False

        with patch.object(delivery, 'fire_hook', public_hook):
            async with delivery.delivery(plugin, target, public):
                assert addressed == ['bob']
                outgoing = delivery.WIRE.get()[(target, 0)]['event']
                assert outgoing.get_extra('provider_request').conversation.cid == 'cid'
        await native_video.close()
    print(json.dumps({"passed": True, "native_history": True, "temporary_injection": True,
        "public_hooks": True, "accepted_only_history": True, "source_isolation": True,
        "image_source_binding": True, "video_request_hook_source": True,
        "runtime_snapshot": True, "live_tool_binding": True}))


if __name__ == "__main__":
    if len(sys.argv) == 2:
        import importlib.util
        spec = importlib.util.spec_from_file_location("astrbot.core.astr_main_agent", sys.argv[1])
        legacy = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = legacy
        spec.loader.exec_module(legacy)
    asyncio.run(check())
