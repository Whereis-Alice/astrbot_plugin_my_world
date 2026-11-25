"""Personal motivation survives decisions without becoming the friend's intent."""
from __future__ import annotations

import copy
import json
import unittest

from tests import fake_astrbot
fake_astrbot.install()

from tests import test_visit_phase_capabilities_20261115 as fixture  # noqa: E402
from tests.test_visits_actor import fixture as engine_fixture  # noqa: E402
from core.visits.flow import VisitFlow  # noqa: E402
from core.visits.intent import activity_intent_context  # noqa: E402
from core.visits.outings import begin_activity  # noqa: E402
from core.visits.persistence import VisitJournal  # noqa: E402
from core.visits.protocol import clean_decision  # noqa: E402
from core.visits.decisions import VisitDecisionError  # noqa: E402


class PersonalVisitIntentTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.VisitPhaseCapabilityTests.asyncSetUp
    node_id = fixture.VisitPhaseCapabilityTests.node_id
    shared_visit = fixture.VisitPhaseCapabilityTests.shared_visit
    request = fixture.VisitPhaseCapabilityTests.request

    async def test_plain_speech_persona_applies_to_say_without_replacing_internal_json(self):
        self.persona.text = "输出方式：只发送你想说的话，不写动作、旁白、内心独白或分析过程。"
        self.local = await self.port.describe(engine_fixture.SESSION)
        value = self.shared_visit("host")
        value["local"] = copy.deepcopy(self.local)
        begin_activity(value, {"place": "书桌", "activity": "看册子", "minutes": 10}, self.clock.now())
        choice, _, captured = await self.request(value, {"say": "今天看到这里就够啦。", "activity_intent":
            {"enough": "看到这处就够"}, "continue_activity": True})
        prompt = captured["system_prompt"]
        self.assertIn(self.persona.text, prompt)
        self.assertLess(prompt.index("人格中只说原话"), prompt.index(self.persona.text))
        self.assertIn("只约束say里的本人台词", prompt)
        self.assertIn("所引用动作支持的具体变化", prompt)
        self.assertEqual(choice["say"], "今天看到这里就够啦。")
        self.assertEqual(choice["activity_intent"]["enough"], "看到这处就够")

    async def test_only_valid_private_intent_is_a_legal_silent_choice(self):
        value = self.shared_visit("host")
        begin_activity(value, {"place": "窗边", "activity": "待一会儿", "minutes": 10}, self.clock.now())
        choice, _, _ = await self.request(value, {"activity_intent": {"feeling": "先安静看一会儿"}})
        self.assertFalse(choice["say"])
        self.assertFalse(choice["action_step"])
        self.assertEqual(VisitFlow._shared_choice_issue(value, choice, self.clock.now()), "")
        for invalid in (None, {}, [], {"feeling": 1}):
            with self.assertRaises(VisitDecisionError):
                await self.request(value, {"activity_intent": invalid})

    async def test_real_adapter_and_core_keep_bounded_optional_intent(self):
        value = self.shared_visit("host")
        for intent in (None, {}, [], {"want": False, "enough": 7},
                       {"want": " 看她新做的东西 ", "enough": "看一件就够", "feeling": "好奇" * 90}):
            choice, _, _ = await self.request(value, {"say": "想看看你新做的东西。", "activity_intent": intent})
            decision = clean_decision(choice)
            self.assertEqual(decision["say"], "想看看你新做的东西。")
            self.assertEqual(decision["activity_intent"], choice["activity_intent"])
            self.assertLessEqual(len(decision["activity_intent"].get("feeling", "")), 100)

    async def test_proposal_is_not_current_until_exact_activity_is_accepted(self):
        value = self.shared_visit("host")
        offer = {"place": "窗边", "activity": "看她新折的纸鸟", "minutes": 10}
        raw = {"outing": offer, "activity_intent": {"want": "看纸鸟", "enough": "试飞一次就够"}}
        VisitFlow._retain_private(value, clean_decision(raw), self.clock.now(), phase="turn")
        value["outing_offer"] = offer
        self.assertEqual(activity_intent_context(value), {"pending_proposal": raw["activity_intent"]})
        wrong = copy.deepcopy(value)
        begin_activity(wrong, {**offer, "minutes": 11}, self.clock.now())
        self.assertEqual(activity_intent_context(wrong), {})
        begin_activity(value, offer, self.clock.now())
        self.assertEqual(activity_intent_context(value), {"current": raw["activity_intent"]})

    async def test_guest_accepted_proposal_and_later_changed_feeling_survive_reload(self):
        value = self.shared_visit()
        offer = {"place": "书桌", "activity": "一起折纸", "minutes": 10, "travel_seconds": 2}
        value["outing_offer"] = offer
        VisitFlow._retain_private(value, clean_decision({"outing_accept": True, "activity_intent":
            {"want": "试试折纸", "enough": "折出一只能飞的就行"}}), self.clock.now(), phase="turn")
        begin_activity(value, offer, self.clock.now())
        self.clock.advance(3)
        VisitFlow._retain_private(value, clean_decision({"continue_activity": True, "activity_intent":
            {"feeling": "今天已经满足，想听她讲别的事"}}), self.clock.now(), phase="turn")
        restored = json.loads(json.dumps(value))
        context = activity_intent_context(restored)["current"]
        self.assertEqual(context["enough"], "折出一只能飞的就行")
        self.assertEqual(context["feeling"], "今天已经满足，想听她讲别的事")
        before = copy.deepcopy(restored)
        VisitFlow._retain_private(restored, clean_decision({"activity_intent": context}),
                                  self.clock.now() + 10, phase="turn")
        self.assertEqual(restored, before)

    async def test_finish_cannot_overwrite_intent_or_publish_private_wishes(self):
        value = self.shared_visit("host")
        begin_activity(value, {"place": "窗边", "activity": "待一会儿", "minutes": 10}, self.clock.now())
        VisitFlow._retain_private(value, clean_decision({"activity_intent": {"want": "私有意愿MARKER"}}),
                                  self.clock.now(), phase="turn")
        before = copy.deepcopy(value["activity_intent"])
        VisitFlow._retain_private(value, clean_decision({"reflection": "我觉得今天已足够", "activity_intent":
            {"want": "结束回顾不能改写"}}), self.clock.now() + 1, phase="finish")
        self.assertEqual(value["activity_intent"], before)
        self.assertNotIn("MARKER", json.dumps(VisitJournal._wire(value)))
        self.assertNotIn("MARKER", json.dumps(value.get("log", [])))
        self.assertNotIn("MARKER", json.dumps(value.get("private_moments", [])))

    async def test_old_intent_does_not_bind_to_different_new_stage(self):
        value = self.shared_visit("host")
        begin_activity(value, {"place": "窗边", "activity": "一起折纸", "minutes": 10}, self.clock.now())
        VisitFlow._retain_private(value, clean_decision({"activity_intent": {"want": "折一只鸟"}}),
                                  self.clock.now(), phase="turn")
        self.clock.advance(1)
        begin_activity(value, {"place": "窗边", "activity": "听朋友讲故事", "minutes": 10}, self.clock.now())
        self.assertEqual(activity_intent_context(value), {})

    async def test_one_recent_attempt_is_available_separately_from_stable_practice(self):
        value = self.shared_visit("host")
        async with self.engine.session_state(engine_fixture.SESSION) as state:
            state.life_interests = {"new": {"label": "昨天试过的新小游戏", "last_at": self.clock.now() - 90,
                "sessions": 1, "minutes": 2, "days": ["2026-11-25"], "episodes": [{"at": self.clock.now() - 90,
                "minutes": 2, "day": "2026-11-25", "period": "晚上"}]}, "other": {"personas": {
                "another-persona": {"label": "别人的兴趣", "last_at": self.clock.now()}}}}
        choice, materials, _ = await self.request(value, {"say": "昨天试过一个小游戏。"})
        self.assertEqual(materials["recent_personal_activities"], [])
        self.assertEqual([row["label"] for row in materials["recent_personal_practice"]], ["昨天试过的新小游戏"])
        self.assertTrue(choice["say"])

    async def test_unfinished_background_allows_real_personal_departure(self):
        value = self.shared_visit("host")
        begin_activity(value, {"place": "书桌", "activity": "看看册子", "minutes": 25}, self.clock.now())
        value["shared_activity"].update(pending=["下次再看未解折口"], done=False)
        choice, materials, _ = await self.request(value, {"leave": True, "say": "今天看到这里就够了。"})
        self.assertTrue(choice["leave"])
        self.assertEqual(materials["shared_activity"]["pending"], ["下次再看未解折口"])
        self.assertEqual(VisitFlow._shared_choice_issue(value, choice, self.clock.now()), "")


if __name__ == "__main__":
    unittest.main()
