"""Format retries correct the wire shape without inventing consent or dialogue."""
from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import Mock

from tests import test_visits_actor as fixture
from tests import test_visits_model_retry as retry_fixture
from core.visits.decisions import FORMAT_ISSUES, VisitDecisionError


class VisitFormatShapeTests(unittest.TestCase):
    def test_failures_are_known_structural_labels_and_never_response_content(self):
        cases = [
            ("turn", "我继续讲下去了。private raw thought", "json_object"),
            ("receive", '{"accept":"false","say":"不能猜同意"}', "invitation_accept"),
            ("turn", '{"say":["不能拼接冒充台词"]}', "text_fields"),
            ("turn", '{"say":"原话","leave":"false"}', "leave_boolean"),
            ("turn", '{"scene":"河边"}', "scene_fields"),
            ("turn", '{"outing_accept":1}', "choice_boolean"),
            ("turn", '{"pause_minutes":"5"}', "pause_number"),
            ("turn", '{}', "empty_choice"),
        ]
        for phase, text, issue in cases:
            with self.subTest(issue=issue), self.assertRaises(VisitDecisionError) as caught:
                fixture.VisitActorPort._parse_decision(phase, text)
            self.assertEqual(caught.exception.format_issue, issue)
            self.assertEqual(caught.exception.category, "format")
            self.assertNotIn(text, str(caught.exception))
        for unknown in ("private raw thought", {}, [], None):
            self.assertEqual(VisitDecisionError("format", format_issue=unknown).format_issue, "")

    def test_retry_guidance_only_applies_to_current_format_failure(self):
        port = fixture.VisitActorPort
        visit = {"model_retry": {"phase": "turn", "category": "format", "failures": 1,
                                  "format_issue": "text_fields"}}
        text = port._decision_output_rules("turn", visit)
        self.assertIn("文字字段没有使用字符串", text)
        self.assertIn("上次输出未执行", text)
        self.assertIn("不能加引号", text)
        for retry in ({"phase": "receive", "category": "format", "attempts": 1},
                      {"phase": "turn", "category": "provider", "attempts": 1},
                      {"phase": "turn", "category": "format", "attempts": 0},
                      {"phase": "turn", "category": "format", "attempts": 3}):
            self.assertNotIn("格式重试：", port._decision_output_rules("turn", {"model_retry": retry}))
        visit["model_retry"]["format_issue"] = {"secret": "private"}
        self.assertNotIn("private", port._decision_output_rules("turn", visit))

    def test_phase_examples_remain_valid_and_do_not_require_unrelated_output(self):
        for phase, snapshot in (("propose", {}), ("receive", {}), ("turn", {}), ("finish", {}),
                                ("scene", {"scene_stage": "event_offer"}),
                                ("scene", {"scene_stage": "event_resolve"})):
            rules = fixture.VisitActorPort._decision_output_rules(phase, snapshot)
            sample = rules.split("结构示例：", 1)[1]
            data, _ = json.JSONDecoder().raw_decode(sample)
            result = fixture.VisitActorPort._parse_decision(phase, json.dumps(data, ensure_ascii=False))
            self.assertIsInstance(result, dict)
        rules = fixture.VisitActorPort._decision_output_rules("turn", {})
        self.assertIn('{"continue_activity":true,"pause_minutes":5}', rules)
        result = fixture.VisitActorPort._parse_decision("turn", '{"continue_activity":true,"pause_minutes":5}')
        self.assertFalse(result["outing_accept"])
        self.assertFalse(result["leave"])
        self.assertEqual(result["say"], "")


class VisitFormatAdapterTests(unittest.IsolatedAsyncioTestCase):
    node_id = "study"
    asyncSetUp = fixture.VisitActorTests.asyncSetUp

    async def test_invalid_output_records_only_safe_shape_and_next_retry_gets_correction(self):
        raw = '{"say":["private wording that must never enter the trace"]}'
        self.llm.default_reply = raw
        self.engine.decision_trace = Mock()
        with self.assertRaises(VisitDecisionError) as caught:
            await self.port.decide(fixture.fixture.SESSION, "turn", self.visit)
        self.assertEqual(caught.exception.format_issue, "text_fields")
        args, details = self.engine.decision_trace.call_args
        self.assertEqual(args[1:3], ("world_visit", "invalid_format"))
        self.assertEqual(details["format_issue"], "text_fields")
        self.assertEqual(details["output_chars"], len(raw))
        self.assertNotIn("private wording", str(details))
        self.assertEqual(len(self.llm.calls), 1)
        self.visit["model_retry"] = {"phase": "turn", "category": "format", "attempts": 1,
                                     "format_issue": caught.exception.format_issue}
        self.llm.default_reply = '{"say":"茶还温着，我们接着聊。"}'
        result = await self.port.decide(fixture.fixture.SESSION, "turn", self.visit)
        self.assertEqual(result["say"], "茶还温着，我们接着聊。")
        self.assertIn("格式重试：文字字段没有使用字符串", self.llm.calls[-1]["system_prompt"])
        self.assertNotIn("private wording", self.llm.calls[-1]["system_prompt"])
        self.assertNotIn("private wording", self.llm.calls[-1]["prompt"])
        self.assertEqual(len(self.llm.calls), 2)


class VisitFormatLifecycleTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = retry_fixture.VisitModelRetryTests.asyncSetUp
    offered = retry_fixture.VisitModelRetryTests.offered
    host_turn = retry_fixture.VisitModelRetryTests.host_turn
    reload_service = retry_fixture.VisitModelRetryTests.reload_service

    async def test_safe_issue_survives_reload_and_recovery_still_stops_at_three_calls(self):
        visit_id, visit = await self.host_turn()
        logs = copy.deepcopy(visit["log"])
        calls = visit["calls"][visit["host_id"]]
        attempts = []
        async def invalid(scope, phase, snapshot):
            attempts.append(copy.deepcopy(snapshot.get("model_retry")))
            raise VisitDecisionError("format", format_issue="text_fields")
        self.b_actor.decide = invalid
        await self.b._advance(visit_id)
        self.assertEqual(visit["model_retry"]["format_issue"], "text_fields")
        self.assertNotIn("format_issue", json.dumps(self.b._wire(visit)))
        self.b = await self.reload_service(self.b)
        visit = self.b._state["visits"][visit_id]
        self.now = visit["model_retry"]["next_at"]
        await self.b._advance(visit_id)
        self.assertEqual(attempts[-1]["format_issue"], "text_fields")
        self.now = visit["model_retry"]["next_at"]
        await self.b._advance(visit_id)
        self.assertEqual(visit["status"], "interrupted")
        self.assertEqual(len(attempts), 3)
        self.assertEqual(visit["calls"][visit["host_id"]], calls + 3)
        self.assertEqual(visit["log"], logs)
        self.assertNotIn("model_retry", visit)
        self.assertEqual(set(FORMAT_ISSUES), {"json_object", "invitation_accept", "text_fields", "leave_boolean",
                                              "scene_fields", "choice_boolean", "pause_number", "empty_choice"})
