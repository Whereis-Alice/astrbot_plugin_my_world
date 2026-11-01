"""Late settlement extends one memory without discarding its confirmed facts."""
from __future__ import annotations

import copy
import json
import unittest

from tests import test_engine as engine_fixture
from tests import test_visits_actor as actor_fixture


class VisitMemoryConfirmationTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = actor_fixture.VisitActorTests.asyncSetUp

    def journey(self, key="settlement", count=12):
        visit = copy.deepcopy(self.visit)
        visit.update(id=key, status="interrupted", end_reason="暂时联系不上")
        visit["log"] = [{"seq": n + 1, "actor_id": visit["remote"]["id"], "kind": "say",
                         "text": "第一次一起尝了樱桃蛋糕" if n == 0 else f"已经确认的经过{n}"}
                        for n in range(count)]
        return visit

    async def stored(self, visit):
        await self.port.remember(engine_fixture.SESSION, visit)
        rows = await self.db.call("query_memories", session_id=engine_fixture.SESSION)
        matches = [row for row in rows if json.loads(row.get("context") or "{}").get("visit_id") == visit["id"]]
        self.assertEqual(len(matches), 1)
        return matches[0]

    async def test_thirteenth_confirmed_row_keeps_the_first_fact_on_the_same_receipt(self):
        visit = self.journey()
        old = await self.stored(visit)
        visit["log"].append({"seq": 13, "actor_id": visit["remote"]["id"], "kind": "say", "text": "最后确认的告别"})
        updated = await self.stored(visit)
        self.assertEqual(updated["id"], old["id"])
        self.assertIn("第一次一起尝了樱桃蛋糕", updated["content"])
        self.assertIn("最后确认的告别", updated["content"])
        self.assertEqual(len(json.loads(updated["context"])["visit_confirmation"]["log"]), 13)

    async def test_full_protocol_log_retains_early_and_late_text_beyond_old_content_cutoff(self):
        visit = self.journey(count=40)
        for n, row in enumerate(visit["log"]):
            row["text"] = f"事实{n}：" + "内容" * 235 + f"尾声{n}"
        stored = await self.stored(visit)
        self.assertGreater(len(stored["content"]), 6500)
        for n in range(40):
            self.assertIn(f"事实{n}：", stored["content"])
            self.assertIn(f"尾声{n}", stored["content"])
        self.assertEqual(len(json.loads(stored["context"])["visit_confirmation"]["log"]), 40)

    async def test_confirmed_reason_only_update_is_idempotent(self):
        visit = self.journey(count=1)
        old = await self.stored(visit)
        visit["end_reason"] = "对方确认本次因原先的安排结束"
        updated = await self.stored(visit)
        self.assertEqual(updated["id"], old["id"])
        self.assertIn(visit["end_reason"], updated["content"])
        self.assertNotIn("暂时联系不上", updated["content"])
        self.assertEqual(json.loads(updated["context"])["end_reason"], visit["end_reason"])
        revisions = self.db.raw._query("SELECT id FROM memory_revision WHERE memory_id=?", (old["id"],))
        self.assertEqual(len(revisions), 1)
        await self.port.remember(engine_fixture.SESSION, visit)
        self.assertEqual(self.db.raw._query("SELECT id FROM memory_revision WHERE memory_id=?", (old["id"],)), revisions)

    async def test_legacy_confirmation_without_reason_metadata_can_accept_the_final_reason(self):
        visit = self.journey(count=1)
        old = await self.stored(visit)
        meta = json.loads(old["context"])
        meta.pop("end_reason", None)
        self.db.raw.update_memory(old["id"], context=json.dumps(meta, ensure_ascii=False))
        visit["end_reason"] = "这次确认是各自回到原先的安排"
        updated = await self.stored(visit)
        self.assertEqual(updated["id"], old["id"])
        self.assertIn(visit["end_reason"], updated["content"])

    async def test_reason_update_respects_manual_pin_fold_and_delete(self):
        for mode in ("manual", "pin", "fold", "delete"):
            with self.subTest(mode=mode):
                visit = self.journey("protected-" + mode, count=1)
                old = await self.stored(visit)
                if mode == "manual":
                    self.db.raw.revise_memory(old["id"], {"content": "我手动写下的回忆"}, manual=True, reason="编辑")
                elif mode == "pin":
                    self.db.raw.update_memory(old["id"], pinned=True)
                elif mode == "fold":
                    self.db.raw.fold_memory(old["id"], text="我整理出的要点")
                else:
                    self.db.raw.delete_memories(memory_id=old["id"])
                protected = await self.db.call("memory_by_id", old["id"])
                visit["end_reason"] = "迟到确认的新原因"
                await self.port.remember(engine_fixture.SESSION, visit)
                self.assertEqual(await self.db.call("memory_by_id", old["id"]), protected)

    async def test_extension_that_rewrites_a_confirmed_fact_is_rejected(self):
        visit = self.journey()
        old = await self.stored(visit)
        visit["log"][0]["text"] = "未经确认的另一段经过"
        visit["log"].append({"seq": 13, "actor_id": visit["remote"]["id"], "kind": "say", "text": "新告别"})
        updated = await self.stored(visit)
        self.assertEqual(updated["content"], old["content"])
        self.assertNotIn("新告别", updated["content"])
