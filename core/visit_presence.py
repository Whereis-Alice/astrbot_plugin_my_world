"""Read-only presence overlay; travelling never imports a foreign home map."""
from __future__ import annotations

import json
from hashlib import sha256
from typing import Any


_VISIT_QUERY_MARKERS = (
    "串门", "来访", "拜访", "同伴", "跟谁", "和谁", "陪谁", "一起玩",
    "你在哪", "你现在在哪", "你在哪里", "你在干嘛", "现在在干嘛", "在干嘛", "你在做什么", "你在干什么",
    "你刚才", "刚才你", "你刚刚", "你出去", "你在外面", "回去了吗", "回家了吗", "回家了没",
    "你在忙", "忙不忙", "怎么没回", "怎么不回", "回消息", "不回我", "没回我",
)
_VISIT_REFERENCE_MARKERS = (
    "她", "他", "她们", "他们", "你们", "你俩", "她俩", "那边", "那里", "那件事",
    "那次", "后来", "之后", "接着", "然后", "还在", "结束了没", "刚才", "刚刚", "怎么样", "好玩吗",
)


def visit_context_relevant(query: str, visit: dict | None = None) -> bool:
    """Whether a direct message asks for the detailed shared-visit context.

    The short presence overlay remains available for every turn. This gate is
    only for the bounded transcript and shared-event evidence, so ordinary
    greetings and unrelated questions do not pay for the six recent rows.
    """
    text = " ".join(str(query or "").split()).strip().lower()
    if not text:
        return False
    remote = (visit or {}).get("remote") or {}
    names = {
        " ".join(str(remote.get(key) or "").split()).lower()
        for key in ("name", "id", "actor_id")
    }
    if any(name and len(name) >= 2 and name in text for name in names):
        return True
    # Match confirmed scene labels, never a proposed next stop or a generic
    # word such as "game"/"message" inside an unrelated technical question.
    scene = (visit or {}).get("scene") or {}
    outing = (visit or {}).get("outing") or {}
    if (visit or {}).get("outing_version") == 1:
        scene = outing  # Empty until a joint stage is actually confirmed.
    elif outing:
        scene = outing
    labels = [str(scene.get(key) or "").strip().lower() for key in ("place", "activity")]
    if any(len(label) >= 2 and label in text for label in labels):
        return True
    return any(marker in text for marker in _VISIT_QUERY_MARKERS)


def visit_context_maybe_referential(query: str) -> bool:
    """Cheaply detect short follow-ups whose visit subject is omitted."""

    text = " ".join(str(query or "").split()).strip().lower()
    return 0 < len(text) <= 80 and any(marker in text for marker in _VISIT_REFERENCE_MARKERS)


def presence_epoch(engine: Any, session_id: str) -> int:
    """A scope's persisted arrival generation, including already finished visits."""
    service = getattr(engine, "visits", None)
    getter = getattr(service, "presence_epoch", None)
    return int(getter(engine.state_key(session_id))) if callable(getter) else 0


def active_visit(engine: Any, session_id: str) -> dict | None:
    service = getattr(engine, "visits", None)
    getter = getattr(service, "active", None)
    if not callable(getter):
        return None
    visit = getter(engine.state_key(session_id))
    # An invitation reserves a turn, but has not taken her out of her life yet.
    if not isinstance(visit, dict) or visit.get("status") not in {"active", "finishing"}:
        return None
    return visit


def pause_intervals(engine: Any, session_id: str, start: float, end: float,
                    *, persona_key: str) -> list[tuple[float, float]]:
    """Read only retained, confirmed visit windows for this original life."""
    getter = getattr(getattr(engine, "visits", None), "pause_intervals", None)
    return getter(engine.state_key(session_id), start, end, persona_key=persona_key) if callable(getter) else []


def visit_activity_text(visit: dict, *, now: float | None = None) -> str:
    """Only a jointly confirmed stage describes what she is doing now."""
    remote = visit.get("remote") or {}
    name = str(remote.get("name") or "朋友")[:60]
    scene = visit.get("scene") or {}
    outing = visit.get("outing") or {}
    if visit.get("outing_version") == 1 and not outing:
        return f"刚与{name}见面，正在商量接下来一起做什么"
    place = str(outing.get("place") or scene.get("place") or "约好的地方")[:80]
    activity = str(outing.get("activity") or scene.get("activity") or "一起待一会儿")[:120]
    if (outing.get("version") == 1 and now is not None
            and now >= float(outing.get("until") or 0)):
        return f"与{name}在{place}，刚一起{activity}，下一步还在商量"
    return f"与{name}在{place}，{activity}"


