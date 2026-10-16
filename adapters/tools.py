"""工具适配：列出与调用 AstrBot 工具，并整形工具结果。
"""

from __future__ import annotations

import inspect
import json
import os
import types
from typing import TYPE_CHECKING, Any

from ..core.message_parts import _component_named, _resolve_image_ref
from ..core.ports import ToolCallResult, ToolInfo
from .session_utils import _swap_event_session
from .vision import _caption_result_images

if TYPE_CHECKING:
    from .. import MyWorldPlugin


def _tool_owner(tool: Any) -> str:
    """这个工具是哪个插件注册的（拿不到线索时返回空串，编辑器会归到「插件 · 其它」）。"""

    module_path = str(getattr(tool, "handler_module_path", "") or "")
    if not module_path:
        return ""
    try:
        from astrbot.core.star.star import star_map

        metadata = star_map.get(module_path)
    except Exception:
        return ""
    return str(getattr(metadata, "name", "") or "")
class AstrBotTools:
    """ToolPort 实现。"""

    def __init__(self, plugin: "MyWorldPlugin") -> None:
        self.plugin = plugin

    def has_context(self) -> bool:
        """手上有没有一条真实消息事件：工具调用要拿它当上下文，没有就只能等。"""

        return self.plugin.last_event_any() is not None

    def list_tools(self) -> list[ToolInfo]:
        manager = self._manager()
        if manager is None:
            return []
        tools: list[ToolInfo] = []
        seen: set[str] = set()
        for tool in list(getattr(manager, "func_list", []) or []):
            name = getattr(tool, "name", "") or ""
            if not name or name in seen:
                continue
            # 在 AstrBot 面板上被停用的工具（例如 helper_tools 里关掉的某个模块）
            # 不会被交给模型，也调不动：不列进清单，免得用户挂成动作之后一头雾水
            if not bool(getattr(tool, "active", True)):
                continue
            seen.add(name)
            tools.append(
                ToolInfo(
                    name=name,
                    description=str(getattr(tool, "description", "") or ""),
                    parameters=dict(getattr(tool, "parameters", {}) or {}),
                    source="plugin",
                    plugin=_tool_owner(tool),
                )
            )
        # AstrBot 自带的内置工具不在 func_list 里（官方明确把它们单独放），
        # 但搜索、知识库这些正好是最常用的，所以一并列出来，标成「官方」。
        for tool in self._builtin_tools(manager):
            name = getattr(tool, "name", "") or ""
            if not name or name in seen:
                continue
            if not bool(getattr(tool, "active", True)):
                continue
            seen.add(name)
            tools.append(
                ToolInfo(
                    name=name,
                    description=str(getattr(tool, "description", "") or ""),
                    parameters=dict(getattr(tool, "parameters", {}) or {}),
                    source="official",
                    plugin=_tool_owner(tool),
                )
            )
        return tools

    @staticmethod
    def _builtin_tools(manager) -> list[Any]:
        """AstrBot 的内置工具列表；老版本没有这个接口时返回空。"""

        iterator = getattr(manager, "iter_builtin_tools", None)
        if not callable(iterator):
            return []
        try:
            return list(iterator() or [])
        except Exception:
            return []

    def _manager(self):
        try:
            return self.plugin.context.get_llm_tool_manager()
        except Exception:
            return None

    async def call_tool(
        self, name: str, params: dict[str, Any], session_id: str = ""
    ) -> ToolCallResult:
        """调用工具。返回结构化结果，失败时带上原因（会写进事件日志，方便排查）。"""

        manager = self._manager()
        if manager is None:
            return ToolCallResult(ok=False, error="拿不到 AstrBot 的工具管理器", tool=name)
        tool = None
        getter = getattr(manager, "get_func", None)
        if callable(getter):
            try:
                tool = getter(name)
            except Exception:
                tool = None
        if tool is None:
            for candidate in list(getattr(manager, "func_list", []) or []):
                if getattr(candidate, "name", "") != name:
                    continue
                # 停用的工具不算"可用"：注释里写清楚原因，比"没有注册"好排查得多
                if not bool(getattr(candidate, "active", True)):
                    continue
                tool = candidate
                break
        # get_func() 可能返回停用对象，包括不在 func_list 里的内置工具。
        if tool is None or not bool(getattr(tool, "active", True)):
            inactive = tool is not None or any(
                getattr(candidate, "name", "") == name
                and not bool(getattr(candidate, "active", True))
                for candidate in list(getattr(manager, "func_list", []) or [])
            )
            if inactive:
                return ToolCallResult(
                    ok=False,
                    error=(
                        f"工具「{name}」在 AstrBot 里被停用了"
                        "（去提供它的插件配置里打开，或在 WebUI 的工具列表里启用）"
                    ),
                    tool=name,
                )
            return ToolCallResult(ok=False, error=f"工具「{name}」没有注册", tool=name)
        # 借同组事件兜底：工具大多只需要「谁在什么群说的」，没有事件连 call()
        # 都进不去。但绝不借别的会话组的——出图这类工具会按事件的会话发结果。
        event = self.plugin.nearby_event(session_id)
        if event is None:
            # 插件刚启动 / 刚重载、群里还没人说话时，一个真实事件都没有。
            # 这时候硬调只会让工具内部炸出「'NoneType' object has no attribute ...」，
            # 看不出到底为什么失败——直接说清楚，日志里一眼能懂。
            return ToolCallResult(
                ok=False,
                error=(
                    "刚启动还没收到过消息，工具需要一条消息当上下文；"
                    "等群里有人说一句，或者先让她做别的事"
                ),
                tool=name,
            )
        call_params = self._filter_params(name, params, tool)
        # AstrBot 的工具其实有三种写法：@filter.llm_tool 的 handler、新版 call()、
        # 以及老的 run()。MCP 工具和新式工具都没有 handler，只能走 call()。
        # 这里跟 AstrBot 自己的执行器保持同一套优先级，否则只会报「没有可调用的 handler」。
        invoke = _tool_invoker(tool)
        if invoke is None:
            return ToolCallResult(
                ok=False, error=f"工具「{name}」没有可调用的 handler", tool=name
            )
        # 借来的事件要指到这个会话上再调：工具可能自己往事件的会话发东西
        restore_session = _swap_event_session(event, session_id)
        try:
            # 工具前后也补一遍钩子：有些插件靠它们显示"正在用工具 / 用完了"
            await self.plugin._fire_hook(
                event, "OnUsingLLMToolEvent", tool, dict(call_params)
            )
            result = invoke(event, call_params, self.plugin)
            images: list[str] = []
            if inspect.isasyncgen(result):
                last: Any = None
                async for item in result:
                    last = item
                text, images = await _result_text(last)
            elif inspect.isawaitable(result):
                text, images = await _result_text(await result)
            else:
                text, images = await _result_text(result)
            await self.plugin._fire_hook(
                event,
                "OnLLMToolRespondEvent",
                tool,
                dict(call_params),
                _as_tool_result(text),
            )
        except Exception as exc:
            self.plugin.logger.warning(f"[my_world] 工具 {name} 调用失败：{exc}")
            # 工具的报错经常是「event 是 None」这种内部细节，补一句人话更好排查
            hint = ""
            if isinstance(exc, AttributeError) and "NoneType" in str(exc):
                hint = "（工具拿不到消息上下文：插件刚启动或刚重载过，等群里有人说一句再试）"
            return ToolCallResult(
                ok=False, error=f"调用出错：{exc}{hint}", tool=name, params=call_params
            )
        finally:
            restore_session()
        captions, pending_images = await _caption_result_images(
            self.plugin, images, scene=str(name)
        )
        if captions:
            text = "\n".join([part for part in (text, "；".join(captions)) if part])
        text = (text or "").strip()
        if not text and not pending_images:
            return ToolCallResult(
                ok=False, error="工具返回了空结果", tool=name, params=call_params
            )
        if not text:
            text = "（工具返回了一张图片）"
        return ToolCallResult(
            ok=True,
            text=text,
            tool=name,
            params=call_params,
            image_urls=pending_images,
            # 原图另存一份：转述成功时 image_urls 会空，但这张图还是得发到群里
            attachments=list(images),
        )

    def _filter_params(
        self, name: str, params: dict[str, Any], tool: Any = None
    ) -> dict[str, Any]:
        """只把工具自己声明过的参数传给它。

        参数是辅助模型按 schema 填的，难免多塞一两个没声明的键；
        直接透传会让工具的 handler 抛 TypeError，最后表现为"工具没返回任何东西"。
        """

        given = dict(params or {})
        if not given:
            return {}
        if tool is None:
            manager = self._manager()
            for candidate in list(getattr(manager, "func_list", []) or []):
                if getattr(candidate, "name", "") == name:
                    tool = candidate
                    break
        schema = dict(getattr(tool, "parameters", {}) or {})
        properties = dict(schema.get("properties") or {})
        if not properties:
            return given
        return {key: value for key, value in given.items() if key in properties}
