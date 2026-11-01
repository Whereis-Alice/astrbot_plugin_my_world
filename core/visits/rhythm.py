"""One daily successful invitation per local life and neighbouring server."""
from __future__ import annotations

from datetime import datetime, timezone


class VisitRhythm:
    @staticmethod
    def _has_daily_opportunity(record, peer_id, day, limit):
        entry = record.get("peer_days", {}).get(peer_id, {}).get("days", {}).get(day)
        return entry is None or (entry.get("picked") and entry.get("successes", 0) < limit)

    def _rhythm_peer_id(self, peer_or_visit):
        """Different roles in one connected world share that world's daily allowance."""
        remote = peer_or_visit.get("remote", peer_or_visit)
        connection_id = (peer_or_visit.get("connection_id") or remote.get("connection_id")
                         or remote.get("instance_id") or remote.get("actor", {}).get("instance_id"))
        if connection_id:
            return connection_id
        scope = peer_or_visit.get("scope")
        peers = [peer for peer in self._state.get("peers", {}).values() if peer["id"] == remote["id"]]
        peer = next((peer for peer in peers if peer["scope"] == scope), peers[0] if peers else {})
        if peer.get("connection_id"):
            return peer["connection_id"]
        # A legacy daily counter can outlive both its actor binding and journal.
        # Public actor IDs retain the authenticated server ID as their prefix.
        worlds = sorted((conn["id"] for conn in self._state.get("connections", {}).values()), key=len, reverse=True)
        return next((world for world in worlds if remote["id"] == world or remote["id"].startswith(world + ":")), remote["id"])

    def _visit_day(self, local, at=None):
        try:
            return datetime.fromisoformat(str(local["local_date"])).date().isoformat()
        except (KeyError, TypeError, ValueError):
            return datetime.fromtimestamp(self._now() if at is None else at, timezone.utc).date().isoformat()

    def _daily_record(self, scope):
        record = self._state["draws"].setdefault(scope, {})
        if record.get("rhythm_version") != 3:
            self._migrate_daily_record(scope, record)
        return record

    def _migrate_daily_record(self, scope, record):
        # Version 2 counted every attempt. Refund known failures while retaining
        # any old counts whose journey has already been trimmed from the journal.
        history, aliases = {}, {}
        for peer in self._state.get("peers", {}).values():
            if peer["scope"] != scope:
                continue
            aliases[peer["id"]] = self._rhythm_peer_id(peer)
        for visit in self._state["visits"].values():
            if visit["scope"] != scope or not visit.get("automatic"):
                continue
            old_id = visit["remote"]["id"]
            aliases[old_id] = self._rhythm_peer_id(visit)
            day = self._visit_day(visit["local"], visit["created_at"])
            peer = history.setdefault(old_id, {"days": {}, "last_at": 0})
            used = peer["days"].setdefault(day, {"attempts": 0, "successes": 0})
            used["attempts"] += 1
            if visit.get("arrived_at"):
                used["successes"] += 1
                visit["automatic_counted"] = True
            peer["last_at"] = max(peer["last_at"], visit["created_at"])
            record["next_auto_at"] = max(record.get("next_auto_at", 0),
                visit["created_at"] + self.config.auto_interval_minutes * 60)

        old_peers, peers = record.get("peer_days", {}), {}
        for old_id in old_peers.keys() | history.keys():
            old, known = old_peers.get(old_id, {}), history.get(old_id, {})
            peer_id = aliases.get(old_id) or self._rhythm_peer_id({"id": old_id, "scope": scope})
            state = peers.setdefault(peer_id, {"days": {}, "last_at": 0})
            state["last_at"] = max(state["last_at"], old.get("last_at", 0), known.get("last_at", 0))
            old_days, known_days = old.get("days", {}), known.get("days", {})
            for day in old_days.keys() | known_days.keys():
                prior, journal = old_days.get(day, {}), known_days.get(day, {})
                attempts = max(prior.get("attempts", 0), journal.get("attempts", 0))
                successes = max(prior.get("successes", 0), journal.get("successes", 0))
                if "successes" not in prior:
                    successes += max(0, prior.get("attempts", 0) - journal.get("attempts", 0))
                entry = state["days"].setdefault(day, {"picked": False, "attempts": 0, "successes": 0})
                entry["picked"] = entry["picked"] or prior.get("picked", bool(journal))
                entry["attempts"] += attempts
                entry["successes"] += successes
        record.update(peer_days=peers, rhythm_version=3)

    def _note_automatic_success(self, visit):
        """Called under the invitation lock when a confirmed arrival is persisted."""
        if not visit.get("automatic") or not visit.get("arrived_at") or visit.get("automatic_counted"):
            return
        record = self._daily_record(visit["scope"])
        if visit.get("automatic_counted"):  # Migration may already have restored this arrival.
            return
        peer_id = self._rhythm_peer_id(visit)
        day = self._visit_day(visit["local"], visit["created_at"])
        state = record.setdefault("peer_days", {}).setdefault(peer_id, {"days": {}, "last_at": 0})
        entry = state["days"].setdefault(day, {"picked": True, "attempts": 0, "successes": 0})
        entry["successes"] = entry.get("successes", 0) + 1
        visit["automatic_counted"] = True

    def _admit_automatic(self, scope, peer, local):
        """Reserve an attempt's cooldown under the caller's invitation lock."""
        if (not self.config.auto_enabled or not self.config.daily_peer_limit
                or not local["ready"] or local.get("chat_busy")
                or not local.get("reception_open", True)):
            return False
        record = self._daily_record(scope)
        if record.get("next_auto_at", 0) > self._now():
            return False
        day = self._visit_day(local)
        state = record.setdefault("peer_days", {}).setdefault(self._rhythm_peer_id(peer), {"days": {}, "last_at": 0})
        entry = state["days"].get(day)
        if entry is None:
            entry = {"picked": self._random() < self.config.auto_probability, "attempts": 0, "successes": 0}
            state["days"][day] = entry
        retained = sorted(key for key in state["days"] if key != day)[-6:] + [day]
        state["days"] = {key: state["days"][key] for key in retained}
        if not entry["picked"] or entry.get("successes", 0) >= self.config.daily_peer_limit:
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

    def _automatic_available(self, scope):
        return (not self._closed and self.config.enabled and self.config.auto_enabled
                and self.config.daily_peer_limit and self._life_enabled(scope) and not self.active(scope)
                and self._touched.get(scope, 0) <= self._now()
                and not self._settlement_backlog_full(scope))

    async def _automatic(self):
        if self._closed or not self.config.enabled or not self.config.auto_enabled or not self.config.daily_peer_limit:
            return
        scopes = sorted(self._participating())
        for scope in scopes:
            async with self._lock:
                if not self._automatic_available(scope):
                    continue
                record = self._state["draws"].get(scope, {})
                if record.get("next_auto_at", 0) > self._now():
                    continue
                candidates = self._automatic_peers(scope)
                if not candidates:
                    continue
                day_getter = getattr(self.actor_port, "local_date", None)
                if record.get("rhythm_version") == 3 and callable(day_getter):
                    raw_day = day_getter(scope)
                    if not raw_day:  # The real adapter returns empty for a disabled life.
                        continue
                    try:
                        day = datetime.fromisoformat(str(raw_day)).date().isoformat()
                    except (TypeError, ValueError):
                        day = ""  # An unavailable calendar uses the original description path.
                    if day and not any(self._has_daily_opportunity(record, self._rhythm_peer_id(peer), day,
                                                                   self.config.daily_peer_limit)
                                       for peer in candidates):
                        continue
            local = await self.actor_port.describe(scope)
            if not local["ready"] or local.get("chat_busy") or not local.get("reception_open", True):
                continue
            async with self._lock:
                if not self._automatic_available(scope):
                    continue
                candidates = self._automatic_peers(scope)
                if not candidates:
                    continue
                record = self._daily_record(scope)
                if record.get("next_auto_at", 0) > self._now():
                    continue
                day = self._visit_day(local)
                peers = [peer for peer in candidates
                         if self._has_daily_opportunity(record, self._rhythm_peer_id(peer), day, self.config.daily_peer_limit)]
                def contact_order(peer):
                    state = record.get("peer_days", {}).get(self._rhythm_peer_id(peer), {})
                    return max(state.get("last_at", 0), state.get("last_prepare_at", 0)), peer["id"]

                peers.sort(key=contact_order)
            for peer in peers:
                async with self._lock:
                    previous_visits = set(self._state["visits"])
                try:
                    await self.invite(scope, peer["id"], automatic=True)
                except ValueError:
                    async with self._lock:
                        if not self._automatic_available(scope):
                            break
                        record = self._state["draws"].get(scope, {})
                        if (record.get("next_auto_at", 0) > self._now()
                                or any(key not in previous_visits and visit["scope"] == scope
                                       for key, visit in self._state["visits"].items())):
                            break
                        peer_id = self._rhythm_peer_id(peer)
                        if (self._has_daily_opportunity(record, peer_id, day, self.config.daily_peer_limit)
                                and any(self._rhythm_peer_id(candidate) == peer_id
                                        for candidate in self._automatic_peers(scope))):
                            # Binding/catalog failures happen before invitation admission.
                            # Retain the opportunity without polling preparation every tick.
                            state = record.setdefault("peer_days", {}).setdefault(peer_id, {"days": {}, "last_at": 0})
                            state["last_prepare_at"] = self._now()
                            # Let another world take the next turn even if this one's
                            # catalog or binding remains unavailable across restarts.
                            record["next_auto_at"] = self._now() + max(self.config.poll_seconds,
                                self.config.auto_interval_minutes * 60)
                            await self._save()
                            break
                    continue
                break  # One actual invitation per life per tick; other neighbours wait their turn.
