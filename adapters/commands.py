"""指令触发适配：借一条真实事件调用别的插件注册的指令。
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

from astrbot.api.message_components import Plain

from ..core.constants import PLUGIN_NAME
from ..core.ports import ToolCallResult
from .session_utils import _swap_event_session
from .tools import _exception_where, _result_text
from .vision import _caption_result_images

if TYPE_CHECKING:
    from .. import MyWorldPlugin


class AstrBotCommands:
    """把「指令触发」型动作转成真正的指令，交给别的插件执行。

    寻找方式与 AstrBot 的指令分发一致：从 handler 注册表里按指令名（含别名）匹配，
    借用这个会话最近一条真实事件，把消息文本换成指令本身，再调用它的处理器。
    处理器 yield 出来的文本会被收集起来交回给大模型。
    """

    def __init__(self, plugin: "MyWorldPlugin") -> None:
        self.plugin = plugin

    def session_with_context(self, candidates: list[str] | None = None) -> str:
        """最近收到过消息的那条会话（在 ``candidates`` 里挑，没给就全局挑）。"""

        events = getattr(self.plugin, "_last_events", None) or {}
        if not events:
            return ""
        if candidates is None:
            return next(reversed(events), "")
        wanted = {str(item) for item in candidates}
        for session_id in reversed(events):
            if session_id in wanted:
                return session_id
        return ""

    async def trigger(
        self, session_id: str, command: str, *, event: Any = None
    ) -> ToolCallResult:
        text = " ".join(str(command or "").split())
        if not text:
            return ToolCallResult(ok=False, error="指令是空的")
        if not text.startswith("/"):
            text = "/" + text
        word = text.split()[0].lstrip("/")
        matched = self._find(text)
        if matched is None:
            return ToolCallResult(ok=False, error=f"没找到指令「{word}」")
        record, command_filter = matched
        # 借别的会话的事件会让指令**在别人的会话里**跑：出图、发语音这类
        # 处理器常按事件的会话把结果直接发出去，于是私聊里让她拍照、图却
        # 出现在群里。``nearby_event`` 只借同一个会话组里的，找不到就宁可不跑。
        real_event = event or self.plugin.nearby_event(session_id)
        if real_event is None:
            return ToolCallResult(
                ok=False, error="还没有收到过这个会话的消息，指令没有上下文可用"
            )
        try:
            params = self._params(record, command_filter, text)
        except Exception as exc:
            return ToolCallResult(ok=False, error=str(exc))
        restore = self._swap_text(real_event, text)
        restore_session = _swap_event_session(real_event, session_id)
        try:
            result = record.handler(real_event, **params)
            texts: list[str] = []
            images: list[str] = []
            if inspect.isasyncgen(result):
                async for item in result:
                    body, item_images = await _result_text(item)
                    if body:
                        texts.append(body)
                    images.extend(item_images)
            elif inspect.isawaitable(result):
                body, item_images = await _result_text(await result)
                if body:
                    texts.append(body)
                images.extend(item_images)
            else:
                body, item_images = await _result_text(result)
                if body:
                    texts.append(body)
                images.extend(item_images)
        except Exception as exc:
            # 指令跑挂时把"最后落在哪个文件哪一行"写进日志与返回值：
            # 报错来自别的插件（例如它拿到的参数类型不对）时，一眼能看出是谁的锅
            where = _exception_where(exc)
            detail = f"{type(exc).__name__}: {exc}"
            if where:
                detail += f"（{where}）"
            self.plugin.logger.warning(
                f"[my_world] 指令「{text}」执行失败：{detail}",
                exc_info=True,
            )
            return ToolCallResult(ok=False, error=detail, tool=word)
        finally:
            restore_session()
            restore()
        captions, pending_images = await _caption_result_images(
            self.plugin, images, scene=text
        )
        if captions:
            texts.append("；".join(captions))
        body = "\n".join(part for part in texts if part).strip()
        if not body:
            body = (
                "（指令执行了，返回了一张图片）"
                if pending_images
                else f"（指令「{word}」执行了，但没有返回内容）"
            )
        return ToolCallResult(
            ok=True,
            text=body,
            tool=word,
            image_urls=pending_images,
            # 原图另存一份：转述成功时 image_urls 会空，但这张图还是得发出去
            attachments=list(images),
        )

    def _find(self, text: str) -> tuple[Any, Any] | None:
        """按指令名找处理器（含别名）；跳过本插件自己的指令，避免绕回自己。"""

        try:
            from astrbot.core.star.star_handler import (
                EventType,
                star_handlers_registry,
            )
        except Exception:
            return None
        try:
            handlers = star_handlers_registry.get_handlers_by_event_type(
                EventType.AdapterMessageEvent
            )
        except Exception:
            return None
        for record in handlers:
            module = str(getattr(record, "handler_module_path", "") or "")
            if PLUGIN_NAME in module:
                continue
            for event_filter in list(getattr(record, "event_filters", []) or []):
                names = getattr(event_filter, "get_complete_command_names", None)
                if not callable(names):
                    continue
                try:
                    candidates = [str(name) for name in names()]
                except Exception:
                    continue
                for name in candidates:
                    if event_filter.equals(text) or text.split()[0].lstrip("/") == name.lstrip("/"):
                        return record, event_filter
        return None

    @staticmethod
    def _params(record: Any, command_filter: Any, text: str) -> dict[str, Any]:
        """按处理器签名解析参数（和 AstrBot 指令分发同一套转换）。

        参数类型优先取 ``CommandFilter.handler_params``——那是 AstrBot 自己解析好的
        （它用 ``inspect.signature(..., eval_str=True)``，会把
        ``from __future__ import annotations`` 留下的**字符串注解**还原成真类型）。

        以前这里是自己再 inspect 一遍、**没有 eval_str**：注解是字符串时，
        `validate_and_convert_params` 会把它当成"默认值"原样透传，
        于是需要 ``MessageChain`` 这类对象的指令收到一个 str，接着就报
        ``'str' object has no attribute 'chain'``。
        """

        param_type = dict(getattr(command_filter, "handler_params", None) or {})
        if not param_type:
            try:
                signature = inspect.signature(record.handler, eval_str=True)
            except (TypeError, ValueError):
                signature = inspect.signature(record.handler)
            for name, parameter in signature.parameters.items():
                if name in ("self", "event"):
                    continue
                param_type[name] = (
                    parameter.annotation
                    if parameter.annotation is not inspect.Parameter.empty
                    else parameter.default
                )
        if not param_type:
            return {}
        args = text.split()[1:]
        return command_filter.validate_and_convert_params(args, param_type)

    @staticmethod
    def _swap_text(event: Any, text: str):
        """临时把事件的消息文本换成指令本身，返回还原用的回调。"""

        message_obj = getattr(event, "message_obj", None)
        previous_str = getattr(event, "message_str", None)
        previous_obj_str = getattr(message_obj, "message_str", None)
        previous_chain = getattr(message_obj, "message", None)
        try:
            event.message_str = text
        except Exception:
            pass
        if message_obj is not None:
            try:
                message_obj.message_str = text
                message_obj.message = [Plain(text=text)]
            except Exception:
                pass

        def restore() -> None:
            if previous_str is not None:
                try:
                    event.message_str = previous_str
                except Exception:
                    pass
            if message_obj is not None:
                if previous_obj_str is not None:
                    try:
                        message_obj.message_str = previous_obj_str
                    except Exception:
                        pass
                if previous_chain is not None:
                    try:
                        message_obj.message = previous_chain
                    except Exception:
                        pass

        return restore
