"""Historical encounters keep their world when its published lives change."""
from __future__ import annotations

import unittest

from tests import test_visits_global_pairing as fixture


class VisitHistoryWorldIdentityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.GlobalPairingTests.asyncSetUp
    advance = fixture.GlobalPairingTests.advance
    completed = fixture.GlobalPairingTests.completed

    async def test_retired_remote_actor_keeps_editor_world_identity_and_scoped_history(self):
        visit_id = await self.completed(self.a, self.a1, self.peer, self.b, self.b1)
        cid = self.connection["id"]
        await self.b.set_enabled_scopes([self.b2])
        await self.a.refresh_connection(cid)
        overview = await self.a.global_overview(self.a1)
        connection = next(row for row in overview["connections"] if row["id"] == cid)
        self.assertNotIn(self.peer, {actor["id"] for actor in connection["actors"]})
        record = next(row for row in overview["history"] if row["id"] == visit_id)
        self.assertEqual(record["connection_id"], cid)
        self.assertEqual(record["remote"]["id"], self.peer)
        self.assertEqual((await self.a.global_overview(self.a2))["history"], [])

        # Older journal entries can predate the local connection annotation.
        stored = self.a._state["visits"][visit_id]
        stored.pop("connection_id", None)
        record = await self.a.public_record(self.a1, visit_id)
        self.assertEqual(record["connection_id"], cid)
        self.assertNotIn("connection_id", self.a._wire(stored))