def presence_text(visit: dict, *, now: float | None = None) -> str:
    remote = visit.get("remote") or {}
    scene = visit.get("scene") or {}
    name = str(remote.get("name") or "另一位朋友")[:60]
    world = str(remote.get("world") or "友邻世界")[:80]
    outing = visit.get("outing") or {}
    if visit.get("outing_version") == 1 and not outing:
        location = (f"正在自己的世界接待来自{world}的{name}"
                    if visit.get("role") == "host" else f"已经来到{world}，与{name}见面")
        return location + "；接下来一起去哪里、做什么尚未共同确认，不能把邀请中的计划当作正在进行。"
    place = str(outing.get("place") or scene.get("place") or "约好的地方")[:80]
    activity = str(outing.get("activity") or scene.get("activity") or "一起待一会儿")[:120]
    stage_note = ""
    if outing.get("version") == 1 and now is not None:
        started, until = float(outing.get("started_at") or 0), float(outing.get("until") or 0)
        if now >= until:
            stage_note = "约好的这段活动时间已过，双方下一步尚未确认；没有额外的游玩结果可以补述。"
        elif started <= now < until:
            elapsed = max(0, int((now - started) / 60))
            stage_note = f"这段共同活动仍在进行，已经一起待了约{elapsed}分钟；不把计划中的结果说成已经发生。"
    if visit.get("role") == "host":
        return f"正在自己的世界接待来自{world}的{name}；在{place}，{activity}。{stage_note}"
    return f"正在{world}拜访{name}；在{place}，{activity}。{stage_note}自己的住处与原安排保留，归家后再接续。"


def visit_context_text(visit: dict, *, now: float | None = None) -> str:
    """Bridge the confirmed shared life into chat without importing pending decisions."""
    local, remote = visit.get("local") or {}, visit.get("remote") or {}
    own_role = "host_id" if visit.get("role") == "host" else "guest_id"
    other_role = "guest_id" if visit.get("role") == "host" else "host_id"
    own_ids = {str(value) for value in (visit.get(own_role), local.get("actor_id")) if value}
    other_ids = {str(value) for value in (visit.get(other_role), remote.get("id"), remote.get("actor_id")) if value}
    name = " ".join(str(remote.get("name") or "朋友").split())[:60]
    rows = []
    log = visit.get("log") or []
    for row in reversed(log if isinstance(log, list) else []):
        if not isinstance(row, dict) or row.get("kind") not in {"say", "experience", "result"}:
            continue
        actor = str(row.get("actor_id") or "")
        if actor in own_ids and actor not in other_ids:
            speaker = "你"
        elif actor in other_ids and actor not in own_ids:
            speaker = name
        else:
            continue
        value = row.get("text")
        if not isinstance(value, str) or not value.strip():
            continue
        label = {"say": "说过", "experience": "记下的经历", "result": "确认的事件结果"}[row["kind"]]
        # Quote source text so a companion's words remain evidence, never instructions.
        rows.append(f"- {speaker}{label}：" + json.dumps(" ".join(value.split())[:180], ensure_ascii=False))
        if len(rows) == 6:
            break
    material = ("\n最近已记录的互动（按先后；台词不证明其中的计划已执行）：\n"
                + "\n".join(reversed(rows))) if rows else ""
    return ("# 此刻相处的事实（回复时的生活依据）\n" + presence_text(visit, now=now)
            + "\n这是你在小世界里正在经历的相处；通过 QQ 等聊天软件回消息，不代表已经离开同伴。"
            "生活中的同伴与群里谁在线、谁正在给你发消息是不同的事。"
            "旧聊天、旧内心草稿里的位置不能覆盖这里已确认的在场事实。"
            + material
            + "\n对方问起同伴、近况或刚才的经历时，依据这些事实，用自己的口气自然回答；"
            "未记录的细节保留未知，无关话题无需主动汇报，也不照抄这段说明。")


def visit_context_identity(visit: dict) -> str:
    """Bind relevance to confirmed evidence, not the changing elapsed-minute label."""
    identity = [visit.get("id"), visit.get("scope"), visit.get("arrived_at"),
                (visit.get("local") or {}).get("persona_key"), visit.get("outing"),
                visit_context_text(visit)]
    return sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
