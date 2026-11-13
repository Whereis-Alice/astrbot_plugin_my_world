"""Bounded views of current visit receipts and their confirmed occurrence time."""
from __future__ import annotations

import hashlib
import math
import re
from typing import Any


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def visit_review_rows(visit: dict, limit: int = 24) -> list[dict]:
    """Keep the encounter, explicit public results and ending for the actor's review."""
    rows = [row for row in visit.get("log", []) if isinstance(row, dict)]
    limit = max(1, int(limit))
    if len(rows) <= limit:
        return rows
    selected = set(range(min(2, len(rows)))) | set(range(max(0, len(rows) - min(10, limit)), len(rows)))
    milestones = [index for index, row in enumerate(rows) if row.get("kind") in {"milestone", "result", "outcome"}]
    for index in reversed(milestones):
        if len(selected) >= limit:
            break
        selected.add(index)
    if len(selected) > limit:
        selected = set(sorted(selected)[-limit:])
    for index in range(len(rows) - 1, -1, -1):
        if len(selected) >= limit:
            break
        selected.add(index)
    return [rows[index] for index in sorted(selected)]


def visit_memory_excerpt(content: str, meta: dict, limit: int = 450) -> str:
    """Keep an unchanged receipt's opening and ending within the recall budget.

    The fingerprint and row count guard the old rendered format: an editor's
    text or a quoted lookalike label is not reinterpreted as a new speaker.
    Only excerpts change; the full durable memory and receipt stay untouched.
    """
    content = str(content or "").strip()
    meta = meta if isinstance(meta, dict) else {}
    limit = max(0, int(limit))
    if not limit:
        return ""
    if len(content) <= limit:
        return content

    def prefix():
        value = _text(content, limit - 1)
        # Do not leave an opening source label with no closing bracket.
        if value.rfind("[") > value.rfind("]"):
            value = value[:value.rfind("[")].rstrip("；")
        return value + "…"

    proof = meta.get("visit_confirmation") if meta.get("kind") == "world_visit" else None
    if (not isinstance(proof, dict) or not isinstance(proof.get("log"), list)
            or proof.get("content") != hashlib.sha256(content.encode("utf-8")).hexdigest()):
        return prefix()
    ending = ("。这次已告别归家。" if meta.get("status") == "completed" else
              "。本次相处已结束：" + _text(meta.get("end_reason"), 500) + "；只记已确认的部分。")
    before, separator, reflection = content.rpartition(ending)
    header, marker, body = before.partition("已经确认的经过：")
    feeling_label = "我当时的感受（主观）："
    if not separator or not marker or (reflection and not reflection.startswith((feeling_label, "。本地经历与阶段回顾："))):
        return prefix()
    facts = re.split(r"；(?=\[)", body)
    if (len(facts) != len(proof["log"]) or not facts
            or any(not fact.startswith("[") or "] " not in fact for fact in facts)):
        return prefix()
    # Keep the first encounter, latest settled event (or penultimate turn),
    # and final words. The same row can satisfy several roles only once.
    resolution = next((index for index in range(len(facts) - 1, -1, -1)
                       if facts[index].startswith(("[outcome] ", "[result] ", "[milestone] ", "[共同确认片刻] "))), max(0, len(facts) - 2))
    selected = sorted({0, resolution, len(facts) - 1})
    note = "已经确认的经过（节选，中段省略）："

    def render(indices):
        parts = [header] + [facts[index] for index in indices] + [ending, reflection]
        floors = [min(len(header), 20)]
        floors += [min(len(fact), fact.index("] ") + 2 + 16) for fact in parts[1:-2]]
        floors += [min(len(ending), 30), min(len(reflection), len(feeling_label) + 16)]
        lengths = [max(floor, min(len(part), 140 if 0 < index < len(parts) - 2 else 90))
                   for index, (part, floor) in enumerate(zip(parts, floors))]
        budget = limit - len(note) - max(0, len(indices) - 1)
        if sum(floors) > budget:
            return None
        while sum(lengths) > budget:
            index = max(range(len(parts)), key=lambda index: lengths[index] - floors[index])
            lengths[index] -= min(sum(lengths) - budget, lengths[index] - floors[index])
        excerpts = [part if len(part) <= size else part[:size - 1] + "…"
                    for part, size in zip(parts, lengths)]
        return excerpts[0] + note + "；".join(excerpts[1:-2]) + "".join(excerpts[-2:])

    # Exceptionally long display names can consume most of three source labels.
    return render(selected) or render(sorted({0, len(facts) - 1})) or prefix()
def recall_excerpt(row: dict, meta: dict, limit: int = 450) -> str:
    """Compatibility adapter; only the currently stored text is summarised."""
    return visit_memory_excerpt(row.get("content", ""), meta, limit)


def _timestamp(value: Any) -> float | None:
    if type(value) not in {int, float}:
        return None
    try:
        stamp = float(value)
    except (ValueError, OverflowError):
        return None
    return stamp if math.isfinite(stamp) and 0 < stamp <= 253402300799 else None


def visit_occurrence_metadata(visit: dict) -> dict[str, float]:
    """New receipts save only actual arrival and ending, never write time."""
    start = _timestamp(visit.get("arrived_at"))
    if start is None:
        return {}
    result = {"occurred_start": start}
    end = _timestamp(visit.get("ended_at"))
    if end is not None and end >= start:
        result["occurred_end"] = end
    return result


def visit_occurrence_interval(meta: Any) -> tuple[float, float] | None:
    """Use confirmed event times; legacy routes prove only their own points.

    Missing endpoints do not turn a write date into the date of an encounter.
    The route fallback bounds confirmed starts, never an unrecorded duration.
    """
    if not isinstance(meta, dict) or meta.get("kind") != "world_visit" or meta.get("confirmed") is not True:
        return None
    start, end = _timestamp(meta.get("occurred_start")), _timestamp(meta.get("occurred_end"))
    if start is not None:
        if end is not None and end < start:
            return None
        return start, end if end is not None else start
    saved = meta.get("visit_location")
    if (not isinstance(saved, dict) or type(saved.get("version")) is not int or saved["version"] != 1
            or saved.get("role") not in ("host", "guest")):
        return None
    route = saved.get("route")
    if not isinstance(route, list):
        return None
    points = [_timestamp(stop.get("started_at")) for stop in route if isinstance(stop, dict)]
    points = [point for point in points if point is not None]
    if not points:
        return None
    first, last = min(points), max(points)
    return first, max(last, end) if end is not None and end >= first else last
