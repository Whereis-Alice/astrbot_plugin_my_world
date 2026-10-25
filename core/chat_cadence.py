"""Receipt-based peer conversation limits; no text or inferred bot identities."""
from __future__ import annotations


def note_input(ledger: dict, row: dict, peers: set[str]) -> dict:
    """Only a newly recorded participant message can reset the peer exchange."""
    uid = str(row.get("user_id") or "")
    seq = int(row.get("seq") or 0)
    if not uid or uid.startswith("__") or row.get("is_self") or seq <= ledger.get("input_seq", 0):
        return ledger
    ledger = {**ledger, "input_seq": seq, "peer_active": uid in peers}
    if uid not in peers:
        ledger["turns"] = []
    return ledger


def note_receipt(ledger: dict, token: str, now: float, window: float,
                 *, retention: float | None = None) -> dict:
    """A split send/repeated receipt consumes one turn, only after acceptance."""
    if not ledger.get("peer_active") or not token:
        return ledger
    turns = [dict(t) for t in ledger.get("turns", []) if 0 <= now - t["at"] < (retention or window)]
    turn = next((t for t in turns if t["token"] == token), None)
    if turn is None:
        turn = {"token": token, "first_at": now, "at": now}
        turns.append(turn)
    else:
        turn["at"] = now
    count = sum(0 <= now - t["at"] < window for t in turns)
    turn["count"] = max(turn.get("count", 0), count)
    return {**ledger, "turns": turns[-120:]}


def pause_until(ledger: dict, *, now: float, cap: int, pause: float,
                window: float, token: str = "") -> float:
    if cap <= 0 or pause <= 0 or not ledger.get("peer_active"):
        return 0.
    turns = [t for t in ledger.get("turns", []) if 0 <= now - t["at"] < max(window, pause)]
    if not turns:
        return 0.
    last = max(turns, key=lambda t: t["at"])
    if token and any(t["token"] == token and 0 <= now - t.get("first_at", t["at"]) <= 120 for t in turns):
        return 0.  # Finish the same accepted expression, including its images.
    if last.get("count", len(turns)) >= cap and now < last["at"] + pause:
        return last["at"] + pause
    return 0.
