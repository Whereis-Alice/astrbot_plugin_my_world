"""Bounded chat excerpts that preserve both the beginning and the latest information."""
from __future__ import annotations


EXCERPT_OMISSION = "…（中间省略）…"


def chat_excerpt(text: str, maximum: int) -> str:
    """Keep short text verbatim and count the omission marker inside the budget."""
    text = str(text or "")
    maximum = max(0, int(maximum))
    if len(text) <= maximum:
        return text
    if maximum <= len(EXCERPT_OMISSION) + 2:
        return text[:maximum]
    remaining = maximum - len(EXCERPT_OMISSION)
    head = (remaining + 1) // 2
    tail = remaining - head
    return text[:head] + EXCERPT_OMISSION + text[-tail:]
