"""Late final confirmation must reach the existing durable visit memory."""
from __future__ import annotations

import copy
import json
import unittest

from tests import test_engine as engine_fixture
from tests import test_visits_actor as actor_fixture


class VisitLateConfirmationReviewTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = actor_fixture.VisitActorTests.asyncSetUp

    async def test_final_confirmation_extends_one_existing_trip_memory(self):
        initial = {**self.visit, "status": "interrupted", "end_reason": "暂时联系不上"}
        await self.port.remember(engine_fixture.SESSION, initial)
        final = copy.deepcopy(initial)
        final["status"] = "completed"
        final["log"].append({"seq": 2, "actor_id": final["remote"]["id"],
                             "kind": "say", "text": "最后记得把茶带回去。"})
        await self.port.remember(engine_fixture.SESSION, final)
        rows = await self.db.call("query_memories", session_id=engine_fixture.SESSION,
                                  life_persona_key=self.local["persona_key"])
        visits = [row for row in rows if row["source"] == "life_event"]
        self.assertEqual(len(visits), 1)
        self.assertIn("最后记得把茶带回去。", visits[0]["content"])
        self.assertEqual(json.loads(visits[0]["context"])["status"], "completed")

    async def trip_memory(self):
        rows = await self.db.call("query_memories", session_id=engine_fixture.SESSION)
        return next(row for row in rows if row["source"] == "life_event")

    def final_trip(self, initial):
        final = copy.deepcopy(initial)
        final["status"] = "completed"
        final["log"].append({"seq": 2, "actor_id": final["remote"]["id"],
                             "kind": "say", "text": "最后记得把茶带回去。"})
        return final

    async def test_same_log_final_status_updates_without_duplicate_or_regression(self):
        initial = {**self.visit, "status": "interrupted", "end_reason": "暂时联系不上"}
        await self.port.remember(engine_fixture.SESSION, initial)
        final = {**initial, "status": "completed"}
        await self.port.remember(engine_fixture.SESSION, final)
        row = await self.trip_memory()
        self.assertIn("这次已告别归家", row["content"])
        await self.port.remember(engine_fixture.SESSION, initial)
        self.assertEqual(await self.trip_memory(), row)
        revisions = (await self.db.call("memory_sources", row["id"]))["revisions"]
        self.assertEqual(len(revisions), 1)
        self.assertFalse(revisions[0]["manual"])

    async def test_rewritten_or_older_confirmation_cannot_replace_confirmed_prefix(self):
        initial = {**self.visit, "status": "interrupted", "end_reason": "暂时联系不上"}
        await self.port.remember(engine_fixture.SESSION, initial)
        final = self.final_trip(initial)
        await self.port.remember(engine_fixture.SESSION, final)
        row = await self.trip_memory()
        changed = copy.deepcopy(final)
        changed["log"][0]["text"] = "错误改写已经确认的台词"
        await self.port.remember(engine_fixture.SESSION, changed)
        await self.port.remember(engine_fixture.SESSION, initial)
        self.assertEqual(await self.trip_memory(), row)

    async def test_late_confirmation_preserves_editor_controls_and_ownership(self):
        cases = {
            "manual": {"content": "我自己整理过的记忆"},
            "restored_manual": {},
            "direct_edit": {"content": "绕过修订保存的正文"},
            "pinned": {"pinned": 1},
            "folded": {"tier": "gist", "folded_at": 123},
            "other_node": {"node_id": "another-place"},
            "other_persona": {"persona_id": "another-persona"},
            "other_scope": {"scope": "global"},
            "other_peer": {"participants": json.dumps(["world_actor:another-peer"])},
        }
        for name, fields in cases.items():
            with self.subTest(name=name):
                initial = {**self.visit, "id": "protected-" + name, "status": "interrupted"}
                await self.port.remember(engine_fixture.SESSION, initial)
                rows = await self.db.call("query_memories", session_id=engine_fixture.SESSION)
                row = next(row for row in rows if json.loads(row["context"] or "{}").get("visit_id") == initial["id"])
                if name in {"manual", "restored_manual"}:
                    await self.db.call("revise_memory", row["id"], fields, manual=True)
                else:
                    await self.db.call("update_memory", row["id"], **fields)
                before = await self.db.call("memory_by_id", row["id"])
                await self.port.remember(engine_fixture.SESSION, self.final_trip(initial))
                self.assertEqual(await self.db.call("memory_by_id", row["id"]), before)

    async def test_late_confirmation_does_not_recreate_deleted_memory(self):
        initial = {**self.visit, "status": "interrupted"}
        await self.port.remember(engine_fixture.SESSION, initial)
        row = await self.trip_memory()
        await self.db.call("delete_memories_by_ids", [row["id"]])
        await self.port.remember(engine_fixture.SESSION, self.final_trip(initial))
        self.assertIsNone(await self.db.call("memory_by_id", row["id"]))
        rows = await self.db.call("query_memories", session_id=engine_fixture.SESSION)
        self.assertFalse([item for item in rows if item["source"] == "life_event"])

    async def test_unedited_legacy_memory_upgrades_when_its_facts_are_still_confirmed(self):
        initial = {**self.visit, "status": "interrupted"}
        await self.port.remember(engine_fixture.SESSION, initial)
        row = await self.trip_memory()
        meta = json.loads(row["context"])
        meta.pop("visit_confirmation")
        await self.db.call("update_memory", row["id"], context=json.dumps(meta, ensure_ascii=False))
        await self.port.remember(engine_fixture.SESSION, self.final_trip(initial))
        after = await self.trip_memory()
        self.assertEqual(after["id"], row["id"])
        self.assertIn("最后记得把茶带回去", after["content"])
        self.assertIn("visit_confirmation", json.loads(after["context"]))

