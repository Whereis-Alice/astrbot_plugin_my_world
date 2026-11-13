"""Cross-check exact life ownership and current-text recall at the adapter seam."""
from __future__ import annotations

import json
import unittest

from tests.fake_astrbot import install

install()

from tests import test_engine as fixture  # noqa: E402
from tests import test_visit_recall_selection_20261113 as recall_fixture  # noqa: E402
from tests import test_visits_actor as actor_fixture  # noqa: E402


class VisitRecallOwnerCrosscheckTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = actor_fixture.VisitActorTests.asyncSetUp
    save = recall_fixture.VisitRecallSelectionTests.save
    recall = recall_fixture.VisitRecallSelectionTests.recall

    async def test_shared_member_receipt_remains_owned_by_the_same_life(self):
        fixture.EngineTestCase.add_group(self, sessions=[fixture.SESSION, fixture.PRIVATE_SESSION],
                                        main=fixture.SESSION)
        row = await self.save("shared-member", 1000, "同一个她在私聊整理的相见")
        self.db.raw.update_memory(row["id"], session_id=fixture.PRIVATE_SESSION)
        recalled = await self.recall()
        self.assertEqual(len(recalled), 1)
        self.assertIn("私聊整理的相见", recalled[0])
        partner = self.visit["remote"]["id"]
        for session, peer in (("foreign-life", partner), (fixture.SESSION, partner + ":other")):
            for index in range(4):
                self.db.raw.add_memory(session_id=session, persona_id=self.local["persona_key"],
                    scope="group_persona", node_id="study", content="别处较新的相见", memory_type="event",
                    source="life_event", created_at=9000 + index,
                    context=json.dumps({"kind": "world_visit", "peer_id": peer,
                                        "persona_key": self.local["persona_key"]}))
        recalled = await self.recall()
        self.assertEqual(len(recalled), 1)
        self.assertIn("私聊整理的相见", recalled[0])

    async def test_manual_revision_and_deletion_are_seen_on_the_next_recall(self):
        row = await self.save("edited", 1000, "原始经过里有一局棋")
        self.db.raw.revise_memory(row["id"], {"content": "我整理后只留她笑了这件事"},
                                  manual=True, reason="整理")
        self.assertEqual(await self.recall(), ["我整理后只留她笑了这件事"])
        self.db.raw.delete_memories_by_ids([row["id"]])
        self.assertEqual(await self.recall(), [])


if __name__ == "__main__":
    unittest.main()
