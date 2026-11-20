"""Farewell speech uses the departed scene without starting another activity."""
from __future__ import annotations

import copy
import json
import unittest

from tests import fake_astrbot
fake_astrbot.install()

from tests import test_visits_actor as fixture  # noqa: E402
from core.visits.decisions import VisitDecisionError  # noqa: E402


class DepartureAdapterTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.VisitActorTests.asyncSetUp
    node_id = fixture.VisitActorTests.node_id

    async def test_departure_can_say_goodbye_after_home_becomes_busy(self):
        visit = copy.deepcopy(self.visit)
        visit.update(status="completed", ended_at=self.clock.now(),
                     end_reason="本次停留到期，只保留已确认经过")
        visit["departure_context"] = {"friend_farewell": "册子先夹好，明天再看。"}
        describe = self.port.describe

        async def busy(scope):
            result = await describe(scope)
            result.update(ready=False, ready_reason="家中的休息已经开始", chat_busy=True)
            return result

        self.port.describe = busy
        self.llm.default_reply = '{"say":"好，夹口先留着。我回去想想，晚安。"}'
        result = await self.port.decide(fixture.fixture.SESSION, "departure", visit)
        self.assertEqual(result, {"say": "好，夹口先留着。我回去想想，晚安。"})
        request = self.llm.calls[-1]
        materials = json.loads(request["prompt"])
        self.assertEqual(materials["departure_context"]["friend_farewell"], "册子先夹好，明天再看。")
        self.assertEqual(materials["current_place"], "窗边")
        self.assertIn("不能延长相处、开启新活动", request["system_prompt"])
        self.assertNotIn("有具体动作可给action_step", request["system_prompt"])
        self.assertEqual(visit["status"], "completed")

    async def test_revoked_departure_dispatch_stops_before_provider(self):
        visit = copy.deepcopy(self.visit)
        visit.update(status="completed", ended_at=self.clock.now(), departure={"inflight": "old-call"})
        self.engine.visits._departure_snapshot_current = lambda _: False
        before = len(self.llm.calls)
        with self.assertRaises(VisitDecisionError) as caught:
            await self.port.decide(fixture.fixture.SESSION, "departure", visit)
        self.assertEqual(caught.exception.category, "identity")
        self.assertFalse(caught.exception.request_started)
        self.assertEqual(len(self.llm.calls), before)

    def test_departure_never_accepts_new_action_or_empty_goodbye(self):
        for raw in ('{"say":""}', '{"say":"回见","action_step":{"description":"再走走"}}',
                    '{"reflection":"已经结束"}'):
            with self.assertRaises(VisitDecisionError) as caught:
                fixture.VisitActorPort._parse_decision("departure", raw)
            self.assertEqual(caught.exception.format_issue, "departure_fields")


if __name__ == "__main__":
    unittest.main()
