"""Validated transport and rhythm settings; no deployment secrets live here."""
from __future__ import annotations

from dataclasses import dataclass
import math
import ipaddress
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

DEFAULT_PORT = 38471


def _number(value: Any, default: float, low: float, high: float) -> float:
    try:
        parsed = float(value)
        if isinstance(value, bool) or not math.isfinite(parsed):
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        parsed = default
    return max(low, min(high, parsed))


def _boolean(value: Any, default: bool = False) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "on"}
    return default if value is None else bool(value)


def normalize_url(value: str) -> str:
    """Accept an HTTP(S) base URL or a direct host[:port], never credentials."""
    value = str(value or "").strip()
    if not value or len(value) > 512 or any(ch.isspace() for ch in value):
        raise ValueError("请填写直连地址或 HTTP(S) 域名")
    explicit = "://" in value
    parsed = urlsplit(value if explicit else "http://" + value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("地址只支持 http:// 或 https://")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("地址不能包含用户名、密码、查询参数或片段")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("端口无效") from exc
    host = parsed.hostname
    if ":" in host:
        host = "[" + host + "]"
    if port is not None:
        host += ":" + str(port)
    elif not explicit:
        try:
            ipaddress.ip_address(parsed.hostname)
            host += ":" + str(DEFAULT_PORT)
        except ValueError:
            parsed = parsed._replace(scheme="https")
    path = parsed.path.rstrip("/")
    if any(part in {".", ".."} for part in path.split("/")):
        raise ValueError("地址路径不能包含相对路径")
    return urlunsplit((parsed.scheme, host, path, "", ""))


@dataclass(frozen=True)
class VisitConfig:
    enabled: bool = False
    listen_host: str = "0.0.0.0"
    listen_port: int = DEFAULT_PORT
    public_url: str = ""
    auto_enabled: bool = True
    auto_probability: float = 1.0
    daily_peer_limit: int = 1
    auto_interval_minutes: int = 60
    weekly_limit: int = 1
    duration_minutes: int = 30
    outings_enabled: bool = False
    outing_duration_minutes: int = 180
    turns_per_actor: int = 3
    max_calls: int = 12
    daily_calls: int = 24
    daily_tokens: int = 0
    max_output_tokens: int = 240
    timeout_seconds: int = 45
    actor: str = "admin"
    reception_start: int = 8
    reception_end: int = 22
    poll_seconds: int = 60
    max_peers: int = 32
    history_keep: int = 128
    error: str = ""

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "VisitConfig":
        def number(key, default, low, high):
            return int(_number(values.get("visits_" + key), default, low, high))
        raw_url = str(values.get("visits_public_url") or "").strip()
        error = ""
        try:
            public_url = normalize_url(raw_url) if raw_url else ""
        except ValueError:
            public_url, error = "", "串门对外地址无效，请填写服务器地址或 HTTP(S) 域名后重载"
        return cls(
            enabled=_boolean(values.get("visits_enabled")),
            listen_host=str(values.get("visits_listen_host") or "0.0.0.0").strip(),
            listen_port=number("listen_port", DEFAULT_PORT, 1, 65535),
            public_url=public_url,
            error=error,
            auto_enabled=_boolean(values.get("visits_auto_enabled"), True),
            auto_probability=_number(values.get("visits_auto_probability"), 1, 0, 1),
            # AstrBot fills new schema defaults before constructing the plugin.
            # The old explicit zero must still keep autonomous visits disabled.
            daily_peer_limit=(0 if number("weekly_limit", 1, 0, 7) == 0
                              else number("daily_peer_limit", 1, 0, 5)),
            auto_interval_minutes=number("auto_interval_minutes", 60, 0, 1440),
            weekly_limit=number("weekly_limit", 1, 0, 7),
            duration_minutes=number("duration_minutes", 30, 5, 60),
            outings_enabled=_boolean(values.get("visits_outings_enabled"), True),
            outing_duration_minutes=number("outing_duration_minutes", 180, 30, 240),
            turns_per_actor=number("turns_per_actor", 3, 1, 5),
            max_calls=number("max_calls", 12, 2, 20),
            daily_calls=number("daily_calls", 24, 1, 240),
            daily_tokens=number("daily_tokens", 0, 0, 10000000),
            max_output_tokens=number("max_output_tokens", 240, 64, 512),
            timeout_seconds=number("timeout_seconds", 45, 5, 90),
            actor="all" if values.get("visits_actor") == "all" else "admin",
            reception_start=number("reception_start", 8, 0, 23),
            reception_end=number("reception_end", 22, 0, 23),
            poll_seconds=number("poll_seconds", 60, 15, 180),
        )
