"""Topic and source boundaries shared by current and archived feelings."""
from __future__ import annotations

import re

from .context_selection import ContextRequest, search_terms
from .evidence import direct_text


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
