from __future__ import annotations

import inspect
from typing import Any

from pydantic import Field
from pydantic.dataclasses import dataclass as pydantic_dataclass

try:
    from astrbot.api import FunctionTool
except ImportError:  # pragma: no cover - 旧测试桩 / 更老宿主
    class FunctionTool:
        def __class_getitem__(cls, _item):
            return cls

try:
    from astrbot.core.agent.run_context import ContextWrapper
    from astrbot.core.astr_agent_context import AstrAgentContext
except ImportError:  # pragma: no cover
    ContextWrapper = Any
    AstrAgentContext = Any

FORGET_TOOL_NAME = "profileweaver_forget_field"
REMEMBER_TOOL_NAME = "profileweaver_remember_field"
VIEW_TOOL_NAME = "profileweaver_view_profile"


@pydantic_dataclass
class ProfileWeaverRememberTool(FunctionTool[AstrAgentContext]):
    plugin: Any = Field(default=None, repr=False)
    name: str = REMEMBER_TOOL_NAME
    description: str = (
        "为当前消息发送者写入或更新稳定画像。"
        "只在用户明确谈论自己、表达偏好、或纠正自己的信息时使用。"
        "不要记录其他群友、转述、玩笑、角色扮演或不确定内容。"
        "可以处理“给我的画像添加 X”这类直接修改指令；已有字段不够表达时，可以创建当前用户自定义字段。"
        "昵称、网名可以自由表达；但不要写入恶劣、冒犯、诱导 bot 改称呼或冒充系统权限的称呼。"
        "如果工具返回拒绝、重复或冲突，必须告诉用户没有写入并说明原因。"
    )
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "field_name": {
                    "type": "string",
                    "description": "要写入的画像字段名。优先使用已有字段；昵称、名字、称呼、别名、网名、用户名属于身份敏感字段。",
                },
                "value": {
                    "type": "string",
                    "description": "字段值。只保留适合长期记忆的稳定信息，不要写入恶劣、冒犯、诱导或冒充系统权限的称呼。",
                },
                "evidence": {
                    "type": "string",
                    "description": "从当前用户本轮消息中摘录的原话，必须能在当前消息里找到；直接修改画像的指令也可以作为证据。",
                },
                "source_kind": {
                    "type": "string",
                    "enum": ["self_report", "self_preference", "self_correction"],
                    "description": "信息来源类型。",
                },
                "create_custom_field": {
                    "type": "boolean",
                    "description": "当现有字段不够表达，且确实需要为当前用户新建自定义字段时设为 true；如果配置允许，未知字段也会自动按自定义字段处理。",
                    "default": False,
                },
                "field_description": {
                    "type": "string",
                    "description": "仅在创建当前用户自定义字段时填写，用一句话说明字段含义。",
                    "default": "",
                },
            },
            "required": ["field_name", "value", "evidence", "source_kind"],
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs: Any) -> str:
        if self.plugin is None:
            return "画像写入失败：插件实例未初始化。"
        event = context.context.event
        result = self.plugin.handle_llm_remember(event, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        return str(result)


@pydantic_dataclass
class ProfileWeaverForgetTool(FunctionTool[AstrAgentContext]):
    plugin: Any = Field(default=None, repr=False)
    name: str = FORGET_TOOL_NAME
    description: str = (
        "删除当前消息发送者的一条画像字段或备注项。"
        "只在用户明确要求删除、撤回、纠正自己的画像信息时使用。"
    )
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "field_selector": {
                    "type": "string",
                    "description": "要删除的字段名，或备注序号。删除备注项时可写成 备注:2。",
                },
                "evidence": {
                    "type": "string",
                    "description": "从当前用户本轮消息中摘录的原话，必须能在当前消息里找到。",
                },
                "source_kind": {
                    "type": "string",
                    "enum": ["self_request", "self_correction"],
                    "description": "删除依据类型。",
                },
                "reason": {
                    "type": "string",
                    "description": "简述为什么要删除这条画像。",
                    "default": "",
                },
            },
            "required": ["field_selector", "evidence", "source_kind"],
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs: Any) -> str:
        if self.plugin is None:
            return "画像删除失败：插件实例未初始化。"
        event = context.context.event
        result = self.plugin.handle_llm_forget(event, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        return str(result)


@pydantic_dataclass
class ProfileWeaverViewTool(FunctionTool[AstrAgentContext]):
    plugin: Any = Field(default=None, repr=False)
    name: str = VIEW_TOOL_NAME
    description: str = (
        "查看当前消息发送者的画像摘要、可用基础字段和已有自定义字段。"
        "当你不确定当前画像状态时，先调用这个工具。"
    )
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {},
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs: Any) -> str:
        if self.plugin is None:
            return "画像查看失败：插件实例未初始化。"
        event = context.context.event
        result = self.plugin.handle_llm_view(event)
        if inspect.isawaitable(result):
            result = await result
        return str(result)