def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    for attr in ("completion_text", "text", "message"):
        found = getattr(value, attr, None)
        if isinstance(found, str) and found:
            return found
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _tool_result_text(value: Any) -> str:
    """把工具返回值变成文本。

    MCP 工具和 AstrBot 的部分内置工具返回的是 ``CallToolResult``（内容是若干 TextContent），
    直接 ``str()`` 会得到一串对象表示，这里把里面的文本挑出来。
    """

    if value is None:
        return ""
    content = getattr(value, "content", None)
    if isinstance(content, (list, tuple)) and content:
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
            else:
                text = getattr(item, "text", None)
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        if parts:
            return "\n".join(parts)
    return _stringify(value)


MAX_RESULT_CHARS = 800
"""工具 / 指令返回内容进提示词前的截断长度（日志里另有一份更短的）。"""


async def _chain_text_and_images(value: Any) -> tuple[str, list[str]] | None:
    """把 ``MessageChain`` / ``MessageEventResult`` 拆成「人话」和「图片地址」。

    直接 ``str()`` 会得到一坨 dataclass 表示：里面虽然夹着正文，但也塞满了
    ``Plain(type=...)`` 之类的噪音，既费 token，模型还容易照抄。这里按组件类型
    老老实实还原——文本归文本、@ 归 @、图片挑出来单独交给多模态模型看。

    返回 ``None`` 表示"这个值不是消息链"，交给别的分支处理。
    """

    chain = getattr(value, "chain", None)
    if not isinstance(chain, (list, tuple)):
        return None
    texts: list[str] = []
    images: list[str] = []
    for component in chain:
        if _component_named(component, "Plain"):
            text = str(getattr(component, "text", "") or "")
            if text:
                texts.append(text)
        elif _component_named(component, "At"):
            who = str(getattr(component, "name", "") or "").strip()
            qq = str(getattr(component, "qq", "") or "").strip()
            texts.append(f"@{who or qq}")
        elif _component_named(component, "Image"):
            ref = await _resolve_image_ref(component)
            if ref and ref not in images:
                images.append(ref)
            texts.append(f"［图片{len(images)}］" if images else "［图片］")
        elif _component_named(component, "Face"):
            texts.append("［表情］")
        elif _component_named(component, "Reply"):
            continue
        else:
            # 其它组件（语音、文件、转发…）只报个类型名，别把对象表示塞进提示词
            name = type(component).__name__
            if name:
                texts.append(f"［{name}］")
    return "".join(texts).strip(), images


