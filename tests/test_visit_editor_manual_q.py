"""A real editor invitation remains usable after both automatic daily ledgers are exhausted."""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from tests import test_visits_web as web_fixture
from tests import test_visits_manual_budget_q as worlds


class EditorManualIntegrationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = worlds.ManualVisitBudgetTests.asyncSetUp
    make_world = worlds.ManualVisitBudgetTests.make_world
    exhaust = worlds.ManualVisitBudgetTests.exhaust

    async def test_editor_phone_arrives_with_both_budgets_exhausted_without_spending_them(self):
        first_ledger, first_before = await self.exhaust(self.a)
        second_ledger, second_before = await self.exhaust(self.b)
        routes = []
        self.a.plugin.context = SimpleNamespace(register_web_api=lambda *row: routes.append(row))
        self.a.plugin.auth = SimpleNamespace(check=lambda token: token == "editor-fixture")
        web_fixture.api.register_visit_apis(self.a.plugin, "plugin")
        invite = next(row[1] for row in routes if row[0].endswith("/visits/invite"))
        payload = {"token": "editor-fixture", "session": self.scope, "peer_id": self.peer_id,
                   "direction": "outgoing", "reason": "一起去湖边走走"}
        request = SimpleNamespace(json=AsyncMock(return_value=payload))
        with patch.object(web_fixture.api, "request", request):
            response = await invite()
        self.assertEqual(response["status_code"], 200, response)
        local = next(iter(self.a.service._state["visits"].values()))
        remote = next(iter(self.b.service._state["visits"].values()))
        self.assertTrue(local["manual"])
        self.assertTrue(remote["manual"])
        await self.b.service._advance(remote["id"])
        await self.a.service._advance(local["id"])
        self.assertTrue(local.get("arrived_at"))
        self.assertTrue(remote.get("arrived_at"))
        self.assertEqual(await self.a.db.call("background_status", first_ledger), first_before)
        self.assertEqual(await self.b.db.call("background_status", second_ledger), second_before)

    async def test_editor_pages_read_the_complete_local_record_and_preserve_its_life(self):
        service = self.a.service
        peer_id = await service._prepare_peer(self.scope, self.peer_id)
        peer = service._find_peer(self.scope, peer_id)
        local = await self.a.port.describe(self.scope)
        visit = service._new_visit(self.scope, peer, local, "readable-record", "host", "一起读书", False)
        visit.update(status="completed", arrived_at=self.a.clock.now(), ended_at=self.a.clock.now(),
                     private=["只属于自己的想法"], reflection="只属于自己的感受")
        actor = service._local_id(visit)
        visit["log"] = [{"seq": index, "actor_id": actor, "kind": "say", "at": self.a.clock.now(),
                         "text": f"读书时留下的第{index}句"} for index in range(1, 301)]
        service._state["visits"][visit["id"]] = visit
        await service._save()
        cursor, pages = 0, []
        while True:
            page = await service.public_record(self.scope, visit["id"], before=cursor, limit=96)
            self.assertNotIn("private", page)
            self.assertNotIn("reflection", page)
            self.assertNotIn("secret", str(page))
            self.assertEqual(page["log_count"], 300)
            pages.insert(0, page["log"])
            cursor = page["next_before"]
            if not cursor:
                break
        self.assertEqual([row["seq"] for page in pages for row in page], list(range(1, 301)))
        self.assertEqual(len(service._state["visits"][visit["id"]]["log"]), 300)
        with self.assertRaises(ValueError):
            await service.public_record("another-life", visit["id"])
        self.assertFalse(self.a.llm.calls)
        self.assertFalse(self.b.llm.calls)
