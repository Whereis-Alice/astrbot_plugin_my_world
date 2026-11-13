"""An agreed destination is not a visited memory location before arrival."""
from __future__ import annotations

import unittest

from tests import fake_astrbot

fake_astrbot.install()

from core.memory_places import visit_location_metadata  # noqa: E402
from core.visits.outings import begin_activity  # noqa: E402


class VisitMemoryTravelClockTests(unittest.TestCase):
    def trip(self):
        return {"role": "guest", "status": "completed", "outing_version": 1,
                "local": {"place": "门口"}, "remote": {"world": "朋友的世界"},
                "host_id": "host", "guest_id": "guest", "expires_at": 5000.,
                "arrived_at": 100., "scene": {"place": "门口"}, "log": []}

    def offer(self, place, *, travel=0):
        return {"place": place, "activity": "一起玩", "minutes": 5., "travel_seconds": travel}

    def test_return_on_the_way_does_not_remember_the_destination_as_visited(self):
        trip = self.trip()
        begin_activity(trip, self.offer("山顶", travel=300.), 100.)
        trip["ended_at"] = 150.
        self.assertEqual(visit_location_metadata(trip)["route"], [])

    def test_earlier_arrived_stop_survives_return_before_the_next_destination(self):
        trip = self.trip()
        begin_activity(trip, self.offer("庭院"), 100.)
        trip["outing"]["route"][-1]["ended_at"] = 180.
        begin_activity(trip, self.offer("山顶", travel=300.), 180.)
        trip["ended_at"] = 230.
        self.assertEqual([stop["place"] for stop in visit_location_metadata(trip)["route"]], ["庭院"])

    def test_changed_destination_does_not_revive_the_abandoned_future_arrival(self):
        trip = self.trip()
        begin_activity(trip, self.offer("山顶", travel=300.), 100.)
        trip["outing"]["route"][-1]["ended_at"] = 150.
        begin_activity(trip, self.offer("路旁长椅"), 150.)
        trip["ended_at"] = 200.
        self.assertEqual([stop["place"] for stop in visit_location_metadata(trip)["route"]], ["路旁长椅"])
