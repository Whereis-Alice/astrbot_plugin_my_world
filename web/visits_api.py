"""Editor-only controls for paired worlds; never exposed by the visit listener."""

from __future__ import annotations

import functools
from typing import Any

from astrbot.api.web import error_response, json_response, request


def _public(value: Any) -> Any:
    """Keep pairing credentials out of ordinary editor responses."""
    if isinstance(value, dict):
        return {key: _public(item) for key, item in value.items()
                if not any(word in str(key).lower() for word in
                           ("secret", "token", "password", "code", "credential", "private_key"))}
    if isinstance(value, (list, tuple)):
        return [_public(item) for item in value]
    return value


def _text(payload: dict[str, Any], key: str, *, limit: int = 500, required: bool = False) -> str:
    value = payload.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"{key} 必须是文字")
    value = value.strip()
    if len(value) > limit:
        raise ValueError(f"{key} 太长，最多 {limit} 字")
    if required and not value:
        raise ValueError(f"请填写 {key}")
    return value


def _scope(plugin: Any, payload: dict[str, Any]) -> str:
    raw = _text(payload, "session", limit=500)
    if not raw:
        raise ValueError("请先选择一个已启用的生活会话")
    session = plugin.engine.scope_session(raw)
    if not plugin.engine.is_enabled(session):
        raise ValueError("这个生活会话尚未启用，或已被停用")
    return plugin.engine.state_key(session)


def register_visit_apis(plugin: Any, plugin_id: str) -> None:
    """Register dashboard controls and mark their handlers for unload cleanup."""
    operations = {
        "visits": ("GET", "查看友邻与串门"),
        "visits/pair-code": ("POST", "生成一次性世界配对码"),
        "visits/pair": ("POST", "配对友邻世界"),
        "visits/remove": ("POST", "解除友邻世界配对"),
        "visits/invite": ("POST", "提出串门邀请"),
        "visits/return": ("POST", "结束串门并归家"),
    }

    async def handle(endpoint: str, method: str) -> Any:
        if plugin.retired:
            return error_response("插件已停止，请重新加载后再试", status_code=503)
        payload = (request.query if method == "GET" else await request.json(default={})) or {}
        if not hasattr(payload, "get"):
            return error_response("请求参数必须是对象")
        token = payload.get("token", "")
        if not isinstance(token, str) or not plugin.auth.check(token):
            return error_response("需要先登录编辑器", status_code=401)
        service = getattr(plugin, "visits", None)
        if service is None:
            return error_response("串门模块尚未准备好，请重新加载插件", status_code=503)
        try:
            scope = _scope(plugin, payload)
            if endpoint == "visits":
                result = await service.overview(scope)
            elif endpoint == "visits/pair-code":
                # This is the only route permitted to return a pairing credential.
                result = await service.create_pair_code(scope)
                return json_response(result if isinstance(result, dict) else {"code": result})
            elif endpoint == "visits/pair":
                result = await service.pair(scope, _text(payload, "url", required=True),
                                            _text(payload, "code", limit=256, required=True),
                                            _text(payload, "alias", limit=80))
            elif endpoint == "visits/remove":
                result = await service.remove_peer(scope, _text(payload, "peer_id", limit=200, required=True))
            elif endpoint == "visits/invite":
                direction = _text(payload, "direction", limit=20) or "outgoing"
                if direction not in {"outgoing", "incoming"}:
                    raise ValueError("邀请方向必须是去做客或邀请来访")
                result = await service.invite(scope, _text(payload, "peer_id", limit=200, required=True),
                                              reason=_text(payload, "reason", limit=300),
                                              direction=direction, automatic=False)
            else:
                result = await service.return_home(scope)
            return json_response(_public(result) if result is not None else {"ok": True})
        except ValueError as exc:
            message = str(exc)
            code = payload.get("code")
            if isinstance(code, str) and code:
                message = message.replace(code, "[配对码]")
            return error_response(message)
        except (ConnectionError, TimeoutError, OSError):
            return error_response("暂时联系不上对方，请检查地址和串门服务后再试")

    for endpoint, (method, description) in operations.items():
        async def dispatch(*args: Any, _endpoint: str = endpoint, _method: str = method, **kwargs: Any) -> Any:
            return await handle(_endpoint, _method)
        # A distinct name improves route diagnostics while closure defaults bind
        # each handler to its own endpoint rather than the loop's final value.
        functools.update_wrapper(dispatch, handle)
        dispatch.__name__ = "visit_editor_" + endpoint.replace("/", "_").replace("-", "_")
        plugin._web_api_handlers = getattr(plugin, "_web_api_handlers", set())
        plugin._web_api_handlers.add(dispatch)
        plugin.context.register_web_api(f"/{plugin_id}/{endpoint}", dispatch, [method], description)
