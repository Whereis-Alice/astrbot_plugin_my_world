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


def validate_outing_request(value: Any, *, now: float | None = None) -> dict[str, Any]:
    """A bounded, ordered human request; the signed sender supplies its actor ID."""
    if (not isinstance(value, dict) or set(value) != {"id", "text", "at", "seq"}
            or not isinstance(value["id"], str) or not ID.fullmatch(value["id"])
            or not isinstance(value["text"], str) or not value["text"].strip() or len(value["text"]) > 300
            or type(value["seq"]) is not int or value["seq"] < 1
            or type(value["at"]) not in {int, float} or not math.isfinite(value["at"])
            or value["at"] < 0 or now is not None and value["at"] > now + 300):
        raise ValueError("真人游玩请求无效")
    return dict(value)


def validate_outing_request_maps(value: dict, actors: set[str], *, now: float | None = None) -> tuple[dict, dict, dict]:
    """Retain at most each participant's latest request and compact acknowledgements."""
    maps = tuple(value.get(name, {}) for name in
                 ("outing_requests", "outing_requests_handled", "outing_request_turn"))
    if any(not isinstance(items, dict) or not set(items) <= actors for items in maps):
        raise ValueError("真人游玩请求来源无效")
    requests, handled, turn = maps
    requests = {actor: validate_outing_request(request, now=now) for actor, request in requests.items()}
    if any(not isinstance(request_id, str) or not ID.fullmatch(request_id)
           for items in (handled, turn) for request_id in items.values()):
        raise ValueError("真人游玩请求确认无效")
    return requests, dict(handled), dict(turn)


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


def original_envelope(body: dict[str, Any]) -> dict[str, Any]:
    """A delivery renewal does not change the original message or its receipt."""
    return {name: value for name, value in body.items() if name != "delivery_retry"}


def renew_delivery(body: dict[str, Any], key: str, now: float) -> dict[str, Any]:
    """Prove that the same authenticated sender is still delivering this exact packet."""
    original = original_envelope(body)
    verify(original, key, now, fresh=False)
    proof = {"at": now, "message_digest": digest(original)}
    proof["signature"] = hmac.new(key.encode(), canonical(proof), hashlib.sha256).hexdigest()
    return {**original, "delivery_retry": proof}


def verify(body: Any, key: str, now: float, *, fresh: bool = True) -> dict[str, Any]:
    fields = {"version", "sender", "target", "message_id", "created_at", "payload", "signature"}
    if not isinstance(body, dict) or set(body) not in (fields, fields | {"delivery_retry"}):
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
    if not math.isfinite(stamp):
        raise ValueError("无效时间戳")
    original = original_envelope(body)
    retry = body.get("delivery_retry")
    if "delivery_retry" in body:
        if (not isinstance(retry, dict) or set(retry) != {"at", "message_digest", "signature"}
                or type(retry["at"]) not in {int, float} or not math.isfinite(retry["at"])
                or retry["at"] < stamp or retry["message_digest"] != digest(original)):
            raise ValueError("串门重试证明无效")
        retry_signature = hmac.new(key.encode(), canonical({name: value for name, value in retry.items()
                                                           if name != "signature"}), hashlib.sha256).hexdigest()
        if not isinstance(retry["signature"], str) or not hmac.compare_digest(retry_signature, retry["signature"]):
            raise ValueError("串门重试身份验证失败")
        stamp = retry["at"]
    if fresh and abs(now - stamp) > 300:
        raise ValueError("消息已过期，请重试")
    signed = {name: value for name, value in original.items() if name != "signature"}
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