def _exception_where(exc: BaseException) -> str:
    """异常最后落在哪个文件:行（哪个函数）：给别人看的"这是谁的锅"。"""

    tb = exc.__traceback__
    last = None
    while tb is not None:
        last = tb
        tb = tb.tb_next
    if last is None:
        return ""
    frame = last.tb_frame
    name = os.path.basename(str(frame.f_code.co_filename or ""))
    return f"{name}:{last.tb_lineno} {frame.f_code.co_name}"


async def _result_text(value: Any) -> tuple[str, list[str]]:
    """工具 / 指令的返回值 → (文字, 图片地址)。"""

    if value is None:
        return "", []
    parts = await _chain_text_and_images(value)
    if parts is not None:
        text, images = parts
        return _clip_brief(text), images
    if _component_named(value, "Image"):
        ref = await _resolve_image_ref(value)
        return "", ([ref] if ref else [])
    return _clip_brief(_tool_result_text(value)), []


def _clip_brief(text: Any) -> str:
    """返回内容截断：进提示词和进日志都用它，别让一坨 base64 撑爆上下文。"""

    body = str(text or "").strip()
    if len(body) <= MAX_RESULT_CHARS:
        return body
    return body[:MAX_RESULT_CHARS] + "…（内容过长已截断）"


def _as_tool_result(text: str) -> Any:
    """把工具返回的文字包装成 AstrBot 钩子认的 ``CallToolResult``。"""

    try:
        from mcp.types import CallToolResult, TextContent
    except Exception:
        return None
    try:
        return CallToolResult(
            content=[TextContent(type="text", text=str(text or ""))],
            isError=False,
        )
    except Exception:
        return None


