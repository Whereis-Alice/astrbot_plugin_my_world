"""Successful autonomous contacts share one small, daily ledger in the existing database."""
from __future__ import annotations
from typing import Any
from uuid import uuid4
from .models import TickOutcome

LIFE_KINDS = {"", "life_share", "schedule", "event"}
CHAT_KINDS = {"participation", "browse_reply", "continuation", "interject"}


class ProactiveContactMixin:
    @staticmethod
    def _outreach_people(outcome: TickOutcome, target: str) -> set[str]:
        """Native FriendMessage and legacy private origins identify the direct recipient."""
        people = set(outcome.contact_people)
        parts = target.split(":", 2)
        if len(parts) == 3 and parts[1] in {"FriendMessage", "PrivateMessage"} and parts[2]:
            people.add(parts[2])
        return people

    def _contact_started(self, ledger: dict, target: str, token: str) -> float:
        started = (ledger.get("started_at") or {}).get(token)
        if started is None:
            # Older ledgers did not date contacts. Reuse the actual-send record
            # when available; an undated old plan must not bypass quotas forever.
            last = (self._outreach_ledger(target).get("sessions") or {}).get(target) or {}
            if last.get("token") == token:
                started = last.get("first_at", last.get("at"))
        return float(started or 0)

    def _contact_token(self, outcome: TickOutcome, target: str, bucket="proactive_contacts") -> str:
        base = ("life:" if bucket == "life_shares" else "") + f"{outcome.contact_id}:{target}"
        token = outcome.contact_tokens.get(base, base)
        ledger = self.db.raw.kv_get(f"{bucket}:{self.state_key(target)}", {}) or {}
        if ledger.get("day") == self._today_key(self._now()) and token in (ledger.get("contacts") or {}):
            started = self._contact_started(ledger, target, token)
            if not started or self._now() - started > 120:
                token = outcome.contact_tokens[base] = base + ":" + uuid4().hex
        return token

    def contact_speech_allowed(self, outcome: TickOutcome, target: str, kind: str, *, reserved: bool = False) -> bool:
        """Reservations cover quotas only; reply priority and scene expiry stay live."""
        if kind != 'reply' and self.reply_busy(target):
            return False
        if kind in CHAT_KINDS:
            if self.peer_chat_pause(target, token=outcome.speech_receipt_batch):
                note = "刚与其他机器人连续聊过，先留白；有人带来新发言时再看"
                if note not in outcome.notes:
                    outcome.notes.append(note)
                return False
            return self._now() < outcome.chat_expires_at.get(target, 0)
        if kind == "reply":
            return True
        if reserved:
            return True
        if kind in LIFE_KINDS:
            return not self.life_share_status(target, token=self._contact_token(outcome, target, "life_shares"))["reason"]
        token = self._contact_token(outcome, target)
        people = self._outreach_people(outcome, target)
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
        started = self._contact_started(ledger, session_id, token) if token and token in contacts else 0
        already = bool(started and 0 <= stamp - started <= 120)
        if user_id and person_left is not None:
            person = [session_id, str(user_id)]
            person_left = max(0, person_left - sum(
                person in receipt["people"] and person not in contacts.get(receipt["token"], [])
                for receipt in active.values()))
            if already and person in contacts[token] and self.world.profile.proactive_person_daily_max > 0:
                person_left = max(1, person_left)
        reason = self.outreach_gate(session_id, token=token, user_id=user_id)
        if cap <= 0 or (total_left <= 0 and not already):
            reason = "今天的主动联系总额度已用完"
        elif person_left is not None and person_left <= 0:
            reason = "今天对这个人的主动联系额度已用完"
        return {"left": total_left, "cap": cap, "person_left": person_left, "reason": reason}

    def _reserve_contact(self, outcome: TickOutcome, target: str, kinds: list[str]) -> tuple[dict | None, str]:
        receipts = []
        if set(kinds) & LIFE_KINDS:
            token = self._contact_token(outcome, target, "life_shares")
            status = self.life_share_status(target, token=token)
            if status["reason"]:
                return None, status["reason"]
            key, day = "life_shares:" + self.state_key(target), self._today_key(self._now())
            identity = (key, day, token)
            if not hasattr(self, "_contact_pending"):
                self._contact_pending = {}
            if any(r["key"] == key for r in self._contact_pending.values()):
                return None, "正在发送另一条生活分享"
            receipt = {"key": key, "day": day, "token": token, "target": target,
                       "people": [], "identity": identity, "kind": "life_share"}
            self._contact_pending[identity] = receipt
            receipts.append(receipt)
        contact, error = self._reserve_outreach(outcome, target, kinds)
        if error:
            for receipt in receipts:
                self._finish_contact(receipt, False)
            return None, error
        if contact:
            receipts.append(contact)
        return ({"receipts": receipts} if receipts else None), ""

    def life_share_status(self, target, *, token=""):
        with self.config_scope(target):
            key, day = "life_shares:" + self.state_key(target), self._today_key(self._now())
            ledger = self.db.raw.kv_get(key, {}) or {}
            contacts = ledger.get("contacts", {}) if ledger.get("day") == day else {}
            active = {r["token"] for r in getattr(self, "_contact_pending", {}).values()
                      if r["key"] == key and r["day"] == day}
            started = self._contact_started(ledger, target, token) if token in contacts else 0
            same = bool(started and 0 <= self._now() - started <= 120)
            cap = self.world.life_share_daily_max
            left = max(0, cap - len(set(contacts) | active))
            reason = "正在回应别人，稍后再分享生活" if self.reply_busy(target) else ""
            recent = (self._outreach_ledger(target).get("sessions") or {}).get(target) or {}
            last = float(recent.get("at") or 0)
            if last and not same and self._now() - last < self.world.life_share_min_interval_seconds:
                reason = "刚说过话，稍后再分享生活"
            if cap <= 0 or (not left and not same):
                reason = "今天的生活分享额度已用完"
            return {"cap": cap, "left": left, "reason": reason}

    def _reserve_outreach(self, outcome: TickOutcome, target: str, kinds: list[str]) -> tuple[dict | None, str]:
        """No await between checking and reserving; never hold the state lock during send."""
        if not any(kind not in LIFE_KINDS | CHAT_KINDS | {"reply", "quiet"} for kind in kinds):
            return None, ""
        if "interject" in kinds and self._now() >= outcome.chat_expires_at.get(target, float("inf")):
            return None, "刚才的话题已不在现场，这轮不再补插话"
        if not self.proactive_chat_allowed(target):
            return None, "自主搭话已暂停"
        # A different module cannot start another contact while this life is sending.
        token = self._contact_token(outcome, target)
        if any(item["token"] != token
               for item in getattr(self, "_contact_pending", {}).values()
               if item["key"] == f"proactive_contacts:{self.state_key(target)}"):
            return None, "正在发送另一条主动联系"
        people = self._outreach_people(outcome, target)
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
                   "target": target,
                   "people": [[target, str(uid)] for uid in sorted(people)], "identity": identity}
        self._contact_pending[identity] = receipt
        return receipt, ""

    def _finish_contact(self, receipt: dict | None, success: bool | set[str]) -> None:
        if receipt is None:
            return
        if "receipts" in receipt:
            for item in receipt["receipts"]:
                self._finish_contact(item, success)
            return
        if isinstance(success, set):
            success = bool(success & LIFE_KINDS) if receipt.get("kind") == "life_share" else bool(
                success - LIFE_KINDS - CHAT_KINDS - {"reply", "quiet"})
        try:
            if not success:
                return
            day = self._today_key(self._now())
            ledger = self.db.raw.kv_get(receipt["key"], {}) or {}
            if ledger.get("day") != day:
                ledger = {"day": day, "contacts": {}}
            contacts = ledger.setdefault("contacts", {})
            ledger["last_at"] = self._now()
            ledger.setdefault("started_at", {}).setdefault(receipt["token"], self._now())
            known = contacts.setdefault(receipt["token"], [])
            for session_id, uid in receipt["people"]:
                if [session_id, uid] not in known:
                    self.profiles.note_proactive(session_id, uid, now=self._now())
                    known.append([session_id, uid])
            self.db.raw.kv_set(receipt["key"], ledger)
            target = receipt["target"]
            self.note_visible_speech(target, token=receipt["token"],
                                     people=[uid for _, uid in receipt["people"]])
        finally:
            self._contact_pending.pop(receipt["identity"], None)
