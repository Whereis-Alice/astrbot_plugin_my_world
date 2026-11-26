"""Topic and source boundaries shared by current and archived feelings."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .context_selection import ContextRequest, search_terms
from .evidence import direct_text, unquoted_segments


EMOTION_SUMMARY_LIMIT = 60
EMOTION_NARRATIVE_LIMIT = 600
_EMOTION_QUOTE_LIMIT = 240
_EMOTION_QUOTES = 3


@dataclass(frozen=True)
class EmotionJudgement:
    summary: str
    narrative: str
    evidence: tuple[str, ...]


def emotion_narrative(value: Any) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").strip() if isinstance(value, str) else ""


def parse_emotion_judgement(value: Any) -> EmotionJudgement | None:
    if not isinstance(value, dict):
        return None
    summary = value.get("summary")
    narrative = emotion_narrative(value.get("narrative"))
    quotes = value.get("evidence")
    if (not isinstance(summary, str) or not isinstance(quotes, list) or not 1 <= len(quotes) <= _EMOTION_QUOTES
            or not 1 <= len(narrative) <= EMOTION_NARRATIVE_LIMIT):
        return None
    summary = " ".join(summary.split())
    if not 1 <= len(summary) <= EMOTION_SUMMARY_LIMIT or any(
            not isinstance(quote, str) or not 2 <= len(quote.strip()) <= _EMOTION_QUOTE_LIMIT for quote in quotes):
        return None
    return EmotionJudgement(summary, narrative, tuple(dict.fromkeys(quote.strip() for quote in quotes)))


def verified_emotion_evidence(judgement: EmotionJudgement, sources: list[dict]) -> list[dict]:
    """Accept only exact excerpts from the direct messages in this turn."""
    verified = []
    for quote in judgement.evidence:
        excerpt = " ".join(quote.split())
        source = next((row for row in reversed(sources)
                       if any(excerpt in part for part in unquoted_segments(str(row.get("text") or "")))), None)
        if source is None:
            return []
        verified.append({"text": excerpt, "at": source.get("at") or 0, "user_name": str(source.get("name") or "")[:80]})
    return verified


_GENERIC = frozenset(search_terms("上次 那次 那件 这件 生气 难过 道歉 原谅 放下 担心 心里 心情 不开心 还在 在意"))
_ASKS_ABOUT_FEELINGS = re.compile(
    r"(?:你(?:怎么了|还生气|还在生气|生我气|生气了吗|有什么心事|心里有什么事|还在意)"
    r"|为什么(?:生气|不高兴|不开心)|谁惹你|还在生气|还生气吗|还没消气|还记我的仇"
    r"|你(?:愿意|能)原谅我|你原谅我了|(?:对不起|抱歉|道歉).{0,18}(?:上次|那次|那件事|我这就补上))"
)


def emotion_topic_match(query: str, text: str) -> bool:
    terms = set(search_terms(query)) - _GENERIC
    hits = terms.intersection(search_terms(text))
    subject = query.strip(" ？！。？，,.!?、～~").casefold()
    entry = text.strip(" ？！。？，,.!?、～~").casefold()
    return (len(hits) >= 2 or any(term.isascii() and len(term) >= 3 for term in hits)
            or subject in hits or entry in hits or (bool(hits) and len(entry) >= 4 and entry in subject))


def asks_about_feelings(query: str) -> bool:
    return bool(_ASKS_ABOUT_FEELINGS.search(query))


def current_emotion_query(request: ContextRequest) -> str:
    if request.task != "reply" or not request.focus_user:
        return ""
    return direct_text(request.current_text if request.focus_text is None else request.focus_text)


def archived_feeling_relevant(request: ContextRequest, people, text: str) -> bool:
    return (not people or request.focus_user in people) and emotion_topic_match(current_emotion_query(request), text)
