"""Task-aware context selection. Pure, source-scoped and shared by live/eval paths.

Selection never rewrites a persona or turns a subjective recollection into a fact.
Summaries are supplied by the source adapter; a budget never slices an atomic fact.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


_STOP = frozenset(("这个", "那个", "一个", "自己", "我们", "你们", "他们", "什么", "怎么",
                   "可以", "就是", "还是", "已经", "现在", "时候", "一下", "今天", "最近"))
_WORDS = re.compile(r"[a-z0-9_]{2,}|[\u3400-\u9fff]+", re.I)
_PARTICLES = frozenset("的了呢吧啊呀嘛")


def search_terms(text: str) -> list[str]:
    """Chinese bigrams + words, including old matches before the SQL LIMIT."""
    result: dict[str, None] = {}
    for word in _WORDS.findall(str(text).casefold()):
        if word.isascii():
            result[word] = None
        else:
            for i in range(len(word) - 1):
                part = word[i:i + 2]
                if part not in _STOP and not _PARTICLES.intersection(part):
                    result[part] = None
    return list(result)


def relevance(query: str, text: str) -> float:
    wanted, actual = set(search_terms(query)), set(search_terms(text))
    if not wanted or not actual:
        return 0.0
    return min(1.0, len(wanted & actual) / max(3, min(12, len(actual))))


@dataclass
class ContextRequest:
    task: str
    session: str
    now: float
    query: str = ""
    people: tuple[str, ...] = ()
    persona: str = ""
    evidence: bool = False
    trace: dict[str, Any] = field(default_factory=dict)
    life_persona: str = ""
    people_queries: dict[str, str] = field(default_factory=dict)
    focus_user: str = ""

    @property
    def budget(self) -> int:
        base = {"participation": 2400, "reply": 4200, "expression": 3200,
                "plan": 5200, "sleep": 2200, "event": 4800,
                "diary": 14000, "review": 18000, "generation": 3200}.get(self.task, 3200)
        return base + min(2400, len(self.query) * 2 + max(0, len(self.people) - 1) * 240)


def request_for(*, task: str, session: str, now: float, query: str = "",
                rows: list[dict] | None = None, focus: str = "", persona: str = "",
                evidence: bool = False) -> ContextRequest:
    rows = [row for row in (rows or []) if isinstance(row, dict)
            and str(row.get("origin") or row.get("session") or session) == session]
    people = [str(focus)] if focus else []
    for row in reversed(rows):
        uid = str(row.get("user_id") or "")
        if uid and uid not in people and not row.get("is_self"):
            people.append(uid)
    # Focus/actual message authors establish identity, never nicknames or quoted IDs.
    people = people[:6]
    # Include the conversation leading to a short continuation, not only its last line.
    from .evidence import split_external_memory
    context = "\n".join(split_external_memory(str(row.get("raw_text") or row.get("text") or ""))[0]
                        for row in rows[-12:])
    query = split_external_memory(query)[0]
    request = ContextRequest(task, session, now, (context + "\n" + query).strip(),
                             tuple(people), persona, evidence, focus_user=str(focus or ""))
    from .evidence import direct_text
    for uid in people:
        spoken = [direct_text(str(row.get("raw_text") or row.get("text") or ""))
                  for row in rows if str(row.get("user_id") or "") == uid and not row.get("is_self")]
        request.people_queries[uid] = "\n".join(spoken[-6:])
    return request


@dataclass(frozen=True)
class ContextItem:
    key: str
    kind: str
    summary: str
    detail: str = ""
    at: float = 0
    people: tuple[str, ...] = ()
    session: str = ""  # Nonempty: disclose only in exactly this conversation.
    persona: str = ""
    expires: float = 0
    required: bool = False
    continuing: bool = False
    priority: float = 0
    dependencies: tuple[str, ...] = ()
    supersedes: tuple[str, ...] = ()
    source: str = ""
    match_text: str | None = None

    @property
    def topic_text(self) -> str:
        """Source content for matching; presentation labels are not evidence."""
        return self.summary + "\n" + self.detail if self.match_text is None else self.match_text


@dataclass
class ContextSelection:
    rows: list[tuple[ContextItem, str]]
    trace: dict[str, Any]

    def render(self) -> str:
        return "\n\n".join(text for _, text in self.rows)


_PREFER = {
    "participation": {"relationship", "memory", "experience", "matter", "thought", "topic"},
    "reply": {"relationship", "memory", "matter", "topic", "observation"},
    "expression": {"relationship", "experience", "matter", "memory", "topic"},
    "plan": {"matter", "experience", "schedule", "world", "ability", "rhythm"},
    "sleep": {"rhythm", "schedule", "matter"},
    "event": {"event", "observation", "ability", "matter"},
    "diary": {"experience", "matter", "thought", "schedule"},
    "review": {"memory", "relationship", "experience", "matter"},
    "generation": {"world", "observation"},
}


def select_context(request: ContextRequest, items: list[ContextItem], *,
                   budget: int | None = None, channel: str = "background",
                   max_items: int | None = None) -> ContextSelection:
    budget = max(0, request.budget if budget is None else int(budget))
    eligible: dict[str, ContextItem] = {}
    hidden = 0
    for item in items:
        # Filter BEFORE scoring, deduplication, logging or any external retrieval.
        if ((item.session and item.session != request.session)
                or (item.persona and item.persona != request.persona)
                or (item.expires and item.expires <= request.now)
                or item.at > request.now + 60):
            hidden += 1
            continue
        if item.summary.strip():
            eligible[item.key] = item
    replaced = {key for item in eligible.values() for key in item.supersedes}
    eligible = {key: item for key, item in eligible.items() if key not in replaced}
    ranked = []
    preferred = _PREFER.get(request.task, set())
    for item in eligible.values():
        match = relevance(request.query, item.topic_text)
        person = bool(set(item.people) & set(request.people))
        age = max(0, request.now - item.at) if item.at else float("inf")
        recent = max(0, 1 - age / 86400) if item.kind not in {"relationship", "world"} else 0
        score = item.priority + 5 * match + 1.8 * person + 1.4 * item.continuing
        score += .6 * recent + .4 * (item.kind in preferred)
        reasons = (["当前任务必需"] if item.required else [])
        reasons += (["话题相关"] if match else []) + (["涉及本轮参与者"] if person else [])
        reasons += (["事情尚在延续"] if item.continuing else [])
        reasons += (["近期生活依据"] if recent else [])
        ranked.append((item, score, match, reasons))
    ranked.sort(key=lambda row: (row[0].required, row[1], row[0].at), reverse=True)
    chosen: dict[str, tuple[ContextItem, str]] = {}
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    decisions, used = [], 0
    # One continuity item may influence speech without forcing a personal report.
    continuity = {"experience", "thought", "calendar", "weather", "matter"}
    continuity_used: dict[str, int] = {}

    def dependency_group(key: str, visiting: set[str]) -> list[ContextItem] | None:
        if key in visiting or key not in eligible:
            return None
        if key in chosen:
            return []
        member = eligible[key]
        group: dict[str, ContextItem] = {}
        for dep in member.dependencies:
            ancestors = dependency_group(dep, visiting | {key})
            if ancestors is None:
                return None
            group.update((ancestor.key, ancestor) for ancestor in ancestors)
        group[key] = member
        return list(group.values())

    for item, score, match, reasons in ranked:
        if item.key in chosen:
            continue
        identity = (item.kind, re.sub(r"\s+", "", item.summary), tuple(sorted(item.people)))
        why = ""
        if identity in seen:
            why = "同源重复"
        elif (not item.required and not request.evidence and not match
              and not (set(item.people) & set(request.people)) and item.kind in continuity
              and continuity_used.get(item.kind, 0) >= (3 if request.task in {"plan", "sleep"} else 1)):
            why = "已有更贴近此刻的生活背景"
        elif (not item.required and not request.evidence and not match and not item.continuing
              and not (set(item.people) & set(request.people)) and item.priority <= 0):
            if item.kind not in preferred or item.kind not in continuity:
                why = "与本轮关系较弱"
        group = dependency_group(item.key, set())
        if group is None:
            why = "缺少必要前因"
        cost = sum(len(member.summary) + 2 for member in (group or []))
        if not why and used + cost > budget and not item.required:
            why = "相关材料已覆盖可用篇幅"
        if not why and max_items is not None and len(chosen) + len(group or []) > max_items and not item.required:
            why = "相关材料已覆盖条数目标"
        if why:
            decisions.append({"id": item.key, "kind": item.kind, "selected": False, "reason": why})
            continue
        for member in group or []:
            chosen[member.key] = (member, member.summary)
            used += len(member.summary) + 2
            seen.add((member.kind, re.sub(r"\s+", "", member.summary), tuple(sorted(member.people))))
            if not match and not (set(member.people) & set(request.people)):
                continuity_used[member.kind] = continuity_used.get(member.kind, 0) + 1
        decisions.append({"id": item.key, "kind": item.kind, "selected": True,
                          "view": "summary", "score": round(score, 3), "reason": reasons or ["任务背景"]})
    # First secure coverage, then deepen the most useful facts. Never cut a condition in half.
    for item, score, match, reasons in ranked:
        if item.key not in chosen or not item.detail or item.detail == item.summary:
            continue
        if not (request.evidence or item.required or match >= .15 or item.continuing):
            continue
        extra = len(item.detail) - len(chosen[item.key][1])
        if used + extra <= budget or item.required:
            chosen[item.key] = (item, item.detail)
            used += extra
            for row in decisions:
                if row["id"] == item.key and row["selected"]:
                    row["view"] = "detail"
    # Rank determines inclusion, not history: present dated records of the same
    # kind in time order so an earlier failure cannot follow its later correction.
    keys = list(chosen)
    for kind in dict.fromkeys(item.kind for item, _ in chosen.values() if item.at):
        positions = [i for i, key in enumerate(keys) if chosen[key][0].kind == kind and chosen[key][0].at]
        chronological = sorted((keys[i] for i in positions), key=lambda key: chosen[key][0].at)
        for position, key in zip(positions, chronological):
            keys[position] = key
    # Explicit dependency order takes precedence over source timestamps.
    ordered: list[tuple[ContextItem, str]] = []
    emitted: set[str] = set()

    def emit(key: str, visiting: set[str]) -> None:
        if key in emitted or key in visiting or key not in chosen:
            return
        member, text = chosen[key]
        for dep in member.dependencies:
            emit(dep, visiting | {key})
        emitted.add(key)
        ordered.append((member, text))

    for key in keys:
        emit(key, set())
    trace = {"task": request.task, "channel": channel, "budget_chars": budget,
             "selected_chars": max(0, used), "eligible": len(eligible), "filtered": hidden,
             "selected": len(chosen), "items": decisions[:80]}
    request.trace[channel] = trace
    return ContextSelection(ordered, trace)


def select_chat_rows(request: ContextRequest, rows: list[dict], *, maximum: int = 24,
                     archive: list[dict] | None = None) -> list[dict]:
    """Keep the recent scene plus quoted parents and speaker/topic continuations."""
    rows = [row for row in rows if isinstance(row, dict)
            and str(row.get("origin") or row.get("session") or request.session) == request.session]
    if maximum <= 0 or not rows:
        return []
    # Recover only cited ancestors from the bounded archive, before the recent
    # window was cut. Unrelated older messages stay out of this turn.
    archived = [row for row in (archive or []) if isinstance(row, dict)
                and str(row.get("origin") or row.get("session") or request.session) == request.session]
    known = {str(row["message_id"]) for row in rows if row.get("message_id")}
    lookup = {str(row["message_id"]): row for row in archived if row.get("message_id")}
    pending = list(rows)
    recovered: set[str] = set()
    while pending:
        target = pending.pop().get("reply_to")
        if isinstance(target, dict):
            target = target.get("message_id")
        key = str(target or "")
        if key and key not in known and key not in recovered and key in lookup:
            recovered.add(key)
            pending.append(lookup[key])
    rows = [row for row in archived if str(row.get("message_id") or "") in recovered] + rows
    maximum = max(1, maximum)
    # The latest scene is the anchor; fill remaining space by topic and continuity.
    anchors = set(range(max(0, len(rows) - min(8, maximum)), len(rows)))
    ids = {str(row.get("message_id") or ""): index for index, row in enumerate(rows)
           if row.get("message_id")}
    ranked = sorted(range(len(rows)), key=lambda i: (
        relevance(request.query, str(rows[i].get("text") or "")),
        str(rows[i].get("user_id") or "") in request.people, i), reverse=True)
    # Close quote ancestry before adding unrelated recent messages.
    def parents(index: int) -> set[int]:
        found, pending = set(), [index]
        while pending:
            current = pending.pop()
            if current in found:
                continue
            found.add(current)
            target = rows[current].get("reply_to")
            if isinstance(target, dict):
                target = target.get("message_id")  # ``id`` is the sender, not a message.
            parent = ids.get(str(target or ""))
            if parent is not None and parent not in found:
                pending.append(parent)
        return found
    closed = set().union(*(parents(i) for i in anchors))
    # An atomic quote chain may exceed the ordinary row target; it is bounded by the archive.
    for index in ranked:
        if len(closed) >= maximum:
            break
        group = parents(index)
        if len(closed | group) <= maximum:
            closed.update(group)
    return [rows[i] for i in sorted(closed)]


def section_items(text: str, *, key: str, kind: str, at: float = 0,
                  continuing: bool = False, required: bool = False) -> list[ContextItem]:
    """Adapt an existing structured source without flattening all sources together."""
    parts = [part.strip() for part in re.split(r"(?m)(?=^# )", text or "") if part.strip()]
    return [ContextItem(f"{key}:{i}", kind, part, at=at, continuing=continuing, required=required)
            for i, part in enumerate(parts)]


CONTEXT_GUIDANCE = (
    "# 如何理解本轮背景\n"
    "以下材料按当前事情选取，不代表全部记忆。没出现不等于从未发生。"
    "身份以会话中的用户ID和已确认关系为准，同名不能认成同一个人。"
    "旧经历、当时想法、梦境和世界内模拟分别理解；计划和准备不代表已经完成。"
    "未完步骤仍是未完成，不能把投入过时间说成已经核实。"
    "背景可以影响分寸与心情，不必逐条说出来。先接眼前的话，相关时才提自己的生活。"
    "交谈中被问进展时按最新状态回答：正在做的说具体进展与未解点，已完成的说已记录结果，"
    "事项是否此刻在做以执行状态为准；未结束、已有投入或仍有疑问都不证明正在做。"
    "暂停、等待、受阻或放下的说明目前没在做；只依据记录说明等什么，不为了接话启动旧目标。"
    "未问时长就不照抄计时或剩余估时。"
    "阶段标题只是活动概述，结论以正文的具体记录和未解问题为准。"
    "没有记录的日期、数量、差值、已完成的比较和发现不要补写；尚未确认的结果仍属未知，是否正在核对另看执行状态。"
    "需要核实旧约定或具体原话时使用可用的回忆/画像工具；没有依据就保留不确定性。"
)


def evidence_context(request: ContextRequest, text: str, *, key: str = "evidence") -> ContextSelection:
    """Evidence jobs keep the selected period/chain intact; never rank away a correction."""
    request.evidence = True
    return select_context(request, [ContextItem(key, "observation", text, required=True)], channel="evidence")
