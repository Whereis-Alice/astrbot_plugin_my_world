"""Local daily opportunities for each neighbour, with no network polling or catch-up."""
from __future__ import annotations

from datetime import datetime, timezone


class VisitRhythm:
    @staticmethod
    def _has_daily_opportunity(record, peer_id, day, limit):
        entry = record.get("peer_days", {}).get(peer_id, {}).get("days", {}).get(day)
        return entry is None or (entry.get("picked") and entry.get("attempts", 0) < limit)

    def _visit_day(self, local, at=None):
        try:
            return datetime.fromisoformat(str(local["local_date"])).date().isoformat()
        except (KeyError, TypeError, ValueError):
            return datetime.fromtimestamp(self._now() if at is None else at, timezone.utc).date().isoformat()

    def _daily_record(self, scope):
        record = self._state["draws"].setdefault(scope, {})
        if record.get("rhythm_version") != 2:
            # Recover already-used opportunities from confirmed old journal entries.
            # Keep the same journal/instance identity and all existing pair credentials.
            peers = record.setdefault("peer_days", {})
            for visit in self._state["visits"].values():
                if visit["scope"] != scope or not visit.get("automatic"):
                    continue
                peer = peers.setdefault(visit["remote"]["id"], {"days": {}, "last_at": 0})
                day = self._visit_day(visit["local"], visit["created_at"])
                used = peer["days"].setdefault(day, {"picked": True, "attempts": 0})
                used["attempts"] += 1
                peer["last_at"] = max(peer["last_at"], visit["created_at"])
                record["next_auto_at"] = max(record.get("next_auto_at", 0),
                    visit["created_at"] + self.config.auto_interval_minutes * 60)
            record["rhythm_version"] = 2
        return record

    def _admit_automatic(self, scope, peer, local):
        """Consume one peer's opportunity under the caller's invitation lock."""
        if (not self.config.auto_enabled or not self.config.daily_peer_limit
                or not local["ready"] or local.get("chat_busy")
                or not local.get("reception_open", True)):
            return False
        record = self._daily_record(scope)
        if record.get("next_auto_at", 0) > self._now():
            return False
        day = self._visit_day(local)
        state = record.setdefault("peer_days", {}).setdefault(peer["id"], {"days": {}, "last_at": 0})
        entry = state["days"].get(day)
        if entry is None:
            entry = {"picked": self._random() < self.config.auto_probability, "attempts": 0}
            state["days"][day] = entry
        retained = sorted(key for key in state["days"] if key != day)[-6:] + [day]
        state["days"] = {key: state["days"][key] for key in retained}
        if not entry["picked"] or entry["attempts"] >= self.config.daily_peer_limit:
            return False
        entry["attempts"] += 1
        state["last_at"] = self._now()
        record["next_auto_at"] = self._now() + self.config.auto_interval_minutes * 60
        week = datetime.fromisoformat(day).strftime("%G-%V")
        if record.get("week") != week:
            record.update(week=week, count=0)
        record["day"] = day
        record["count"] = record.get("count", 0) + 1  # Legacy diagnostic counter only.
        return True

    async def _automatic(self):
        if not self.config.auto_enabled or not self.config.daily_peer_limit:
            return
        for scope in sorted({peer["scope"] for peer in self._state["peers"].values()}):
            if self.active(scope) or self._touched.get(scope, 0) > self._now():
                continue
            local = await self.actor_port.describe(scope)
            if not local["ready"] or local.get("chat_busy") or not local.get("reception_open", True):
                continue
            async with self._lock:
                record = self._daily_record(scope)
                if record.get("next_auto_at", 0) > self._now():
                    continue
                day = self._visit_day(local)
                peers = [peer for peer in self._state["peers"].values()
                         if peer["scope"] == scope and peer.get("enabled", True) and peer["url"]
                         and peer["local"]["persona_key"] == local["persona_key"]
                         and self._has_daily_opportunity(record, peer["id"], day, self.config.daily_peer_limit)]
                peers.sort(key=lambda peer: (record.get("peer_days", {}).get(peer["id"], {}).get("last_at", 0),
                                            peer["id"]))
            for peer in peers:
                try:
                    await self.invite(scope, peer["id"], automatic=True)
                except ValueError:
                    continue
                break  # One actual invitation per life per tick; other neighbours wait their turn.
