"""AstrBot 模型调用适配（LLMPort 实现）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..core.llm_usage import record_llm_usage
from ..core.ports import LLMReply

if TYPE_CHECKING:
    from .. import MyWorldPlugin


class AstrBotLLM:
    """LLMPort 实现。"""

    def __init__(
        self,
        plugin: "MyWorldPlugin",
        provider_id: str = "",
        role: str = "main",
        sampling: dict[str, float] | None = None,
    ) -> None:
        self.plugin = plugin
        self.provider_id = provider_id
        self.role = role
        """``main`` = 说话用的主模型（它的原始响应要留给别的插件看），其余是辅助模型。"""
        self.sampling = dict(sampling or {})
        """采样参数（温度 / top_p / 重复惩罚）。空 = 不覆盖，用 Provider 自己的设置。

        这些参数在最外层适配器上统一加：多轮聊天的复读和"每轮都一个腔调"，
        一半是采样参数的事，而插件自己的每次调用都不该各写一遍。
        """
        self._sampling_rejected = False
        """Provider 不认这些参数时，退回只传温度，别让主回复跟着挂掉。"""

    @staticmethod
    def _record_usage(response: Any) -> None:
        """把 Provider 返回的 usage 记进当前 Web 生成任务（不在任务里时为空操作）。"""

        record_llm_usage(getattr(response, "usage", None))

    async def generate(
        self,
        *,
        session_id: str,
        system_prompt: str,
        prompt: str,
        contexts: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        image_urls: list[str] | None = None,
    ) -> LLMReply:
        context = self.plugin.context
        provider_id = (self.provider_id or self.plugin.llm_provider_id or "").strip()
        if not provider_id and self.role == "main":
            # 别的插件（世界树这类）可能在这一轮里临时指定了模型：
            # 接管回复是我们自己调模型的，得认它一眼，不然"这一条换模型"会静默失效
            provider_id = self.plugin.turn_provider_override(session_id)
        if not provider_id:
            try:
                provider_id = await context.get_current_chat_provider_id(session_id)
            except Exception as exc:
                return LLMReply(ok=False, error=f"没有可用的 LLM Provider: {exc}")
        kwargs: dict[str, Any] = {}
        if temperature is not None:
            kwargs["temperature"] = temperature
        # 采样参数由适配器统一加（配置关掉时 self.sampling 是空的，等于不覆盖）
        if self.sampling and not self._sampling_rejected:
            for key in ("temperature", "top_p", "frequency_penalty", "presence_penalty"):
                if key in self.sampling and (key != "temperature" or temperature is None):
                    kwargs[key] = self.sampling[key]
        images = [str(item) for item in (image_urls or []) if str(item).strip()]
        token = self.plugin.set_self_initiated()
        try:
            response = await context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=system_prompt,
                contexts=list(contexts) if contexts else None,
                image_urls=images or None,
                **kwargs,
            )
            self._record_usage(response)
        except Exception as exc:
            # 上游按内容安全拒了这次生成：同一段内容再要一次还是会被拒，
            # 别再把它当成"Provider 不认采样参数"反复重试——那会把真正的原因埋掉，
            # 而且白白多打几次请求（露骨内容这条路上这条日志误导过一次排查）。
            blocked = any(
                marker in str(exc)
                for marker in ("content_filter", "PROHIBITED_CONTENT", "内容安全", "content policy")
            )
            # Provider 不认采样参数：退回只传温度，别让主回复跟着挂掉
            if self.sampling and not self._sampling_rejected and len(kwargs) > 1 and not blocked:
                self._sampling_rejected = True
                self.plugin.logger.warning(
                    f"[my_world] Provider 不接受采样参数（{exc}），"
                    "已退回只用温度；要调就去 Panel 的 Provider 里调"
                )
                kwargs = {key: value for key, value in kwargs.items() if key == "temperature"}
                try:
                    response = await context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=prompt,
                        system_prompt=system_prompt,
                        contexts=list(contexts) if contexts else None,
                        image_urls=images or None,
                        **kwargs,
                    )
                    self._record_usage(response)
                    if self.role == "main":
                        self.plugin.remember_llm_response(session_id, response)
                    text = getattr(response, "completion_text", "") or ""
                    return LLMReply(text=text, ok=True)
                except Exception as retry_exc:
                    return LLMReply(ok=False, error=f"LLM 调用失败: {retry_exc}")
            # Provider 不支持图片（或图片取不回来）时，退回纯文字再来一次
            if images:
                try:
                    response = await context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=prompt,
                        system_prompt=system_prompt,
                        contexts=list(contexts) if contexts else None,
                        **kwargs,
                    )
                    self._record_usage(response)
                    self.plugin.logger.warning(
                        f"[my_world] 带图片调用失败（{exc}），已退回纯文字"
                    )
                    if self.role == "main":
                        self.plugin.remember_llm_response(session_id, response)
                    text = getattr(response, "completion_text", "") or ""
                    return LLMReply(text=text, ok=True)
                except Exception as retry_exc:
                    return LLMReply(ok=False, error=str(retry_exc))
            if blocked:
                self.plugin.logger.warning(
                    f"[my_world] 上游按内容安全拒了这次生成（{str(exc)[:160]}）："
                    "换一段说法、或换一个 Provider（Panel → Provider）"
                )
                return LLMReply(ok=False, error=str(exc))
            # 历史上下文格式不被某些 Provider 接受时，退化成不带历史再试一次
            if contexts:
                try:
                    response = await context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=prompt,
                        system_prompt=system_prompt,
                        **kwargs,
                    )
                    self._record_usage(response)
                except Exception as retry_exc:
                    return LLMReply(ok=False, error=str(retry_exc))
            else:
                return LLMReply(ok=False, error=str(exc))
        finally:
            self.plugin.reset_self_initiated(token)
        if self.role == "main":
            # 主模型的原始响应留给「回复钩子」用：别的插件可能要看 usage / id 这些字段，
            # 自己拼一个假的响应它们会读不到而报错。
            self.plugin.remember_llm_response(session_id, response)
        text = getattr(response, "completion_text", "") or ""
        return LLMReply(text=text, ok=True)
