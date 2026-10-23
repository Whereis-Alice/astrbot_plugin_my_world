"""Bounded, detached diagnostics; never serialize media payloads or request objects."""

from __future__ import annotations

from itertools import islice
import re
from typing import Any


MAX_CHARS = 160_000
MAX_TEXT_CHARS = 64_000
MAX_ITEMS = 128
MAX_NODES = 4096
MAX_DEPTH = 12
_DATA_URL = re.compile(r"data:[^\s,]{1,160};base64,[A-Za-z0-9+/=_\s-]*", re.IGNORECASE)
_MEDIA_TYPES = {"image", "image_url", "input_image", "audio", "audio_url", "input_audio",
                "video", "video_url", "file", "input_file"}
_PAYLOAD_KEYS = {"data", "base64", "b64_json", "image_url", "audio_url", "video_url", "file_data"}
_OBJECT_FIELDS = ("type", "role", "text", "content", "name", "id", "tool_calls",
                  "tool_call_id", "function", "arguments", "_no_save")


class _Copy:
    def __init__(self) -> None:
        self.left = MAX_CHARS
        self.nodes = 0
        self.truncated = False
        self.media_parts = 0

    def copy(self, value: Any, depth: int = 0) -> Any:
        self.nodes += 1
        if self.nodes > MAX_NODES or depth > MAX_DEPTH or self.left <= 0:
            self.truncated = True
            return "（快照已截断）"
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            limit = min(self.left, MAX_TEXT_CHARS)
            clipped = len(value) > limit
            self.truncated |= clipped
            # Slice before substitution: a multi-megabyte data URL is never copied whole.
            text = _DATA_URL.sub("（媒体载荷省略）", value[:limit])
            self.left -= len(text)
            return text + ("（快照已截断）" if clipped else "")
        if isinstance(value, (bytes, bytearray, memoryview)):
            return {"media_bytes": len(value), "payload": "省略"}
        if isinstance(value, (list, tuple)):
            self.truncated |= len(value) > MAX_ITEMS
            result = []
            for item in value[:MAX_ITEMS]:
                if self.nodes >= MAX_NODES or self.left <= 0:
                    self.truncated = True
                    break
                result.append(self.copy(item, depth + 1))
            return result
        if not isinstance(value, dict):
            # Do not call str(), model_dump() or deepcopy() on arbitrary SDK objects.
            value = {key: getattr(value, key) for key in _OBJECT_FIELDS if hasattr(value, key)}
            if not value:
                return {"omitted": "不支持的对象"}
        kind = value.get("type")
        if isinstance(kind, str) and kind in _MEDIA_TYPES:
            self.media_parts += 1
            return {"type": kind, "payload": "省略"}
        self.truncated |= len(value) > MAX_ITEMS
        result = {}
        for key, item in islice(value.items(), MAX_ITEMS):
            if self.nodes >= MAX_NODES or self.left <= 0:
                self.truncated = True
                break
            if not isinstance(key, str):
                continue
            name = self.copy(key, depth + 1)
            result[name] = "（媒体载荷省略）" if key in _PAYLOAD_KEYS else self.copy(item, depth + 1)
        return result


def bounded_snapshot(data: dict[str, Any]) -> dict[str, Any]:
    copier = _Copy()
    result = copier.copy(data)
    result["snapshot_limits"] = {
        "truncated": copier.truncated, "max_chars": MAX_CHARS,
        "max_text_chars": MAX_TEXT_CHARS, "max_items": MAX_ITEMS,
        "media_parts_in_preview": copier.media_parts,
    }
    return result


def request_sizes(system: str, prompt: str, contexts: Any, extra: Any = ()) -> dict[str, int]:
    return {"system_chars": len(system or ""), "prompt_chars": len(prompt or ""),
            "contexts": len(contexts or []), "extra_parts": len(extra or [])}
