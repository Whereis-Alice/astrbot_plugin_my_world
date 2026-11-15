"""Bounded identities of actual room speech, independent of chat context retention."""
from __future__ import annotations


def note_speech_turn(turns: list[dict], token: str, now: float) -> list[dict]:
    """Accepted segments and images share one turn; drafts never call this helper."""
    latest = max([now] + [float(turn.get("at") or 0) for turn in turns])
    kept = [dict(turn) for turn in turns if 0 <= latest - float(turn.get("at") or 0) <= 3600]
    if not token:
        return kept
    turn = next((turn for turn in kept if turn.get("token") == token), None)
    if turn is None:
        seq = max([int(turn.get("seq") or 0) for turn in turns] or [0]) + 1
        kept.append({"token": token, "at": now, "seq": seq})
    else:
        turn["at"] = max(float(turn["at"]), now)
    return sorted((turn for turn in kept if latest - float(turn["at"]) <= 3600),
                  key=lambda turn: float(turn["at"]))[-120:]
