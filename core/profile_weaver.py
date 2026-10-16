"""心迹画像（``astrbot_plugin_profile_weaver``）只读桥。

那个插件把用户画像存在自己的插件数据目录里（``profiles.json``），
字段是「一人一份 + 证据 + 逐字段锁」的形态，并且它自己会往系统提示词里注入。

我们这边**不抢它的写入**：只把它已经记下来的东西搬一份过来，
变成自己的事实（``user_fact``）。搬完之后提示词、通讯录、睡眠整理都在本插件这边
继续跑；用户想停掉那边也不再丢数据。整个过程只读对方的文件，不修改、不删除。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PLUGIN_NAME = "astrbot_plugin_profile_weaver"
PROFILE_FILE = "profiles.json"
NOTES_FIELD = "备注"

#: 这些字段是「她怎么叫他」，单独处理成名字，不塞进事实里。
_NAME_FIELDS = ("昵称", "对用户的称呼", "称呼", "名字", "姓名", "别名", "网名", "用户名")


def profiles_path(data_root: str | Path) -> Path | None:
    """在插件数据根目录下找心迹画像的 ``profiles.json``；找不到返回 ``None``。"""

    if not data_root:
        return None
    root = Path(data_root)
    candidates = [root / PLUGIN_NAME / PROFILE_FILE]
    # 目录名被改过（或者装在别的地方）时，扫一眼同级目录里谁有这份文件
    if root.exists():
        candidates.extend(
            sorted(
                item / PROFILE_FILE
                for item in root.iterdir()
                if item.is_dir() and item.name != PLUGIN_NAME
            )
        )
    for path in candidates:
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def load_profiles(path: str | Path) -> dict[str, Any]:
    """读 profiles.json；坏文件当空，不要让编辑器跟着炸。"""

    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    profiles = payload.get("profiles")
    if isinstance(profiles, dict):
        return profiles
    return payload


def _text(value: Any, limit: int = 120) -> str:
    return " ".join(str(value or "").split()).strip()[:limit]


def _notes(value: Any) -> list[str]:
    if isinstance(value, list):
        return [item for item in (_text(entry, 160) for entry in value) if item]
    text = _text(value, 160)
    return [text] if text else []


def _user_id_from_key(key: str) -> str:
    """``session_12345`` → ``12345``；没有会话前缀时原样返回。"""

    text = _text(key, 200)
    if "_" not in text:
        return text
    return text.rsplit("_", 1)[-1] or text


def iter_import_items(profiles: dict[str, Any]) -> list[dict[str, Any]]:
    """把它的记录整理成「一个人 + 若干条要搬过来的事实」。

    只读，不改动传进来的数据；结构看不懂的记录直接跳过。
    """

    items: list[dict[str, Any]] = []
    for key, record in (profiles or {}).items():
        if not isinstance(record, dict):
            continue
        user_id = _text(record.get("subject_user_id"), 60) or _user_id_from_key(str(key))
        if not user_id:
            continue
        name = ""
        facts: list[tuple[str, str]] = []
        fields = record.get("fields")
        fields = fields if isinstance(fields, dict) else {}
        for field_name, raw_value in fields.items():
            label = _text(field_name, 40)
            if not label:
                continue
            if label in _NAME_FIELDS:
                candidate = _text(raw_value, 40)
                if candidate and not name:
                    name = candidate
                continue
            if label == NOTES_FIELD:
                for note in _notes(raw_value):
                    facts.append(("note", note))
                continue
            value = _text(raw_value, 160)
            if value:
                facts.append(("profile", f"{label}：{value}"))
        custom = record.get("custom_fields")
        custom = custom if isinstance(custom, dict) else {}
        for field_name in custom:
            label = _text(field_name, 40)
            if not label or label not in fields:
                continue
            value = _text(fields.get(label), 160)
            if value:
                facts.append(("profile", f"{label}：{value}"))
        locked = [
            _text(item, 40)
            for item in (record.get("locked_fields") or [])
            if _text(item, 40)
        ]
        source_session = str(key)[: -(len(user_id) + 1)] if str(key).endswith(f"_{user_id}") else ""
        items.append(
            {
                "user_id": user_id,
                "name": name,
                "facts": facts,
                "locked": locked,
                "source_session": source_session,
            }
        )
    return items


def summarize(items: list[dict[str, Any]]) -> dict[str, int]:
    """给编辑器看的一行统计。"""

    people = len(items)
    facts = sum(len(item.get("facts") or []) for item in items)
    return {"people": people, "facts": facts}
