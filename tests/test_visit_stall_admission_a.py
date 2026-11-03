"""Human-restarted invitations do not consume automatic invitation successes."""
from __future__ import annotations

from datetime import datetime, timezone
import unittest

from tests import fake_astrbot
fake_astrbot.install()

from core.visits import VisitConfig  # noqa: E402
from core.visits.flow import VisitFlow  # noqa: E402
from core.visits.rhythm import VisitRhythm  # noqa: E402


class QuotaHarness(VisitRhythm):
    def __init__(self):
        self.now = datetime(2026, 11, 3, 12, tzinfo=timezone.utc).timestamp()
        self.day = "2026-11-03"
        self.config = VisitConfig()
        self._state = {"draws": {"life-a": {"rhythm_version": 3}}, "visits": {},
                       "peers": {}, "connections": {"friend-world": {"id": "friend-world"}}}

    def _now(self):
        return self.now


class HumanContinuationQuotaTests(unittest.TestCase):
    def setUp(self):
        self.service = QuotaHarness()

    def visit(self, *, manual, counted=False, arrived=True):
        visit = {"id": "trip", "scope": "life-a", "automatic": True, "manual": manual,
                 "connection_id": "friend-world", "remote": {"id": "friend"},
                 "created_at": self.service.now, "status": "inviting", "outing_version": 1,
                 "local": {"local_date": self.service.day}}
        if arrived:
            visit["arrived_at"] = self.service.now + 60
        if counted:
            visit["automatic_counted"] = True
        self.service._state["visits"]["trip"] = visit
        return visit

    def entry(self):
        return self.service._state["draws"]["life-a"]["peer_days"]["friend-world"]["days"][self.service.day]

    def test_human_restarts_an_automatic_proposal_without_consuming_automatic_success(self):
        visit = self.visit(manual=False, arrived=False)
        VisitFlow._mark_manual_request(self.service, visit, "去她家一起玩")
        self.assertTrue(visit["manual"])
        self.assertTrue(visit["automatic"])
        visit["arrived_at"] = self.service.now + 60
        self.service._note_automatic_success(visit)
        self.assertNotIn("peer_days", self.service._state["draws"]["life-a"])
        self.assertNotIn("automatic_counted", visit)

    def test_human_extension_does_not_refund_a_prior_successful_automatic_arrival(self):
        visit = self.visit(manual=False)
        self.service._note_automatic_success(visit)
        visit["manual"] = True
        self.service._note_automatic_success(visit)
        self.assertEqual(self.entry()["successes"], 1)
        self.assertTrue(visit["automatic_counted"])

    def test_legacy_migration_does_not_manufacture_success_for_uncounted_human_arrival(self):
        visit = self.visit(manual=True)
        self.service._state["draws"]["life-a"] = {"rhythm_version": 2, "peer_days": {
            "friend": {"days": {self.service.day: {"picked": True, "attempts": 1}}}}}
        self.service._daily_record("life-a")
        self.assertEqual(self.entry()["successes"], 0)
        self.assertEqual(self.entry()["attempts"], 1)
        self.assertNotIn("automatic_counted", visit)

    def test_legacy_migration_retains_already_counted_arrival_after_human_extension(self):
        self.visit(manual=True, counted=True)
        self.service._state["draws"]["life-a"] = {"rhythm_version": 2}
        self.service._daily_record("life-a")
        self.assertEqual(self.entry()["successes"], 1)


if __name__ == "__main__":
    unittest.main()
