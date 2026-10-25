"""AstrBot 模型调用适配（LLMPort 实现）。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import uuid4

from ..core.model_calls import call_context
from ..core.model_call_runtime import tracked_generate
from ..core.ports import LLMReply

if TYPE_CHECKING:
    from .. import MyWorldPlugin


def _retry_kind(exc: Exception, *, sampling: bool, images: bool, contexts: bool,
                max_tokens: bool) -> str:
    """Only adapt an explicitly rejected request shape; keep transport failures intact."""

    text = str(exc).lower()
    if any(word in text for word in (
        "content_filter", "prohibited_content", "内容安全", "content policy",
        "rate limit", "rate_limit", "quota", "429", "timeout", "timed out",
        "connection", "authentication", "unauthorized", "401", "403", "502", "503",
    )):
        return ""
    unsupported = any(word in text for word in (
        "unsupported", "not support", "not accept", "unexpected keyword",
        "unknown parameter", "unrecognized", "not permitted", "不支持", "不接受",
    ))
    if sampling and unsupported and any(word in text for word in (
        "sampling", "top_p", "frequency_penalty", "presence_penalty",
    )):
        return "sampling"
    if max_tokens and unsupported and "max_tokens" in text:
        return "max_tokens"
    malformed = unsupported or any(word in text for word in (
        "invalid", "malformed", "decode", "format", "fetch", "download", "not found", "无效",
    ))
    if images and malformed and any(word in text for word in ("image", "multimodal", "vision", "图片")):
        return "images"
    if contexts and malformed and any(word in text for word in ("contexts", "history", "messages", "历史")):
        return "contexts"
    return ""


class AstrBotLLM:
    """LLMPort 实现；仅在 Provider 明确拒绝参数格式时做兼容重试。"""

    def __init__(self, plugin: "MyWorldPlugin", provider_id: str = "", role: str = "main",
                 sampling: dict[str, float] | None = None) -> None:
        self.plugin = plugin
        self.provider_id = provider_id
        self.role = role
        self.sampling = dict(sampling or {})
        # Provider 可以随会话切换；某一个不支持参数不能禁用其它 Provider 的配置。
        self._sampling_rejected_providers: set[str] = set()
        self._max_tokens_rejected_providers: set[str] = set()

    async def resolve_provider_id(self, session_id: str) -> str:
        """Resolve this role's current provider without generating or caching a turn."""
        provider_id = (self.provider_id or self.plugin.llm_provider_id or "").strip()
        if not provider_id and self.role == "main":
            provider_id = self.plugin.turn_provider_override(session_id)
        if not provider_id:
            provider_id = await self.plugin.context.get_current_chat_provider_id(session_id)
        return provider_id

    async def generate(self, *, session_id: str, system_prompt: str, prompt: str,
                       contexts: list[dict] | None = None, temperature: float | None = None,
                       max_tokens: int | None = None, image_urls: list[str] | None = None) -> LLMReply:
        context = self.plugin.context
        try:
            provider_id = await self.resolve_provider_id(session_id)
        except Exception as exc:
            return LLMReply(ok=False, error=f"没有可用的 LLM Provider: {exc}")
        kwargs: dict[str, Any] = {}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if self.sampling:
            for key in ("temperature", "top_p", "frequency_penalty", "presence_penalty"):
                if (key in self.sampling and (key != "temperature" or temperature is None)
                        and (key == "temperature" or provider_id not in self._sampling_rejected_providers)):
                    kwargs[key] = self.sampling[key]
        # AstrBot 的 OpenAI/Anthropic/Gemini/Responses 适配器均支持这个输出上限。
        # 若某个自定义 Provider 明确拒绝，再只对它省略此参数。
        if max_tokens is not None and provider_id not in self._max_tokens_rejected_providers:
            kwargs["max_tokens"] = max_tokens
        images = [str(item) for item in (image_urls or []) if str(item).strip()]
        attempt = 0
        retry_reason = ""
        trace_id = call_context().get("trace_id") or uuid4().hex
        from ..core.speech_runtime import SPEECH_REQUEST
        speech = SPEECH_REQUEST.get()
        agent, toolset, native = None, None, None

        async def invoke(**options):
            nonlocal attempt
            attempt += 1
            return await tracked_generate(self.plugin, session_id=session_id, role=self.role,
                trace_id=trace_id, attempt=attempt, retry_reason=retry_reason,
                invoke=(lambda **kw: agent.invoke(toolset, runner_options=native.runner_options if native else None, **kw)) if toolset else native.invoke if native else None,
                coverage="agent_final" if toolset else "direct", **options)

        options = dict(chat_provider_id=provider_id, prompt=prompt, system_prompt=system_prompt,
                       contexts=list(contexts) if contexts else None, image_urls=images or None, **kwargs)
        adapted: set[str] = set()
        response = None
        token = self.plugin.set_self_initiated()
        try:
            speech_role = bool(speech and (self.role == "main" or (
                self.role == "helper" and speech.get("channel") == "helper")))
            if speech_role and callable(getattr(context, "tool_loop_agent", None)):
                try:
                    from .proactive_context import ProactiveContext
                    native = ProactiveContext(self.plugin, speech)
                    options = await native.prepare(options)
                    if self.role == "main" and speech.get("allow_tools") and self.plugin.engine.world.decider.proactive_tools:
                        from .proactive_agent import ProactiveAgent
                        agent = ProactiveAgent(self.plugin, speech)
                        toolset = await agent.prepare(event=native.event)
                except Exception as exc:
                    return LLMReply(ok=False, error=f"主动上下文准备失败：{type(exc).__name__}: {exc}")
            while True:
                try:
                    response = await invoke(**options)
                    break
                except Exception as exc:
                    if toolset:
                        # A retry of the whole loop could repeat tool side effects.
                        return LLMReply(ok=False, error=str(exc))
                    kind = _retry_kind(exc, sampling=any(key in options for key in (
                        "top_p", "frequency_penalty", "presence_penalty")),
                        images=bool(options.get("image_urls")), contexts=bool(options.get("contexts")),
                        max_tokens="max_tokens" in options)
                    if not kind or kind in adapted:
                        return LLMReply(ok=False, error=str(exc))
                    adapted.add(kind)
                    if kind == "sampling":
                        self._sampling_rejected_providers.add(provider_id)
                        for key in ("top_p", "frequency_penalty", "presence_penalty"):
                            options.pop(key, None)
                        retry_reason = "采样参数不兼容，退回只传温度"
                    elif kind == "max_tokens":
                        self._max_tokens_rejected_providers.add(provider_id)
                        options.pop("max_tokens", None)
                        retry_reason = "输出上限参数不兼容，使用 Provider 默认上限"
                    elif kind == "images":
                        options["image_urls"] = None
                        retry_reason = "图片格式不兼容，退回纯文字"
                    elif native:
                        # Never silently discard the native history that the
                        # user explicitly expects proactive replies to use.
                        return LLMReply(ok=False, error=str(exc))
                    else:
                        options["contexts"] = None
                        retry_reason = "历史上下文格式不兼容，去除历史后重试"
                    self.plugin.logger.warning(f"[my_world] {retry_reason}")
        finally:
            try:
                if native is not None:
                    try:
                        await native.close(retain=getattr(getattr(response, "completion_text", None), "images", ()))
                    except Exception as exc:
                        # Cleanup cannot replace the model result, its original
                        # failure or a cancellation already unwinding this call.
                        self.plugin.logger.warning(f"[my_world] 主动请求资源清理失败：{type(exc).__name__}")
            finally:
                self.plugin.reset_self_initiated(token)
        if response is None or getattr(response, "role", "") == "err":
            return LLMReply(ok=False, error=str(getattr(response, "completion_text", "") or "Provider 返回错误响应"))
        if self.role == "main":
            self.plugin.remember_llm_response(session_id, response)
        return LLMReply(text=getattr(response, "completion_text", "") or "", ok=True)
