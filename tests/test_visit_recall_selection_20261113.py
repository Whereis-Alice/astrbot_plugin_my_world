"""Remember the latest encounters, not the latest delayed database writes."""
from __future__ import annotations

import copy
import json
import unittest

from tests.fake_astrbot import install
install()

from tests import test_visits_actor as actor_fixture  # noqa: E402
from tests import test_engine as engine_fixture  # noqa: E402


class VisitRecallSelectionTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = actor_fixture.VisitActorTests.asyncSetUp

    async def save(self, key, start, value):
        visit = copy.deepcopy(self.visit)
        visit.update(id=key, status="completed", arrived_at=start, ended_at=start + 60)
        visit["log"] = [{"seq": 1, "kind": "say", "actor_id": visit["remote"]["id"], "text": value}]
        await self.port.remember(engine_fixture.SESSION, visit)
        rows = await self.db.call("query_memories", session_id=engine_fixture.SESSION)
        return next(row for row in rows if json.loads(row.get("context") or "{}").get("visit_id") == key)

    async def recall(self):
        return await self.port._past_visits(engine_fixture.SESSION, self.local["persona_key"], self.visit["remote"])

    async def test_delayed_old_receipt_does_not_replace_the_three_latest_encounters(self):
        for index in range(4):
            row = await self.save(f"new-{index}", 2000 + index * 100, f"最近相处第{index}次")
            self.db.raw.update_memory(row["id"], created_at=3000 + index)
        old = await self.save("late-old", 1000, "迟到入账的旧相处")
        self.db.raw.update_memory(old["id"], created_at=9000)
        recalled = await self.recall()
        self.assertEqual(len(recalled), 3)
        self.assertTrue(all(f"最近相处第{index}次" in recalled[3 - index] for index in (3, 2, 1)))
        self.assertNotIn("迟到入账", "".join(recalled))

    async def test_many_unrelated_mentions_cannot_hide_an_exact_partner_receipt(self):
        await self.save("actual", 1000, "她还记得窗边那局棋")
        partner = self.visit["remote"]["id"]
        for index in range(90):
            self.db.raw.add_memory(session_id=engine_fixture.SESSION, persona_id=self.local["persona_key"],
                scope="group_persona", node_id="study", content=f"别的活动提到了{partner}，第{index}条",
                memory_type="event", source="life_event",
                context=json.dumps({"kind": "other_event", "persona_key": self.local["persona_key"]}))
        recalled = await self.recall()
        self.assertEqual(len(recalled), 1)
        self.assertIn("窗边那局棋", recalled[0])

    async def test_old_receipt_without_occurrence_keeps_current_edited_text(self):
        row = await self.save("old-schema", 1000, "原始经过")
        meta = json.loads(row["context"])
        meta.pop("occurred_start", None)
        meta.pop("occurred_end", None)
        self.db.raw.update_memory(row["id"], context=json.dumps(meta, ensure_ascii=False))
        self.db.raw.revise_memory(row["id"], {"content": "我整理后只记得她笑了一下"}, manual=True, reason="整理")
        recalled = await self.recall()
        self.assertEqual(recalled, ["我整理后只记得她笑了一下"])

    async def test_exact_owner_filter_precedes_limit_and_ignores_invalid_context(self):
        await self.save("owned", 1000, "同一位她的相见")
        partner = self.visit["remote"]["id"]
        for index in range(85):
            self.db.raw.add_memory(session_id=engine_fixture.SESSION, persona_id="another-persona",
                scope="group_persona", node_id="study", content=f"另一个人格的相见{index}",
                memory_type="event", source="life_event", context=json.dumps({"kind": "world_visit",
                    "peer_id": partner, "persona_key": "another-persona"}))
        for context in ("not json", "[]", "true", '"plain"'):
            self.db.raw.add_memory(session_id=engine_fixture.SESSION, persona_id=self.local["persona_key"],
                scope="group_persona", node_id="study", content=partner, memory_type="event",
                source="life_event", context=context)
        self.assertIn("同一位她的相见", (await self.recall())[0])
        self.assertEqual(await self.db.call("query_visit_memories", session_ids=[],
            persona_key=self.local["persona_key"], peer_id=partner), [])

    async def test_timestamp_lookalikes_do_not_override_known_occurrence(self):
        old = await self.save("old-invalid-end", 1000, "较早的真实相见")
        newest = await self.save("newest-real", 2000, "最近的真实相见")
        self.db.raw.update_memory(newest["id"], created_at=3000)
        for ending in (True, "99999999", 253402300800, -1):
            meta = json.loads(old["context"])
            meta["occurred_end"] = ending
            self.db.raw.update_memory(old["id"], context=json.dumps(meta), created_at=9000)
            recalled = await self.recall()
            self.assertIn("最近的真实相见", recalled[0])

    async def test_corrected_occurrence_updates_the_original_receipt_without_changing_facts(self):
        row = await self.save("corrected-time", 1000, "原本已经确认的经过")
        visit = copy.deepcopy(self.visit)
        visit.update(id="corrected-time", status="completed", arrived_at=1005, ended_at=1055)
        visit["log"] = [{"seq": 1, "kind": "say", "actor_id": visit["remote"]["id"], "text": "原本已经确认的经过"}]
        await self.port.remember(engine_fixture.SESSION, visit)
        updated = await self.db.call("memory_by_id", row["id"])
        self.assertEqual(updated["content"], row["content"])
        meta = json.loads(updated["context"])
        self.assertEqual(meta["occurred_start"], 1005)
        self.assertEqual(meta["occurred_end"], 1055)
        revisions = self.db.raw._query("SELECT id FROM memory_revision WHERE memory_id=?", (row["id"],))
        self.assertEqual(len(revisions), 1)
        await self.port.remember(engine_fixture.SESSION, visit)
        self.assertEqual(self.db.raw._query("SELECT id FROM memory_revision WHERE memory_id=?", (row["id"],)), revisions)

    async def test_incomplete_old_snapshot_does_not_erase_known_occurrence(self):
        row = await self.save("retained-time", 1000, "已确认的相处")
        visit = copy.deepcopy(self.visit)
        visit.update(id="retained-time", status="completed", accepted_at=1000)
        visit.pop("arrived_at", None)
        visit["log"] = [{"seq": 1, "kind": "say", "actor_id": visit["remote"]["id"], "text": "已确认的相处"}]
        await self.port.remember(engine_fixture.SESSION, visit)
        updated = await self.db.call("memory_by_id", row["id"])
        meta = json.loads(updated["context"])
        self.assertEqual(meta["occurred_start"], 1000)
        self.assertEqual(meta["occurred_end"], 1060)


if __name__ == "__main__":
    unittest.main()
