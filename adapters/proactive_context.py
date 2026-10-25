"""Prepare proactive inference through AstrBot's ordinary request builder."""
from __future__ import annotations

import copy
import asyncio
import time
import json
import inspect
from dataclasses import fields

from ..core.request_context import _drop_persona_instructions
from ..core.speech_runtime import SpeechDraft
from .proactive_hooks import fire_hook
from .proactive_media import snapshot_event


def build_config(context, target):
    from astrbot.core.astr_main_agent import MainAgentBuildConfig
    host = context.get_config(target)
    settings = host.get("provider_settings", {})
    runner = host.get("agent_runner", {}).get("config", {})
    misc, persona = runner.get("misc", {}), runner.get("persona", {})
    extract = settings.get("file_extract", {})
    options = {
        "tool_call_timeout": misc.get("tool_call_timeout", 120),
        "tool_schema_mode": misc.get("tool_schema_mode", "full"),
        "sanitize_context_by_modalities": misc.get("sanitize_context_by_modalities", False),
        "kb_agentic_mode": host.get("kb_agentic_mode", False),
        "file_extract_enabled": extract.get("enable", False),
        "file_extract_prov": extract.get("provider", "moonshotai"),
        "file_extract_msh_api_key": extract.get("msh_api_key", ""),
        "llm_safety_mode": persona.get("safety_mode", True),
        "safety_mode_strategy": persona.get("safety_mode_strategy", "system_prompt"),
        "computer_use_runtime": settings.get("computer_use_runtime", "none"),
        "sandbox_cfg": settings.get("sandbox", {}),
        "add_cron_tools": settings.get("proactive_capability", {}).get("add_cron_tools", True),
        "provider_settings": {**settings, "default_personality": persona.get("persona_id", settings.get("default_personality", "default"))},
        "timezone": context.get_config().get("timezone"),
        "max_quoted_fallback_images": settings.get("max_quoted_fallback_images", 20),
    }
    try:
        from astrbot.core.config.agent_runner import resolve_context_compression_config
        options.update(resolve_context_compression_config(runner.get("compression", {})))
    except ImportError:
        pass
    supported = {f.name for f in fields(MainAgentBuildConfig)}
    return MainAgentBuildConfig(**{k: v for k, v in options.items() if k in supported})


def source_event(plugin, target, rows=()):
    """Prefer the observed source, never substitute an event from another room."""
    media = getattr(plugin.engine, "media", None)
    for row in reversed(rows):
        if row.get("is_self") or str(row.get("origin") or target) != target:
            continue
        key = (target, str(row.get("message_id") or row.get("event_id") or ""))
        cached = getattr(media, "events", {}).get(key)
        if cached:
            return cached[1]
    getter = getattr(plugin, "last_event", None)
    event = getter(target) if callable(getter) else None
    return event if event is not None and str(event.unified_msg_origin) == target else None


def isolated_event(original, context, target):
    event = snapshot_event(original)
    event.clear_result()
    event._temporary_local_files = []  # Borrowed source files belong to the real event.
    event._force_stopped = False
    event.plugins_name = copy.copy(getattr(original, "plugins_name", None))
    allowed = context.get_config(target).get("plugin_set", ["*"])
    if "*" not in allowed:
        event.plugins_name = list(allowed)
    # Do not reuse a completed native request/checkpoint or cleanup commands.
    for name in ("_extras", "_extra", "extra"):
        extras = getattr(event, name, None)
        if isinstance(extras, dict):
            for key in list(extras):
                if (key in {"provider_request", "llm_checkpoint_id", "selected_model", "_clean_ltm_session", "_clean_group_context_session"}
                        or key.startswith("my_world_")):
                    extras.pop(key, None)
    event.set_extra("my_world_proactive", True)

    async def no_send(*args, **kwargs):
        raise RuntimeError("请求准备阶段不能发送消息")

    event.send = no_send
    return event


