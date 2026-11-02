"""Small authenticated JSON envelopes, independent of AstrBot admin access."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import secrets
from typing import Any

VERSION = 1
MAX_BYTES = 32768
MAX_OUTING_BYTES = 524288
ID = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
TERMINAL = frozenset({"completed", "declined", "expired", "interrupted"})
RESERVED = frozenset({"inviting", "offered", "active", "finishing"})


def new_id() -> str:
    return secrets.token_hex(16)


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def message_limit(body: Any) -> int:
    payload = body.get("payload", {}) if isinstance(body, dict) else {}
    snapshot = payload.get("visit", {}) if isinstance(payload, dict) else {}
    extended = (isinstance(payload, dict) and (type(payload.get("outing_version")) is int and payload["outing_version"] == 1
                                              or payload.get("manual") is True)
                or isinstance(snapshot, dict) and (type(snapshot.get("outing_version")) is int and snapshot["outing_version"] == 1
                                                    or snapshot.get("manual") is True))
    return MAX_OUTING_BYTES if extended else MAX_BYTES


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def code_id(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()[:24]


def derive_key(code: str, first: str, second: str) -> str:
    return hmac.new(code.encode(), ("mw-visits-v1|" + "|".join(sorted((first, second)))).encode(),
                    hashlib.sha256).hexdigest()


def envelope(key: str, sender: str, target: str, payload: dict[str, Any], now: float,
             *, message_id: str | None = None) -> dict[str, Any]:
    body = {"version": VERSION, "sender": sender, "target": target,
            "message_id": message_id or new_id(), "created_at": now, "payload": payload}
    body["signature"] = hmac.new(key.encode(), canonical(body), hashlib.sha256).hexdigest()
    return body


def verify(body: Any, key: str, now: float, *, fresh: bool = True) -> dict[str, Any]:
    if not isinstance(body, dict) or set(body) != {
            "version", "sender", "target", "message_id", "created_at", "payload", "signature"}:
        raise ValueError("无效协议消息")
    if body["version"] != VERSION or not isinstance(body["payload"], dict):
        raise ValueError("串门协议版本不兼容")
    for name in ("sender", "message_id"):
        if not isinstance(body[name], str) or not ID.fullmatch(body[name]):
            raise ValueError("无效消息身份")
    if not isinstance(body["target"], str) or (body["target"] and not ID.fullmatch(body["target"])):
        raise ValueError("无效目标身份")
    try:
        stamp = float(body["created_at"])
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("无效时间戳") from exc
    if not math.isfinite(stamp) or (fresh and abs(now - stamp) > 300):
        raise ValueError("消息已过期，请重试")
    signed = {key: value for key, value in body.items() if key != "signature"}
    signature = hmac.new(key.encode(), canonical(signed), hashlib.sha256).hexdigest()
    if not isinstance(body["signature"], str) or not hmac.compare_digest(signature, body["signature"]):
        raise ValueError("串门身份验证失败")
    if len(canonical(body)) > message_limit(body):
        raise ValueError("串门消息过长")
    return body["payload"]


def text(value: Any, limit: int = 400) -> str:
    return " ".join(str(value or "").split())[:limit]


def public_actor(actor: dict[str, Any], instance_id: str) -> dict[str, str]:
    actor_id = str(actor.get("actor_id") or "")
    if not ID.fullmatch(actor_id):
        raise ValueError("生活角色身份无效")
    return {"id": instance_id + ":" + actor_id, "actor_id": actor_id,
            "instance_id": instance_id, "name": text(actor.get("name"), 60) or "她",
            "world": text(actor.get("world"), 80) or "小世界"}


def validate_actor(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"id", "actor_id", "instance_id", "name", "world"}:
        raise ValueError("无效角色名片")
    if any(not isinstance(value[key], str) for key in value):
        raise ValueError("无效角色名片")
    if not ID.fullmatch(value["actor_id"]) or not ID.fullmatch(value["instance_id"]):
        raise ValueError("无效角色身份")
    if value["id"] != value["instance_id"] + ":" + value["actor_id"]:
        raise ValueError("角色身份不一致")
    if len(value["id"]) > 160 or len(value["name"]) > 60 or len(value["world"]) > 80:
        raise ValueError("角色名片过长")
    return dict(value)


def clean_scene(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        value = {}
    return {"place": text(value.get("place"), 80), "activity": text(value.get("activity"), 120),
            "event": text(value.get("event"), 240)}


def clean_outing_offer(value: Any) -> dict[str, Any]:
    """A concrete proposal, with no assertion that the other character accepted it."""
    if not isinstance(value, dict):
        return {}
    place, activity = text(value.get("place"), 80), text(value.get("activity"), 120)
    minutes = value.get("minutes", 45)
    if not place or not activity or type(minutes) not in {int, float} or not math.isfinite(minutes):
        return {}
    return {"place": place, "activity": activity, "minutes": max(5, min(240, int(minutes))),
            "event": text(value.get("event"), 240), "return_after": value.get("return_after") is True,
            "pause_minutes": clean_pause(value)}


def clean_pause(value: dict) -> float:
    pause = value.get("pause_minutes", value.get("next_contact_minutes", 5))
    return max(0, min(240, float(pause))) if type(pause) in {int, float} and math.isfinite(pause) else 5.


def clean_decision(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("模型未提供有效串门选择")
    return {"accept": value.get("accept") is True, "say": text(value.get("say"), 400),
            "action": text(value.get("action"), 200), "thought": text(value.get("thought"), 240),
            "leave": value.get("leave") is True, "scene": clean_scene(value.get("scene")),
            "reflection": text(value.get("reflection"), 500),
            "promise": text(value.get("promise"), 180), "outcome": text(value.get("outcome"), 500),
            "outing": clean_outing_offer(value.get("outing")), "outing_accept": value.get("outing_accept") is True,
            "return_after": value.get("return_after") is True, "farewell": text(value.get("farewell"), 100),
            "pause_minutes": clean_pause(value), "continue_activity": value.get("continue_activity") is True}
