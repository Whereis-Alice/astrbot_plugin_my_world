"""Cheap attention scheduling; an opportunity never requires the character to speak.

No model call, profile mutation, random draw or cross-session lookup lives here.
Lexical overlap only prioritizes a read; the character still interprets the scene.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import re

from .context_selection import relevance
from .state import chat_core_text

_ACKNOWLEDGEMENTS = {"嗯", "嗯嗯", "好", "好的", "哈哈", "哈哈哈", "收到", "+1", "ok"}


def attention_text(row: dict) -> str:
    """Quoted text/vision annotations are context, not the author's new contribution."""
    text = chat_core_text(str(row.get("reading_text", row.get("text", "")) or "")).strip()
    if re.fullmatch(r"(?:\s*[\[［「](?:图片|图像|动画表情|表情包|表情|image)(?:[:：][^\]］」]*)?[\]］」]\s*)+", text, re.I):
        return ""
    return text


def media_scene_allowed(*, rows: list[dict], new: list[dict], now: float,
                        mode: str = "contextual") -> bool:
    """A bare image alone is not a summons or evidence of topic relevance."""
    if not any(row.get("has_images") or row.get("images") for row in new):
        return True
    turns = visible_turns(rows, now)
    own_ids = {mid for t in turns for mid in t["message_ids"]}
    for row in new:
        media = row.get("has_images") or row.get("images")
        explicit = (row.get("addressing") == "me" or
                    str((row.get("reply_to") or {}).get("message_id") or "") in own_ids)
        text = attention_text(row)
        if (text and text.casefold() not in _ACKNOWLEDGEMENTS or explicit
                or media and (mode == "normal" or mode == "contextual" and turns
                              and row.get("addressing") != "others")):
            return True
    return False


def visible_turns(rows: list[dict], now: float, *, window: float = 120) -> list[dict]:
    """Collapse real receipt batches; adjacent legacy segments get a bounded fallback."""
    turns: list[dict] = []
    identities: dict[str, dict] = {}
    previous = None
    for row in rows:
        if not 0 <= now - float(row.get("at") or 0) <= window:
            continue
        if not row.get("is_self"):
            previous = None
            continue
        token = str(row.get("speech_turn") or "")
        event = str(row.get("event_id") or "")
        if not token and event.startswith("speech:"):
            token = event.split(":", 2)[1]
        turn = identities.get(token) if token else None
        if not token and previous and not previous["token"] and float(row["at"]) - previous["at"] <= 20:
            turn = previous
        if turn is None:
            turn = {"token": token, "at": float(row["at"]), "text": "", "message_ids": []}
            turns.append(turn)
            if token:
                identities[token] = turn
        turn["text"] += "\n" + str(row.get("text") or "")
        turn["at"] = max(turn["at"], float(row["at"]))
        if row.get("message_id"):
            turn["message_ids"].append(str(row["message_id"]))
        previous = turn
    return turns


@dataclass(frozen=True)
class Opportunity:
    ready: bool
    reason: str
    due_at: float
    relevance: float = 0
    relationship: float = 0
    own_turns: int = 0
    continuation: bool = False
    continuation_priority: bool = False

    def payload(self) -> dict:
        return asdict(self)


def assess_opportunity(*, now: float, rows: list[dict], new: list[dict],
                       observation: dict, attempts: list[float], persona: str,
                       affinities: dict[str, float], level: str = "natural",
                       hourly_max: int = 12, relation_weight: float = 1,
                       continuation_interval: float = 20, continuation_turns: int = 2,
                       continuation_window: float = 600, media_mode: str = "contextual") -> Opportunity:
    """Bound judgement calls while related scenes and close authors get earlier reads."""
    turns = visible_turns(rows, now, window=continuation_window)
    live_turns = visible_turns(rows, now)
    if not media_scene_allowed(rows=rows, new=new, now=now, mode=media_mode):
        return Opportunity(False, "只有图片或表情，尚无与她相接的当前对话；先看不接话", now + 30,
                           own_turns=len(turns))
    # Retrieval tokens describe overlap, not whether a message has meaning.
    # Short Chinese statements and emoji can be meaningful without any bigram.
    content = []
    for row in new:
        text = attention_text(row)
        if ((text and text.casefold() not in _ACKNOWLEDGEMENTS) or row.get("has_images")
                or row.get("images") or row.get("addressing") == "me"):
            content.append(row)
    if not content:
        return Opportunity(False, "这批只有简短应答或空白，等待新的具体内容", now + 30,
                           own_turns=len(turns))
    query = "\n".join(attention_text(row)[:600] for row in content[-8:])
    related = min(1., 3 * relevance(query, persona[:12000]))
    continuity = max((min(1., 3 * relevance(query, turn["text"])) for turn in live_turns), default=0.)
    own_ids = {mid for turn in live_turns for mid in turn["message_ids"]}
    explicit = any(row.get("addressing") == "me" or
                   str((row.get("reply_to") or {}).get("message_id") or "") in own_ids
                   for row in content)
    continuing = explicit or continuity >= .5
    related = max(related, continuity)
    # A person's own relevant contribution supplies their weight. A close
    # author somewhere else in this burst cannot boost another person's topic.
    relationship = max((max(0., min(1., affinities.get(str(row.get("user_id") or ""), 0.)))
                        * min(1., 3 * max(relevance(attention_text(row), persona[:12000]),
                            max((relevance(attention_text(row), turn["text"])
                                 for turn in live_turns), default=0.)))
                        for row in content), default=0.)
    recent = sorted(at for at in attempts if 0 <= now - at < 3600)
    if len(recent) >= hourly_max:
        return Opportunity(False, "本轮观察预算已用完，等待新现场", recent[-hourly_max] + 3600,
                           related, relationship, len(turns), continuing)
    # Degree controls attention cadence, never the probability of saying a line.
    base = {"quiet": 900., "natural": 450., "lively": 240.}[level]
    interval = base / (1 + 2 * related + .6 * relation_weight * relationship)
    priority = continuing and len(turns) < continuation_turns
    if priority:
        interval = min(interval, continuation_interval)
    else:
        interval *= 1 + .35 * min(3, len(turns))
    last = float(observation.get("evaluated_at") or 0)
    # Sparse old messages cannot become a timed obligation to catch up.
    due = last + interval if last else now
    if continuing and turns:
        due = max(due, max(t["at"] for t in turns) + continuation_interval)
    ready = now >= due
    reason = ("连续接话后恢复普通观察节奏" if continuing and not priority else
              "现场延续了她刚参与的话题" if continuing else
              "当前话题与她有关，优先读一读" if related else "留意新的群聊现场")
    if not ready:
        reason = "先留一点聊天空间，稍后只看仍在进行的现场"
    return Opportunity(ready, reason, due, related, relationship, len(turns), continuing, priority)
