"""人设适配（PersonaPort 实现）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .. import MyWorldPlugin


class AstrBotPersona:
    """PersonaPort 实现。拿不到人格时降级为空字符串。"""

    def __init__(self, plugin: "MyWorldPlugin") -> None:
        self.plugin = plugin

    def _config(self):
        engine = getattr(self.plugin, "engine", None)
        world = getattr(engine, "world", None) if engine is not None else None
        return getattr(world, "persona", None)

    async def astrbot_persona_text(self, session_id: str) -> str:
        """AstrBot 给这个会话选的那份人格（按会话 / 配置文件解析）。"""

        manager = getattr(self.plugin.context, "persona_manager", None)
        if manager is None:
            return ""
        persona = None
        resolver = getattr(manager, "get_default_persona_v3", None)
        if callable(resolver):
            try:
                persona = await resolver(session_id)
            except Exception:
                persona = None
        if persona is None:
            return ""
        for field in ("prompt", "system_prompt", "text"):
            value = getattr(persona, field, None)
            if isinstance(value, str) and value.strip():
                return value
        if isinstance(persona, dict):
            for field in ("prompt", "system_prompt", "text"):
                value = persona.get(field)
                if isinstance(value, str) and value.strip():
                    return value
        return ""

    async def get_persona_text(self, session_id: str) -> str:
        """实际交给她的人设：按配置里的模式决定用哪一份。"""

        config = self._config()
        mode = str(getattr(config, "mode", "astrbot") or "astrbot")
        own = str(getattr(config, "text", "") or "").strip()
        if mode == "plugin" and own:
            return own
        base = await self.astrbot_persona_text(session_id)
        if mode == "append" and own:
            return f"{base}\n\n{own}".strip() if base.strip() else own
        # astrbot 模式，或者 plugin 模式但没填：都用 AstrBot 那份
        return base
