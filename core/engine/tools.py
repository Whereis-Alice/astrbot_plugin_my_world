"""工具：可用清单、参数补全与调用。"""

from __future__ import annotations

from typing import Any
from ..tool_policy import allowed_tools
from .util import (
    normalize_param_schema,
    render_param_text,
)


class ToolsMixin:
    # ================= 工具 =================

    def available_tools(self) -> dict[str, str]:
        """工具名 -> 描述（描述里会带上工具自带的参数说明，供提示词直接使用）。"""

        if self.tools is None:
            return {}
        try:
            result: dict[str, str] = {}
            for info in self.tools.list_tools():
                description = (info.description or "").strip()
                param_text = render_param_text(info.parameters or {})
                if param_text and param_text != "（这个工具不需要参数）":
                    description = f"{description}｜参数：{param_text}" if description else f"参数：{param_text}"
                result[info.name] = description
            return result
        except Exception:
            return {}

    def tool_schemas(self) -> dict[str, dict[str, Any]]:
        """工具名 -> 工具自带的参数 schema（来自 AstrBot 工具定义，不需要用户手写）。"""

        if self.tools is None:
            return {}
        try:
            return {
                info.name: dict(info.parameters or {}) for info in self.tools.list_tools()
            }
        except Exception:
            return {}

    def tool_sources(self) -> dict[str, str]:
        """工具名 -> 来源（``official`` = AstrBot 内置，``plugin`` = 插件 / MCP）。"""

        if self.tools is None:
            return {}
        try:
            return {
                info.name: (info.source or "plugin")
                for info in self.tools.list_tools()
            }
        except Exception:
            return {}

    def tool_owners(self) -> dict[str, str]:
        """工具名 -> 提供它的插件名（拿不到就空串，编辑器按空串归到「插件 · 其它」）。"""

        if self.tools is None:
            return {}
        try:
            return {
                info.name: str(getattr(info, "plugin", "") or "")
                for info in self.tools.list_tools()
            }
        except Exception:
            return {}

    def tool_param_text(self, tool_name: str) -> str:
        """把某个工具的参数渲染成一行中文说明，用于提示词与 Web 编辑器。"""

        schema = self.tool_schemas().get(tool_name) or {}
        return render_param_text(schema)

    def missing_tool_params(
        self,
        action_id: str,
        params: dict[str, Any],
        *,
        node_id: str = "",
        tool_name: str = "",
    ) -> list[str]:
        """检查工具必填参数是否缺失（参数定义来自工具自身 schema）。"""

        chosen = self.resolve_tool(action_id, node_id, tool_name)
        if not chosen:
            return []
        return self.missing_params_for(chosen, params)

    def missing_params_for(self, chosen: str, params: dict[str, Any]) -> list[str]:
        """某个已确定的工具还缺哪些必填参数。"""

        schema = normalize_param_schema(self.tool_schemas().get(chosen) or {})
        required = [str(name) for name in (schema.get("required") or [])]
        given = {str(key) for key, value in (params or {}).items() if value not in ("", None)}
        return [name for name in required if name not in given]

    def declared_params(self, chosen: str) -> list[str]:
        """工具自己声明了哪些参数（不分必填可选）。"""

        schema = normalize_param_schema(self.tool_schemas().get(chosen) or {})
        return [str(name) for name in (schema.get("properties") or {})]

    def unfilled_params(self, chosen: str, params: dict[str, Any]) -> list[str]:
        """声明了但这次还没给的参数。

        工具作者经常把参数写成"可选"，实现里却必须要（例如天气工具的 city），
        所以只要工具声明了参数、而这次一个都没给全，就值得让辅助模型试一次。
        """

        given = {str(key) for key, value in (params or {}).items() if value not in ("", None)}
        return [name for name in self.declared_params(chosen) if name not in given]

    def allowed_tool_names(self, node_id: str) -> set[str]:
        return allowed_tools(self.world, self.node(node_id))
