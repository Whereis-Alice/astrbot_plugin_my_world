"""只读检查其它插件的功能归属，不修改其配置或启动状态。"""

from __future__ import annotations

from typing import Any

DYNAMIC_CARD_PLUGIN = "astrbot_plugin_dynamic_card_plus"


def group_card_owner(context: Any, session_id: str, event: Any = None) -> str:
    """灵动名片已加载、启用且适用于本群时，让它独占群名片写入。"""

    parts = str(session_id).split(":", 2)
    if len(parts) != 3 or parts[1] != "GroupMessage":
        return ""
    getter = getattr(context, "get_all_stars", None)
    if not callable(getter):
        return ""
    try:
        stars = getter() or []
    except Exception:
        return ""
    for star in stars:
        names = {str(getattr(star, key, "") or "") for key in ("name", "root_dir_name")}
        if DYNAMIC_CARD_PLUGIN not in names or not getattr(star, "activated", False):
            continue
        instance = getattr(star, "star_cls", None)
        if instance is None:
            continue
        config = getattr(instance, "config", None)
        if config is None:
            config = getattr(star, "config", None)
        if not hasattr(config, "get"):
            continue
        common = config.get("common", {})
        legacy = config.get("general", {})
        common = common if isinstance(common, dict) else {}
        legacy = legacy if isinstance(legacy, dict) else {}
        enabled = common.get("enabled", legacy.get("enabled", True))
        if str(enabled).strip().lower() in {"false", "0", "no", "off"}:
            continue
        group_ids = common.get("blacklist_group_ids", legacy.get("blacklist_group_ids", []))
        origins = common.get(
            "blacklist_unified_origins", legacy.get("blacklist_unified_origins", [])
        )
        if parts[2] in _ids(group_ids) or session_id in _ids(origins):
            continue
        # 跟随宿主的会话插件范围；不能借其它群事件的 plugins_name。
        allowed = None
        if str(getattr(event, "unified_msg_origin", "")) == session_id:
            allowed = getattr(event, "plugins_name", None)
        if allowed is None:
            get_config = getattr(context, "get_config", None)
            if callable(get_config):
                try:
                    allowed = get_config(session_id).get("plugin_set", ["*"])
                except Exception:
                    pass
        if allowed is not None and "*" not in allowed and not names.intersection(allowed):
            continue
        return "灵动名片（dynamic_card_plus）"
    return ""


def _ids(value: Any) -> set[str]:
    if not isinstance(value, (list, tuple, set)):
        return set()
    return {str(item).strip() for item in value}


PROFILE_WEAVER_PLUGIN = "astrbot_plugin_profile_weaver"
"""心迹画像：它自己也会往系统提示词里注入画像，所以要知道它开没开。"""


def plugin_active(context: Any, names: str | tuple[str, ...]) -> bool:
    """这个插件装没装、启用没有（只读，不碰它的配置）。"""

    wanted = {names} if isinstance(names, str) else {str(item) for item in names}
    getter = getattr(context, "get_all_stars", None)
    if not callable(getter):
        return False
    try:
        stars = getter() or []
    except Exception:
        return False
    for star in stars:
        labels = {str(getattr(star, key, "") or "") for key in ("name", "root_dir_name")}
        if not labels.intersection(wanted):
            continue
        if bool(getattr(star, "activated", False)):
            return True
    return False


def profile_weaver_active(context: Any) -> bool:
    """心迹画像在不在跑（它在跑就会自己注入画像，我们别重复注入）。"""

    return plugin_active(context, PROFILE_WEAVER_PLUGIN)


def motion_media_refs(context: Any, event: Any, request: Any, *, sources: list[str] | None = None) -> tuple[bool, set[str]]:
    """Use observed Motion Vision provenance, never disable ordinary vision globally.

    No files are opened, copied, retained or deleted here. The media plugin owns
    frame/audio lifetimes and its review registry.
    """
    from urllib.parse import urlsplit
    parts = getattr(request, "extra_user_content_parts", []) or []
    texts = [str(part.get("text", "") if isinstance(part, dict) else getattr(part, "text", "")) for part in parts]
    touched = any("【动态媒体解析】" in text or "【整片视频模型报告｜" in text for text in texts)
    if not touched:
        return False, set()
    excluded = set()
    try:
        for star in context.get_all_stars() or []:
            labels = {str(getattr(star, key, "") or "") for key in ("name", "root_dir_name")}
            if "astrbot_plugin_motion_vision" not in labels or not getattr(star, "activated", False):
                continue
            instance = getattr(star, "star_cls", None)
            base = str(getattr(getattr(instance, "store", None), "base", "") or "").replace("\\", "/").rstrip("/")
            for ref in getattr(request, "image_urls", []) or []:
                normal = str(ref).replace("\\", "/")
                if base and normal.startswith(base + "/"):
                    excluded.add(str(ref))
            registry = getattr(instance, "registry", None)
            records = registry.records(event.unified_msg_origin) if registry else []
            since = float(getattr(event, "created_at", 0) or 0)
            for record in records:
                if since and float(getattr(record, "received_at", 0)) < since - 3:
                    continue
                for name in ("source_url", "path"):
                    ref = getattr(record, name, None)
                    if ref:
                        excluded.add(str(ref))
    except Exception:
        pass  # Incomplete provenance preserves ordinary image fallback.
    # Motion Vision removes decoded GIF originals from image_urls. GIFs still
    # present may have failed decoding or exceeded its processing count: retain
    # ordinary fallback for those, even when another clip was injected.
    remaining = set(getattr(request, "image_urls", []) or [])
    for ref in sources or []:
        if ref not in remaining and urlsplit(str(ref)).path.lower().endswith(".gif"):
            excluded.add(str(ref))
    return True, excluded
