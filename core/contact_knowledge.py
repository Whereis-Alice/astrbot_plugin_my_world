"""Read models shared by the address book and the model's profile lookup tool.

Keep original records intact. Old field copies are hidden only when the structured
profile still contains that field; native experiences and their evidence survive.
"""

from __future__ import annotations

import re
from typing import Any


def contact_knowledge(
    detail: dict[str, Any], facts: list[dict[str, Any]]
) -> dict[str, Any]:
    fields = list(detail.get("fields") or [])
    names = {str(field.get("name") or "").strip() for field in fields}
    if detail.get("notes"):
        names.add("备注")
    notes = {str(note).strip() for note in detail.get("notes") or []}
    visible: list[dict[str, Any]] = []
    mirrored = 0
    for fact in facts:
        text = str(fact.get("text") or "").strip()
        label = re.split(r"[:：]", text, maxsplit=1)[0].strip()
        imported = str(fact.get("evidence") or "").startswith("导入自")
        if imported and (label in names or (fact.get("kind") == "note" and text in notes)):
            mirrored += 1
            continue
        visible.append(dict(fact))
    return {"fields": fields, "facts": visible, "mirrored_fact_count": mirrored}


def profile_summary(detail: dict[str, Any]) -> str:
    """Format the same scoped, merged fields that the editor displays."""

    lines = [
        f"- {field['name']}：{field.get('value', '')}" + ("（已锁定）" if field.get("locked") else "")
        for field in detail.get("fields") or []
        if field.get("name") and str(field.get("value") or "").strip()
    ]
    lines.extend(f"- 备注：{note}" for note in detail.get("notes") or [] if note)
    return "\n".join(lines)
