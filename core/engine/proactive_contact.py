"""Successful autonomous contacts share one small, daily ledger in the existing database."""
from __future__ import annotations
from typing import Any
from .models import TickOutcome


class ProactiveContactMixin:
    def contact_speech_allowed(self, outcome: TickOutcome, target: str, kind: str) -> bool:
        if kind in {"reply", "schedule", "event"}:
            return True
        token = f"{outcome.contact_id}:{target}"
        people = set(outcome.contact_people)
        if ":PrivateMessage:" in target:
            people.add(target.rsplit(":", 1)[-1])
        return not self.contact_status(target, token=token)["reason"] and all(
            not self.contact_status(target, uid, token=token)["reason"] for uid in people)

    def contact_status(self, session_id: str, user_id: str = "", *, token: str = "", now: float | None = None) -> dict[str, Any]:
        """Return remaining shared/person allowance and the exact blocking reason."""
        with self.config_scope(session_id):
            return self._contact_status(session_id, user_id, token=token, now=now)

    def _contact_status(self, session_id, user_id="", *, token="", now=None):
        stamp = self._now() if now is None else float(now)
        key = f"proactive_contacts:{self.state_key(session_id)}"
        day = self._today_key(stamp)
        ledger = self.db.raw.kv_get(key, {}) or {}
        contacts = ledger.get("contacts", {}) if ledger.get("day") == day else {}
        pending = getattr(self, "_contact_pending", {})
        active = {receipt["token"]: receipt for receipt in pending.values()
                  if receipt["key"] == key and receipt["day"] == day}
        cap = max(0, int(self.world.profile.proactive_daily_max))
        used = len(set(contacts) | set(active))
        total_left = max(0, cap - used)
        person_left = self.profiles.proactive_quota_left(session_id, user_id, now=stamp) if user_id else None
        already = bool(token and token in contacts)
        if user_id and person_left is not None:
            person = [session_id, str(user_id)]
            person_left = max(0, person_left - sum(
                person in receipt["people"] and person not in contacts.get(receipt["token"], [])
                for receipt in active.values()))
            if already and person in contacts[token] and self.world.profile.proactive_person_daily_max > 0:
                person_left = max(1, person_left)
        reason = ""
        if cap <= 0 or (total_left <= 0 and not already):
            reason = "今天的主动联系总额度已用完"
        elif person_left is not None and person_left <= 0:
            reason = "今天对这个人的主动联系额度已用完"
        return {"left": total_left, "cap": cap, "person_left": person_left, "reason": reason}

    def _reserve_contact(self, outcome: TickOutcome, target: str, kinds: list[str]) -> tuple[dict | None, str]:
        """No await between checking and reserving; never hold the state lock during send."""
        if not any(kind not in {"reply", "schedule", "event", "quiet"} for kind in kinds):
            return None, ""
        if not self.proactive_chat_allowed(target):
            return None, "自主搭话已暂停"
        token = f"{outcome.contact_id}:{target}"
        people = set(outcome.contact_people)
        if ":PrivateMessage:" in target:
            people.add(target.rsplit(":", 1)[-1])
        status = self.contact_status(target, token=token)
        if status["reason"]:
            return None, status["reason"]
        for uid in people:
            status = self.contact_status(target, uid, token=token)
            if status["reason"]:
                return None, status["reason"]
        if not hasattr(self, "_contact_pending"):
            self._contact_pending = {}
        key = f"proactive_contacts:{self.state_key(target)}"
        identity = (key, self._today_key(self._now()), token)
        if identity in self._contact_pending:
            return None, "同一次主动联系正在发送"
        receipt = {"key": key, "day": identity[1], "token": token,
                   "people": [[target, str(uid)] for uid in sorted(people)], "identity": identity}
        self._contact_pending[identity] = receipt
        return receipt, ""

    def _finish_contact(self, receipt: dict | None, success: bool) -> None:
        if receipt is None:
            return
        try:
            if not success:
                return
            day = self._today_key(self._now())
            ledger = self.db.raw.kv_get(receipt["key"], {}) or {}
            if ledger.get("day") != day:
                ledger = {"day": day, "contacts": {}}
            contacts = ledger.setdefault("contacts", {})
            known = contacts.setdefault(receipt["token"], [])
            for session_id, uid in receipt["people"]:
                if [session_id, uid] not in known:
                    self.profiles.note_proactive(session_id, uid, now=self._now())
                    known.append([session_id, uid])
            self.db.raw.kv_set(receipt["key"], ledger)
        finally:
            self._contact_pending.pop(receipt["identity"], None)
