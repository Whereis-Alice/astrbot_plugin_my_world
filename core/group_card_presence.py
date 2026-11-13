"""Read-only, confirmed character presence for group-card integrations."""
from __future__ import annotations

from contextlib import nullcontext
from typing import Any

from .visit_presence import active_visit


async def engine_group_card_override(engine: Any, session_id: str, *,
                                     persona_key: str | None = None) -> dict[str, str] | None:
    """Return presence; read the current local persona, never world state or LLM."""
    parts = str(session_id).split(":", 2)
    if (len(parts) != 3 or parts[1] != "GroupMessage"
            or not engine.is_enabled(session_id)):
        return None
    scope = getattr(engine, "config_scope", None)
    with scope(session_id) if callable(scope) else nullcontext():
        service = getattr(engine, "visits", None)
        if (not engine.world.nickname_sync.enabled or service is None
                or getattr(service, "_closed", False)
                or not getattr(getattr(service, "config", None), "enabled", False)):
            return None
        if hasattr(service, "_loaded") and not service._loaded:
            raise RuntimeError("小世界正在恢复串门状态，请稍后重试")
        visit = active_visit(engine, session_id)
        if (not visit or not visit.get("arrived_at")
                or visit.get("scope") != engine.state_key(session_id)):
            return None
        if persona_key is None:
            _, persona_key = await engine._journal_persona(session_id)
            visit = active_visit(engine, session_id)
            if (not visit or not visit.get("arrived_at")
                    or visit.get("scope") != engine.state_key(session_id)):
                return None
        if not persona_key or (visit.get("local") or {}).get("persona_key") != persona_key:
            return None
        remote = visit.get("remote") or {}
        companion = " ".join(str(remote.get("name") or "朋友").split())[:12]
        result = {"source": "astrbot_plugin_my_world", "token": str(visit.get("id") or ""),
                  "status": f"接待{companion}" if visit.get("role") == "host" else f"拜访{companion}"}
        # A temporary situation replaces the ordinary status. Its prefix is
        # this confirmed character's public name, never a previous full card.
        name = (visit.get("local") or {}).get("name")
        if isinstance(name, str) and name.strip():
            result["name"] = " ".join(name.split())
        return result


async def group_card_override(plugin: Any, session_id: str) -> dict[str, str] | None:
    """Public protocol: None means no override; unavailable state raises for retry."""
    engine = getattr(plugin, "engine", None)
    if (engine is None or not getattr(plugin, "enabled", False)
            or getattr(plugin, "retired", False)):
        return None
    return await engine_group_card_override(engine, session_id)
