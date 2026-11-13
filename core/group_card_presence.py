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


async def group_card_baseline(plugin: Any, session_id: str, card: str) -> str | None:
    """Recover only a same-character card proven to have an automatic World suffix."""
    engine = getattr(plugin, "engine", None)
    parts = str(session_id).split(":", 2)
    if (engine is None or not getattr(plugin, "enabled", False)
            or getattr(plugin, "retired", False) or len(parts) != 3
            or parts[1] != "GroupMessage" or not isinstance(card, str) or not engine.is_enabled(session_id)):
        return None
    scope = getattr(engine, "config_scope", None)
    with scope(session_id) if callable(scope) else nullcontext():
        config = engine.world.nickname_sync
        if not config.enabled:
            return None
        signature_getter = getattr(engine, "_journal_config_signature", None)
        signature = signature_getter() if callable(signature_getter) else None
        _, persona_key = await engine._journal_persona(session_id)
        if not persona_key:
            return None
        state_key = engine.state_key(session_id)
        state = await engine.db.call("get_state", state_key)
        if not isinstance(state, dict) or state.get("bot_nickname_locked"):
            return None
        base = state.get("bot_base_nickname")
        if (not isinstance(base, str) or not base or base == card
                or state.get("bot_current_nickname") != card):
            return None
        labels = {"刚醒", "睡觉中", "小睡中", "犯困了", str(getattr(config, "event_text", "") or "")}
        for field in ("status_map", "node_status"):
            labels.update(str(value) for value in (getattr(config, field, {}) or {}).values())
        for field in ("actions", "nodes"):
            labels.update(str(getattr(item, "nickname_text", "") or "")
                          for item in (getattr(engine.world, field, []) or []))
        history = await engine.db.call("kv_get", "world_visits.v1", {})
        visits = history.get("visits", {}) if isinstance(history, dict) else {}
        for visit in visits.values() if isinstance(visits, dict) else []:
            if (not isinstance(visit, dict) or not visit.get("arrived_at")
                    or visit.get("scope") != state_key
                    or (visit.get("local") or {}).get("persona_key") != persona_key
                    or visit.get("role") not in {"host", "guest"}):
                continue
            remote = visit.get("remote") or {}
            companion = " ".join(str(remote.get("name") or "朋友").split())[:12]
            labels.add(f"接待{companion}" if visit["role"] == "host" else f"拜访{companion}")
        # Recognize precise generated templates, including the earlier default;
        # never split an arbitrary user-selected name at a separator.
        templates = {str(config.template), "{base} | {status}", "{base}｜{status}", "{base} ｜ {status}"}
        limit = max(1, int(config.max_length or 30))
        if not any(label and template.replace("{base}", base).replace("{status}", label)[:limit] == card
                   for label in labels for template in templates):
            return None
        events = await engine.db.call("query_events", session_id=session_id,
                                      event_type="nickname", limit=200)
        if not any(isinstance(event, dict) and event.get("session_id") == session_id
                   and event.get("persona_id") == persona_key
                   and isinstance(event.get("detail"), dict)
                   and event["detail"].get("ok") is True and event["detail"].get("to") == card
                   and not any(event["detail"].get(key) for key in ("manual", "live", "base"))
                   for event in events):
            return None
        _, latest_persona = await engine._journal_persona(session_id)
        latest = await engine.db.call("get_state", state_key)
        if (not getattr(plugin, "enabled", False) or getattr(plugin, "retired", False)
                or not engine.is_enabled(session_id) or not engine.world.nickname_sync.enabled
                or engine.state_key(session_id) != state_key
                or latest_persona != persona_key
                or not isinstance(latest, dict) or latest.get("bot_nickname_locked")
                or latest.get("bot_base_nickname") != base or latest.get("bot_current_nickname") != card
                or (callable(signature_getter) and signature_getter() != signature)):
            return None
        return base