class _ToolRunContext:
    """新版工具的 ``call(context, **kwargs)`` 需要的运行上下文。

    等价于 AstrBot 的 ``ContextWrapper[AstrAgentContext]``：工具真正用到的只有
    ``context.context.event``、``context.context.context`` 和 ``context.tool_call_timeout``。
    拿得到 AstrBot 自己的类时用真的，拿不到（老版本 / 单测环境）就用这个替身。
    """

    def __init__(self, plugin: "MyWorldPlugin", event: Any) -> None:
        try:
            from astrbot.core.agent.run_context import ContextWrapper
            from astrbot.core.astr_agent_context import AstrAgentContext

            inner = AstrAgentContext(context=plugin.context, event=event)
            self._wrapper = ContextWrapper(context=inner, tool_call_timeout=120)
        except Exception:
            self._wrapper = None
        self.context = types.SimpleNamespace(context=plugin.context, event=event)
        self.messages: list[Any] = []
        self.tool_call_timeout = 120

    def __getattr__(self, name: str) -> Any:
        wrapper = self.__dict__.get("_wrapper")
        if wrapper is not None:
            return getattr(wrapper, name)
        raise AttributeError(name)


def _tool_invoker(tool: Any):
    """按 AstrBot 的优先级挑出调用方式，返回 ``fn(event, params, plugin)``。"""

    handler = getattr(tool, "handler", None)
    if callable(handler):

        def call_handler(event: Any, params: dict[str, Any], plugin: Any):
            return handler(event, **params)

        return call_handler

    call = getattr(tool, "call", None)
    if callable(call) and _is_call_override(call):

        def call_method(event: Any, params: dict[str, Any], plugin: Any):
            return call(_ToolRunContext(plugin, event), **params)

        return call_method

    run = getattr(tool, "run", None)
    if callable(run):

        def call_run(event: Any, params: dict[str, Any], plugin: Any):
            return run(event, **params)

        return call_run

    return None


def _is_call_override(call: Any) -> bool:
    """判断 ``call`` 是工具自己实现的，而不是 ``FunctionTool`` 那个只抛错的默认版。"""

    func = getattr(call, "__func__", None)
    if func is None:
        return True
    try:
        from astrbot.core.agent.tool import FunctionTool

        return func is not FunctionTool.call
    except Exception:
        return not str(getattr(func, "__qualname__", "")).endswith("FunctionTool.call")
