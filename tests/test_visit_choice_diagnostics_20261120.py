"""Unused optional fields and scene capacity cannot swallow otherwise valid choices."""
from __future__ import annotations

import copy
import json
import unittest

from tests import fake_astrbot
fake_astrbot.install()

from tests import test_visit_phase_capabilities_20261115 as fixture  # noqa: E402
from core.visits.activities import apply_scene_update, record_action  # noqa: E402
from core.visits.decisions import VisitDecisionError  # noqa: E402
from core.visits.flow import VisitFlow  # noqa: E402
from core.visits.outings import begin_activity  # noqa: E402


class VisitChoiceDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.VisitPhaseCapabilityTests.asyncSetUp
    node_id = fixture.VisitPhaseCapabilityTests.node_id
    shared_visit = fixture.VisitPhaseCapabilityTests.shared_visit
    request = fixture.VisitPhaseCapabilityTests.request
    step = staticmethod(fixture.VisitPhaseCapabilityTests.step)

    async def test_unused_template_objects_preserve_the_valid_personal_action(self):
        visit = self.shared_visit("host")
        begin_activity(visit, {"place": "书桌", "activity": "看看折线", "minutes": 5}, self.clock.now())
        before = copy.deepcopy(visit)
        for empty in ({}, None):
            raw = {"say": "这里还有一道折线。", "continue_activity": True,
                   "action_step": self.step("看看折线的朝向", 0.75),
                   "action_control": empty, "scene_update": empty}
            choice, _, _ = await self.request(visit, raw)
            self.assertEqual(choice["say"], raw["say"])
            self.assertEqual(choice["action_step"]["description"], raw["action_step"]["description"])
            self.assertEqual(choice["action_step"]["seconds"], 0.75)
            self.assertEqual(VisitFlow._shared_choice_issue(visit, choice, self.clock.now()), "")
            self.assertEqual(visit, before)

    async def test_unused_control_fields_allow_a_real_reply_but_not_an_empty_turn(self):
        visit = self.shared_visit()
        template = {"action_step": {}, "action_control": {}, "scene_update": {}}
        choice, _, _ = await self.request(visit, {"say": "先看看你那一边。", **template})
        self.assertEqual(choice["say"], "先看看你那一边。")
        self.assertEqual(VisitFlow._shared_choice_issue(visit, choice, self.clock.now()), "")
        with self.assertRaises(VisitDecisionError) as caught:
            await self.request(visit, template)
        self.assertEqual(caught.exception.format_issue, "empty_choice")

    async def test_nonempty_bad_controls_remain_rejected_with_the_exact_field(self):
        visit = self.shared_visit()
        cases = (("action_step", {"description": "还没给耗时"}, "action_step_fields"),
                 ("action_step", [], "action_step_fields"),
                 ("action_control", {"action_id": "s0-a1", "operation": False}, "action_control_fields"),
                 ("action_control", False, "action_control_fields"),
                 ("scene_update", {"summary": "缺少引用"}, "scene_update_fields"),
                 ("scene_update", {"summary": "结果", "action_ids": ["s0-a1"], "objects": "不是列表"},
                  "scene_update_fields"))
        for field, value, issue in cases:
            with self.subTest(field=field, value=value), self.assertRaises(VisitDecisionError) as caught:
                await self.request(visit, {"say": "已有台词不能掩盖坏选择。", field: value})
            self.assertEqual(caught.exception.format_issue, issue)
            self.assertNotIn("已有台词", str(caught.exception))

    async def test_ninth_object_gets_local_capacity_correction_without_losing_facts_or_action(self):
        visit = self.shared_visit("host")
        begin_activity(visit, {"place": "书桌", "activity": "检查册子的装订缝", "minutes": 5}, self.clock.now())
        objects = ["册子", "册子的页沿", "裁口", "纸须", "折角", "半截括号", "边注", "装订缝"]
        visit["shared_activity"]["objects"] = list(objects)
        visit["shared_activity"]["pending"] = ["插页的来路还没确认"]
        self.assertTrue(record_action(visit, visit["host_id"], self.step("检查装订缝中的痕迹", 1), self.clock.now()))
        self.clock.advance(2)
        reference = visit["shared_activity"]["action_steps"][visit["host_id"]][0]["id"]
        raw = {"say": "缝里有一点干浆壳。", "continue_activity": True,
               "action_step": self.step("再看看干浆壳的形状", 2), "action_control": {},
               "scene_update": {"summary": "检查装订缝后看见干浆壳", "action_ids": [reference],
                                "objects": [*objects, "干浆壳"]}}
        before = copy.deepcopy(visit)
        with self.assertRaises(VisitDecisionError) as caught:
            await self.request(visit, raw)
        self.assertEqual(caught.exception.format_issue, "scene_update_capacity")
        self.assertEqual(visit, before)
        self.assertEqual(json.loads(caught.exception.repair_text), raw)
        visit["model_retry"] = {"phase": "turn", "category": "format", "format_attempts": 1,
                                "format_issue": caught.exception.format_issue,
                                "repair_text": caught.exception.repair_text}
        repaired = copy.deepcopy(raw)
        repaired["scene_update"]["objects"] = ["册子及册子的页沿", *objects[2:], "干浆壳"]
        choice, materials, captured = await self.request(visit, repaired)
        self.assertEqual(materials["failed_unexecuted_choice"]["format_issue"], "scene_update_capacity")
        draft = json.loads(materials["failed_unexecuted_choice"]["text"])
        self.assertEqual(len(draft["scene_update"]["objects"]), 9)
        caps = materials["decision_capabilities"]
        self.assertEqual(caps["scene_update_limits"], {"summary_chars": 500, "action_ids": 12,
                                                        "objects": 8, "pending": 8, "item_chars": 160})
        self.assertEqual(caps["scene_object_count"], 8)
        self.assertEqual(caps["scene_pending_count"], 1)
        self.assertIn("列表超过8项", captured["system_prompt"])
        self.assertEqual(VisitFlow._shared_choice_issue(visit, choice, self.clock.now()), "")
        self.assertEqual(choice["say"], raw["say"])
        self.assertEqual(choice["action_step"], raw["action_step"])
        self.assertTrue(all(any(item in entry for entry in choice["scene_update"]["objects"]) for item in objects))
        self.assertEqual(visit["shared_activity"], before["shared_activity"])
        self.assertTrue(apply_scene_update(visit, visit["host_id"], choice["scene_update"], self.clock.now()))
        self.assertEqual(visit["shared_activity"]["pending"], before["shared_activity"]["pending"])
        self.assertTrue(visit["shared_activity"]["action_steps"][visit["host_id"]][0]["settled_revision"])

    async def test_pending_list_and_item_limits_do_not_silently_crop_the_draft(self):
        visit = self.shared_visit("host")
        valid = {"summary": "仍待确认", "action_ids": ["s0-a1"]}
        for extra, issue in (({"pending": ["未确认事项" + str(i) for i in range(9)]}, "scene_update_capacity"),
                             ({"objects": ["细节" * 81]}, "scene_update_fields"),
                             ({"action_ids": ["s0-a1", "s0-a1"]}, "scene_update_fields"),
                             ({"summary": "事实" * 251}, "scene_update_fields")):
            raw = {"say": "仍有原意。", "scene_update": {**valid, **extra}}
            with self.subTest(issue=issue, extra=extra), self.assertRaises(VisitDecisionError) as caught:
                await self.request(visit, raw)
            self.assertEqual(caught.exception.format_issue, issue)
            self.assertEqual(json.loads(caught.exception.repair_text), raw)


if __name__ == "__main__":
    unittest.main()