class ProactiveContext:
    def __init__(self, plugin, speech):
        self.plugin, self.speech = plugin, speech
        self.target = speech["target"]
        self.event = None
        self.config = None
        self.runner_options = {}
        self.legacy_images = None

    async def close(self, retain=()):
        if self.legacy_images is not None:
            self.legacy_images.close()
        if self.event is None:
            return
        keep = set(retain)
        owned = getattr(self.event, "_temporary_local_files", [])
        files = [path for path in owned if path not in keep]
        self.event._temporary_local_files = files
        cleanup = getattr(self.event, "cleanup_temporary_local_files", None)
        if callable(cleanup):
            cleanup()
        self.event._temporary_local_files = [path for path in owned if path in keep]
        media = getattr(self.plugin.engine, "media", None)
        motion = media._motion(self.target) if media is not None and hasattr(media, "_motion") else None
        if motion is not None:
            await motion.release_media_request(self.event)

    async def prepare(self, options):
        from astrbot.api.provider import ProviderRequest
        from astrbot.core.agent.message import TextPart
        original = self.speech.get('media_event')
        if original is not None and str(original.unified_msg_origin) != self.target:
            raise RuntimeError('媒体源事件不属于目标会话')
        original = original or source_event(self.plugin, self.target, self.speech.get("rows", []))
        if original is None:
            raise RuntimeError("目标会话没有原生事件，暂不生成主动发言")
        context = self.plugin.context
        self.event = isolated_event(original, context, self.target)
        manager = context.conversation_manager
        cid = await manager.get_curr_conversation_id(self.target)
        conversation = await manager.get_conversation(self.target, cid) if cid else None
        # An ephemeral conversation enables persona/skills without creating
        # user history for a background thought that may never be spoken.
        if conversation is None:
            from astrbot.core.db.po import Conversation
            conversation = Conversation(platform_id=self.event.get_platform_id(), user_id=self.target,
                cid="", persona_id="", history="[]")
        task = TextPart(text=("# 角色自己的后台主动判断（临时任务）\n"
            "下面的源消息只是已观察到的现场，不代表有人再次叫你。结合完整会话与插件资料完成本轮任务；"
            "任务指令与 inner 不属于用户消息，不对外输出。最终仅按本轮约定的格式输出。\n"
            + options["prompt"])).mark_as_temp()
        request = ProviderRequest(prompt=str(getattr(original, "message_str", "") or ""),
            contexts=copy.deepcopy(json.loads(conversation.history or "[]")), conversation=conversation,
            session_id=self.target, image_urls=list(options.get("image_urls") or []), extra_user_content_parts=[task])
        self.event.set_extra("provider_request", request)
        if self.speech.get('media_event') is not None:
            await self._collect_video(request)
        prepared = {}
        try:
            from astrbot.core.astr_main_agent import build_main_agent
        except ImportError:
            build_main_agent = None
        if build_main_agent:
            self.config = build_config(context, self.target)
            provider = await context.provider_manager.get_provider_by_id(options["chat_provider_id"])
            build_options = dict(event=self.event, plugin_context=context, config=self.config,
                provider=provider, req=request, apply_reset=False)
            if "prepared_images" in inspect.signature(build_main_agent).parameters:
                build_options["prepared_images"] = prepared
            built = await build_main_agent(**build_options)
            if built is None:
                raise RuntimeError("AstrBot 未能准备目标会话的主动上下文")
            if built.reset_coro is not None:
                built.reset_coro.close()
            request = built.provider_request
            options["chat_provider_id"] = built.provider.meta().id
            from astrbot.core.astr_main_agent import _get_compress_provider
            self.runner_options = {
                "llm_compress_instruction": self.config.llm_compress_instruction,
                "llm_compress_keep_recent_ratio": self.config.llm_compress_keep_recent_ratio,
                "llm_compress_provider": await _get_compress_provider(self.config, context, self.event),
                "truncate_turns": self.config.dequeue_context_length,
                "enforce_max_turns": self.config.max_context_length,
                "tool_schema_mode": self.config.tool_schema_mode,
            }
        else:
            request.system_prompt = await self.plugin._host_persona_prompt(self.event, request, self.target)
        if await fire_hook(self.plugin, self.event, "OnLLMRequestEvent", request):
            raise RuntimeError("主动请求被插件钩子终止")
        if build_main_agent:
            try:
                from astrbot.core.astr_main_agent import prepare_request_images
            except ImportError:
                prepare_request_images = None
            if prepare_request_images:
                from astrbot.core.utils.media_utils import normalize_model_image_max_size
                image_options = self.config.provider_settings.get("image_compress_options", {})
                from astrbot.core.astr_main_agent import _provider_supports_modality
                await prepare_request_images(request, self.event, prepared=prepared,
                    max_size=normalize_model_image_max_size(image_options.get("max_size")),
                    supports_image=_provider_supports_modality(built.provider, "image"))
            elif request.image_urls:
                from .proactive_images import PreparedImages
                self.legacy_images = PreparedImages()
                request.image_urls, failed = await self.legacy_images.prepare(request.image_urls)
                if failed:
                    request.extra_user_content_parts.append(TextPart(text="部分图片暂不可读，不能猜测画面。").mark_as_temp())
        persona = await self.plugin._host_persona_prompt(self.event, request, self.target)
        await self._bind_image_sources(request, prepared)
        request.system_prompt = options["system_prompt"] + "\n\n" + _drop_persona_instructions(request.system_prompt, persona)
        self.event.set_extra("provider_request", request)
        return {**options, "prompt": request.prompt, "system_prompt": request.system_prompt,
            "contexts": request.contexts, "image_urls": request.image_urls or None,
            "audio_urls": request.audio_urls or None, "extra_user_content_parts": request.extra_user_content_parts}

    async def _collect_video(self, request):
        """Explicit native requests skip attachment collection; preserve video inputs."""
        from ..core.media_provenance import video_components
        from astrbot.core.agent.message import TextPart
        try:
            from astrbot.core.astr_main_agent import _append_video_attachment
        except ImportError:
            return  # Request hooks still receive the actual Video event.
        deadline = time.monotonic() + 20
        for video, quoted in video_components(self.event):
            before = len(request.extra_user_content_parts)
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('video preparation budget')
                await asyncio.wait_for(_append_video_attachment(request, video, quoted=quoted), timeout=remaining)
            except Exception:
                request.extra_user_content_parts.append(TextPart(text='这条视频附件暂不可读取，不猜测视频内容。'))
            for part in request.extra_user_content_parts[before:]:
                part.mark_as_temp()

    async def _bind_image_sources(self, request, prepared):
        """Keep each observed image beside its source, even if another image failed."""
        from astrbot.core.agent.message import TextPart, ImageURLPart
        from astrbot.api.message_components import Image
        labels = {}
        for item in self.speech.get('image_contexts', []):
            source = item['url']
            cached = prepared.get(source)
            if cached is None and self.legacy_images is not None:
                cached = self.legacy_images.cache.get(source)
            target = (cached.get('path') if isinstance(cached, dict)
                      else cached[0] if isinstance(cached, tuple) and len(cached) == 4
                      else cached if isinstance(cached, str) else source)
            if not target:
                continue
            labels[target] = item['text']
        unbound = []
        deadline = time.monotonic() + 20
        for ref in request.image_urls or []:
            label = labels.get(ref)
            if not label:
                unbound.append(ref)
                continue
            request.extra_user_content_parts.append(TextPart(text=label + '（以下图片只对应这一来源，引用图与当前新图分开理解；内部身份不照读给群友）').mark_as_temp())
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('image source binding budget')
                encoded = await asyncio.wait_for(Image(file=ref).convert_to_base64(), timeout=min(10, remaining))
                if not encoded:
                    raise ValueError('empty image')
            except Exception:
                request.extra_user_content_parts.append(TextPart(text='这张图片本轮无法读取，不推测画面。').mark_as_temp())
                continue
            mime = 'image/png' if encoded.startswith('iVBORw') else 'image/jpeg'
            url = encoded if encoded.startswith('data:') else f'data:{mime};base64,{encoded}'
            request.extra_user_content_parts.append(ImageURLPart(image_url=ImageURLPart.ImageURL(url=url)).mark_as_temp())
        request.image_urls = unbound

    async def invoke(self, **options):
        # The SDK helper carries temporary ContentParts and uses native context
        # compression. A no-tool request gets exactly one generation step.
        if self.plugin.engine.native_turn_busy(self.target):
            raise RuntimeError("正常回复已开始，取消主动判断")
        response = await self.plugin.context.tool_loop_agent(event=self.event, tools=None,
            max_steps=1, request_max_retries=0, **self.runner_options, **options)
        response.completion_text = SpeechDraft(response.completion_text or "", native_event=self.event)
        return response
